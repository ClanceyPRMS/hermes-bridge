#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hermes-bridge —— 多系统 Hermes 数据桥

同一台机器装了两个（或多个）系统、或者你有两台电脑时，各边的 Hermes profile 各活各的。
这个脚本让它们「只增不改」地合并：你在这边新说的话、新存的记忆、新写的技能，
push 到桥（vault）里；到另一边 pull 下来。谁也没被覆盖，冲突留副本。

    hermes-bridge status          # 看两边差多少
    hermes-bridge push            # 离开这台机器之前跑
    hermes-bridge pull            # 到这台机器之后跑

设计底线（对应「数据无价」）：
  * 只 INSERT 缺失的东西，永不 UPDATE/DELETE 已有消息
  * 每次 pull 前自动给本地 state.db 做一致快照（保留最近 5 份）
  * 活库只在短事务里写，busy_timeout 30s
  * 记忆文件冲突时不猜：本地保留，对方的存成 .from-<side>.md 等你裁决

零依赖：只用 Python 标准库。单文件可拷走。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

PROFILE_ENV = "HERMES_PROFILE"       # 想指定 profile 名就设它，或用 --profile
DB = "state.db"
PROJECTS_DB = "projects.db"
MEM_FILES = ["memories/MEMORY.md", "memories/USER.md", "SOUL.md"]
SKILL_DIRS = ["skills"]
SYNC_TREES = ["skills", "scripts"]      # scripts/ 是 cron --script 放脚本的地方，也要过桥
CRON_JOBS = "cron/jobs.json"
PATHS_FILE = "pathmap.json"          # 两台机器的路径对应表（cwd / 项目目录翻译用）
# 这些字段是「这台机器上的运行状态」，跨机器搬任务定义时要清掉
CRON_RUNTIME_KEYS = ("next_run_at", "last_run_at", "last_status", "last_error",
                     "last_delivery_error", "last_delivery_unverified", "last_failure",
                     "last_dispatch", "last_delivery_queued", "fire_claim",
                     "failure_streak", "catch_up_from")
KEEP_BACKUPS = 5

# 桌面端恢复一个本地会话，除了 sessions 行还要这两张 state_meta 收据；
# 桥只搬 sessions+messages 时，会话在另一台点开就是 storage_unavailable。
POLICY_PREFIX = "gateway.local_policy.v1:"
LEGACY_PREFIX = "gateway.local_legacy.v1:"


