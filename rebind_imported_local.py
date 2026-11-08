#!/usr/bin/env python3
"""Rebuild the missing local-session receipts of rows imported from another install.

The Linux<->Windows bridge merges `sessions` + `messages` only: the imported rows arrive with
their route identity intact (session_key / chat_id / user_id = uid:1000) but WITHOUT the
`state_meta['gateway.local_policy.v1:<sid>']` receipt that the desktop needs to resume them,
so every click ends in `storage_unavailable at local_receipt:394` (row is None).

This script writes, per imported row, the same shape a normal local conversation has:
  * gateway.local_policy.v1:<sid>   receipt (entry/origin rebuilt from the row, legacy_session_id)
  * gateway.local_legacy.v1:<sid>    the companion lineage root bind_legacy_target() writes
  * gateway_routing (scope '')       the route's entry_json == the receipt's entry

It never touches `sessions`, `messages` or any other row. Verification runs the REAL
functions (restore_policy, validate_local_lineage, build_session_key, SessionEntry.from_dict),
not copies of their logic.

  python rebind_imported_local.py --db <state.db>              # dry run on a throwaway copy
  python rebind_imported_local.py --db <state.db> --apply       # write to the live db
"""
import argparse
import json
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

POLICY = 'gateway.local_policy.v1:'
LEGACY = 'gateway.local_legacy.v1:'
AGENT = Path('.')


def load(raw):
    return raw if isinstance(raw, dict) else json.loads(raw)


def dump(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':'))


def iso(ts):
    return datetime.fromtimestamp(float(ts)).isoformat() if ts else datetime.now().isoformat()


def plan(conn, home, profile):
    from gateway.config import Platform
    from gateway.session import SessionSource, build_session_key

    # template: the most recently active healthy local receipt (same profile, same desktop surface)
    rows = conn.execute(
        "SELECT m.value FROM state_meta m JOIN sessions s "
        "ON s.id = json_extract(m.value, '$.session_id') "
        "WHERE m.key GLOB ? ORDER BY COALESCE(s.last_activity_at, 0) DESC",
        (POLICY + '*',)).fetchall()
    template = None
    for (raw,) in rows:
        cand = load(raw)
        if cand.get('profile_id') == home and 'legacy_session_id' in cand:
            template = cand
            break
    if template is None:
        sys.exit('no healthy template receipt to model the import on')

    missing = conn.execute(
        "SELECT * FROM sessions WHERE id GLOB 'local-*' AND id NOT IN "
        "(SELECT substr(key, ?) FROM state_meta WHERE key GLOB ?) ORDER BY started_at",
        (len(POLICY) + 1, POLICY + '*')).fetchall()

    plan_items = []
    for row in missing:
        sid = row['id']
        source = SessionSource(platform=Platform.LOCAL, chat_id=sid, user_id=row['user_id'],
                               chat_type='dm', profile=profile)
        route = build_session_key(source, profile=profile)
        if route != row['session_key']:
            print(f'  SKIP {sid[:24]} route would change: {row["session_key"]} -> {route}')
            continue
        entry = dict(template['entry'])
        entry.update({'session_id': sid, 'session_key': route, 'origin': source.to_dict(),
                      'created_at': iso(row['started_at']),
                      'updated_at': iso(row['last_activity_at'] or row['started_at']),
                      'display_name': row['display_name'], 'platform': 'local',
                      'chat_type': 'dm', 'prev_session_id': None, 'is_fresh_reset': False,
                      'was_auto_reset': False, 'auto_reset_reason': None,
                      'reset_had_activity': False, 'suspended': False})
        receipt = {
            'entry': entry,
            'legacy_session_id': sid,
            'lineage': [sid],
            'policy': template['policy'],
            'principal_id': row['user_id'],
            'profile_id': home,
            'request_id': f'legacy:{sid}',
            'route': route,
            'session_id': sid,
        }
        legacy = {'session_id': sid, 'profile_id': home,
                  'principal_id': row['user_id'], 'legacy_session_id': sid}
        plan_items.append({'sid': sid, 'title': row['title'], 'msgs': row['message_count'],
                           'receipt': receipt, 'legacy': legacy,
                           'routing_missing': conn.execute(
                               "SELECT 1 FROM gateway_routing WHERE scope='' AND session_key=?",
                               (route,)).fetchone() is None})
    return plan_items


