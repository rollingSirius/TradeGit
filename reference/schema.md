# 记录字段定义

一条记录就是 JSONL 里的一行。三种 `kind`：

| kind | 含义 |
|---|---|
| `trade` | 一笔成交。驱动持仓与盈亏计算。 |
| `cash` | 非成交的现金事件：股息、利息、手续费、税、出入金。不计入会让盈亏失真。 |
| `void` | 作废标记，逻辑删除一条记录。 |

## trade

### 必填

| 字段 | 类型 | 说明 |
|---|---|---|
| `ts` | string | UTC，`YYYY-MM-DDTHH:MM:SSZ`。输入时接受多种券商格式，自动归一。 |
| `symbol` | string | 股票代码；期权用 OSI 21 位符号（`AAPL  260717C00200000`）。 |
| `side` | enum | `BUY` / `SELL` / `SHORT` / `COVER`。开空 `SHORT`，平空 `COVER`。 |
| `quantity` | number | 正数。方向由 `side` 决定。 |
| `price` | number | 单价（期权是每股权利金，不是每张）。 |

### 自动派生（不要手填）

| 字段 | 说明 |
|---|---|
| `id` | `trd_<时间戳>_<标的>_<内容哈希前8位>` |
| `dedup_key` | 去重键。有券商成交号时用 `ext:<broker>:<id>`，否则用内容哈希。 |
| `multiplier` | 期权默认 100，其余 1。可用 `--multiplier` 覆盖（期货用）。 |
| `gross_amount` | `quantity × price × multiplier` |
| `fees_total` | `fees` 各项之和 |
| `net_amount` | 现金影响：买入为负，卖出为正，已扣费 |
| `signed_quantity` | `BUY`/`COVER` 为正，`SELL`/`SHORT` 为负 |
| `hash` / `created_at` / `updated_at` | 完整性与审计 |

### 交易日志字段（这个工具的价值所在）

| 字段 | CLI 参数 | 说明 |
|---|---|---|
| `thesis` | `--why` | **交易理由**。为什么现在买/卖这个。最重要的一个字段。 |
| `strategy` | `--strategy` | 策略名，如 `swing` / `mean-reversion` / `earnings`。用于分组统计。 |
| `setup` | `--setup` | 具体形态/入场信号。 |
| `exit_plan` | `--exit-plan` | 事先写好的离场计划。 |
| `market_context` | `--market-context` | 当时的大盘/板块环境。 |
| `conviction` | `--conviction` | 1–5 的信心度。 |
| `emotions` | `--emotions` | 当时的情绪。复盘时和结果对照很有用。 |
| `tags` | `--tags` | 逗号分隔，自动转小写去重。 |
| `review` | `amend --review` | **事后**复盘。开仓时通常还没有。 |
| `mistake` | `amend --mistake` | 事后认定的执行错误。 |
| `notes` | `--notes` | 其他备注。 |

### 风险

| 字段 | CLI 参数 | 说明 |
|---|---|---|
| `risk.stop` | `--stop` | 止损价。**有它才能算 R 倍数**，强烈建议记。 |
| `risk.target` | `--target` | 目标价。 |
| `risk.risk_amount` | — | 由 `\|price − stop\| × quantity × multiplier` 自动算出。 |
| `risk.planned_r` | — | 计划盈亏比 `\|target − price\| / \|price − stop\|`。 |

### 期权

`option` 子对象：`underlying`、`expiry`（`YYYY-MM-DD`）、`strike`、`right`（`C`/`P`）。
从 OSI 符号或券商写法自动解析，也可以显式传。

### 手续费

`fees` 是键值明细，键名自定（常用 `commission` / `regulatory` / `exchange` /
`clearing` / `tax` / `other`）。CLI 里 `--fees 1.25` 记为 commission，
`--fees '{"commission":1,"regulatory":0.02}'` 记明细。

### 来源

`source`：`kind`（`manual` / `import`）、`importer`、`external_id`（券商成交号）、
`file`（导入的文件名）。去重靠 `external_id`，所以同一份对账单重复导入是安全的。

## cash

| 字段 | 说明 |
|---|---|
| `cash_type` | `DIVIDEND` / `INTEREST` / `FEE` / `TAX` / `DEPOSIT` / `WITHDRAWAL` / `REBATE` / `ADJUSTMENT` / `OTHER` |
| `amount` | 有符号金额。收入为正，支出为负。 |
| `symbol` | 可选，股息类事件会带上标的。 |

`DEPOSIT` / `WITHDRAWAL` 是资金进出，**不计入业绩**（`cash_events_net` 会排除它们）。

## void / 更正

记录只追加，不改写：

```jsonc
// 更正：写一条新记录，指向被替换的记录
{"kind": "trade", "supersedes": "trd_2026...", "price": 213.50, ...}
// 作废
{"kind": "void", "voids": "trd_2026...", "ts": "..."}
```

读取时 `resolve()` 会应用这些操作，给出当前视图。原始记录留在 git 历史里，
`git log -p journal/2026/2026-05.jsonl` 可以看到全部改动过程。

**注意**：如果更正的只是理由、标签等非经济字段，新记录的 `id` 与原记录相同
（`id` 由成交内容派生），这是正常的——后写入的那条生效。

## 完整示例

```json
{
  "id": "trd_20260504T093112Z_AAPL_3f9a1c2d",
  "schema_version": 1,
  "kind": "trade",
  "ts": "2026-05-04T09:31:12Z",
  "account": "ibkr-main",
  "broker": "IBKR",
  "symbol": "AAPL",
  "asset_class": "STK",
  "currency": "USD",
  "side": "BUY",
  "quantity": 100,
  "price": 213.45,
  "multiplier": 1,
  "fees": {"commission": 1.0025},
  "fees_total": 1.0025,
  "gross_amount": 21345.0,
  "net_amount": -21346.0025,
  "signed_quantity": 100,
  "thesis": "突破 3 月以来的箱体上沿，量能放大到 20 日均量 1.8 倍",
  "strategy": "swing",
  "setup": "box-breakout",
  "conviction": 4,
  "tags": ["breakout", "tech"],
  "risk": {"stop": 205.0, "target": 240.0, "risk_amount": 845.0, "planned_r": 3.142},
  "source": {"kind": "manual"},
  "dedup_key": "content:9c1e...",
  "created_at": "2026-05-04T09:32:01Z",
  "updated_at": "2026-05-04T09:32:01Z",
  "hash": "sha256:1a2b..."
}
```
