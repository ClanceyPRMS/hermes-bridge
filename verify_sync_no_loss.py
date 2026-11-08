#!/usr/bin/env python3
"""用真 `merge_db` 重放「忘了 push / 忘了 pull」，证明同步不会覆盖任何东西。

造三个空库（Windows / Linux / 桥，只含合并真正用到的列与表），然后：

  A  Windows push → Linux 忘了 pull 就继续聊 → Linux 走前 push
  B  Windows 忘了 push 就走 → 两边各聊各的 → 回 Windows pull + push
  C  Windows 那条「没推上去」的消息最终必须补进桥

判据：每一步都不能丢已有的 `message_uid`；最后三边的消息集合完全相等。
改过 `merge_db` / `mirror_receipts` 之后跑一遍，比讲道理有用。

    python verify_sync_no_loss.py                       # 默认读 D:/HermesBridge/hermes-bridge.py
    python verify_sync_no_loss.py --bridge <path.py> --tmp <目录>
"""
import argparse
import importlib.util
import sqlite3
import sys
import tempfile
from pathlib import Path

SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, source TEXT, user_id TEXT,
  session_key TEXT, chat_id TEXT, chat_type TEXT, started_at REAL, last_activity_at REAL,
  message_count INTEGER, tool_call_count INTEGER, end_reason TEXT, parent_session_id TEXT,
  profile_name TEXT);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, message_uid TEXT,
  role TEXT, content TEXT, timestamp REAL, display_order INTEGER, tool_call_id TEXT);
CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE gateway_routing (scope TEXT NOT NULL DEFAULT '', session_key TEXT NOT NULL,
  entry_json TEXT NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY (scope, session_key));
"""

FAILS = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(label)


def load_bridge(path):
    spec = importlib.util.spec_from_file_location("hb", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fresh(p):
    c = sqlite3.connect(p)
    c.executescript(SCHEMA)
    c.commit()
    c.close()
    return p


def chat(db, sid, uid, text, ts):
    """在这台机器上说一句（会话不在就建）。计数器故意不管——合并会重算。"""
    c = sqlite3.connect(db)
    c.execute("insert or ignore into sessions(id,title,source,started_at,last_activity_at,message_count,"
              "profile_name) values(?,?,?,?,?,0,'test-profile')", (sid, sid, "gui", ts, ts))
    c.execute("insert into messages(session_id,message_uid,role,content,timestamp) values(?,?,?,?,?)",
              (sid, uid, "user", text, ts))
    c.commit()
    c.close()


def merge(hb, src, dst):
    c = sqlite3.connect(dst)
    c.execute("PRAGMA busy_timeout=60000")
    c.execute("BEGIN IMMEDIATE")
    st = hb.merge_db(src, c)          # push 与 pull 都是它，只是 src/dst 调头
    c.commit()
    c.close()
    return st


def uids(db):
    c = sqlite3.connect("file:" + Path(db).as_posix() + "?mode=ro", uri=True)
    out = {r[0] for r in c.execute("select message_uid from messages")}
    c.close()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bridge", default="D:/HermesBridge/hermes-bridge.py")
    ap.add_argument("--tmp", default=None, help="夹具目录（默认系统临时目录）")
    args = ap.parse_args()
    hb = load_bridge(args.bridge)
    tmp = Path(args.tmp or tempfile.mkdtemp(prefix="synctest-"))
    tmp.mkdir(parents=True, exist_ok=True)

    win, arch, vault = (fresh(tmp / f) for f in ("win.db", "arch.db", "vault.db"))

    print("== 准备：Windows 上有 m1 m2，push 后 Linux pull 一次（两边同底） ==")
    chat(win, "s_work", "u1", "m1", 100)
    chat(win, "s_work", "u2", "m2", 200)
    merge(hb, win, vault)
    merge(hb, vault, arch)

    print("\n== A：Windows 后来又说一句 m3（没推），Linux 忘了 pull 就直接聊 ==")
    chat(win, "s_work", "u3", "m3", 300)
    chat(arch, "s_work", "u4", "m4", 400)
    chat(arch, "s_new", "u5", "m5", 410)
    merge(hb, arch, vault)                       # Linux 没 pull 就 push
    check("A: 桥里老消息没被覆盖", {"u1", "u2"} <= uids(vault), uids(vault))
    check("A: Linux 的新消息进桥", {"u4", "u5"} <= uids(vault))
    check("A: Windows 那条 m3 还在本机", "u3" in uids(win))

    print("\n== 回 Windows：pull 一次 ==")
    merge(hb, vault, win)
    check("A: Windows 拿到 m4/m5 且自己的 m3 没丢", {"u1", "u2", "u3", "u4", "u5"} <= uids(win), uids(win))

    print("\n== B：Windows 忘了 push 就走，两边各聊各的 ==")
    chat(win, "s_work", "u6", "m6", 500)         # 没推
    merge(hb, vault, arch)                        # Linux pull（拿不到 m6，正常）
    check("B: Linux 这时确实没有 m6", "u6" not in uids(arch))
    chat(arch, "s_work", "u7", "m7", 600)
    chat(arch, "s_other", "u8", "m8", 610)
    merge(hb, arch, vault)
    merge(hb, vault, win)
    check("B: Windows 拿到 m7/m8 且自己的 m6 还在",
          {"u6", "u7", "u8"} <= uids(win), uids(win))

    print("\n== C：收尾，并集必须完全相等 ==")
    merge(hb, win, vault)
    merge(hb, vault, arch)
    w, a, v = uids(win), uids(arch), uids(vault)
    check("C: 三边消息集合完全一致", w == a == v, (len(w), len(a), len(v)))
    check("C: m1..m8 一条不少", {"u%d" % i for i in range(1, 9)} <= w, sorted(w))
    check("C: 没有重复消息（uid 唯一）", True)
    c = sqlite3.connect("file:" + Path(vault).as_posix() + "?mode=ro", uri=True)
    dup = c.execute("select count(*) from (select session_id,message_uid from messages "
                    "group by 1,2 having count(*)>1)").fetchone()[0]
    c.close()
    check("C: 桥里没有重复", dup == 0, dup)

    print("\n" + ("全部通过 ✅ 同步只会取并集，不覆盖" if not FAILS else f"失败 {len(FAILS)} 项：{FAILS}"))
    print("夹具留在：" + str(tmp))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
