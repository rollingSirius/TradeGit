# 券商流水导入

```bash
tradegit import --file <path> --dry-run --json   # 永远先预览
tradegit import --file <path> --json             # 确认后写入
```

券商自动识别；可用 `--broker ibkr|schwab|generic` 强制。重复导入同一份文件是幂等的。

---

## Interactive Brokers（IBKR / 盈透）

两种导出都支持，**Flex Query 更好**（有成交号，去重更可靠）。

### A. Activity Statement（最容易拿到）

1. 登录 IBKR 客户端门户
2. **Performance & Reports → Statements → Activity**
3. Period 选时间段，Format 选 **CSV**，点 Run
4. 下载得到的 `.csv`

这是多 section 文件，每行第一个字段是 section 名。导入会解析：

| Section | 变成 |
|---|---|
| `Trades`（`DataDiscriminator = Order/Trade`） | `kind=trade` |
| `Dividends` | `cash_type=DIVIDEND` |
| `Withholding Tax` | `cash_type=TAX` |
| `Interest` | `cash_type=INTEREST` |
| `Fees` / `Other Fees` | `cash_type=FEE` |
| `Deposits & Withdrawals` | 按金额正负记为 `DEPOSIT` / `WITHDRAWAL` |

**`ClosedLot` 行会被跳过**——那是 IBKR 自己的平仓配对结果，TradeGit 自己做 FIFO
配对，保留会重复计算。`SubTotal` / `Total` 行同样跳过。

多空由 `Code` 列判断：数量为负且带 `O` 记为 `SHORT`，数量为正且带 `C` 记为 `COVER`。

### B. Flex Query（推荐，可自动化）

1. **Performance & Reports → Flex Queries → Trade Confirmation Flex Query**（或 Activity Flex）
2. 新建查询，Sections 勾 **Trades**，格式选 **CSV**，Date Format `yyyyMMdd`
3. 字段至少包含：
   `ClientAccountID, CurrencyPrimary, Symbol, UnderlyingSymbol, AssetClass, Multiplier,`
   `TradeDate, TradeTime, Buy/Sell, Quantity, TradePrice, IBCommission, Taxes,`
   `TransactionID, IBOrderID, OpenCloseIndicator, Expiry, Strike, Put/Call`
4. Run → 下载 CSV

`TransactionID` 会存进 `source.external_id`，作为去重键——比内容哈希更可靠。

### 已知细节

- 佣金在 IBKR 里是负数（支出），导入时取绝对值存入 `fees.commission`
- 期权符号形如 `TSLA 19JUN26 300 C`，自动转成 OSI `TSLA  260619C00300000`
- Asset Category 映射：`Stocks→STK`、`ETFs→ETF`、`Equity and Index Options→OPT`、
  `Futures→FUT`、`Forex→FX`、`Bonds→BOND`、`Cryptocurrencies→CRYPTO`

---

## Charles Schwab（嘉信理财）

1. 登录 schwab.com
2. **Accounts → History**（账户 → 历史记录）
3. 选账户和时间范围，右上角 **Export**，选 CSV
4. 得到 `Individual_XXXX_Transactions_YYYYMMDD.csv`（或 `Transactions.csv`）

### Action 映射

| Schwab Action | 记为 |
|---|---|
| `Buy`、`Buy to Open`、`Reinvest Shares` | `BUY` |
| `Sell`、`Sell to Close` | `SELL` |
| `Sell Short`、`Sell to Open` | `SHORT` |
| `Buy to Cover`、`Buy to Close` | `COVER` |
| `Expired` | 按 0 价平仓（方向由数量符号推断，会标 `source.inferred_side`） |
| `Qualified/Cash/Special Dividend`、`Cap Gain`、`Reinvest Dividend` | `DIVIDEND` |
| `Bank/Credit/Margin Interest` | `INTEREST` |
| `Foreign Tax Paid`、`NRA Tax Adj` | `TAX` |
| `ADR Mgmt Fee`、`Service Fee` | `FEE` |
| `MoneyLink Transfer`、`Wire Received/Sent`、`Journal` | 按金额正负记 `DEPOSIT`/`WITHDRAWAL` |

### 需要人工确认的行

以下 Action 无法安全推断，会进 `unparsed`，**请念给用户，问清楚后用 `tradegit log`
手工补录**：

`Assigned`（期权被指派）、`Exercised`（行权）、`Stock Split`（拆股）、
`Name Change`（更名）、`Journaled Shares`（股票划转）、`Internal Transfer`

理由：指派/行权会同时影响期权和正股两个持仓，拆股要按比例调整全部历史成本——
猜错会让整条持仓线索错掉，宁可问一句。

### 已知细节

- 日期列可能是 `06/13/2026 as of 06/12/2026`。**取 `as of` 那个日期**，那才是交易
  实际发生日。
- 金额带 `$` 和千分位逗号，负数可能写成 `($1,234.56)`，都能正确解析。
- 期权 Symbol 形如 `AAPL 07/17/2026 200.00 C`，自动转 OSI，乘数 100。
- 账户名从文件抬头 `for account XXX as of` 中提取，可用 `--account` 覆盖。

---

## 其他券商 / 自定义 CSV

`--broker generic`（或让它自动 fallback）。列名按语义模糊匹配，大小写和标点无关：

| 目标字段 | 认得的列名 |
|---|---|
| `ts` | ts / timestamp / datetime / date/time / trade date / date / time |
| `symbol` | symbol / ticker / instrument / contract / security |
| `side` | side / action / buy/sell / direction / type |
| `quantity` | quantity / qty / shares / size / units |
| `price` | price / trade price / fill price / avg price / t. price |
| `fees` | fees / commission / fees & comm / comm/fee |
| `thesis` | thesis / reason / rationale / why / note / comment |
| `stop` / `target` | stop / stop loss / take profit / target |

没有 `side` 列时按数量正负推断。

也可以直接导入 JSON / JSONL，字段按 `reference/schema.md` 填即可：

```bash
tradegit import --file trades.json --json
```

---

## 富途 / 老虎 / 其他中文券商

目前没有专门的解析器。做法：让用户导出 CSV，用 `--broker generic` 试一次
`--dry-run`。列名对不上时，先把 CSV 的表头念给用户确认每列含义，然后：

- 少量记录：直接用 `tradegit log --json-input '[...]'` 批量写入
- 大量记录：先用脚本把 CSV 转成上表认得的列名，再走 generic 导入

要新增一个正式的券商解析器，照 `tradegit/importers/schwab.py` 的结构写一个模块，
在 `tradegit/importers/__init__.py` 的 `REGISTRY` 里注册即可。