# ---------------------------------------------------------------- 路径 / 工具
def _console_safe() -> None:
    """Windows 控制台是 GBK 时，别让 ✓ 之类的字符把脚本打崩"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def profile_root() -> Path:
    """Hermes 放所有 profile 的目录"""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return base / "hermes" / "profiles"
    return Path.home() / ".hermes" / "profiles"


def default_profile() -> str:
    """自动找 profile 名：$HERMES_PROFILE > profile 目录下唯一的那个 > 报错让人指定"""
    env = os.environ.get(PROFILE_ENV)
    if env:
        return env
    base = profile_root()
    cands = []
    if base.is_dir():
        cands = sorted(p.name for p in base.iterdir()
                       if p.is_dir() and not p.name.startswith(".")
                       and ((p / DB).exists() or (p / "config.yaml").exists()))
    if len(cands) == 1:
        return cands[0]
    if cands:
        raise SystemExit(
            "找到多个 Hermes profile，请用 --profile 指定（或设 $" + PROFILE_ENV + "）：\n  "
            + "\n  ".join(cands))
    raise SystemExit(
        "没找到 Hermes profile。请用 --profile <名字> 或 --profile-home <目录> 指定。")


def side_name() -> str:
    """这台机器在桥里的名字（manifest 的基线按它分账）"""
    if os.name == "nt":
        return "windows"
    return "macos" if sys.platform == "darwin" else "linux"


# 老版本把 Linux 侧记成 "arch"，读旧 manifest 时得认
SIDE_ALIASES = {"linux": ("arch",)}


def profile_home(pname: str | None = None, explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    return profile_root() / (pname or default_profile())


def vault_dir(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    if os.environ.get("HERMES_BRIDGE"):
        return Path(os.environ["HERMES_BRIDGE"]).expanduser().resolve()
    here = Path(__file__).resolve().parent
    if (here / ".hermes-bridge").exists() or here.name == "HermesBridge":
        return here
    # 兜底：在常见的挂载点里找带标记的目录
    pats = ["D:/HermesBridge", "/mnt/HermesBridge"]
    pats += [str(p) for p in Path("/run/media").glob("*/**/HermesBridge")]
    for p in pats:
        if Path(p).is_dir():
            return Path(p).resolve()
    raise SystemExit(
        "找不到桥目录（HermesBridge）。\n"
        "  桥通常挂在另一块盘上，先确认那块盘挂上了，然后：\n"
        "    hermes-bridge --vault /run/media/<你>/<卷标>/HermesBridge status\n"
        "  或者设环境变量 HERMES_BRIDGE=<桥目录>"
    )


def q(p) -> str:
    """给 sqlite URI 用的路径"""
    s = str(Path(p).resolve()).replace("\\", "/")
    return urllib.parse.quote(s, safe="/:")


def ro(path) -> sqlite3.Connection:
    return sqlite3.connect("file:%s?mode=ro" % q(path), uri=True, timeout=30)


def sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024.0
    return str(n)


# ---------------------------------------------------------------- 快照 / 备份
def snapshot_db(db_path: Path, out_path: Path) -> Path:
    """把活的 WAL 库（db+wal+shm）复制到一边，再用 backup API 做成一个干净的单文件"""
    tmp = Path(tempfile.mkdtemp(prefix="hbridge-snap-"))
    try:
        work = tmp / DB
        shutil.copy2(db_path, work)
        for ext in ("-wal", "-shm"):
            src = Path(str(db_path) + ext)
            if src.exists():
                shutil.copy2(src, Path(str(work) + ext))
        src_conn = sqlite3.connect(str(work), timeout=30)
        dst_conn = sqlite3.connect(str(out_path))
        src_conn.backup(dst_conn)
        dst_conn.close()
        src_conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out_path


def backup_local(db_path: Path, where: Path) -> Path:
    where.mkdir(parents=True, exist_ok=True)
    out = where / f"state-{time.strftime('%Y%m%dT%H%M%S')}.db"
    snapshot_db(db_path, out)
    olds = sorted(where.glob("state-*.db"), key=lambda p: p.stat().st_mtime)
    for p in olds[:-KEEP_BACKUPS]:
        p.unlink(missing_ok=True)
    return out


# ---------------------------------------------------------------- 合并核心
def tcols(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]


def free_title(conn: sqlite3.Connection, title: str) -> str:
    """sessions.title 上有唯一索引，撞了就要改个名字"""
    if not title:
        return title
    exists = conn.execute("select 1 from sessions where title=?", (title,)).fetchone()
    if not exists:
        return title
    for i in range(1, 200):
        cand = f"{title}（另一台 {i}）"
        if not conn.execute("select 1 from sessions where title=?", (cand,)).fetchone():
            return cand
    return f"{title}（另一台 {int(time.time())}）"


def _abs_here(value) -> bool:
    """这个路径在本机算不算绝对路径。

    必须用 ``Path().is_absolute()`` 而不是 ``os.path.isabs()``：Windows 的 ntpath 认为
    ``/home/x`` 是绝对的（只是没有盘符），而网关用的 ``pathlib`` 认为不是——两边判断不一致，
    收据就会被 ``restore_policy`` 判成 ``storage_unavailable``。
    """
    if not isinstance(value, str) or not value:
        return False
    try:
        return Path(value).is_absolute()
    except (OSError, ValueError):
        return False


def mirror_receipts(src: sqlite3.Connection, dst: sqlite3.Connection,
                    target_home: Path | None = None) -> dict:
    """把 src 里的本地会话收据补进 dst —— 没有收据的会话在桌面端点开会被拒绝。

    只 INSERT，已有收据一律不动（桥的「只增不改」照旧）。两处机器相关的东西必须换掉：

      * ``profile_id`` 是 profile home 的绝对路径，两台机器不同
      * ``policy.cwd`` / ``TERMINAL_CWD`` 必须是**本机**的绝对路径

    ``target_home`` 为 None 表示 dst 是桥（中立载体）：原样保存，只补 ``legacy_session_id``。
    给出 target_home 表示 dst 是活 profile：重写 profile_id、把另一台 OS 的 cwd 换成本机存在的
    目录。``legacy_session_id`` 必须无条件写上：会话 id 是
    ``sha256(profile_id, principal_id, request_id)``，换了 home 就再也算不出来，只剩这条路认得它。
    """
    st = {"new": 0, "cwd_mapped": [], "foreign": [], "skipped": []}
    dst_ids = {r[0] for r in dst.execute("select id from sessions")}
    if not dst_ids:
        return st

    # 规范写法：优先抄 dst 里已有健康收据的 profile_id 字符串和 cwd，
    # 保证和网关自己写的一字不差（大小写、分隔符都算）。
    canon_profile = canon_cwd = None
    if target_home is not None:
        for (raw,) in dst.execute("select value from state_meta where key glob ?",
                                  (POLICY_PREFIX + "*",)):
            try:
                r = json.loads(raw)
            except Exception:
                continue
            if not isinstance(r, dict):
                continue
            p, c = r.get("profile_id"), (r.get("policy") or {}).get("cwd")
            if canon_profile is None and isinstance(p, str) and Path(p) == Path(target_home):
                canon_profile = p
            if canon_cwd is None and _abs_here(c):
                canon_cwd = c
    profile_id = canon_profile if canon_profile is not None else (
        str(target_home) if target_home is not None else None)
    fallback_cwd = canon_cwd or str(Path.home())

    for key, raw in src.execute("select key, value from state_meta where key glob ?",
                                (POLICY_PREFIX + "*",)).fetchall():
        sid = key[len(POLICY_PREFIX):]
        if sid not in dst_ids:
            st["skipped"].append(sid)  # 会话行没搬过来（或撞了唯一约束），别留孤儿收据
            continue
        try:
            receipt = json.loads(raw)
        except Exception:
            st["skipped"].append(sid)
            continue
        if not isinstance(receipt, dict) or receipt.get("session_id") != sid \
                or not receipt.get("principal_id"):
            st["skipped"].append(sid)
            continue

        have = dst.execute("select value from state_meta where key=?", (key,)).fetchone()
        if have is not None:
            if target_home is not None:
                try:
                    cur = json.loads(have[0])
                except Exception:
                    cur = {}
                if isinstance(cur, dict) and cur.get("profile_id") != profile_id:
                    st["foreign"].append(sid)  # 别的主机的收据：留给 rebind_imported_local.py
            continue

        r = json.loads(json.dumps(receipt))            # 深拷贝，不动 src 的内容
        lineage = r.get("lineage")
        if not isinstance(lineage, list) or not lineage:
            lineage = [sid]
        if lineage[0] != sid:                          # 收据指向别的会话根：不是我们的活，别猜
            st["skipped"].append(sid)
            continue
        r["lineage"] = lineage
        r["legacy_session_id"] = sid
        if target_home is not None:
            r["profile_id"] = profile_id
            policy = r.get("policy") or {}
            old_cwd = policy.get("cwd")
            if not _abs_here(old_cwd):
                policy["cwd"] = fallback_cwd
                term = policy.get("terminal_json")
                if isinstance(term, str):
                    try:
                        t = json.loads(term)
                        if t.get("TERMINAL_CWD") == old_cwd:
                            t["TERMINAL_CWD"] = fallback_cwd
                            policy["terminal_json"] = json.dumps(
                                t, ensure_ascii=False, separators=(",", ":"))
                    except Exception:
                        pass
                st["cwd_mapped"].append((sid, old_cwd, fallback_cwd))
            r["policy"] = policy

        blob = json.dumps(r, ensure_ascii=False, separators=(",", ":"))
        dst.execute("insert into state_meta(key,value) values(?,?)", (key, blob))
        dst.execute(
            "insert or ignore into state_meta(key,value) values(?,?)",
            (LEGACY_PREFIX + sid, json.dumps(
                {"session_id": sid, "profile_id": r["profile_id"],
                 "principal_id": r["principal_id"], "legacy_session_id": sid},
                ensure_ascii=False, separators=(",", ":"))),
        )
        route = r.get("route")
        if route:
            dst.execute(
                "insert into gateway_routing(scope,session_key,entry_json,updated_at) "
                "values('',?,?,?) on conflict(scope,session_key) do update set "
                "entry_json=excluded.entry_json, updated_at=excluded.updated_at",
                (route, json.dumps(r.get("entry") or {}, ensure_ascii=False,
                                   separators=(",", ":")), time.time()),
            )
        st["new"] += 1

    # 收尾自检：本机还有哪些本地会话没有收据（两边都没有收据的，永远点不开）
    st["missing"] = []
    if target_home is not None:
        st["missing"] = [r[0] for r in dst.execute(
            "select s.id from sessions s where s.id glob 'local-*' and s.id not in "
            "(select substr(key, ?) from state_meta where key glob ?)",
            (len(POLICY_PREFIX) + 1, POLICY_PREFIX + "*"))]
    return st


def merge_db(src_path: Path, dst: sqlite3.Connection, dry: bool = False,
             target_home: Path | None = None, paths: list | None = None) -> dict:
    """把 src 里 dst 没有的会话/消息/本地收据补进去。只增不改（例外见下）。

    ``paths`` 给出时（pull 才给）：把会话的 ``cwd`` 从另一台 OS 的写法翻成本机写法。
    会话的 cwd 是**用户在桌面端「移进某个项目」时被改写的那个字段**，不翻的话
    另一台永远显示在「主页」；翻了之后两台的终端目录也是各自真实存在的。
    """
    src = ro(src_path)
    st = {
        "sessions_new": 0,
        "cwd_changed": [],
        "sessions_renamed": [],
        "messages_new": 0,
        "by_session": {},
        "touched_sessions": [],
        "skipped_uidless": 0,
    }

    d_sess_cols = tcols(dst, "sessions")
    s_sess_cols = tcols(src, "sessions")
    sess_cols = [c for c in s_sess_cols if c in d_sess_cols and c != "id"]

    dst_ids = {r[0] for r in dst.execute("select id from sessions")}
    rows = src.execute(
        "select %s from sessions order by started_at" % ",".join(["id"] + sess_cols)
    ).fetchall()
    for row in rows:
        sid = row[0]
        data = dict(zip(sess_cols, row[1:]))
        if sid in dst_ids:
            # 已存在：只补「活动时间」和空标题，外加 cwd（= 桌面端的项目归属）
            want_cwd = resolve_cwd(data.get("cwd"), paths)
            if want_cwd:
                cur = dst.execute("select cwd from sessions where id=?", (sid,)).fetchone()
                old = cur[0] if cur else None
                if (old or "") != want_cwd:
                    dst.execute("update sessions set cwd=? where id=?", (want_cwd, sid))
                    st["cwd_changed"].append((sid, old, want_cwd))
            dst.execute(
                "update sessions set last_activity_at = ? where id = ? and "
                "coalesce(last_activity_at,0) < ?",
                (data.get("last_activity_at"), sid, data.get("last_activity_at") or 0),
            )
            dst.execute(
                "update sessions set title = ? where id = ? and (title is null or title='')",
                (data.get("title"), sid),
            )
            continue
        if paths and data.get("cwd"):
            data["cwd"] = resolve_cwd(data["cwd"], paths) or data["cwd"]
        if data.get("title"):
            new_title = free_title(dst, data["title"])
            if new_title != data["title"]:
                st["sessions_renamed"].append((sid, data["title"], new_title))
            data["title"] = new_title
        cols = list(data.keys())
        try:
            dst.execute(
                "insert into sessions (%s) values (%s)"
                % (",".join(["id"] + cols), ",".join("?" * (len(cols) + 1))),
                [sid] + [data[c] for c in cols],
            )
            dst_ids.add(sid)
            st["sessions_new"] += 1
        except sqlite3.IntegrityError as e:  # 极少见：其它唯一约束
            print(f"  ! 跳过会话 {sid[:18]}…：{e}")
            continue

    # ---- 消息：按 (session_id, message_uid) 去重
    d_msg_cols = tcols(dst, "messages")
    s_msg_cols = tcols(src, "messages")
    msg_cols = [c for c in s_msg_cols if c in d_msg_cols and c != "id"]
    idx = {c: i for i, c in enumerate(msg_cols)}

    existing = set(
        dst.execute(
            "select session_id, message_uid from messages where message_uid is not null"
        ).fetchall()
    )
    seen = set()
    to_insert = []
    for r in src.execute(
        "select %s from messages order by timestamp, id" % ",".join(msg_cols)
    ):
        sid = r[idx["session_id"]]
        uid = r[idx["message_uid"]]
        if not uid:  # 理论上不出现；真出现就用内容指纹兜底
            h = hashlib.sha1(
                f"{sid}|{r[idx['role']]}|{r[idx['timestamp']]}|{r[idx['content']]}".encode()
            ).hexdigest()
            uid = "sha1:" + h
            st["skipped_uidless"] += 1
        key = (sid, uid)
        if key in existing or key in seen:
            continue
        seen.add(key)
        to_insert.append(r)

    ph = ",".join("?" * len(msg_cols))
    sql = "insert into messages (%s) values (%s)" % (",".join(msg_cols), ph)
    BATCH = 200
    for i in range(0, len(to_insert), BATCH):
        chunk = to_insert[i : i + BATCH]
        dst.executemany(sql, chunk)
        st["messages_new"] += len(chunk)

    touched = sorted({r[idx["session_id"]] for r in to_insert})
    st["touched_sessions"] = touched

    # ---- 受影响会话：按时间重排 display_order（界面就按这个排序），重算计数
    for sid in touched:
        rows2 = dst.execute(
            "select id from messages where session_id=? order by timestamp, id", (sid,)
        ).fetchall()
        for order, (mid,) in enumerate(rows2, 1):
            dst.execute(
                "update messages set display_order=? where id=? and coalesce(display_order,-1)<>?",
                (order, mid, order),
            )
        cnt, tools, last = dst.execute(
            "select count(*), sum(role='tool'), max(timestamp) from messages where session_id=?",
            (sid,),
        ).fetchone()
        dst.execute(
            "update sessions set message_count=?, tool_call_count=?, last_activity_at=? where id=?",
            (cnt, tools or 0, last, sid),
        )
        st["by_session"][sid] = cnt

    # ---- 本地会话收据（state_meta）：搬过去，另一台才能点开继续聊
    rc = mirror_receipts(src, dst, target_home)
    st["receipts_new"] = rc["new"]
    st["receipts_cwd_mapped"] = rc["cwd_mapped"]
    st["receipts_foreign"] = rc["foreign"]
    st["receipts_skipped"] = rc["skipped"]
    st["receipts_missing"] = rc["missing"]

    src.close()
    return st


# ---------------------------------------------------------------- 路径映射（跨系统 cwd）
# 两台机器的绝对路径不可能相同（D:\x vs /mnt/d/x），所以 cwd / 项目目录要靠一张前缀表翻译。
# 表放在 <桥>/pathmap.json，长前缀优先。格式见仓库里的 pathmap.example.json。
# 没配表不会出事：翻译不出来就不改 cwd，只是会话的项目归属要手动认。
DEFAULT_PATHS: list = []


def load_paths(vault: Path) -> list:
    """读 pathmap.json；没有就写一份空的（并提示怎么填）。条目：{"win": "...", "linux": "..."}"""
    p = vault / PATHS_FILE
    if p.exists():
        try:
            data = json.loads(p.read_text("utf-8"))
            return [x for x in data if isinstance(x, dict) and x.get("win") and x.get("linux")]
        except (OSError, ValueError):
            print(f"! {PATHS_FILE} 读不动（格式错？），这次不做 cwd 翻译", file=sys.stderr)
            return list(DEFAULT_PATHS)
    try:
        p.write_text(json.dumps(DEFAULT_PATHS, ensure_ascii=False, indent=2), "utf-8")
        print(f"· 已建空 {PATHS_FILE}（没填就不同步会话的 cwd；"
              f"格式见 pathmap.example.json）", file=sys.stderr)
    except OSError:
        pass
    return list(DEFAULT_PATHS)


def _winish(path: str) -> bool:
    """看起来是 Windows 路径（有盘符）"""
    return len(path) > 1 and path[1] == ":" or "\\" in path


def _native(path: str) -> bool:
    """这个路径在本机算绝对路径（不是另一台的写法）"""
    if not path:
        return False
    try:
        return Path(path).is_absolute()
    except (OSError, ValueError):
        return False


def resolve_cwd(raw: str, paths: list | None) -> str | None:
    """本机该用哪个 cwd：本来就是本机写法就照用；是另一台的写法就翻；翻不出来就别改。"""
    if not raw:
        return None
    if _native(raw):
        return raw
    if paths:
        mapped = map_path(raw, paths, to_here=True)
        if mapped != raw:
            return mapped
    return None


def map_path(path: str, paths: list, to_here: bool, must_exist: bool = True) -> str:
    """把另一台 OS 的绝对路径翻成本机路径。翻不动（没对应前缀 / 目标不存在）就原样返回。

    ``to_here=True``：Windows → 本机；``False``：本机 → Windows。
    比较时统一成 ``/``、Windows 那侧不区分大小写；**前缀里的根分隔符不能丢**（``D:`` + ``/x``）。
    """
    if not path or not paths:
        return path
    best = None
    norm_path = path.replace("\\", "/")
    for item in paths:
        win, lin = item["win"].replace("\\", "/").rstrip("/"), item["linux"].rstrip("/")
        src, dst = (win, lin) if to_here else (lin, win)
        if to_here:
            hit = norm_path.lower() == src.lower() or norm_path.lower().startswith(src.lower() + "/")
        else:
            hit = norm_path == src or norm_path.startswith(src + "/")
        if hit and (best is None or len(src) > len(best[0])):
            best = (src, dst)
    if best is None:
        return path
    src, dst = best
    rest = norm_path[len(src):]                      # 一定以 "/" 开头（或为空）
    out = dst + rest
    if os.sep == "\\":
        out = out.replace("/", "\\")
    else:
        out = out.replace("\\", "/")
    if must_exist and not os.path.exists(out):
        parent = os.path.dirname(out.rstrip("/\\"))
        if not parent or not os.path.isdir(parent):
            return path
    return out


# ---------------------------------------------------------------- 定时任务（cron）
def _read_jobs(path: Path):
    """读 jobs.json：接受 {"jobs":[...]} / 裸列表 / id 字典三种形态（Hermes 自己三种都认）"""
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if isinstance(jobs, dict):
        jobs = [{**v, "id": v.get("id") or k} for k, v in jobs.items() if isinstance(v, dict)]
    return [j for j in (jobs or []) if isinstance(j, dict) and j.get("id")]


def _write_jobs(path: Path, jobs: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".bridge-tmp")
    tmp.write_text(json.dumps({"jobs": jobs, "updated_at": now_iso()},
                              ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, path)


# 「定义」用白名单，不用黑名单：黑名单会漏掉运行时一直在变的字段——
# repeat.completed 每跑一次 +1、canonical_completions 每跑一次换一个 id，
# 于是同一个任务永远被判成「两边定义不同」（实测：Linux 与 Windows 逐字段比完是
# 0 处差异，冲突全是这两个字段闹的）。桥里那份还可能少 created_at 之类的键，
# 所以缺字段一律按 None 统一，不能拿「键的集合」当差异。
CRON_DEF_KEYS = ("name", "schedule", "schedule_display", "prompt", "script", "skill",
                 "skills", "model", "provider", "deliver", "enabled", "no_agent",
                 "timezone", "workdir", "enabled_toolsets", "attach_to_session",
                 "context_from", "base_url", "monitor_script", "monitor_url",
                 "state", "origin", "paused_reason")


def _job_def(job: dict) -> str:
    """任务定义指纹：只比白名单里的行为字段（运行状态与计数一律不参与）。"""
    d = {k: job.get(k) for k in CRON_DEF_KEYS}
    rep = job.get("repeat")
    d["repeat_times"] = rep.get("times") if isinstance(rep, dict) else None
    return json.dumps(d, ensure_ascii=False, sort_keys=True)


def merge_cron(src_dir: Path, dst_dir: Path, dry: bool = False) -> dict:
    """把 src 的 cron/jobs.json 并进 dst：按 job id 求并集，**只增不改**。

    cron 以前完全不过桥，于是在 Linux 上建的定时任务到 Windows 就不存在。合并规则：

      * 只有对面有 → 搬过来，但把 ``next_run_at`` / ``last_run_at`` 等**本机运行状态清空**，
        免得「上次在另一台跑过」被当成本机事实（清空了调度器会按 cron 表达式重新算）
      * 两边都有且定义相同 → 什么都不动
      * 两边都有但定义不同 → **保留本地那份**，对面那份整份存成
        ``backups/cron-jobs.from-<机器>-<时间>.json`` 等裁决
      * ``executions.db`` / ``deliveries.db``（跑过的历史）**不搬**：那是每台自己的流水
    """
    st = {"jobs_new": 0, "matched": 0, "conflicts": [], "copied": False}
    src_file, dst_file = src_dir / "jobs.json", dst_dir / "jobs.json"
    src_jobs = _read_jobs(src_file)
    if not src_jobs:
        return st
    dst_jobs = _read_jobs(dst_file)

    if dst_jobs is None:                     # 目标侧从来没见过 jobs.json
        fresh = []
        for j in src_jobs:
            jj = {k: v for k, v in j.items() if k not in CRON_RUNTIME_KEYS}
            jj["next_run_at"] = None
            fresh.append(jj)
        if not dry:
            _write_jobs(dst_file, fresh)
        st["copied"] = True
        st["jobs_new"] = len(fresh)
        return st

    have = {j["id"]: j for j in dst_jobs}
    changed = False
    for j in src_jobs:
        cur = have.get(j["id"])
        if cur is None:
            jj = {k: v for k, v in j.items() if k not in CRON_RUNTIME_KEYS}
            jj["next_run_at"] = None
            dst_jobs.append(jj)
            have[j["id"]] = jj
            st["jobs_new"] += 1
            changed = True
        elif _job_def(cur) == _job_def(j):
            st["matched"] += 1
        else:
            st["conflicts"].append(j["id"])

    if changed and not dry:
        _write_jobs(dst_file, dst_jobs)
    if st["conflicts"] and not dry:
        bak_dir = dst_dir.parent / "backups"
        bak_dir.mkdir(parents=True, exist_ok=True)
        bak = bak_dir / f"cron-jobs.from-{side_name()}-{time.strftime('%m%dT%H%M%S')}.json"
        shutil.copy2(src_file, bak)
    return st


def sync_cron(src_dir: Path, dst_dir: Path, dry: bool = False) -> dict:
    st = merge_cron(src_dir, dst_dir, dry=dry)
    if st["copied"]:
        print(f"定时任务：目标侧还没有，整份建好（{st['jobs_new']} 个）")
    elif st["jobs_new"] or st["conflicts"]:
        print(f"定时任务：新增 {st['jobs_new']} 个 · 相同 {st['matched']} 个 · "
              f"冲突 {len(st['conflicts'])} 个")
    else:
        print(f"定时任务：无变化（{st['matched']} 个一致）")
    for jid in st["conflicts"]:
        print(f"  ! 任务 {jid} 两边定义不同：本地保留，对面那份存进 backups/")
    return st


# ---------------------------------------------------------------- 项目（Projects）
def backup_projects(db_path: Path, where: Path) -> Path:
    where.mkdir(parents=True, exist_ok=True)
    out = where / f"projects-{time.strftime('%Y%m%dT%H%M%S')}.db"
    snapshot_db(db_path, out)
    for p in sorted(where.glob("projects-*.db"), key=lambda p: p.stat().st_mtime)[:-KEEP_BACKUPS]:
        p.unlink(missing_ok=True)
    return out


def merge_projects(src_path: Path, dst_path: Path, dry: bool = False,
                   paths: list | None = None) -> dict:
    """把 src 的 projects.db 合进 dst（projects.db = 桌面端「Projects」/ 命名工作区）。

    桥以前完全不碰 projects.db：在 Linux 上新建的项目到 Windows 就是不存在，它名下的会话
    于是全被归到「主页」。合并规则：

      * 项目按 ``slug`` 对齐，再退到 ``name``；同一项目两边 id 不同时，用**本机那份的 id**
      * 只增不改：缺的插入；description / icon / color / board_slug 只在本地为空时补
      * 文件夹 path 是绝对路径、两台 OS 不同 → **两边的都留着**（对方的主目录降级为 0），
        这样对方那台建的会话带着它自己的 cwd 过来，也能归到同一个项目里
      * ``active_id`` 是本机状态，不覆盖；``discovered_repos`` 是扫出来的，不搬
    """
    st = {"projects_new": 0, "projects_matched": 0, "folders_new": 0, "meta_new": 0, "copied": False}
    if not src_path.exists():
        return st
    if not dst_path.exists():
        if not dry:
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            snapshot_db(src_path, dst_path)
        st["copied"] = True
        return st

    src = ro(src_path)
    src.row_factory = sqlite3.Row          # 这里按列名取值（本机函数的其它地方用序号，别改全局）
    dst = sqlite3.connect(str(dst_path), timeout=60)
    dst.row_factory = sqlite3.Row
    try:
        dst.execute("PRAGMA busy_timeout=60000")
        dst.execute("BEGIN IMMEDIATE")
        local = {(r["slug"] or "").lower(): r["id"] for r in dst.execute("select id, slug from projects")}
        by_name = {(r["name"] or "").casefold(): r["id"] for r in dst.execute("select id, name from projects")}
        ids = {r[0] for r in dst.execute("select id from projects")}

        for p in src.execute("select * from projects"):
            slug, name = (p["slug"] or "").lower(), p["name"] or ""
            target = local.get(slug) or by_name.get(name.casefold())
            if target:                                   # 同一个项目，两边 id 可能不同
                st["projects_matched"] += 1
                dst.execute(
                    "update projects set description=coalesce(description, ?), icon=coalesce(icon, ?),"
                    " color=coalesce(color, ?), board_slug=coalesce(board_slug, ?),"
                    " primary_path=coalesce(primary_path, ?) where id=?",
                    (p["description"], p["icon"], p["color"], p["board_slug"], p["primary_path"], target))
                local[slug] = target
                by_name.setdefault(name.casefold(), target)
                continue
            new_id = p["id"]
            if new_id in ids:                            # id 撞了但 slug 不同：绝不改本地那行
                new_id = "p_" + hashlib.sha1(
                    f"{p['id']}|{slug}|{time.time()}".encode()).hexdigest()[:8]
            dst.execute(
                "insert into projects(id, slug, name, description, icon, color, board_slug,"
                " primary_path, created_at, archived) values(?,?,?,?,?,?,?,?,?,?)",
                (new_id, p["slug"], name, p["description"], p["icon"], p["color"],
                 p["board_slug"], p["primary_path"], p["created_at"], p["archived"]))
            local[slug] = new_id
            by_name.setdefault(name.casefold(), new_id)
            ids.add(new_id)
            st["projects_new"] += 1

        src_slug = {r["id"]: (r["slug"] or "").lower() for r in src.execute("select id, slug from projects")}
        for f in src.execute("select * from project_folders"):
            pid = local.get(src_slug.get(f["project_id"], ""))
            if not pid:
                continue
            primary = f["is_primary"]
            if primary and dst.execute("select 1 from project_folders where project_id=? and is_primary=1",
                                       (pid,)).fetchone():
                primary = 0                               # 本机已有主目录，别抢
            cur = dst.execute("insert or ignore into project_folders"
                              "(project_id, path, label, is_primary, added_at) values(?,?,?,?,?)",
                              (pid, f["path"], f["label"], primary, f["added_at"]))
            st["folders_new"] += max(cur.rowcount or 0, 0)
            # 同一目录的「本机写法」也登记一份（is_primary=0），这样两台都能按自己的
            # 绝对路径把会话归到这个项目里。目标不存在就跳过（没挂盘/没这个目录）。
            if paths:
                local_path = map_path(f["path"], paths, to_here=True)
                if local_path != f["path"] and os.path.exists(local_path):
                    cur2 = dst.execute(
                        "insert or ignore into project_folders"
                        "(project_id, path, label, is_primary, added_at) values(?,?,0,0,?)",
                        (pid, local_path, f["added_at"]))
                    st["folders_new"] += max(cur2.rowcount or 0, 0)

        for m in src.execute("select key, value from project_meta"):
            if m["key"] == "active_id":
                continue
            cur = dst.execute("insert or ignore into project_meta(key, value) values(?,?)",
                              (m["key"], m["value"]))
            st["meta_new"] += max(cur.rowcount or 0, 0)

        dst.rollback() if dry else dst.commit()
    finally:
        src.close()
        dst.close()
    return st


def sync_projects(src_path: Path, dst_path: Path, dry: bool = False,
                  paths: list | None = None) -> dict:
    st = merge_projects(src_path, dst_path, dry=dry, paths=paths)
    if st["copied"]:
        n = 0
        try:
            c = ro(dst_path)
            n = c.execute("select count(*) from projects").fetchone()[0]
            c.close()
        except Exception:
            pass
        print(f"项目：目标侧还没有 projects.db，整份复制（{n} 个项目）")
    elif st["projects_new"] or st["folders_new"] or st["meta_new"]:
        print(f"项目：新增 {st['projects_new']} 个 · 目录追加 {st['folders_new']} · "
              f"其它 {st['meta_new']}（{st['projects_matched']} 个按 slug 对齐）")
    else:
        print(f"项目：无变化（{st['projects_matched']} 个已对齐）")
    return st


# ---------------------------------------------------------------- 记忆 / 技能
def load_manifest(vault: Path) -> dict:
    p = vault / "manifest.json"
    if p.exists():
        try:
            return json.loads(p.read_text("utf-8"))
        except Exception:
            pass
    return {"version": 1, "files": {}, "last_sync": {}}


def save_manifest(vault: Path, man: dict) -> None:
    (vault / "manifest.json").write_text(
        json.dumps(man, ensure_ascii=False, indent=2), "utf-8"
    )


def _mem_entry(files: dict, rel: str) -> dict:
    """每个记忆文件的记录：``seen[机器] = {"local": h, "vault": h}``。

    记的是**这台机器最后一次看到的**两侧哈希。只记一个共享基线是不够的：另一台 push
    时会把它改写，于是这一台的差异再也没人认得出来 —— MEMORY.md 就是这样每轮都
    「复制 0 · 冲突 0」却又永远不一致。
    """
    e = dict(files.get(rel) or {})
    seen = e.get("seen")
    if not isinstance(seen, dict):
        seen = {}
        sides = e.get("sides")
        if isinstance(sides, dict):
            for k, h in sides.items():
                if isinstance(h, str):
                    seen[k] = {"local": h, "vault": h}
        elif isinstance(e.get("hash"), str):
            seen["*"] = {"local": e["hash"], "vault": e["hash"]}
    e["seen"] = {k: dict(v) for k, v in seen.items() if isinstance(v, dict)}
    e.pop("hash", None)
    e.pop("sides", None)
    return e


def _prev(seen: dict, side: str, which: str):
    """*side* 这台机器上次看到的 *which*（local / vault）那一侧的哈希。

    老版本的桥把 Linux 侧写作 "arch"，所以要顺着 SIDE_ALIASES 回退一次，
    否则旧 manifest 换个名字就等于「没有基线」，白跑一轮冲突。
    """
    rec = seen.get(side)
    if not isinstance(rec, dict):
        for alias in SIDE_ALIASES.get(side, ()):
            if isinstance(seen.get(alias), dict):
                rec = seen[alias]
                break
    if not isinstance(rec, dict):
        rec = seen.get("*") or {}
    return rec.get(which)


def _record(seen: dict, side: str, local: Path, vault: Path) -> None:
    seen[side] = {"local": sha256(local), "vault": sha256(vault)}


def _label(which: str, local_side: str) -> str:
    """副本文件名里的人话标签：本地那一侧用机器名（windows / arch），另一侧就是 vault"""
    return local_side if which == "local" else "vault"


def _stash(src: Path, root: Path, label: str, stem: str | None = None,
           suffix: str | None = None) -> str:
    """把一份内容存进 *root*/backups/bridge-memory/，返回相对 root 的路径。

    刻意**不放在 memories/ 里**：那个目录是 Hermes 的记忆库，多出来的 .md 有可能被当成
    第二份记忆读进去。副本只作留档，不参与读取。
    """
    d = root / "backups" / "bridge-memory"
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{(stem or src.stem)}.from-{label}-{time.strftime('%m%dT%H%M%S')}{suffix or src.suffix}"
    shutil.copy2(src, out)
    try:
        return str(out.relative_to(root))
    except ValueError:
        return str(out)


def sync_memory(src_root: Path, dst_root: Path, man: dict, dry=False, role="push") -> dict:
    """记忆文件三方比对：源 / 目标 / **上次同步时两侧各自的哈希**。

    为什么不能只记一个共享哈希：push 会把它改写成「刚推上去的那份」，于是另一台的
    差异就再也没人认得出来——每一轮 pull 都判成「目标侧有改动，保留」，两边永远不一致
    （MEMORY.md 就是这样：一边 832f55…、一边 2f392c…，却每轮都说「复制 0 · 冲突 0」）。

    四种结果：相同 / 源侧新 → 覆盖目标（先把目标存副本）/ 目标侧新 → 保留并明说 /
    两边各自动过 → 目标原样保留 + 源侧内容存成同级副本，等裁决。
    """
    out = {"copied": [], "kept": [], "conflicts": [], "backed_up": [], "same": 0}
    files = man.setdefault("files", {})
    local_side = side_name()
    src_which = "local" if role == "push" else "vault"
    dst_which = "vault" if role == "push" else "local"

    def local_path(rel: str) -> Path:
        return (src_root / rel) if role == "push" else (dst_root / rel)

    def vault_path(rel: str) -> Path:
        return (dst_root / rel) if role == "push" else (src_root / rel)

    for rel in MEM_FILES:
        s, d = src_root / rel, dst_root / rel
        e = _mem_entry(files, rel)
        seen = e["seen"]
        hs, hd = sha256(s), sha256(d)
        if hs is None and hd is None:
            continue
        prev_s = _prev(seen, local_side, src_which)
        prev_d = _prev(seen, local_side, dst_which)

        if hs == hd:
            out["same"] += 1
            if not dry:
                # 不在这里清 force_vault：那是对「还没同步的那台」的承诺，
                # 由真正执行覆盖的那次 sync 清（否则另一边永远拿不到裁决结果）。
                _record(seen, local_side, local_path(rel), vault_path(rel))
                files[rel] = e
            continue

        forced = role == "pull" and bool(e.get("force_vault"))
        if forced or hd is None:
            take_src, conflict = True, False
        elif prev_s is None or prev_d is None:
            # 这台机器没有基线（第一次同步这个文件）：按修改时间判，而不是一律报冲突
            take_src = hs is not None and s.stat().st_mtime > d.stat().st_mtime
            conflict = False
        else:
            src_changed, dst_changed = hs != prev_s, hd != prev_d
            if src_changed and not dst_changed:
                take_src, conflict = True, False        # 只有对面动过
            elif dst_changed and not src_changed:
                take_src, conflict = False, False       # 只有这边动过 → 保留，等这边 push
            else:
                take_src, conflict = False, True        # 两边都动过 / 早就没对齐

        if conflict:
            # 两边各自动过：目标侧原样保留，源侧内容存成**目标旁边**的副本供裁决
            if not dry:
                out["backed_up"].append(_stash(s, dst_root, _label(src_which, local_side),
                                              stem=Path(rel).stem, suffix=Path(rel).suffix))
            out["conflicts"].append(rel)
            files[rel] = e            # 基线不动：这份分歧要一直挂着，直到 memory-resolve
            continue

        if take_src:
            if not dry:
                d.parent.mkdir(parents=True, exist_ok=True)
                if d.exists() and hd is not None:
                    out["backed_up"].append(_stash(d, dst_root, _label(dst_which, local_side)))
                shutil.copy2(s, d)
                e.pop("force_vault", None)
                _record(seen, local_side, local_path(rel), vault_path(rel))
                files[rel] = e
            out["copied"].append(rel)
        else:
            files[rel] = e            # 基线不动：本地改动还没被桥接受，等这台 push 带出去
            out["kept"].append(rel)
    return out


def mem_states(home: Path, vault: Path, man: dict) -> list:
    """每个记忆文件的三方状态：(rel, 判定, 本地字节, 桥字节)"""
    files = man.get("files") or {}
    side = side_name()
    out = []
    for rel in MEM_FILES:
        a, b = home / rel, vault / rel
        ha, hb = sha256(a), sha256(b)
        e = _mem_entry(files, rel)
        sa, sb = _prev(e["seen"], side, "local"), _prev(e["seen"], side, "vault")
        if ha is None and hb is None:
            verdict = "两边都没有"
        elif ha == hb:
            verdict = "一致"
        elif e.get("force_vault"):
            verdict = "桥为准（本机 pull 后一致）"
        elif ha is not None and ha == sa and hb != sb:
            verdict = "桥较新"
        elif hb is not None and hb == sb and ha != sa:
            verdict = "本地较新"
        elif ha is None:
            verdict = "本地缺失"
        elif hb is None:
            verdict = "桥缺失"
        else:
            verdict = "两边都改过（需裁决）"
        out.append((rel, verdict, a.stat().st_size if a.exists() else None,
                    b.stat().st_size if b.exists() else None))
    return out


def sync_tree(src_root: Path, dst_root: Path, subdirs, dry=False, limit=None) -> dict:
    """目录树：只把「对边没有 / 对边更旧」的文件搬过去。不删任何东西。"""
    out = {"copied": 0, "same": 0, "newer_local": 0, "examples": []}
    for sub in subdirs:
        sroot = src_root / sub
        if not sroot.is_dir():
            continue
        for f in sroot.rglob("*"):
            if not f.is_file() or f.name.startswith("."):
                continue
            rel = f.relative_to(src_root)
            d = dst_root / rel
            if d.exists():
                sm, dm = f.stat().st_mtime, d.stat().st_mtime
                if dm >= sm - 1:
                    out["same"] += 1
                    if dm > sm + 1:
                        out["newer_local"] += 1
                    continue
            if limit and out["copied"] >= limit:
                continue
            if not dry:
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, d)
            out["copied"] += 1
            if len(out["examples"]) < 8:
                out["examples"].append(str(rel))
    return out


# ---------------------------------------------------------------- 统计
def counts(db_path: Path) -> dict:
    c = ro(db_path)
    try:
        n_msg = c.execute("select count(*) from messages").fetchone()[0]
        n_ses = c.execute("select count(*) from sessions").fetchone()[0]
        last = c.execute(
            "select datetime(timestamp,'unixepoch','localtime') from messages order by id desc limit 1"
        ).fetchone()[0]
        return {"messages": n_msg, "sessions": n_ses, "last": last}
    finally:
        c.close()


def diff_counts(local_db: Path, vault_db: Path) -> dict:
    a, v = ro(local_db), ro(vault_db)
    try:
        sa = {r[0] for r in a.execute("select id from sessions")}
        sv = {r[0] for r in v.execute("select id from sessions")}
        ua = set(a.execute("select session_id, message_uid from messages"))
        uv = set(v.execute("select session_id, message_uid from messages"))
        return {
            "sessions_only_local": len(sa - sv),
            "sessions_only_vault": len(sv - sa),
            "messages_only_local": len(ua - uv),
            "messages_only_vault": len(uv - ua),
        }
    finally:
        a.close()
        v.close()


# ---------------------------------------------------------------- 命令
def receipt_gaps(db_path: Path) -> list:
    """本机这些本地会话缺收据 —— 它们点开会 storage_unavailable"""
    c = ro(db_path)
    try:
        return [r[0] for r in c.execute(
            "select s.id from sessions s where s.id glob 'local-*' and s.id not in "
            "(select substr(key, ?) from state_meta where key glob ?)",
            (len(POLICY_PREFIX) + 1, POLICY_PREFIX + "*"))]
    finally:
        c.close()


def _entries(path: Path) -> list:
    try:
        text = path.read_text("utf-8", errors="replace").replace("\r\n", "\n")
    except OSError:
        return []
    return [x.strip() for x in text.split("\n§\n") if x.strip()]


def _entry_diff(a: Path, b: Path, limit=8) -> list:
    """两个记忆文件的条目级差异（MEMORY.md 的条目用独立的 ``§`` 行分隔）"""
    ea, eb = _entries(a), _entries(b)
    sb, sa = set(eb), set(ea)
    only_a = [x for x in ea if x not in sb]
    only_b = [x for x in eb if x not in sa]
    if not only_a and not only_b:
        return [f"条目 {len(ea)}/{len(eb)} 完全相同集合（差异只在措辞或换行）"]
    lines = [f"条目 本地 {len(ea)} / 桥 {len(eb)} —— 本地独有 {len(only_a)}，桥独有 {len(only_b)}"]
    for x in only_a[:limit]:
        lines.append("  只在本机: " + x.splitlines()[0][:64])
    for x in only_b[:limit]:
        lines.append("  只在桥  : " + x.splitlines()[0][:64])
    if len(only_a) > limit or len(only_b) > limit:
        lines.append("  …（更多条目略）")
    return lines


def print_mem(mem: dict) -> None:
    line = (f"记忆：复制 {len(mem['copied'])} · 相同 {mem['same']} · "
            f"保留 {len(mem['kept'])} · 冲突 {len(mem['conflicts'])}")
    if mem["backed_up"]:
        line += f" · 副本 {len(mem['backed_up'])}"
    print(line)
    for rel in mem["kept"]:
        print(f"  · {rel}：目标侧有改动，保留它 —— 要把这边的内容带过去，就在这台再跑一次对应命令")
    for rel in mem["conflicts"]:
        print(f"  ! {rel}：两边都改过，谁都没覆盖谁 —— 用 memory-resolve 裁决")
    for name in mem["backed_up"]:
        print(f"    · 已留副本 {name}")


def cmd_memory_resolve(args):
    """记忆文件裁决：先看差异，再指定哪一份为准，把它压到两边（并记进基线）"""
    home = profile_home(args.profile, args.profile_home)
    vault = vault_dir(args.vault)
    man = load_manifest(vault)
    local_side = side_name()
    use = (getattr(args, "use", None) or "").strip()
    use_file = (getattr(args, "use_file", None) or "").strip()
    apply_ = _apply_guard(args)

    print(f"本机 : {home}  [{local_side}]")
    print(f"桥   : {vault}\n")
    for rel, verdict, sa, sb in mem_states(home, vault, man):
        la = f"{sa}B" if sa is not None else "—"
        lb = f"{sb}B" if sb is not None else "—"
        print(f"  {rel:<22} 本地 {la:>7} · 桥 {lb:>7} · {verdict}")

    if not use and not use_file:
        for rel in MEM_FILES:
            a, b = home / rel, vault / rel
            if a.exists() and b.exists() and sha256(a) != sha256(b):
                print(f"\n--- {rel} ---")
                for line in _entry_diff(a, b):
                    print("  " + line)
        print("\n（只报告。真要写：--use local|vault 或 --use-file <文件>，再加 --apply）")
        return 0

    if use_file:
        src_path = Path(use_file).expanduser()
        if not src_path.exists():
            raise SystemExit(f"没有这个文件：{src_path}")
        targets = [(rel, src_path) for rel in MEM_FILES if Path(rel).name == src_path.name]
        if not targets:
            raise SystemExit(f"{src_path.name} 不是记忆文件（只认 MEMORY.md / USER.md / SOUL.md）")
    elif use in ("local", "vault"):
        targets = [(rel, (home if use == "local" else vault) / rel) for rel in MEM_FILES]
    else:
        raise SystemExit("--use 只认 local 或 vault（或给 --use-file <文件>）")

    force = bool(getattr(args, "force_vault", False))
    for rel, src_path in targets:
        if not src_path.exists():
            print(f"  · {rel}: 来源不存在，跳过")
            continue
        blob = src_path.read_bytes()
        h = hashlib.sha256(blob).hexdigest()
        for root, label in ((home, local_side), (vault, "vault")):
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            same = dst.exists() and dst.read_bytes() == blob
            if not same and dst.exists() and apply_:
                kept = _stash(dst, root, label + "-pre-resolve")
                print(f"  · 备份 {rel}（{label}）-> {kept}")
            if not same and apply_:
                dst.write_bytes(blob)
        e = _mem_entry(man.setdefault("files", {}), rel)
        e["seen"][local_side] = {"local": h, "vault": h}
        if force:
            e["force_vault"] = True
        else:
            e.pop("force_vault", None)
        e["at"] = now_iso()
        man["files"][rel] = e
        print(f"  {'[OK]' if apply_ else '[dry-run]'} {rel} <- {src_path}  ({h[:12]}…)")

    if not apply_:
        print("\n[dry-run] 什么都没写。加 --apply 真执行。")
        return 0
    save_manifest(vault, man)
    print("[OK] 本地与桥现在同一份")
    if force:
        print("已标记 force_vault：另一台下次 pull 会以桥为准（它本地那份先存成 .from-<机器> 副本）")
    return 0


def cmd_status(args):
    home = profile_home(args.profile, args.profile_home)
    vault = vault_dir(args.vault)
    ldb = home / DB
    vdb = vault / DB
    print(f"桥目录   : {vault}")
    print(f"本地     : {home}  [{side_name()}]")
    print(f"本地库   : {ldb}  ({human(ldb.stat().st_size) if ldb.exists() else '缺失'})")
    if not ldb.exists():
        raise SystemExit("本地没有 state.db —— 这个 profile 还没初始化过？")
    cl = counts(ldb)
    print(f"  会话 {cl['sessions']} · 消息 {cl['messages']} · 最新 {cl['last']}")
    gaps = receipt_gaps(ldb)
    if gaps:
        print(f"  ! {len(gaps)} 个本地会话缺收据（点开会 storage_unavailable）：")
        for sid in gaps[:5]:
            print(f"      {sid}")
        print("    修：scripts/rebind_imported_local.py --db <本文件> --apply，或从另一台 pull 一次")
    else:
        print("  本地会话收据：齐全")
    if not vdb.exists():
        print("桥里还没有 state.db —— 先跑一次 push 建立桥")
    else:
        cv = counts(vdb)
        print(f"桥       : {vdb}  ({human(vdb.stat().st_size)})")
        print(f"  会话 {cv['sessions']} · 消息 {cv['messages']} · 最新 {cv['last']}")
        d = diff_counts(ldb, vdb)
        print("差异（本地多 / 桥多）：")
        print(f"  会话  {d['sessions_only_local']:>4} / {d['sessions_only_vault']:>4}")
        print(f"  消息  {d['messages_only_local']:>4} / {d['messages_only_vault']:>4}")
        chk = ro(vdb).execute("PRAGMA quick_check").fetchone()[0]
        print(f"  桥库完整性: {chk}")
    man = load_manifest(vault)
    print(f"上次同步 : {man.get('last_sync', {}) or '（无记录）'}")
    for rel, verdict, sa, sb in mem_states(home, vault, man):
        sizes = f"本地 {sa}B / 桥 {sb}B" if sa is not None and sb is not None else ""
        print(f"  {rel:<22} {verdict:<18} {sizes}")
    return 0


def _apply_guard(args) -> bool:
    return not args.dry_run and args.apply


def _refresh_anchor(args, home: Path, vault: Path, dry: bool) -> None:
    """push/pull 之后顺手刷新小分区保险箱；找不到小分区就安静跳过"""
    if getattr(args, "no_anchor", False):
        return
    root = anchor_root(getattr(args, "anchor", None))
    if root is None:
        return
    try:
        st = anchor_push(home, vault, root, dry=dry, force=getattr(args, "force", False))
    except SystemExit as e:
        print(f"  ! 保险箱刷新失败：{e}")
        return
    except Exception as e:  # 保险箱是加分项，绝不能因为它把 push/pull 弄挂
        print(f"  ! 保险箱刷新出错（不影响桥）：{type(e).__name__}: {e}")
        return
    if dry:
        print("保险箱：[dry-run] 会刷新")
    elif st["skipped"]:
        print("保险箱：计数没变，跳过快照")
    elif st["snapshot"]:
        print(f"保险箱：{st['snapshot']} · 解压校验"
              f"{'通过' if st['verified'] else '未做'}")

def do_push(args) -> int:
    home = profile_home(args.profile, args.profile_home)
    vault = vault_dir(args.vault)
    ldb, vdb = home / DB, vault / DB
    dry = not _apply_guard(args)
    if not ldb.exists():
        raise SystemExit("本地没有 state.db")
    vault.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(prefix="hbridge-push-"))
    snap = snapshot_db(ldb, tmpdir / "local.db")
    try:
        if not vdb.exists():
            if dry:
                print("[dry-run] 桥里还没有库，会新建：", vdb)
                return 0
            shutil.copy2(snap, vdb)
            print("[OK] 桥已建立（整份复制）")
        else:
            # 合并进桥：先把桥复制一份到临时位置，合完再原子替换
            # 注意：临时文件必须和桥在同一个卷上，否则 Windows 上 os.replace 跨盘会失败
            work = vault / f".merge-{os.getpid()}.db"
            if work.exists():
                work.unlink()
            shutil.copy2(vdb, work)
            conn = sqlite3.connect(str(work), timeout=60)
            conn.execute("PRAGMA busy_timeout=60000")
            conn.execute("BEGIN IMMEDIATE")
            st = merge_db(snap, conn)
            if dry:
                conn.rollback()
            else:
                conn.commit()
            conn.close()
            print(f"桥 <- 本地：新增会话 {st['sessions_new']} · 新增消息 {st['messages_new']}")
            if st.get("receipts_new"):
                print(f"  收据：{st['receipts_new']} 张一起放进桥（另一台 pull 时才有用）")
            if st["sessions_renamed"]:
                for sid, old, new in st["sessions_renamed"]:
                    print(f"  · 标题重名，改作「{new}」（原「{old}」）")
            if not dry:
                if st["messages_new"] or st["sessions_new"] or st.get("receipts_new"):
                    backup_local(vdb, vault / "backups")
                    os.replace(work, vdb)
                else:
                    work.unlink(missing_ok=True)  # 没变化：不留垃圾、不白白备份 66MB

        man = load_manifest(vault)
        mem = sync_memory(home, vault, man, dry=dry)
        sk = sync_tree(home, vault, SYNC_TREES, dry=dry)
        print_mem(mem)
        if (vault / PROJECTS_DB).exists() and not dry:
            backup_projects(vault / PROJECTS_DB, vault / "backups")
        sync_projects(home / PROJECTS_DB, vault / PROJECTS_DB, dry=dry)
        sync_cron(home / "cron", vault / "cron", dry=dry)
        print(f"技能/脚本：复制 {sk['copied']} 个（对方已有的更新 {sk['newer_local']} 个保留不动）")
        if not dry:
            man.setdefault("last_sync", {})["push_from_" + side_name()] = now_iso()
            save_manifest(vault, man)
            _refresh_anchor(args, home, vault, dry=False)
            print("[OK] push 完成")
        else:
            print("[dry-run] 什么都没写。加 --apply 真跑。")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return 0


def do_pull(args) -> int:
    home = profile_home(args.profile, args.profile_home)
    vault = vault_dir(args.vault)
    ldb, vdb = home / DB, vault / DB
    dry = not _apply_guard(args)
    if not vdb.exists():
        raise SystemExit("桥里还没有 state.db —— 先在另一台跑一次 push")
    if not ldb.exists():
        raise SystemExit("本地没有 state.db")
    tmpdir = Path(tempfile.mkdtemp(prefix="hbridge-pull-"))
    try:
        vsnap = snapshot_db(vdb, tmpdir / "vault.db")
        conn = sqlite3.connect(str(ldb), timeout=60)
        conn.execute("PRAGMA busy_timeout=60000")
        if not dry:
            b = backup_local(ldb, home / "backups" / "bridge")
            print(f"已备份本地库 -> {b.name}")
        conn.execute("BEGIN IMMEDIATE")
        _paths = load_paths(vault)
        st = merge_db(vsnap, conn, target_home=home, paths=_paths)
        if dry:
            conn.rollback()
        else:
            conn.commit()
        conn.close()
        print(f"本地 <- 桥：新增会话 {st['sessions_new']} · 新增消息 {st['messages_new']}")
        if st.get("receipts_new"):
            print(f"  补了 {st['receipts_new']} 张本地会话收据 —— 这些旧会话现在能点开继续聊")
        for sid, old, new in (st.get("receipts_cwd_mapped") or [])[:3]:
            print(f"  · 工作目录 {old} -> {new}")
        if st.get("receipts_foreign"):
            print(f"  ! {len(st['receipts_foreign'])} 张收据仍是别的主机路径，"
                  f"跑 scripts/rebind_imported_local.py 修")
        missing = st.get("receipts_missing") or []
        if missing:
            print(f"  ! {len(missing)} 个旧会话两边都没有收据，点开会 storage_unavailable；"
                  f"跑 scripts/rebind_imported_local.py 补")
        if st.get("cwd_changed"):
            print(f"  工作目录翻成本机路径 {len(st['cwd_changed'])} 个（这样项目归属/终端目录才对）")
        if st["touched_sessions"]:
            print(f"  补齐了 {len(st['touched_sessions'])} 个会话的尾巴，界面里关掉重开即可看到")
        if st["sessions_renamed"]:
            for sid, old, new in st["sessions_renamed"]:
                print(f"  · 标题重名，改作「{new}」（原「{old}」）")

        man = load_manifest(vault)
        mem = sync_memory(vault, home, man, dry=dry, role="pull")
        sk = sync_tree(vault, home, SYNC_TREES, dry=dry)
        print_mem(mem)
        if (home / PROJECTS_DB).exists() and not dry:
            backup_projects(home / PROJECTS_DB, home / "backups" / "bridge")
        sync_projects(vault / PROJECTS_DB, home / PROJECTS_DB, dry=dry, paths=load_paths(vault))
        sync_cron(vault / "cron", home / "cron", dry=dry)
        print(f"技能/脚本：复制 {sk['copied']} 个")
        if not dry:
            man.setdefault("last_sync", {})["pull_to_" + side_name()] = now_iso()
            save_manifest(vault, man)
            _refresh_anchor(args, home, vault, dry=False)
            print("[OK] pull 完成")
        else:
            print("[dry-run] 什么都没写。加 --apply 真跑。")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return 0


# ---------------------------------------------------------------- 锚点（99MB 小分区）
ANCHOR_MARK = ".hermes-anchor"
ANCHOR_KEEP = 2
ANCHOR_NAME = "HermesAnchor"


def anchor_root(explicit: str | None = None) -> Path | None:
    """找小分区：Linux 上看挂载根目录有没有标记文件，Windows 上扫盘符"""
    if explicit:
        return Path(explicit).expanduser().resolve()
    if os.environ.get("HERMES_ANCHOR"):
        return Path(os.environ["HERMES_ANCHOR"]).expanduser().resolve()
    if os.name == "nt":
        for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
            p = Path(letter + ":/")
            try:
                if (p / ANCHOR_MARK).exists():
                    return p
            except OSError:
                continue
        return None
    import glob as _glob

    for pat in ("/run/media/*/*/" + ANCHOR_MARK, "/run/media/*/" + ANCHOR_MARK,
                "/mnt/*/" + ANCHOR_MARK, "/Volumes/*/" + ANCHOR_MARK):
        for hit in _glob.glob(pat):
            return Path(hit).parent
    return None


def _human_mb(n: int | float) -> str:
    return f"{n / 1048576:.1f}MB"


def anchor_push(home: Path, vault: Path, root: Path, dry=False, force=False) -> dict:
    """把「不可再生」的东西 + 一份压缩整库 放进小分区

    快照内容 = 桥 ∪ 本地（本地可能比桥新：还没 push 就先 anchor 也不会漏消息）
    """
    import gzip
    import tarfile

    if not (home / DB).exists() and not (vault / DB).exists():
        raise SystemExit("没有可快照的 state.db")
    prof_dir, snap_dir = root / "profile", root / "snapshots"
    out = {"copied": [], "snapshot": None, "skipped": False, "counts": {}, "verified": None}
    if not dry:
        prof_dir.mkdir(parents=True, exist_ok=True)
        snap_dir.mkdir(parents=True, exist_ok=True)

    # 1) 小而不可再生：记忆 / 人格 / 配置
    for rel in MEM_FILES + ["config.yaml", CRON_JOBS]:
        s = home / rel
        if not s.exists():
            continue
        d = prof_dir / rel
        if d.exists() and sha256(d) == sha256(s):
            continue
        if not dry:
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
        out["copied"].append(rel)

    # 1b) 项目定义（projects.db）：不可再生，才几十 KB，值得进保险箱
    pj = home / PROJECTS_DB
    if pj.exists():
        tgt_pj = prof_dir / PROJECTS_DB
        if not tgt_pj.exists() or sha256(tgt_pj) != sha256(pj):
            if not dry:
                snapshot_db(pj, tgt_pj)
            out["copied"].append(PROJECTS_DB)

    # 2) 技能打包
    sk = home / "skills"
    if sk.is_dir():
        tgt = prof_dir / "skills.tar.gz"
        scr = home / "scripts"
        stamp = max((f.stat().st_mtime for f in list(sk.rglob("*")) + list(scr.rglob("*"))
                     if f.is_file()), default=0)
        if not tgt.exists() or tgt.stat().st_mtime < stamp or dry:
            if not dry:
                tmp = prof_dir / ".skills.tar.gz.tmp"
                with tarfile.open(tmp, "w:gz") as tf:
                    tf.add(sk, arcname="skills")
                    if scr.is_dir():
                        tf.add(scr, arcname="scripts")
                os.replace(tmp, tgt)
            out["copied"].append("skills.tar.gz")

    # 3) 快照：桥 ∪ 本地 → 一致副本 → gzip -9
    tmpdir = Path(tempfile.mkdtemp(prefix="hbridge-anchor-"))
    try:
        local_snap = snapshot_db(home / DB, tmpdir / "local.db") if (home / DB).exists() else None
        mix = tmpdir / "mix.db"
        has_vault = (vault / DB).exists()
        if has_vault:
            shutil.copy2(vault / DB, mix)          # 桥不是活库，直接复制
        elif local_snap:
            shutil.copy2(local_snap, mix)
        else:
            raise SystemExit("没有可快照的 state.db")
        if has_vault and local_snap:
            conn = sqlite3.connect(str(mix), timeout=60)
            conn.execute("PRAGMA busy_timeout=60000")
            conn.execute("BEGIN IMMEDIATE")
            merge_db(local_snap, conn)
            conn.commit()
            conn.close()

        cnts = counts(mix)
        out["counts"] = cnts
        metas = sorted(snap_dir.glob("snap-*.json"))
        if metas and not force:
            try:
                last = json.loads(metas[-1].read_text("utf-8"))
                if (last.get("messages") == cnts["messages"]
                        and last.get("sessions") == cnts["sessions"]):
                    out["skipped"] = True
            except Exception:
                pass

        if not out["skipped"]:
            ts = time.strftime("%Y%m%dT%H%M%S")
            free = shutil.disk_usage(root).free
            # gzip 实测约 37%，再留 12MB 余量给临时文件
            need = int(mix.stat().st_size * 0.45) + (12 << 20)
            # 库会长大：99MB 小分区装不下“旧快照 + 新快照”时，先轮转掉最旧的再写。
            # 不这么做的话一旦 free < need 就永久失败（旧快照自己占着位置）。
            for old in sorted(snap_dir.glob("snap-*.db.gz")):
                if free >= need:
                    break
                size = old.stat().st_size
                if not dry:
                    old.unlink(missing_ok=True)
                    (snap_dir / (old.name[:-len(".db.gz")] + ".json")).unlink(missing_ok=True)
                free += size
                out.setdefault("pruned", []).append(old.name)
            if free < need:
                raise SystemExit(
                    f"小分区空间不够：预计需要 {_human_mb(need)}，现在只剩 {_human_mb(free)}"
                )
            gz = snap_dir / f"snap-{ts}.db.gz"
            tmp = snap_dir / f".snap-{ts}.db.gz.tmp"
            if not dry:
                with mix.open("rb") as fi, gzip.open(tmp, "wb", compresslevel=9) as fo:
                    shutil.copyfileobj(fi, fo, 1 << 20)
                os.replace(tmp, gz)
                # 4) 校验：解压回去 → quick_check + 计数
                back = tmpdir / "back.db"
                with gzip.open(gz, "rb") as fi, back.open("wb") as fo:
                    shutil.copyfileobj(fi, fo, 1 << 20)
                c = ro(back)
                chk = c.execute("PRAGMA quick_check").fetchone()[0]
                n_msg = c.execute("select count(*) from messages").fetchone()[0]
                n_ses = c.execute("select count(*) from sessions").fetchone()[0]
                c.close()
                ok = (chk == "ok" and n_msg == cnts["messages"] and n_ses == cnts["sessions"])
                out["verified"] = ok
                if not ok:
                    raise SystemExit(
                        f"快照校验失败：quick_check={chk}，消息 {n_msg}/{cnts['messages']}，"
                        f"会话 {n_ses}/{cnts['sessions']}"
                    )
                (snap_dir / f"snap-{ts}.json").write_text(json.dumps(
                    {"snapshot": gz.name, "messages": n_msg, "sessions": n_ses,
                     "bytes": gz.stat().st_size, "sha256": sha256(gz), "at": now_iso()},
                    ensure_ascii=False, indent=2), "utf-8")
                out["snapshot"] = gz.name
                # 5) 轮转：只留最新 ANCHOR_KEEP 份
                for old in sorted(snap_dir.glob("snap-*.db.gz"))[:-ANCHOR_KEEP]:
                    old.unlink(missing_ok=True)
                    (snap_dir / (old.name[:-len(".db.gz")] + ".json")).unlink(missing_ok=True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # 6) 总清单
    if not dry:
        files = {}
        for f in sorted(prof_dir.rglob("*")):
            if f.is_file():
                files[str(f.relative_to(prof_dir))] = {"sha256": sha256(f), "bytes": f.stat().st_size}
        man = {
            "updated": now_iso(),
            "side": side_name(),
            "counts": out["counts"],
            "snapshot": out["snapshot"],
            "snapshots": [f.name for f in sorted(snap_dir.glob("snap-*.db.gz"))],
            "files": files,
        }
        (root / "ANCHOR.json").write_text(json.dumps(man, ensure_ascii=False, indent=2), "utf-8")
    return out


def cmd_anchor(args) -> int:
    home = profile_home(args.profile, args.profile_home)
    vault = vault_dir(args.vault)
    root = anchor_root(args.anchor)
    if root is None:
        raise SystemExit("找不到小分区（99MB 那个卷）。挂上它，或用 --anchor 指定路径。")
    st = anchor_push(home, vault, root, dry=args.dry_run, force=args.force)
    free = shutil.disk_usage(root).free
    print(f"锚点 : {root}  （剩余 {_human_mb(free)}）")
    print(f"文字件复制 {len(st['copied'])} 个：{', '.join(st['copied']) or '无变化'}")
    if st["skipped"]:
        print("库快照：计数没变，跳过（已有那份就是最新的）")
    elif st["snapshot"]:
        print(f"库快照：{st['snapshot']} · {st['counts']['messages']} 消息 / "
              f"{st['counts']['sessions']} 会话 · 解压校验 {'通过' if st['verified'] else '未做'}")
    if args.dry_run:
        print("[dry-run] 什么都没写。")
    return 0


def cmd_verify(args) -> int:
    root = anchor_root(args.anchor)
    if root is None:
        raise SystemExit("找不到小分区。")
    man_p = root / "ANCHOR.json"
    if not man_p.exists():
        raise SystemExit("小分区里还没有 ANCHOR.json —— 先跑一次 anchor")
    man = json.loads(man_p.read_text("utf-8"))
    print(f"锚点 : {root}")
    print(f"写入 : {man['updated']}（来自 {man['side']}）")
    print(f"记录 : {man['counts']['messages']} 消息 / {man['counts']['sessions']} 会话")
    ok = True
    for name in man.get("snapshots", []):
        gz = root / "snapshots" / name
        if not gz.exists():
            print(f"  ! 快照缺失：{name}")
            ok = False
            continue
        h = sha256(gz)
        print(f"  {name:<28} {_human_mb(gz.stat().st_size):>9}  sha256 {h[:16]}…")
    for rel, info in man.get("files", {}).items():
        f = root / "profile" / rel
        if not f.exists() or sha256(f) != info.get("sha256"):
            print(f"  ! 文件与清单不符：{rel}")
            ok = False
    home = profile_home(args.profile, args.profile_home)
    vault = vault_dir(args.vault)
    for label, db in (("本地库", home / DB), ("桥库", vault / DB)):
        if not db.exists():
            continue
        c = counts(db)
        drift = c["messages"] - man["counts"]["messages"]
        print(f"  {label}：{c['messages']} 消息（比锚点多 {drift:+d}）")
    print("结论：" + ("锚点完好，与清单一致 ✅" if ok else "有东西对不上，见上面的感叹号"))
    return 0 if ok else 2


def cmd_restore(args) -> int:
    root = anchor_root(args.anchor)
    if root is None:
        raise SystemExit("找不到小分区。")
    snaps = sorted((root / "snapshots").glob("snap-*.db.gz"))
    if not snaps:
        raise SystemExit("小分区里没有快照")
    gz = root / "snapshots" / args.which if args.which else snaps[-1]
    if not gz.exists():
        raise SystemExit(f"没有这个快照：{gz.name}")
    import gzip

    target = Path(args.to).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    out = target / DB
    if out.exists() and not args.force:
        raise SystemExit(f"{out} 已存在；加 --force 覆盖")
    with gzip.open(gz, "rb") as fi, out.open("wb") as fo:
        shutil.copyfileobj(fi, fo, 1 << 20)
    c = ro(out)
    chk = c.execute("PRAGMA quick_check").fetchone()[0]
    n = c.execute("select count(*) from messages").fetchone()[0]
    s = c.execute("select count(*) from sessions").fetchone()[0]
    c.close()
    print(f"已还原 {gz.name} -> {out}")
    print(f"完整性 {chk} · {n} 消息 · {s} 会话")
    print("用法：关掉 Hermes，把这份 state.db 复制成 "
          "<profile>/state.db（原库先改名备份）。")
    return 0


def main(argv=None):
    _console_safe()
    ap = argparse.ArgumentParser(
        prog="hermes-bridge",
        description="多系统 Hermes 数据桥（只增不改）：Windows ⇄ Linux ⇄ macOS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "日常：离开前 push，回来后 pull。加 --dry-run 先看会做什么。\n"
            "push/pull 会自动把保险箱（小分区）刷新一遍（--no-anchor 可跳过）。\n"
            "anchor = 只刷新保险箱；verify = 体检；restore = 从保险箱还原一份 state.db。"
        ),
    )
    ap.add_argument("command", choices=["status", "push", "pull", "anchor", "verify", "restore",
                                        "memory-resolve"])
    ap.add_argument("--vault", help="桥目录（默认：脚本所在目录 / $HERMES_BRIDGE）")
    ap.add_argument("--anchor", help="保险箱所在卷（默认自动找带 .hermes-anchor 的卷）")
    ap.add_argument("--profile", default=None,
                    help=f"profile 名（默认：${PROFILE_ENV} 或 profiles 目录里唯一的那个）")
    ap.add_argument("--profile-home", help="直接指定 profile 目录（测试用）")
    ap.add_argument("--dry-run", action="store_true", help="只看计划，不写任何东西")
    ap.add_argument("--apply", action="store_true", help="真的执行（push/pull 需要它）")
    ap.add_argument("--no-anchor", action="store_true", help="push/pull 时不动小分区")
    ap.add_argument("--force", action="store_true", help="强制重做快照 / 覆盖还原目标")
    ap.add_argument("--which", help="restore 指定快照文件名（默认最新的）")
    ap.add_argument("--to", help="restore 输出目录")
    ap.add_argument("--use", help="memory-resolve：以哪一侧为准（local | vault）")
    ap.add_argument("--use-file", help="memory-resolve：以一个现成文件为准（MEMORY.md / USER.md / SOUL.md）")
    ap.add_argument("--force-vault", action="store_true",
                    help="memory-resolve：标记「桥为准」，让另一台下次 pull 无条件变成一致")
    args = ap.parse_args(argv)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "push":
        return do_push(args)
    if args.command == "pull":
        return do_pull(args)
    if args.command == "memory-resolve":
        return cmd_memory_resolve(args)
    if args.command == "anchor":
        return cmd_anchor(args)
    if args.command == "verify":
        return cmd_verify(args)
    return cmd_restore(args)


if __name__ == "__main__":
    sys.exit(main())
