# 存储与同步：设计取舍

## 为什么是 JSONL 而不是一个大 JSON

| 方案 | 追加一条记录的 diff | 并发追加 | 流式读取 | git 友好 |
|---|---|---|---|---|
| 单个 `trades.json` | 整个文件重写 | 必冲突 | 否 | 差 |
| **按月分片 JSONL** | **一行** | **`merge=union` 自动合并** | **是** | **好** |
| SQLite 提交进仓库 | 整个二进制文件 | 必冲突 | 否 | 很差 |
| Parquet | 整个二进制文件 | 必冲突 | 部分 | 很差 |

交易日志是典型的**只追加、按时间查询、体量小**的负载：一个交易频繁的人一年也就
几千行，十年不到十万行——纯扫描只要几十毫秒。Parquet/SQLite 的列存优势在这个量级
体现不出来，而它们的二进制性质会让 git 历史完全失去可读性（这恰恰是"用 git
做交易日志"的核心价值：`git log -p` 能看到每一次修改）。

所以：**JSONL 是唯一真相，SQLite 只是本地派生的查询加速器。**

```
journal/2026/2026-05.jsonl    ← 真相，进 git
journal/2026/2026-06.jsonl
manifest.json                 ← 派生，进 git（方便人和别的工具看）
~/.tradegit/cache/index.sqlite ← 派生，不进 git，删了会自动重建
```

按**月**分片而不是按年或按天：一个月的文件在 GitHub 网页上打开还能读，而按天会
产生几百个碎文件。

## 本地存储 = git 仓库

TradeGit 支持两种模式：

| 模式 | 本地目录 | 远端 |
|---|---|---|
| Git-backed private repo | `~/.tradegit/repo/` 是 GitHub 私有仓库 clone | GitHub private repo |
| Local-only | `~/.tradegit/repo/` 是普通本地 git 仓库 | 无 remote |

这个选择让"日志历史"和"多设备一致性"尽量交给 git 解决：

- GitHub 模式下「远端有没有变动」= `git ls-remote origin <branch>` 和本地 HEAD 比对，
  **不需要拉取任何对象**，几十毫秒，所以每个命令都能默认跑一次
- 「拉取变动」= `git pull --rebase --autostash`
- 「历史版本」= `git log`，不用自己实现
- 「另一台机器也在记」= GitHub 模式下两边都 append，`merge=union` 自动合并
- 「完全本地」= `tradegit init --local` 后只在本机 commit，不检查远端、不 push

### 冲突处理

仓库的 `.gitattributes`：

```
*.jsonl merge=union
manifest.json -merge
```

`merge=union` 是 git 内置的合并驱动，对只追加的文件会把两边的新行都保留——两台
机器同一天各记一笔，自动合并，不需要人工介入。

`manifest.json` 是完全派生的，冲突时直接重新生成。

其他文件冲突则会停下来交给用户处理（正常情况下不会发生）。

## 索引什么时候重建

`store.fingerprint()` 由所有 JSONL 文件的**文件名 + 大小 + mtime** 组成。查询前
比对指纹，不一致就整表重建。重建一万条记录大约几十毫秒，所以没做增量。

`git pull` 或本地写入改动了文件 → mtime 变 → 指纹变 → 下次查询自动重建。不需要手动刷新。

## 去重

每条记录带 `dedup_key`：

- 券商给了成交号：`ext:<broker>:<TransactionID>`
- 否则：`(kind, account, broker, symbol, side, ts, quantity, price, amount)` 的 SHA-256

`store.append()` 默认跳过已存在的 key，所以：

- 同一份对账单重复导入 → 幂等
- 同一笔交易手动记了两次 → 第二次会被拦下（真要记就加 `--allow-duplicates`）
- 手动记过、之后又从券商导入 → 只要标的/时间/数量/价格一致就会被识别为同一笔

**注意**：手动记录的时间通常是"当时的整分钟"，券商记的是精确到秒的成交时间，
两者可能不完全一致而导致重复。导入时留意 `--dry-run` 预览里的数量。

## 只追加与更正

记录不原地修改。`amend` 写一条带 `supersedes` 的新记录，`void` 写一条作废标记，
读取时 `resolve()` 应用这些操作得到当前视图。好处：

- 交易记录有审计价值，"事后改了什么"本身就是信息
- `git log -p` 能看到完整的心路历程（尤其是 `review` / `mistake` 字段的演变）
- 只追加的文件才能用 `merge=union`

代价是文件会比"当前记录数"大一些，但相对于 git 本身的存储开销可以忽略。

## 认证

TradeGit **不存储任何 token**。它按顺序使用宿主环境已有的凭证：

1. `gh` CLI（Claude Code / Codex 环境通常已经登录）
2. `GITHUB_TOKEN` / `GH_TOKEN` 环境变量
3. git 自身的 credential helper

GitHub 模式下 `init` 会检查仓库可见性，非 private 直接拒绝。local-only 模式不需要
GitHub 认证，也不会存储或使用 token。

## 目录总览

```
~/.tradegit/
  config.json              storage_mode、仓库 slug、默认账户、是否自动 push
  repo/                    git 仓库（GitHub clone 或 local-only，真实数据）
    journal/<YYYY>/<YYYY-MM>.jsonl
    manifest.json
    schema/trade.schema.json
    .gitattributes
  cache/
    index.sqlite           派生索引
    state.json             上次同步时间、上次推送的 HEAD
  imports/                 --keep-source 时保留的原始券商文件
```

GitHub 模式换机器只要重跑 `tradegit init --repo <owner>/<name> --use-existing`，
全部历史随 clone 回来。local-only 模式没有远端，备份和迁移需要你自己复制或给本地
仓库添加 remote。

用 `TRADEGIT_HOME` 环境变量可以改根目录（测试和多套账本时有用）。