def write(conn, items):
    conn.execute('PRAGMA busy_timeout=60000')
    conn.execute('BEGIN IMMEDIATE')
    try:
        for it in items:
            sid = it['sid']
            conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
                         (POLICY + sid, dump(it['receipt'])))
            conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
                         (LEGACY + sid, dump(it['legacy'])))
            entry_json = dump(it['receipt']['entry'])
            if it['routing_missing']:
                conn.execute("INSERT INTO gateway_routing(scope,session_key,entry_json,updated_at) "
                             "VALUES('',?,?,?)", (it['receipt']['route'], entry_json, _now()))
            else:
                conn.execute("UPDATE gateway_routing SET entry_json=?, updated_at=? "
                             "WHERE scope='' AND session_key=?", (entry_json, _now(), it['receipt']['route']))
        conn.execute('COMMIT')
    except Exception:
        conn.execute('ROLLBACK')
        raise


def _now():
    import time
    return time.time()


def verify(conn, home, profile):
    from gateway.config import Platform
    from gateway.session import SessionSource, SessionEntry, build_session_key
    from gateway.session_policy import restore_policy
    from hermes_state_local_lineage import validate_local_lineage
    from hermes_state_keys import local_identity

    rows = conn.execute("SELECT key, value FROM state_meta WHERE key GLOB ?", (POLICY + '*',)).fetchall()
    ok = 0
    for key, raw in rows:
        receipt = load(raw)
        sid = receipt['session_id']
        why = []
        try:
            if receipt['profile_id'] != home:
                why.append('profile_mismatch')
            # owned_receipt(): legacy_session_id wins, else the sha256 of the creation request
            chat_id = (receipt['legacy_session_id'] if 'legacy_session_id' in receipt else
                       local_identity(receipt['profile_id'], receipt['principal_id'], receipt['request_id']))
            source = SessionSource(platform=Platform.LOCAL, chat_id=chat_id,
                                   user_id=receipt['principal_id'], chat_type='dm', profile=profile)
            route = build_session_key(source, profile=profile)
            entry = SessionEntry.from_dict(receipt['entry'])
            if receipt['session_id'] != sid or chat_id != sid:
                why.append('identity mismatch')
            if receipt['route'] != route:
                why.append(f'route {receipt["route"]} != {route}')
            if entry.session_key != route:
                why.append('entry.session_key mismatch')
            if entry.origin.to_dict() != source.to_dict():
                why.append('entry.origin mismatch')
            stored = conn.execute('SELECT session_key, chat_id, user_id FROM sessions WHERE id=?',
                                  (entry.session_id,)).fetchone()
            if stored is None or tuple(stored) != (route, chat_id, source.user_id):
                why.append(f'stored identity mismatch {tuple(stored) if stored else None}')
            restore_policy(receipt['policy'])
            validate_local_lineage(conn, receipt)
        except Exception as exc:            # noqa: BLE001 - any failure refuses the receipt
            why.append(f'{type(exc).__name__}: {getattr(exc, "reason", exc)}')
        if why:
            print(f'  BAD  {sid[:24]} {why}')
        else:
            ok += 1
    print(f'verified with the real functions: {ok}/{len(rows)} receipts pass')
    return ok == len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--hermes-agent', default=None,
                    help='install tree for the real-function verification '
                         '(default: three levels up from the profile home, i.e. <hermes>/hermes-agent)')
    args = ap.parse_args()
    db = Path(args.db)
    global AGENT
    AGENT = Path(args.hermes_agent) if args.hermes_agent else db.parent.parent.parent / 'hermes-agent'
    home = str(db.parent)
    profile = db.parent.name
    sys.path.insert(0, str(AGENT))

    if not args.apply:
        tmp = Path(tempfile.mkdtemp(prefix='receipt-dry-')) / 'state.db'
        con = sqlite3.connect(db, timeout=60)
        out = sqlite3.connect(tmp)
        with out:
            con.backup(out)
        con.close()
        con = out
        print(f'DRY RUN on copy {tmp}')
    else:
        con = sqlite3.connect(db, timeout=60, isolation_level=None)

    con.row_factory = sqlite3.Row
    items = plan(con, home, profile)
    print(f'\n{len(items)} imported rows without a receipt:')
    for it in items:
        print(f'  {it["sid"][:24]} {it["msgs"]:5d} msgs  {it["title"] or ""}'
              f'{"  [+gateway_routing]" if it["routing_missing"] else ""}')
    if not items:
        print('nothing to do')
        return 0

    write(con, items)
    print(f'\nwritten: {len(items)} receipts + {len(items)} legacy roots')
    good = verify(con, home, profile)
    if args.apply:
        con.close()
    if good:
        print('OK - now have the user click one imported chat; '
              'gateway.log must gain no new "receipt refused" line')
        return 0
    return 3


if __name__ == '__main__':
    sys.exit(main())
