---
name: tradegit
description: |
  基于 GitHub 私有仓库的交易日志（trading journal）。用于记录每一笔交易及其理由、
  导入券商流水、复盘历史盈亏。触发场景：用户说「记一笔交易 / 记录一下我买了 XXX /
  log this trade」、「导入 IBKR / 盈透 / 嘉信 / Schwab 的对账单」、「看看我过去
  三个月的盈亏 / 哪笔亏得最多 / 复盘一下我的交易」、「我的持仓」、
  或首次配置时说「安装/连接交易日志」。也在用户提到 trade journal、交易记录、
  round trip、win rate、盈亏分析、R 倍数时触发。
  仅负责记录、同步与盈亏计算；不下单、不提供投资建议、不生成图表或面板。
---

# TradeGit — GitHub 私有仓库交易日志

一个 CLI 承载全部功能。所有命令都支持 `--json`，**你应该始终加 `--json` 并解析结果**，
再用自然语言向用户汇报。

```bash
{SKILL_DIR}/scripts/tradegit <command> --json
```

`{SKILL_DIR}` 是本 skill 所在目录。若 `tradegit` 已在 PATH 上（安装脚本会尝试软链到
`~/.local/bin`），直接用 `tradegit` 即可。零依赖，只需要 python3 和 git。

## 数据放在哪

- **模式**：GitHub 私有仓库，或 `tradegit init --local` 的完全本地模式
- **远端**：GitHub 模式下是用户自己的 GitHub **私有**仓库（默认 `<user>/trading-journal`）；
  local-only 模式没有远端
- **本地**：`~/.tradegit/repo/` —— GitHub clone 或本地 git 仓库；
  `~/.tradegit/cache/index.sqlite` 是派生的查询索引，可随时删除重建
- **格式**：`journal/<YYYY>/<YYYY-MM>.jsonl`，一行一条记录，只追加

细节见 `reference/storage.md`。

## 第一次使用

1. 先跑 `tradegit status --json`。
2. 如果 `initialized` 为 false：
   - 先问用户要 GitHub 私有仓库还是完全本地模式。若用户选择本地，执行
     `tradegit init --local --json`，不需要 GitHub，也不会推送远端。
   - 若用户选择 GitHub，再检查 `auth.connected`。为 false 时告诉用户二选一连接 GitHub：
     `gh auth login`，或 `export GITHUB_TOKEN=<有 repo 权限的 token>`。
     **不要向用户索要 token 内容，也不要把 token 写进任何文件。**
   - **创建仓库前先向用户确认**（这是在他们的 GitHub 账户下创建东西）：
     告诉他们将要创建的仓库名、可见性为 private，得到明确同意后再执行
     `tradegit init --json`。
   - 用户已有日志仓库时用 `tradegit init --repo owner/name --use-existing --json`。
3. `init` 会拒绝非 private 的仓库。这是有意为之，不要绕过。

## 记录一笔交易

```bash
tradegit log --symbol AAPL --side BUY --qty 100 --price 213.45 \
  --why "突破前高且量能放大，财报前布局" --stop 205 --target 240 \
  --strategy swing --tags "tech,breakout" --conviction 4 --json
```

要点：

- `--side` 是 `BUY` / `SELL` / `SHORT` / `COVER`。开空用 `SHORT`，平空用 `COVER`。
- **`--why`（交易理由）是这个 skill 存在的意义。** 用户没说理由时要主动问一句，
  而不是留空。同样地，有止损价才能算 R 倍数，顺带问一下。
- 未指定 `--at` 则用当前时间。补记历史交易要显式给 `--at "2026-05-04T09:31:12Z"`。
- 期权：`--asset-class OPT`，`--symbol` 可用券商写法（`AAPL 07/17/2026 200.00 C`）
  或 OSI（`AAPL  260717C00200000`），会自动归一并按 100 倍乘数计算。
- 一次记多笔：`--json-input '[{...},{...}]'`，字段见 `reference/schema.md`。
- 重复记录会被自动跳过（按内容去重），返回 `written: 0` 时如实告诉用户"已经记过了"。
- 写入后自动 commit + push。离线时会提示"已提交本地，稍后 `tradegit sync`"。

## 导入券商流水

**先 `--dry-run`，把预览结果讲给用户听，得到确认后再真正写入。**

```bash
tradegit import --file ~/Downloads/statement.csv --dry-run --json   # 预览
tradegit import --file ~/Downloads/statement.csv --json             # 写入
```

- 券商自动识别；可用 `--broker ibkr|schwab|generic` 强制。
- 支持 IBKR Activity Statement CSV、IBKR Flex Query CSV、嘉信 Transactions CSV、
  通用 CSV、JSON/JSONL。用户不知道去哪导出时，念 `reference/brokers.md` 里的步骤。
