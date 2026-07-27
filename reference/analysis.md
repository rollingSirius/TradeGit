# 分析配方

## 盈亏是怎么算的

1. 按 `(account, symbol)` 分别维护持仓，成交按时间排序逐笔处理。
2. 与当前持仓**同向**的成交开新批次（lot）；**反向**的成交按 **FIFO** 平掉最早的批次，
   每平掉一部分就生成一条 round trip；数量有剩余则反向开仓（多翻空）。
3. 单笔 round trip 盈亏 = `(平仓价 − 开仓价) × 数量 × 乘数 × 方向 − 分摊手续费`。
   手续费按数量比例从开仓单和平仓单两边分摊。
4. 期权按 `multiplier`（默认 100）计算。
5. 股息 / 利息 / 税 / 手续费单独归入 `cash_events_net`；出入金**不计入业绩**。
6. R 倍数 = `净盈亏 / (|开仓价 − 止损价| × 数量 × 乘数)`，没记止损就是 `null`。

浮动盈亏需要现价，用 `--mark SYMBOL=PRICE`（可重复）。不给就是 `null`，
汇报时要明说"未计入浮动盈亏"。

## 多币种

**不同币种的金额永远不会被相加。** 港股 + 美股混合持仓很常见，把 5000 HKD 和
1000 USD 加成 6000 是彻底错误的数字。

| 情况 | 行为 |
|---|---|
| 单一币种 | 和以前一样，`metrics.currency` 标明是哪个币种 |
| 多币种 + 传了 `--fx` | 全部折算到基准币，给出统一合计，`metrics.fx_rates` 留档 |
| 多币种 + 没传 `--fx` | 金额类指标全部为 `null`，真实数字在 `by_currency` 里 |

```bash
# 分币种查看（默认）
tradegit analyze --json

# 折算成 USD（1 HKD = 0.128 USD）
tradegit analyze --fx HKD=0.128 --base-currency USD --json
```

汇率不全会直接报错并指出缺哪个币种，不会拿 1.0 顶替。

**汇报时必须说清口径**：折算过要讲明汇率，没折算就分币种讲，不要自己在心里加总。

计数类指标（笔数、胜率、平均持有天数）与币种无关，任何情况下都有效。
跨币种的分组桶（如某个月同时有港股和美股平仓）`net_pnl` 也是 `null`，
并带 `currencies` 字段说明原因。

## 指标含义

| 指标 | 含义 |
|---|---|
| `realized_pnl` | 已平仓净盈亏（含手续费） |
| `total_pnl` | `realized_pnl + cash_events_net + unrealized_pnl` |
| `win_rate` | 盈利 round trip 占比 |
| `profit_factor` | 总盈利 / 总亏损。> 1 才是赚的 |
| `expectancy` | 每笔平均盈亏 |
| `payoff_ratio` | 平均盈利 / 平均亏损 |
| `max_drawdown` | 已实现权益曲线的最大回撤（负数） |
| `avg_r_multiple` | 有止损记录的交易的平均 R |
| `best_streak` / `worst_streak` | 最长连胜 / 连亏笔数 |
| `currency` | 指标的计价币种；多币种未折算时为 `null` |
| `currency_mixed` | 是否涉及多个币种 |
| `fx_rates` | 折算用的汇率（传了 `--fx` 时） |

## 常见问题的命令

```bash
# 过去 90 天整体表现
tradegit analyze --since 90d --json

# 今年哪个标的亏最多（分组结果按净盈亏升序，亏损在最前）
tradegit analyze --since ytd --group-by symbol --json

# 亏损最重的 10 笔平仓交易
tradegit roundtrips --sort pnl --limit 10 --json

# 某个标的的全部交易
tradegit list --symbol NVDA --json

# 按月看盈亏趋势
tradegit analyze --since 2y --group-by month --json

# 某个策略是否有效
tradegit analyze --strategy swing --group-by month --json

# 当前持仓 + 浮动盈亏
tradegit positions --mark AAPL=213.4 --mark NVDA=1180 --json
```

## SQL cookbook

`tradegit sql "<只读查询>" --json`。表名 `trades`，一行一条记录（已应用
amend/void）。可用列：

