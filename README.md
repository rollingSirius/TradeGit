# 交易日志 Skill（TradeGit）

作者：[@rollingSirius](https://x.com/rollingSirius)

English: [README.en.md](README.en.md)

**把交易日志记在你自己的 GitHub 私有仓库里，用一句话记录，用 git 留住全部演变。**

这个 skill 的目标不是做一张更好看的交易流水表，而是让 AI 工具帮你把**每笔交易当时的
判断**留下来，并在几个月后能和**实际结果**对上。它面向真实在交易的人：随手记一笔、
月底导一次券商对账单、季度复盘时问"我到底错在哪"。数据全程在你自己的私有仓库里，
工具不下单、不给投资建议。

## 核心定位

多数交易记录工具停在"记下买了什么、赚了多少"。这个 skill 刻意往前多走一步：

| 能力 | 设计要求 |
|---|---|
| 记录**理由**而不只是流水 | 交易理由、止损/目标、信心度、情绪、策略标签都是一等字段；没写理由时 AI 会主动问一句。 |
| 数据是你的 | 存在你自己的 GitHub **私有**仓库，非 private 直接拒绝初始化；换电脑 clone 回来就是全部历史。 |
| 改动全程留痕 | 记录只追加，更正写新版本、删除写作废标记；`git log -p` 能看到你当时怎么想、后来怎么改。 |
| 券商流水直接导入 | IBKR（对账单 / Flex Query）、嘉信理财，含股息利息税费；重复导入幂等；猜不准的行会停下来问你。 |
| 盈亏算得住 | FIFO 配对，含手续费、支持做空/部分平仓/多翻空/期权乘数；测试用 IBKR 自己的 `Realized P/L` 列做交叉验证。 |
| 多币种不糊弄 | 港美股混合时不会把 HKD 和 USD 加在一起——要么按币种分开给，要么你传汇率折算，绝不给一个看起来合理的错数。 |
| 复盘给事实不给建议 | 输出"亏损单平均持有 34 天 vs 盈利单 21 天"这类可证伪的观察，不给买卖推荐。 |
| 零依赖 | 只要 python3 和 git，没有任何第三方包，不需要账号、不需要订阅。 |
| 凭证不落盘 | 只调用宿主环境已有的 `gh` / `GITHUB_TOKEN`，token 不进配置、不进 argv、不进 `.git/config`、不进报错信息。 |

## 用起来是什么样

```
你：  今天 213.45 买了 100 股 AAPL，突破前高，止损 205
Claude： ✓ 已记录并同步到 yourname/trading-journal

你：  导入一下我下载的嘉信流水
Claude： 识别为 Schwab，新增 9 条（2026-05-01 → 2026-07-10）。
        有 1 行 "Journaled Shares" 需要你确认是什么划转，要现在处理吗？

你：  这个季度哪笔亏得最多？
Claude： NVDA，−4,114.50，2026-05-20 持有到 06-02。开仓理由写的是"回调买入"，
        没有记止损。这段时间亏损单平均持有 34 天、盈利单 21 天。
```

**为什么不是 Excel 或笔记软件**：交易日志的价值在于**当时想的**和**后来发生的**能
对上。Git 天然记录"你什么时候改了什么"，所以一笔交易的理由、事后复盘、认定的错误，
演变过程全都留痕；而私有仓库意味着数据是你的，换电脑 clone 一下就回来了。

---

## 目录

- [它做什么 / 不做什么](#它做什么--不做什么)
- [安装](#安装)
- [首次配置](#首次配置)
- [日常使用](#日常使用)
- [命令参考](#命令参考)
- [支持的券商](#支持的券商)
- [数据长什么样](#数据长什么样)
- [盈亏怎么算](#盈亏怎么算)
- [同步与多设备](#同步与多设备)
- [安全与隐私](#安全与隐私)
- [故障排查](#故障排查)
- [开发](#开发)
- [许可](#许可)

---

## 它做什么 / 不做什么

**做**

- 记录每一笔交易，以及**当时的理由**、止损/目标、信心度、情绪、标签
- 导入 IBKR / 嘉信理财的对账单，去重、归一到同一套字段
- 全部同步到你的 GitHub 私有仓库；本地留一份 clone 供离线分析
- FIFO 配对开平仓，算已实现盈亏、胜率、盈亏因子、期望值、最大回撤、R 倍数
- 事后追加复盘和错误归因，`git log -p` 能看到完整演变

**不做**

- **不下单、不连券商交易接口。** 你自己下单，回来记一笔。
- **不给投资建议。** 只陈述数据（"这个标的过去 6 笔亏了 4 笔"），不推荐买卖。
- **不生成图表、报告文件或面板。** 见下方[可视化](#可视化)。

### 可视化

刻意留在外面。所有分析命令都能 `--json` 输出结构化数据，交给你手上任何渲染组件
（Claude 的图表能力、Artifact、你自己的看板、BI 工具）：

| 输出 | 适合画什么 |
|---|---|
| `analyze --json` → `equity_curve[]` | 累计已实现盈亏曲线（`ts` / `pnl` / `cumulative`） |
| `analyze --group-by month --json` → `grouped[]` | 每月盈亏柱状图 |
| `analyze --group-by symbol --json` → `grouped[]` | 按标的盈亏排序 |
| `analyze --json` → `metrics{}` | 指标卡片 |
| `roundtrips --json` | 平仓明细表 |
| `positions --mark ... --json` | 持仓表 |
| `sql "..." --json` | 任意自定义切片 |

这样 TradeGit 的职责边界清楚 —— 负责把数字算对，不把一套图表样式强加给你。

---

## 安装

```bash
git clone https://github.com/rollingSirius/TradeGit.git ~/TradeGit
cd ~/TradeGit && ./install.sh
```

安装脚本会自动检测你装了哪些工具，并把 skill 注册进去：

| 目标 | 装到哪 | 怎么触发 |
|---|---|---|
| Claude Code / Desktop / claude.ai | `~/.claude/skills/tradegit` | 直接说「记一笔交易」 |
| Codex | `~/.codex/AGENTS.md` + `~/.codex/prompts/tradegit.md` | 直接说，或 `/tradegit` |
| 命令行 | `~/.local/bin/tradegit` | `tradegit <command>` |

其他用法：

```bash
./install.sh --claude              # 只装 Claude
./install.sh --codex               # 只装 Codex
./install.sh --project ~/myrepo    # 装成某个项目的 skill（.claude/skills/）
./install.sh --copy                # 复制文件而不是软链
./install.sh --no-bin              # 不往 ~/.local/bin 放软链
./install.sh --uninstall           # 卸载（不会动你的交易记录）
```

脚本是幂等的，重复运行安全。默认用软链，所以 `git pull` 更新代码后无需重装。

**依赖只有 python3 (3.9+) 和 git**，没有任何第三方包。创建 GitHub 仓库需要
`gh` CLI 或 `GITHUB_TOKEN`。

---

## 首次配置

```bash
gh auth login          # 或 export GITHUB_TOKEN=<有 repo 权限的 token>
tradegit init          # 创建 <你的账号>/trading-journal（private）并克隆到本地
```

| 场景 | 命令 |
|---|---|
| 换个仓库名 | `tradegit init --name my-trades` |
| 复用已有的日志仓库 | `tradegit init --repo owner/name --use-existing` |
| 换电脑，取回全部历史 | 同上 —— clone 回来就有了 |
| 指定默认账户名 | `tradegit init --account ibkr-main` |

`init` 会检查仓库可见性，**不是 private 就直接拒绝**。这是有意为之。

在 Claude / Codex 里，agent 会在创建仓库前先问你一句 —— 那是在你的 GitHub 账户下
真实创建东西。

---

## 日常使用

### 记一笔交易

```bash
tradegit log --symbol AAPL --side BUY --qty 100 --price 213.45 \
             --why "突破前高，量能放大到 20 日均量 1.8 倍" \
             --stop 205 --target 240 --strategy swing \
             --tags "tech,breakout" --conviction 4
```

- `--side`：`BUY` / `SELL` / `SHORT` / `COVER`。开空用 `SHORT`，平空用 `COVER`。
- **`--why` 是这个工具存在的意义。** 没有理由的交易记录只是一行流水。
- `--stop` 决定了事后能不能算 R 倍数，尽量记。
- 不给 `--at` 就用当前时间；补记历史交易用 `--at "2026-05-04T09:31:12Z"`。
- 期权：`--asset-class OPT`，`--symbol` 用券商写法（`AAPL 07/17/2026 200.00 C`）
  或 OSI（`AAPL  260717C00200000`）都行，自动归一并按 100 倍乘数计算。
- 重复记录会按内容自动跳过；真要记两笔一样的加 `--allow-duplicates`。
- 写完自动 commit + push；离线会提示稍后 `tradegit sync`。

批量写入：

```bash
tradegit log --json-input '[{"symbol":"NVDA","side":"BUY","quantity":30,
  "price":1220.5,"ts":"2026-07-08T13:45:00Z","thesis":"..."}]'
```

字段全集见 [`reference/schema.md`](reference/schema.md)。

### 导入券商流水

```bash
tradegit import --file ~/Downloads/statement.csv --dry-run   # 先预览
tradegit import --file ~/Downloads/statement.csv             # 确认后写入
```

`--dry-run` 会告诉你识别成了哪家券商、新增多少条、时间范围、涉及哪些标的，以及
**哪些行需要你人工判断**（比如嘉信的 `Journaled Shares`、期权 `Assigned`/`Exercised`
——这些猜错会让整条持仓线索错掉，所以宁可问你一句）。

重复导入同一份文件是幂等的。导入的记录没有交易理由，可以事后补：

```bash
tradegit amend <id> --why "当时是看了财报电话会才加的仓"
```

### 复盘

```bash
tradegit analyze --since 90d                    # 总览 + 事实性观察
tradegit analyze --since ytd --group-by symbol  # 按标的（亏损排在最前）
tradegit roundtrips --sort pnl --limit 10       # 亏得最多的 10 笔
tradegit positions --mark AAPL=213.4            # 持仓（给了现价才算浮动盈亏）
tradegit analyze --fx HKD=0.128 --base-currency USD   # 港美股混合，折算到统一币种
```

`analyze` 会附带几条从数据里得出的**观察**，例如：

```
复盘观察：
  · 平均盈利 1895.47 < 平均亏损 3165.51（盈亏比 0.60），胜率 57.1% 需要高于 62.5% 才能打平。
  · 亏损单平均持有 34.2 天 vs 盈利单 21.4 天——典型的「截断利润、让亏损奔跑」形态。
  · 6/7 笔交易没有记录止损价，无法计算 R 倍数。
```

这些是事实，不是建议。把结论写回日志，下次复盘就能看见：

```bash
tradegit amend <id> --review "回调买入没有配合量能确认，属于接飞刀" --mistake "无信号入场"
```

更多配方和 SQL cookbook 见 [`reference/analysis.md`](reference/analysis.md)。

---

## 命令参考

| 命令 | 作用 |
|---|---|
| `init` | 连接 GitHub，创建/复用私有仓库并克隆到本地 |
| `log` | 记录一笔交易（或用 `--json-input` 批量） |
| `import` | 导入券商流水，`--dry-run` 预览 |
| `list` | 列出记录，支持全部筛选条件 |
| `positions` | 当前持仓（FIFO），`--mark` 给现价算浮动盈亏 |
| `analyze` | 盈亏指标 + 分组统计 + 复盘观察 |
| `roundtrips` | 平仓明细，可按盈亏/收益率排序 |
| `sql` | 对本地索引跑只读 SQL，表名 `trades` |
| `amend` | 追加一条更正版本（不改历史） |
| `void` | 作废一条记录 |
| `check` | 私有仓库有没有本地没有的变动（不同步时退出码 1） |
| `sync` | 拉取 + 推送 |
| `status` | 仓库、同步状态、记录数、持仓数 |
| `doctor` | 环境自检 |
| `config` | 查看/修改配置 |

**通用参数**

- 筛选：`--since` `--until` `--symbol` `--account` `--broker` `--strategy` `--tag`
- 时间写法：`30d` / `3w` / `3m` / `1y` / `ytd` / `mtd` / `today` / `2026-01-01`
- `--json`：结构化输出（agent 就是这么用的）
- `--no-sync`：跳过远端检查（离线时用）
- `--no-push`：只提交本地，不推送

任何命令加 `-h` 看完整参数。

---

## 支持的券商

| 券商 | 导出文件 |
|---|---|
| **Interactive Brokers（盈透）** | Activity Statement CSV、Flex Query CSV |
| **Charles Schwab（嘉信理财）** | Accounts → History → Export 的 Transactions CSV |
| 其他 | 通用 CSV（列名模糊匹配）、JSON / JSONL |

具体导出步骤、字段映射、已知的坑（IBKR 的 `ClosedLot` 行、嘉信的 `as of` 日期等）
见 [`reference/brokers.md`](reference/brokers.md)。

导入会同时处理**现金事件**——股息、利息、预扣税、账户费用、出入金。忽略它们会让
盈亏失真。

新增一家券商：照 `tradegit/importers/schwab.py` 写一个模块，在
`tradegit/importers/__init__.py` 的 `REGISTRY` 里注册即可。

---

## 数据长什么样

```
~/.tradegit/
  config.json                            仓库地址、默认账户、同步偏好
  repo/                                  ← 私有仓库的 git clone（真实数据）
    journal/2026/2026-05.jsonl           ← 一行一条记录，只追加
    journal/2026/2026-06.jsonl
    manifest.json                        记录数/时间范围（自动生成）
    schema/trade.schema.json             JSON Schema 定义
    .gitattributes                       *.jsonl merge=union
  cache/index.sqlite                     派生索引，删了会自动重建
  imports/                               --keep-source 保留的原始券商文件
```

**为什么是按月分片的 JSONL**：追加一笔只产生一行 diff，git 历史干净；两台机器同时
记录靠 `merge=union` 自动合并；按月而不是按天，是为了在 GitHub 网页上还能直接读。
SQLite 只是本地派生的查询加速器，JSONL 才是唯一真相。完整取舍见
[`reference/storage.md`](reference/storage.md)。

**记录只追加**：`amend` 写一条 `supersedes` 原记录的新版本，`void` 写作废标记，
原始记录永远留在 git 历史里。读取时自动折叠成当前视图。

一条记录长这样（节选）：

```json
{
  "id": "trd_20260504T093112Z_AAPL_3f9a1c2d",
  "kind": "trade", "ts": "2026-05-04T09:31:12Z",
  "account": "ibkr-main", "broker": "IBKR",
  "symbol": "AAPL", "asset_class": "STK", "side": "BUY",
  "quantity": 100, "price": 213.45, "fees": {"commission": 1.0025},
  "net_amount": -21346.0025, "signed_quantity": 100,
  "thesis": "突破 3 月以来的箱体上沿，量能放大到 20 日均量 1.8 倍",
  "strategy": "swing", "conviction": 4, "tags": ["breakout", "tech"],
  "risk": {"stop": 205.0, "target": 240.0, "risk_amount": 845.0, "planned_r": 3.142}
}
```

字段全集见 [`reference/schema.md`](reference/schema.md)。

---

## 盈亏怎么算

FIFO 配对开平仓：同向成交开新批次，反向成交按时间顺序平掉最早的批次，数量有剩余则
反向开仓（支持多翻空）。单笔盈亏含两边分摊的手续费，期权按乘数计算。

- 股息 / 利息 / 税 / 账户费单独归入 `cash_events_net`
- 出入金**不计入业绩**
- 有 `--stop` 的交易会算 R 倍数
- 浮动盈亏需要现价（`--mark`），不给就是 `null`

指标：已实现盈亏、胜率、盈亏因子、期望值、平均盈亏、最大单笔盈亏、最大回撤、
连胜连亏、平均持有天数、平均 R。可按标的 / 月份 / 策略 / 标签 / 方向分组。

测试里用 IBKR 对账单自己的 `Realized P/L` 列做交叉验证。

---

## 同步与多设备

本地目录就是私有仓库的 git clone，所以一致性问题交给 git 解决：

- **检查远端有无变动** = `git ls-remote` 比对 HEAD，不拉取任何对象，几十毫秒 ——
  所以每个读写命令默认都会跑一次，落后就自动 rebase 拉取
- **多台机器同时记** = 两边都追加，`merge=union` 自动合并，正常不会有冲突
- **离线** = 加 `--no-sync`（跳过检查）或 `--no-push`（只提交本地），
  恢复网络后 `tradegit sync` 会把积压的提交推上去

```bash
tradegit check          # 远端有没有本地没有的变动
tradegit check --pull   # 有就直接拉
tradegit sync           # 拉取 + 推送
```

---

## 安全与隐私

- **不存储任何 token。** 按序使用宿主环境已有的凭证：`gh` CLI → `GITHUB_TOKEN` /
  `GH_TOKEN` 环境变量 → git 自身的 credential helper。
- **token 不落盘、不进 argv。** 存进 git 的 remote URL 永远不含凭证（否则会明文留在
  `.git/config` 里）；认证走一次性的 credential helper，token 只经环境变量传递，
  所以同机其他用户 `ps` 看不到。所有 git 输出在进入报错信息前都会做脱敏。
- **`init` 拒绝非 private 仓库。**
- `~/.tradegit` 以 `0700` 创建 —— 里面是财务数据，默认 umask 会让它对同机其他用户可读。
- `tradegit sql` 只读：索引会先刷新，再以只读模式重开，查询无法改动任何数据。
- **仓库里只放交易记录。** 不要写入 token、账户密码、券商登录信息。
- 记录里含有账户标识（如嘉信导出的 `individual-xxxx-1234`）；介意的话用
  `--account` 覆盖成自定义名字。
- 卸载脚本不会碰 `~/.tradegit` 和你的 GitHub 仓库，也不会改写不属于它的文件。

以上每一条都有对应的测试（`TestSecurity`），不是靠自觉维持的。

---

## 故障排查

先跑 `tradegit doctor`，它会逐项检查并给出修复命令。

| 症状 | 原因 / 处理 |
|---|---|
| `没有检测到已连接的 GitHub 账户` | `gh auth login`，或 `export GITHUB_TOKEN=<有 repo 权限的 token>` |
| `创建仓库需要 gh CLI 登录` | 装 `gh` 并登录，或先在 GitHub 上手工建好 private 仓库再 `--use-existing` |
| `xxx 当前是 public 仓库` | 到 GitHub 设置里改成 private 再重跑 `init` |
| `TradeGit 尚未初始化` | `tradegit init` |
| 推送失败 | 记录已提交在本地，联网后 `tradegit sync` |
| `pull failed and could not be auto-resolved` | 非 JSONL 文件冲突，到 `~/.tradegit/repo` 手工解决 |
| 分析结果和券商对不上 | 多半是缺了现金事件或某段流水没导入；`tradegit list --kind cash` 看一下 |
| 导入后条数比预期少 | 看 `--dry-run` 里的 `unparsed` 和 `duplicates` |
| 索引好像不对 | 删掉 `~/.tradegit/cache/index.sqlite`，下次查询自动重建 |

多套账本：用 `TRADEGIT_HOME` 环境变量切换根目录。

---

## 开发

```bash
python3 -m unittest tests.test_tradegit -v
```

35 个测试，覆盖字段归一、FIFO（多头/空头/部分平仓/多翻空/期权乘数）、三种券商
解析器、存储去重与索引刷新、凭证不外泄的安全保证，以及一整套用本地 bare 仓库当
"GitHub"的 CLI 端到端流程（记录 → 去重 → 导入 → 分析 → 更正 → 检测远端变动并拉取
→ 补推离线提交）。

```
SKILL.md              Claude 入口（skill 定义）
AGENTS.md             Codex 入口
README.md / .en.md    中 / 英文文档
install.sh            安装 / 卸载
scripts/tradegit      CLI 启动器
tradegit/             实现（零第三方依赖）
  cli.py              命令行
  schema.py           记录归一、校验、只追加语义
  store.py            JSONL 存储 + SQLite 派生索引
  sync.py             GitHub 连接、远端漂移检测、冲突自动解决
  analytics.py        FIFO 配对与绩效指标
  config.py           配置与路径
  scaffold.py         初始化时写进私有仓库的文件
  importers/          ibkr / schwab / generic
reference/            schema / brokers / analysis / storage
tests/                测试与券商样例文件
```

---

## 许可

[MIT](LICENSE) © [rollingSirius](https://github.com/rollingSirius)

欢迎 fork、修改、商用。如果你加了新的券商解析器，欢迎提 PR 回来。

## 免责声明

本工具仅用于**记录和统计**你自己的交易，不构成投资建议，不代表任何买卖推荐。
盈亏计算基于你录入或导入的数据，**不能替代券商对账单**，报税和对账请以券商官方
文件为准。使用本工具产生的任何投资决策与结果由使用者自行承担。
