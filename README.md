# hermes-bridge

**双系统 / 两台机器之间，把 Hermes 的「你」搬过去 —— 只增不改，一条消息都不丢。**

零依赖（纯 Python 标准库，单文件可拷走），Windows / Linux / macOS 通用。

> *Sync a Hermes Agent profile across a dual-boot machine or two computers.
> Append-only merge, never overwrites, no dependencies. [English →](#english)*

---

## 问题

Hermes 把所有东西都存在一个 profile 里：聊天库 `state.db`、`memories/`、`skills/`、
项目、定时任务。**一个 profile 只属于一台机器的一个系统。**

于是装双系统的人会遇到这件事：

```
Windows 上聊了三个月的"她" ──┐
                            ├─ 换到 Linux 启动后：全新的空白 Hermes
Linux 上新聊的一切 ─────────┘   两边的记忆、技能、项目互不相识
```

「手动导出 / 导入 profile」能用，但有两个致命问题：

1. **是覆盖，不是合并**。导入一份旧包，中间那几天说的话就没了。
2. **你得记得做**。忘了就丢——而丢的那部分是不可再生的。

## 它怎么解决

一份**桥**目录（放在两个系统都能读写的分区上）＋ 两个命令：

```
      系统 A                                    系统 B
  <profile A>                               <profile B>
       │                                          │
       │ ①push（离开前）                          │ ①push（离开前）
       ▼                                          ▼
  ┌────────────────── 桥（vault）──────────────────┐
  │  state.db      合并库 · 只增不改                │
  │  memories/  skills/  projects.db  cron/         │
  └────────────────────────────────────────────────┘
       ▲                                          ▲
       │ ②pull（回来后）                          │ ②pull（回来后）

  可选的保险箱（anchor，一块小分区）：
  profile/   = 记忆、人格、配置、技能打包（不可再生的那一小撮）
  snapshots/ = 整库压缩快照（留最新 N 份）
```

桥**不是**两边共用的活库——那在 NTFS + WAL 下迟早坏库。它是中转站：
A 投进去，B 取出来，两边各自保留完整的本地库。

## 核心承诺：只增不改

这是整个项目唯一不能妥协的设计：

- 消息按指纹 `message_uid` 去重，合并**只有 `INSERT`**，永远不 `UPDATE` / `DELETE` 已有消息
- 会话补齐后按 `(timestamp, id)` 重排 `display_order`，否则桌面端分页会新旧交错
- 合并完重算 `message_count` 等计数（不信源库里的计数）
- 每次写入活库前用 `sqlite3.Connection.backup()` 做一致快照（**不是 `cp`**——WAL 里可能还有几千条没落盘），留最近 5 份
- 活库只用 `BEGIN IMMEDIATE` + `busy_timeout` 短事务写，网关开着也能过
- 每次合并后 `PRAGMA quick_check`

**「跑一次 push / pull」这个动作本身，没有任何代码路径能删掉你的会话或消息。**
这句话有测试兜底，见 [验证](#验证)。

## 快速开始

```bash
git clone https://github.com/ClanceyPRMS/hermes-bridge.git
cd hermes-bridge
```

**1. 把桥放在两边都能读写的地方**，比如 Windows 的 `D:\HermesBridge`，
Linux 下挂载后在 `/mnt/d/HermesBridge` 或 `/run/media/<你>/<卷标>/HermesBridge`。

最省事的做法：**直接把这个仓库 clone 成桥目录**，代码和数据同住。

```bash
git clone https://github.com/ClanceyPRMS/hermes-bridge.git /mnt/d/HermesBridge
cd /mnt/d/HermesBridge
echo "" > .hermes-bridge          # 标记：这个目录是桥
python3 hermes-bridge.py status
```

**2. 装启动器**（Linux / macOS）：

```bash
install -m 755 bin/hermes-bridge ~/.local/bin/hermes-bridge
```

Windows 用仓库里的 `hermes-bridge.cmd`（会自动去找 Hermes 自带的 Python）：

```bat
D:\HermesBridge\hermes-bridge.cmd status
```

### 桥已经跑起来了，只想接上这个仓库

`git clone` 到非空目录会失败。在**已有的桥目录**里这么做（只签出代码，数据一个字节不碰）：

```bash
cd /mnt/d/HermesBridge
git init -b main
git remote add origin https://github.com/ClanceyPRMS/hermes-bridge.git
git fetch --depth=1 origin main
git checkout -f FETCH_HEAD -- \
  .gitignore LICENSE README.md pathmap.example.json bin \
  hermes-bridge.py hermes-bridge.cmd \
  rebind_imported_local.py verify_sync_no_loss.py
```

之后 `git status` 只会看到这几个代码文件；`state.db`、`memories/`、`pathmap.json`、`manifest.json`
全在 `.gitignore` 里，`git` 看不见它们。

> **NTFS 上的提醒**：桥放在 NTFS 分区时，Linux 下没有真正的 Unix 权限位，`git status` 会出现大量
> "mode change" 噪音，可执行位也可能丢。正经开发时把仓库放在 ext4（如 `~/projects/hermes-bridge`），
> 改完再把代码文件拷进桥目录——桥目录那份只当「运行用的副本」。

> **Hermes 有多个 profile 时**：自动探测会拒绝猜，`status` 会列出候选并要你指定。设一次
> `HERMES_PROFILE=<名字>`（Windows：`setx HERMES_PROFILE <名字>`）或每次加 `--profile <名字>`。

**3. 开始用**：

```bash
hermes-bridge status            # 先看：两边差多少、记忆一致不、桥库完好不
hermes-bridge push --apply      # 离开这台机器前
hermes-bridge pull --apply      # 到这台机器后
```

不加 `--apply` 就只报告、不写任何东西。想看计划用 `--dry-run`。

## 命令

| 命令 | 做什么 |
|---|---|
| `status` | 只读体检：本机 vs 桥的差量、记忆三个文件是否一致、桥库 `quick_check` |
| `push --apply` | 把本机新增的推进桥（并顺手刷新保险箱） |
| `pull --apply` | 把桥里本机没有的取回来（取之前先给活库做快照） |
| `anchor` | 只刷新保险箱：不可再生文件 + 一份压缩整库 |
| `verify` | 对保险箱清单、比对计数与 sha256 |
| `restore --to <目录>` | 从最新快照解出一份 `state.db` 并验完整性 |
| `memory-resolve` | 记忆冲突的裁决（只报告；加 `--use/--use-file --apply` 才写） |

常用开关：

| 开关 | 说明 |
|---|---|
| `--dry-run` | 只看会做什么，一个字都不写 |
| `--apply` | 真的执行（`push`/`pull`/`memory-resolve` 需要） |
| `--vault <目录>` | 指定桥目录（默认：脚本所在目录 / `$HERMES_BRIDGE`） |
| `--profile <名字>` | 指定 profile（默认：`$HERMES_PROFILE`，或 profiles 目录里唯一的那个） |
| `--profile-home <目录>` | 直接指定 profile 路径（测试用） |
| `--anchor <卷>` | 指定保险箱所在卷（默认自动找带 `.hermes-anchor` 的卷） |
| `--no-anchor` | push/pull 时不动保险箱 |

## 它搬什么 / 不搬什么

| 搬 | 怎么合 |
|---|---|
| `state.db` 的 `sessions` / `messages` | 按 `message_uid` 去重，只 INSERT |
| 会话收据 `state_meta` 的 `gateway.local_policy.v1:*` | 只补缺失的；把 `profile_id`、`policy.cwd` 换成本机路径，并补 `legacy_session_id`（跨机器 id 无法重算哈希） |
| `memories/MEMORY.md`、`memories/USER.md`、`SOUL.md` | 三方比对，基线按机器分账；冲突不猜、双方都保留 |
| `skills/`、`scripts/` | 逐文件比，只搬「对面没有或更旧」的，不删 |
| `projects.db` | 按 `slug`（再退到 `name`）对齐；两边的目录都留着；`active_id` 不覆盖 |
| `cron/jobs.json` | 按 job `id` 求并集；搬来的任务清掉 `next_run_at` 等本机运行状态 |

| 不搬（故意的） | 为什么 |
|---|---|
| `.env` | API 密钥不进这个文件夹 |
| `config.yaml` | 两台机器的路径/模型配置本来就该不同 |
| `plugins/`、`attachments/` | 体积大、多为本机状态 |
| `cron/executions.db`、`deliveries.db` | 「上次跑过」是本机流水，合了会判断变奇怪 |
| 项目源码本身 | 桥不碰你真正的工程文件 |

## 跨系统路径：`pathmap.json`

两台机器的绝对路径必然不同（`D:\proj` vs `/mnt/d/proj`）。桌面端「把一个对话移进项目」
**本质是改这个会话的 `cwd`**，所以路径不通 = 项目归属不通。

桥目录下的 `pathmap.json` 是这张前缀表（长前缀优先）：

```json
[
  { "win": "D:\\",              "linux": "/mnt/d/" },
  { "win": "C:\\Users\\alice",  "linux": "/home/alice" }
]
```

- `pull` 时把会话 `cwd` 翻成本机写法；**翻不出来就不改**（不会把好路径盖成死路径）
- `push` 时不翻（桥存本机写法，让对方 pull 时自己翻）
- 没有这张表也不会坏——只是会话的项目归属要手动认

详见 `pathmap.example.json`。

## 保险箱（anchor）

桥躺在和下载、游戏挤在一起的分区上，谁手滑删一下、文件系统抽风一次就没了。
所以支持一块**平时不碰的小分区**当保险箱：

```
.hermes-anchor          标记文件（脚本靠它认路）
ANCHOR.json             时间 / 计数 / 每个文件的 sha256 / 快照列表
profile/                记忆、SOUL.md、config.yaml、skills.tar.gz
snapshots/snap-<ts>.db.gz        整库压缩快照（留最新 N 份）
snapshots/snap-<ts>.json         这份的计数与 sha256
```

- 快照内容 = **桥 ∪ 本地**，所以「还没 push 就先 anchor」也不会漏消息
- 每份快照生成后解压回去跑 `quick_check` 并核对计数，不过就不落盘
- 空间预检按 `源大小 × 0.45 + 12MB`，不够就报错，不写一半
- 库长大了会自动轮转旧快照（结果里出现 `pruned`）

```bash
hermes-bridge anchor                      # 刷新
hermes-bridge verify                      # 体检：对清单、比计数
hermes-bridge restore --to ~/restore-test # 解出一份 state.db 验证
```

这块盘在**同一块硬盘**上，所以它防的是误删 / 桥损坏 / 重装系统，**不防整盘故障**。
真要防硬件，得往云上或另一块物理盘再放一份。

## 记忆冲突：唯一需要人裁决的地方

聊天记录是「一条一条」的，有指纹，合并永远是并集。
但 `MEMORY.md` 是个整篇文件——**Hermes 每次存记忆都会整份重写**，
所以两台都用过之后，「两边都改过」是常态，不是坏了。

桥的做法：**不猜**。

| 情形 | 动作 |
|---|---|
| 哈希相同 | 记基线，什么都不动 |
| 只有对面动过 | 收下并覆盖目标（目标先存副本） |
| 只有这边动过 | **保留，并明确打印出来**——要带出去就在这台再 push |
| 两边都动过 | **报冲突、两边都不覆盖**，对方那份存进 `backups/bridge-memory/` |

```bash
hermes-bridge memory-resolve                                    # 只看差异（条目级）
hermes-bridge memory-resolve --use local  --apply               # 以本机为准
hermes-bridge memory-resolve --use vault  --apply               # 以桥为准
hermes-bridge memory-resolve --use-file ./merged.md --apply     # 用你手写的合并版
hermes-bridge memory-resolve --use local --force-vault --apply  # 并让另一台下次 pull 无条件收敛
```

任何覆盖都会**先把被覆盖的那份存进 `backups/bridge-memory/`**，绝不放进 `memories/`
（那会让 Hermes 把它当成第二份记忆读进去）。

## 忘了 push / 忘了 pull 会怎样

**不会丢，也不会被覆盖。** 这是结构决定的，不是运气：

| 情况 | 实际结果 |
|---|---|
| A 台 push 了，到 B 台忘了 pull 就直接聊 | B 只是没拿到 A 那段；那段一直在桥里。B 的新东西照常追加。下次 pull 补齐 |
| A 台忘了 push 就换系统 | A 的新东西只在本机（不是丢，是还没出发）。B 拿到的旧状态。回 A 时 push 一次就补成并集 |
| 两台各聊几天没同步 | 并集。会话标题撞车会自动改成「标题（另一台 N）」，内容不动 |

最省心的顺序（忘了也不致命）：

```
离开 A：push  →  到 B：pull  →  离开 B：push  →  回 A：pull
```

## 验证

`verify_sync_no_loss.py` 用**真的 `merge_db`** 造三个空库（A / B / 桥），重放三种翻车场景：

```
A: B 忘了 pull 就继续聊
B: A 忘了 push 就走
C: 各聊几天后收尾
```

断言每一步都**不能丢已有的 `message_uid`**，最后三边的消息集合必须完全相等。

```bash
python3 verify_sync_no_loss.py --bridge ./hermes-bridge.py
```

改过 `merge_db` / `mirror_receipts` 之后跑一遍，比讲道理有用。

## 安全设计一览

| 风险 | 做法 |
|---|---|
| 覆盖丢消息 | 只 `INSERT`；按 `message_uid` 去重 |
| WAL 里的新消息没落盘 | 一律 `sqlite3.Connection.backup()`，不用 `cp` |
| 网关正开着库 | `BEGIN IMMEDIATE` + `busy_timeout`，短事务 |
| 跨卷 `os.replace` 失败（Windows `WinError 17`） | 临时文件建在**桥目录内**，不建在 `%TEMP%` |
| 桥库被写坏 | 替换前先备份，原子 `os.replace`，事后 `quick_check` |
| Windows 控制台 GBK 打崩脚本 | 输出用 ASCII 标记，`stream.reconfigure(errors="replace")` 兜底 |
| 密钥泄露 | 不碰 `.env`；`.gitignore` 挡住全部运行数据 |

## 已知限制

- **只在一侧运行时才是安全的**。双系统天然满足；两台电脑同时开着同一个 profile 会重复触发定时任务。
- **依赖 Hermes 的内部表结构**（`sessions` / `messages` / `state_meta` / `projects.db`）。
  Hermes 升级改了 schema，这里可能就要跟着改——跑 `status` 看 `quick_check` 和计数是否正常。
- **不搬 `config.yaml`**：两台机器的路径和模型配置本来就该不同，强行同步只会更糟。
- **定时任务脚本要两台都能跑**：`.sh` 在 Windows 上需要有 bash；`.py` 里的绝对路径要自己按 OS 分支。
- 会话标题有唯一索引，撞车会自动改名（`标题（另一台 N）`），这是设计而非 bug。

## 兼容性

| 平台 | 状态 |
|---|---|
| Windows | 支持（`hermes-bridge.cmd` 自动找 Hermes 自带 Python） |
| Linux | 支持（`bin/hermes-bridge` 启动器） |
| macOS | 支持（`side_name`、保险箱 `/Volumes/*` 已处理，未在真机长期验证） |

Python 3.9+（源码已 `from __future__ import annotations`，不需要 3.10 的新语法）。

## 相关

- 整包搬家（换电脑、重装）用 `hermes profile export` / `import`，不要用桥。
  注意导出包**不含 `state.db-wal`**，整包搬前先让 Hermes 正常退出，或先 `push` 一次。

---

## English

**hermes-bridge** syncs a [Hermes Agent](https://hermes-agent.nousresearch.com) profile
between two operating systems installed on the same machine (or two computers).

The Hermes desktop app keeps everything in one profile per OS: chat history (`state.db`),
`memories/`, `skills/`, projects, cron jobs. A dual-boot machine therefore ends up with two
unrelated copies of "you". Manual `profile export/import` is a *replace*, not a *merge* —
and it loses whatever was said in between.

This tool puts a **vault** directory on a partition both systems can read/write, and provides
`push` / `pull` that merge **append-only**:

```bash
hermes-bridge status          # read-only health check
hermes-bridge push --apply    # before leaving this machine
hermes-bridge pull --apply    # after arriving on this machine
```

Guarantees:

- messages are deduplicated by a per-message fingerprint (`message_uid`) and **only INSERTed** —
  no code path can delete or overwrite an existing message
- every write to a live DB is preceded by a consistent `sqlite3.Connection.backup()` snapshot
  (5 kept) taken with `BEGIN IMMEDIATE` + `busy_timeout`
- memory files use a three-way merge with a per-machine baseline; when both sides changed,
  it **stops and reports** instead of guessing
- zero dependencies — pure Python standard library, one file
- optional **anchor**: a tiny partition holding the irreplaceable text files plus compressed
  full-DB snapshots, verified with `quick_check` before being written

Then `python3 verify_sync_no_loss.py --bridge ./hermes-bridge.py` replays three
"forgot to push / forgot to pull" scenarios against the real merge code and asserts that
the three sides converge to the exact same message set — nothing lost.

MIT licensed.

## License

MIT © 2026 ClanceyPRMS