```
id ts day month year kind account broker symbol underlying asset_class currency
side cash_type quantity signed_quantity price multiplier gross_amount fees_total
net_amount amount strategy setup thesis conviction tags stop target risk_amount
source_kind external_id dedup_key
```

```sql
-- 交易最频繁的标的
SELECT symbol, COUNT(*) n, SUM(gross_amount) volume
FROM trades WHERE kind='trade' GROUP BY symbol ORDER BY n DESC LIMIT 20;

-- 每月手续费
SELECT month, ROUND(SUM(fees_total),2) fees FROM trades GROUP BY month ORDER BY month;

-- 没写交易理由的交易（复盘时的盲区）
SELECT day, symbol, side, quantity, price FROM trades
WHERE kind='trade' AND (thesis IS NULL OR thesis='') ORDER BY ts DESC LIMIT 50;

-- 没设止损的开仓
SELECT day, symbol, side, quantity, price FROM trades
WHERE kind='trade' AND side IN ('BUY','SHORT') AND stop IS NULL;

-- 信心度和实际结果的关系（配合 roundtrips 一起看）
SELECT conviction, COUNT(*) n FROM trades
WHERE conviction IS NOT NULL GROUP BY conviction ORDER BY conviction;

-- 按小时看下单时段
SELECT substr(ts,12,2) hour, COUNT(*) n FROM trades
WHERE kind='trade' GROUP BY hour ORDER BY hour;

-- 各类现金事件汇总
SELECT cash_type, ROUND(SUM(amount),2) total FROM trades
WHERE kind='cash' GROUP BY cash_type ORDER BY total;
```

只允许 `SELECT` / `WITH` / `PRAGMA` / `EXPLAIN`，写操作会被拒绝——索引是派生的，
真实数据在 JSONL 里。

## 复盘怎么做才有用

`analyze` 返回的 `notes` 是从数据里得出的**事实性观察**，例如：

- 盈亏比 < 1 时，打平所需的胜率是多少
- 最大单笔亏损占全部盈亏绝对值的比例
- 亏损单的平均持有天数 vs 盈利单（"截断利润、让亏损奔跑"的典型形态）
- 有多少笔没记止损 / 没记交易理由

把这些转述给用户，并**追问而不是下结论**：

> "这 3 个月亏损最重的是 NVDA，6 笔亏了 4 笔，合计 −8,240。这 4 笔的开仓理由里
> 有 3 笔写的是「回调买入」——你觉得是这个入场条件本身有问题，还是止损设得太紧？"

用户回答后，把结论写回日志，下次复盘就能看见：

```bash
tradegit amend <id> --review "回调买入没有配合量能确认，属于接飞刀" --mistake "无信号入场" --json
```

**不要**给出"建议减仓 / 建议买入 / 这个位置可以进"之类的操作建议——这个工具做的是
记录和统计，不是投资顾问。

## 可视化交给别的组件

TradeGit **不生成图表、报告文件或面板**——它只负责把数字算对。需要可视化时，取
`--json` 数据再交给合适的渲染组件：

| 输出 | 适合画什么 |
|---|---|
| `analyze --json` → `equity_curve[]` | 累计已实现盈亏曲线（`ts` / `pnl` / `cumulative`） |
| `analyze --group-by month --json` → `grouped[]` | 每月盈亏柱状图 |
| `analyze --group-by symbol --json` → `grouped[]` | 按标的盈亏排序的横向柱状图 |
| `analyze --json` → `metrics{}` | 指标卡片（胜率 / 盈亏因子 / 期望值 / 最大回撤） |
| `roundtrips --json` | 平仓明细表 |
| `positions --mark ... --json` | 持仓表 |
| `sql "..." --json` | 任意自定义切片 |

每个分组桶的结构一致：

```json
{"key": "2026-06", "roundtrips": 5, "net_pnl": -1914.66, "wins": 3, "losses": 2,
 "win_rate": 60.0, "avg_pnl": -382.93, "profit_factor": 0.8, "fees": 12.4,
 "volume": 184320.0}
```

这样职责边界清楚：TradeGit 负责记录、同步和算数，怎么呈现由你和用户决定。