- 预览里的 `unparsed` 是需要人工判断的行（如嘉信的 `Journaled Shares`、期权
  `Assigned`/`Exercised`）。**要主动把这些念给用户，问清楚后用 `tradegit log` 补录**，
  不要自己猜。
- 导入的记录没有交易理由。导入完成后可以问用户要不要给其中几笔重要的补上：
  `tradegit amend <id> --why "..." --json`。
- 重复导入同一份文件是安全的（幂等）。

## 分析与复盘

```bash
tradegit analyze --since 90d --json                    # 总览
tradegit analyze --since ytd --group-by symbol --json  # 按标的/月份/策略/标签分组
tradegit roundtrips --sort pnl --limit 10 --json       # 亏得最多的平仓交易
tradegit positions --mark AAPL=213.4 --json            # 持仓（给了现价才算浮动盈亏）
tradegit report --since 90d --markdown --json          # Markdown 复盘报告
tradegit report --since 180d --pdf --output report.pdf --json
tradegit sql "SELECT ..." --json                       # 任意只读 SQL，表名 trades
```

- 时间参数支持 `30d` / `90d` / `180d` / `360d` / `3m` / `1y` / `ytd` / `mtd` / `2026-01-01`。
- 盈亏用 **FIFO** 配对开平仓，含手续费，期权按乘数计算；股息/利息/税费单独计入。
- `analyze` 返回的 `notes` 是从数据里得出的事实性观察（如"亏损单平均持有天数是盈利
  单的 2 倍"）。可以转述并追问，但**不要变成投资建议或对下一笔交易的推荐**。
- 浮动盈亏需要现价。有行情工具（如已连接的券商/行情 MCP）就先取价再传 `--mark`；
  没有就直接说明"未计入浮动盈亏"。
- **多币种**：港美股混合时 `metrics` 里的金额会是 `null`，真实数字在 `by_currency`。
  这时**不要自己把不同币种加起来**——要么分币种汇报，要么问用户汇率后传
  `--fx HKD=0.128 --base-currency USD`。折算过一定要讲明用了什么汇率。
- 更多查询配方见 `reference/analysis.md`。

**这个 skill 可以生成确定性的 Markdown/PDF 复盘报告，但不生成图表或面板。**
用户要可视化时，用 `analyze --json` /
`roundtrips --json` / `sql --json` 取到数据，再交给你手上任何合适的组件去渲染
（Artifact、图表 skill、用户自己的看板）。TradeGit 只负责记录、同步和算数。

## 关键操作前检查远端

GitHub 模式下，每个读写命令默认会先比对远端 HEAD（`git ls-remote`，很轻），发现落后就自动 rebase 拉取。
返回结果里的 `sync` 字段说明发生了什么。

- 手动检查：`tradegit check --json`（`in_sync` 为 false 时退出码为 1）
- 手动拉取：`tradegit check --pull --json`
- 离线：给命令加 `--no-sync`；恢复网络后 `tradegit sync --json`
- 本地模式：`tradegit init --local --json` 后只做本地 commit，`sync` 不会推送。

多端并发追加靠 `.gitattributes` 里的 `merge=union` 自动合并，正常不会有冲突。

## 修改记录

日志是只追加的，不改写历史：

```bash
tradegit amend <id> --review "追高了，没等回调" --mistake "FOMO" --json
tradegit void <id> --reason "记错了" --json
```

`amend` 写入一条 `supersedes` 原记录的新版本，`void` 写入一条作废标记。原始记录永远
留在 git 历史里。

## 边界

- 这是**记录和分析工具**，不下单、不连券商交易接口。用户说"帮我买 100 股"时，明确
  说明你只能记录，让他们自己在券商下单后再回来记。
- 不提供个性化投资建议。可以陈述数据（"这个标的过去 6 笔亏了 4 笔"），不要给
  买卖推荐或仓位建议。
- 私有仓库里只放交易记录。不要写入 token、账户密码、券商登录信息。
- 不生成图表或交互面板。需要可视化时把 `--json` 数据交给别的组件。
- GitHub 模式下 `init` 会创建 GitHub 仓库、每次记录会 push——这些都是对外可见的动作，
  第一次执行前要向用户确认。local-only 模式不连接远端。

## 参考文档

| 文件 | 内容 |
|---|---|
| `reference/schema.md` | 记录的完整字段定义 |
| `reference/brokers.md` | 各券商导出流水的具体步骤、字段映射、已知坑 |
| `reference/analysis.md` | 分析配方与 SQL cookbook |
| `reference/storage.md` | 存储格式与同步机制的设计取舍 |
