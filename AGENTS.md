# TradeGit — GitHub 私有仓库交易日志（Codex / 通用 agent 入口）

> 这份文件是给 **Codex CLI** 和其他读 `AGENTS.md` 的 agent 用的。
> Claude Code / Claude Desktop / claude.ai 读同目录下的 `SKILL.md`，内容等价。

当用户要求**记录一笔交易**、**导入券商流水**、**复盘/分析历史盈亏**、**查看持仓**，
或首次要求**配置交易日志**时，使用本工具。

## 工具

只有一个零依赖 CLI（python3 + git 即可），所有命令都支持 `--json`：

```bash
<TRADEGIT_DIR>/scripts/tradegit <command> --json
```

安装脚本会尝试把它软链到 `~/.local/bin/tradegit`。**始终加 `--json` 并解析结果**，
再用自然语言回复用户。

## 命令速查

| 目的 | 命令 |
|---|---|
| 环境/同步状态 | `tradegit status --json` |
| 首次连接 GitHub 并建私有仓库 | `tradegit init --json` |
| 记录一笔交易 | `tradegit log --symbol AAPL --side BUY --qty 100 --price 213.45 --why "理由" --stop 205 --json` |
| 导入券商流水（先预览） | `tradegit import --file <path> --dry-run --json` |
| 导入券商流水（写入） | `tradegit import --file <path> --json` |
| 盈亏总览 | `tradegit analyze --since 90d --json` |
| 分组统计 | `tradegit analyze --since ytd --group-by symbol --json` |
| 亏损最重的平仓交易 | `tradegit roundtrips --sort pnl --limit 10 --json` |
| 当前持仓 | `tradegit positions --mark AAPL=213.4 --json` |
| 任意只读 SQL（表名 `trades`） | `tradegit sql "SELECT ..." --json` |
| 检查远端是否有变动 | `tradegit check --json`（不同步时退出码 1） |
| 更正 / 作废记录 | `tradegit amend <id> --review "..." --json` / `tradegit void <id> --json` |
| 环境自检 | `tradegit doctor --json` |

时间参数支持 `30d` / `3m` / `1y` / `ytd` / `mtd` / `2026-01-01`。

## 必须遵守的流程

1. **首次使用**：先 `tradegit status --json`。未初始化时，先确认 GitHub 已连接
   （`gh auth login` 或 `GITHUB_TOKEN` 环境变量），**并在创建仓库前征得用户明确同意**，
   再执行 `tradegit init --json`。不要索要或存储 token 内容。
2. **记录交易时必须有交易理由**。用户没给 `--why` 就主动问一句；顺带问止损价
   （有止损才能算 R 倍数）。
3. **导入必须先 `--dry-run`**，把识别到的券商、新增条数、时间范围、无法解析的行
   讲给用户，确认后再真正写入。`unparsed` 里的行要念给用户，问清楚后手工补录，不要猜。
4. **不下单、不给投资建议**。只记录和统计。用户要求下单时说明工具边界。
5. **本工具不生成图表或面板**。用户要可视化时，用 `analyze --json` / `roundtrips --json` /
   `sql --json` 取数，再交给其他组件渲染。
6. **私有仓库里只放交易记录**，不写入任何凭证。

## 数据布局

- 远端：用户的 GitHub **私有**仓库（默认 `<user>/trading-journal`）
- 本地：`~/.tradegit/repo/`，就是该仓库的 git clone
- 格式：`journal/<YYYY>/<YYYY-MM>.jsonl`，一行一条记录，只追加
- 每个命令默认会先用 `git ls-remote` 比对远端 HEAD，落后就自动拉取；离线加 `--no-sync`

## 详细文档

`reference/schema.md`（字段定义）、`reference/brokers.md`（IBKR / 嘉信导出步骤与坑）、
`reference/analysis.md`（分析配方与 SQL）、`reference/storage.md`（设计取舍）。
