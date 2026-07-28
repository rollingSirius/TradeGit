# Trading Journal Skill (TradeGit)

By [@rollingSirius](https://x.com/rollingSirius)

中文文档：[README.md](README.md)

**Keep your trading journal in your own private GitHub repo — log a trade in one sentence, and let git keep every revision of your thinking.**

The goal of this skill is not a prettier trade blotter. It is to capture **what you
were thinking when you placed the trade**, so that months later it can be lined up
against **what actually happened**. It is built for people who actually trade: jot a
trade down as you place it, import a broker statement at month end, and ask "where
exactly did I go wrong" at quarter end. The data can live in a GitHub private repo
or in a fully local git repo. The tool does not place orders and does not give
investment advice.

## What makes it different

Most trade trackers stop at "what you bought and how much you made". This skill
deliberately goes one step further:

| Capability | Design requirement |
|---|---|
| Records the **reasoning**, not just the fill | Thesis, stop/target, conviction, emotion and strategy tags are first-class fields; if you leave the thesis out, the assistant asks for it. |
| The data is yours | Two modes: a Git-backed private repo, or a fully local-only git repo with no remote. |
| Every revision is preserved | Records are append-only: a correction writes a new version, a deletion writes a tombstone. `git log -p` shows what you thought then and how you revised it. |
| Broker statements import directly | TradeGit is broker-neutral: built-in parsers cover common export formats, and generic CSV / JSON work too. Re-importing the same file is idempotent, and rows that can't be inferred safely stop and ask you. |
| P&L you can trust | P&L is computed by TradeGit's deterministic rules from recorded fills, fees, directions, partial exits, position flips and option multipliers; it does not depend on external lot-matching output. |
| Honest about currencies | A mixed HK/US book never has HKD added to USD. Either it reports per currency, or you supply the rates — it will not hand you a plausible-looking wrong number. |
| Review gives facts, not advice | It surfaces falsifiable observations like "losers held 34 days on average vs 21 for winners" — never a buy or sell recommendation. |
| Reports are portable | `tradegit report --since 90d --markdown` writes Markdown; `--pdf --output review.pdf` writes a PDF. |
| Zero dependencies | python3 and git. No third-party packages, no account, no subscription. |
| Credentials never hit disk | It only uses the `gh` / `GITHUB_TOKEN` credentials your environment already has. The token never enters the config file, argv, `.git/config`, or an error message. |

## What it looks like

```
You:     Bought 100 AAPL at 213.45 today, breakout above the range, stop at 205
Assistant:  ✓ Logged and synced to yourname/trading-journal

You:     Import the Schwab statement I just downloaded
Assistant:  Detected Schwab, 9 new records (2026-05-01 → 2026-07-10).
         One "Journaled Shares" row needs you to confirm what the transfer was —
         want to handle it now?

You:     What was my worst loss this quarter?
Assistant:  NVDA, −4,114.50, held 2026-05-20 to 06-02. The entry thesis says
         "buying the dip" and no stop was recorded. Over this period your losers
         were held 34 days on average versus 21 days for winners.
```

**Why not a spreadsheet or a notes app**: the value of a trading journal is in
lining up **what you thought** with **what happened**. Git records who changed what
and when for free, so a trade's thesis, its post-mortem and the mistake you
eventually named are all preserved as a trail. A private repo is best for multi-device
sync; local-only is best when you want to start on one machine without connecting GitHub.

---

## Contents

- [What it does / doesn't do](#what-it-does--doesnt-do)
- [Install](#install)
- [First-time setup](#first-time-setup)
- [Daily use](#daily-use)
- [Command reference](#command-reference)
- [Import formats](#import-formats)
- [How the data is stored](#how-the-data-is-stored)
- [How P&L is calculated](#how-pl-is-calculated)
- [Sync and multiple machines](#sync-and-multiple-machines)
- [Security and privacy](#security-and-privacy)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [License](#license)

---

## What it does / doesn't do

**Does**

- Records every trade along with **the reason at the time**, stop/target, conviction, emotion, tags
- Imports broker statement exports / CSV / JSON, deduplicated and normalized to one schema
- Syncs through a private GitHub repo, or stays fully local
- FIFO lot matching for realized P&L, win rate, profit factor, expectancy, max drawdown, R-multiples
- Lets you append a post-mortem and a named mistake later; `git log -p` shows the whole evolution
- Generates deterministic Markdown / PDF review reports

**Doesn't**

- **No order placement, no broker trading API.** You place the order, then log it.
- **No investment advice.** It states data ("4 of the last 6 trades in this name lost money"), never a recommendation.
- **No charts or interactive dashboards.** See [Visualization](#visualization).

### Visualization

Deliberately left out. Every analysis command emits `--json`, so you can hand the
data to whatever renderer you have (Claude / Workbuddy charting, an Artifact, your own
dashboard, a BI tool):

| Output | What it plots well |
|---|---|
| `analyze --json` → `equity_curve[]` | Cumulative realized P&L curve (`ts` / `pnl` / `cumulative`) |
| `analyze --group-by month --json` → `grouped[]` | Monthly P&L bars |
| `analyze --group-by symbol --json` → `grouped[]` | P&L ranked by symbol |
| `analyze --json` → `metrics{}` | Stat tiles |
| `roundtrips --json` | Closed-trade table |
| `positions --mark ... --json` | Positions table |
| `sql "..." --json` | Any custom slice |

This keeps the boundary clean: TradeGit's job is getting the numbers right, not
imposing a chart style on you.

---

## Install

```bash
git clone https://github.com/rollingSirius/TradeGit.git ~/TradeGit
cd ~/TradeGit && ./install.sh
```

The installer detects which host tools you have and registers the skill:

| Target | Installed to | How to trigger |
|---|---|---|
| Claude Code / Desktop / claude.ai / Workbuddy | `~/.claude/skills/tradegit` | Just say "log a trade" |
| Codex / Workbuddy | `~/.codex/AGENTS.md` + `~/.codex/prompts/tradegit.md` | Just say it, or `/tradegit` |
| Command line | `~/.local/bin/tradegit` | `tradegit <command>` |

Other options:

```bash
./install.sh --claude              # Claude / Workbuddy only
./install.sh --codex               # Codex / Workbuddy only
./install.sh --project ~/myrepo    # install as a project skill (.claude/skills/)
./install.sh --copy                # copy files instead of symlinking
./install.sh --no-bin              # skip the ~/.local/bin symlink
./install.sh --uninstall           # uninstall (never touches your journal data)
```

The script is idempotent — running it again is safe. It symlinks by default, so
`git pull` updates the skill without reinstalling.

**The only dependencies are python3 (3.9+) and git.** Creating the GitHub repo needs
the `gh` CLI or a `GITHUB_TOKEN`.

---

## First-time setup

### Git-backed private repo

```bash
gh auth login          # or export GITHUB_TOKEN=<token with repo scope>
tradegit init          # creates <you>/trading-journal (private) and clones it
```

| Scenario | Command |
|---|---|
| Different repo name | `tradegit init --name my-trades` |
| Reuse an existing journal repo | `tradegit init --repo owner/name --use-existing` |
| New machine, restore full history | Same as above — the clone brings everything back |
| Set a default account name | `tradegit init --account ibkr-main` |

`init` checks repo visibility and **refuses anything that isn't private**. That is
deliberate.

Inside Claude / Codex / Workbuddy the assistant asks you before creating the repo — that step
creates something real under your GitHub account.

### Local-only

```bash
tradegit init --local
```

This creates `~/.tradegit/repo/` as a local git repository. Writes are committed
locally, no remote is configured, and nothing is pushed.

### Try the sample journal

```bash
TRADEGIT_HOME="$(pwd)/examples/sample-journal" python3 -m tradegit report --since 180d --markdown
```

`examples/sample-journal/` is a fictional local-only journal and does not require GitHub.

---

## Daily use

### Log a trade

```bash
tradegit log --symbol AAPL --side BUY --qty 100 --price 213.45 \
             --why "Breakout above the range on 1.8x average volume" \
             --stop 205 --target 240 --strategy swing \
             --tags "tech,breakout" --conviction 4
```

- `--side`: `BUY` / `SELL` / `SHORT` / `COVER`. Open a short with `SHORT`, close it with `COVER`.
- **`--why` is the whole point of the tool.** A record without a reason is just a blotter line.
- `--stop` is what makes an R-multiple computable later. Record it when you can.
- Without `--at` it uses now; backfill history with `--at "2026-05-04T09:31:12Z"`.
- Options: `--asset-class OPT`. `--symbol` accepts broker notation (`AAPL 07/17/2026 200.00 C`) or OSI (`AAPL  260717C00200000`); both normalize, with a 100x multiplier.
- Duplicates are skipped by content. Use `--allow-duplicates` if you really mean two identical fills.
- Commits and pushes automatically. Offline, it tells you to run `tradegit sync` later.

Batch input:

```bash
tradegit log --json-input '[{"symbol":"NVDA","side":"BUY","quantity":30,
  "price":1220.5,"ts":"2026-07-08T13:45:00Z","thesis":"..."}]'
```

Full field list in [`reference/schema.md`](reference/schema.md).

### Import a broker statement

```bash
tradegit import --file ~/Downloads/statement.csv --dry-run   # preview first
tradegit import --file ~/Downloads/statement.csv             # then write
```

`--dry-run` reports which broker was detected, how many records are new, the date
range, the symbols involved, and **which rows need a human decision** (Schwab's
`Journaled Shares`, option `Assigned`/`Exercised` — guessing wrong on those corrupts
the whole position history, so it asks instead).

Re-importing the same file is idempotent. Imported records have no thesis; you can
add one afterwards:

```bash
tradegit amend <id> --why "Added after listening to the earnings call"
```

### Review

```bash
tradegit analyze --since 90d                    # summary + factual observations
tradegit analyze --since ytd --group-by symbol  # by symbol (losses first)
tradegit roundtrips --sort pnl --limit 10       # 10 biggest losers
tradegit positions --mark AAPL=213.4            # positions (marks give unrealized P&L)
tradegit analyze --fx HKD=0.128 --base-currency USD   # mixed-currency book, one base
tradegit report --since 90d --markdown          # Markdown review report
tradegit report --since 180d --pdf --output review.pdf
```

`analyze` adds a few **observations** derived from the data:

```
Review observations:
  · Average win 1895.47 < average loss 3165.51 (payoff 0.60); a 57.1% win rate
    needs to exceed 62.5% to break even.
  · Losers held 34.2 days on average vs 21.4 for winners — the classic
    "cut your winners, let your losers run" shape.
  · 6 of 7 trades recorded no stop, so no R-multiple can be computed.
```

These are facts, not advice. Write your conclusion back into the journal so the next
review can see it:

```bash
tradegit amend <id> --review "Dip buy with no volume confirmation — catching a falling knife" \
                    --mistake "entry without a signal"
```

More recipes and a SQL cookbook in [`reference/analysis.md`](reference/analysis.md).

---

## Command reference

| Command | Purpose |
|---|---|
| `init` | Create/connect a journal; `--local` creates a fully local journal |
| `log` | Record a trade (or a batch via `--json-input`) |
| `import` | Import a broker statement; `--dry-run` to preview |
| `list` | List records with any filter |
| `positions` | Current positions (FIFO); `--mark` for unrealized P&L |
| `analyze` | P&L metrics, grouped stats, review observations |
| `report` | Markdown / PDF review report |
| `roundtrips` | Closed trades, sortable by P&L or return |
| `sql` | Read-only SQL over the local index, table `trades` |
| `amend` | Append a corrected version (history is not rewritten) |
| `void` | Void a record |
| `check` | Has the private repo changed? (exit code 1 when out of sync) |
| `sync` | Pull + push |
| `status` | Repo, sync state, record count, positions |
| `doctor` | Environment self-check |
| `config` | View / change configuration |

**Common flags**

- Filters: `--since` `--until` `--symbol/-s` `--account` `--broker` `--strategy` `--tag`
- Time syntax: `30d` / `90d` / `180d` / `360d` / `3w` / `3m` / `1y` / `ytd` / `mtd` / `today` / `2026-01-01`
- `--json`: structured output (this is what the assistant uses)
- `--no-sync`: skip the remote check (offline)
- `--no-push`: commit locally without pushing

Add `-h` to any command for its full options.

---

## Import formats

TradeGit is not tied to any broker. Built-in parsers simply reduce field-mapping
work; anything that can be exported as CSV / JSON can be normalized into the same
journal schema.

| Source | Export |
|---|---|
| Built-in parsers | Common Activity / Transactions / Flex Query CSV formats |
| Generic tables | Generic CSV with fuzzy column matching |
| Structured records | JSON / JSONL |

Export steps, field mappings and known gotchas are in
[`reference/brokers.md`](reference/brokers.md).

Imports also capture **cash events** — dividends, interest, withholding tax, account
fees, deposits and withdrawals. Dropping those skews P&L.

To add a formal import format: copy the shape of an existing importer and register
it in `REGISTRY` in `tradegit/importers/__init__.py`.

---

## How the data is stored

```
~/.tradegit/
  config.json                            storage mode, repo slug, default account, sync prefs
  repo/                                  ← git repo: GitHub clone or local-only
    journal/2026/2026-05.jsonl           ← one record per line, append-only
    journal/2026/2026-06.jsonl
    manifest.json                        record counts, date range (generated)
    schema/trade.schema.json             JSON Schema
    .gitattributes                       *.jsonl merge=union
  cache/index.sqlite                     derived index; delete it and it rebuilds
  imports/                               raw broker files kept by --keep-source
```

**Why month-partitioned JSONL**: appending one trade produces a one-line diff, so git
history stays readable; two machines writing on the same day merge automatically via
`merge=union`; and monthly (rather than daily) files stay browsable on github.com.
SQLite is only a local query accelerator — the JSONL is the source of truth. Full
rationale in [`reference/storage.md`](reference/storage.md).

**Records are append-only**: `amend` writes a new version carrying `supersedes`,
`void` writes a tombstone, and the original stays in git history forever. Reads
collapse the stream into the current view.

A record looks like this (abridged):

```json
{
  "id": "trd_20260504T093112Z_AAPL_3f9a1c2d",
  "kind": "trade", "ts": "2026-05-04T09:31:12Z",
  "account": "ibkr-main", "broker": "IBKR",
  "symbol": "AAPL", "asset_class": "STK", "side": "BUY",
  "quantity": 100, "price": 213.45, "fees": {"commission": 1.0025},
  "net_amount": -21346.0025, "signed_quantity": 100,
  "thesis": "Broke out of the range that has held since March, on 1.8x average volume",
  "strategy": "swing", "conviction": 4, "tags": ["breakout", "tech"],
  "risk": {"stop": 205.0, "target": 240.0, "risk_amount": 845.0, "planned_r": 3.142}
}
```

Full field list in [`reference/schema.md`](reference/schema.md).

---

## How P&L is calculated

FIFO lot matching: a fill in the same direction as the position opens a new lot; an
opposing fill closes the oldest lots first, and any residual quantity flips the
position (shorts are fully supported). Each round trip is net of fees allocated from
both sides, and options use their contract multiplier.

- Dividends / interest / tax / account fees land in `cash_events_net`
- Deposits and withdrawals **do not count as performance**
- Trades with a `--stop` get an R-multiple
- Unrealized P&L needs marks (`--mark`); without them it is `null`
- **Currencies are never summed together**: for a mixed HK/US book, either pass
  `--fx HKD=0.128 --base-currency USD` to convert, or read the per-currency
  breakdown. No total beats a wrong total

Metrics: realized P&L, win rate, profit factor, expectancy, average win/loss, largest
win/loss, max drawdown, win/loss streaks, average hold days, average R. Groupable by
symbol / month / strategy / tag / direction.

Every metric is computed from recorded fills, fees, cash events and risk fields.
Imported files provide raw facts; broker-side lot matching is not used as the
calculation source.

---

## Sync and multiple machines

TradeGit has two storage modes:

| Mode | Init | Behavior |
|---|---|---|
| Git-backed private repo | `tradegit init` | Local clone of a GitHub private repo; reads/writes check remote drift and writes push by default. |
| Local-only | `tradegit init --local` | Plain local git repo; writes commit locally, no remote is configured, nothing is pushed. |

- **Checking for remote changes** = `git ls-remote` against local HEAD. It fetches no
  objects and takes tens of milliseconds, which is why GitHub mode reads and writes
  do it by default and rebase automatically when behind.
- **Two machines journaling at once** = in GitHub mode, both append; `merge=union` resolves it.
- **Offline** = `--no-sync` (skip the check) or `--no-push` (commit only); a later
  `tradegit sync` pushes whatever piled up.
- **Fully local** = after `tradegit init --local`, `tradegit sync` only makes sure
  the local git commit exists.

```bash
tradegit check          # has the private repo moved?
tradegit check --pull   # and pull if so
tradegit sync           # pull + push
tradegit init --local   # local-only git journal
```

---

## Security and privacy

- **No token is ever stored.** It uses, in order: the `gh` CLI → `GITHUB_TOKEN` /
  `GH_TOKEN` → git's own credential helper.
- **The token never hits disk or argv.** The stored git remote never contains a
  credential (embedding one would leave it in plaintext in `.git/config`);
  authentication goes through a per-invocation credential helper that reads the token
  from the environment, so `ps` on a shared machine cannot see it. All git output is
  redacted before it can reach an error message.
- In GitHub mode, **`init` refuses a non-private repo**; local-only mode never connects a remote.
- `~/.tradegit` is created `0700` — it holds financial records, and the default umask
  would leave it readable by other users on the machine.
- `tradegit sql` is read-only: the index is refreshed, then reopened read-only, so a
  query cannot mutate anything.
- **Only trade records go in the repo.** Never put tokens, passwords or broker logins there.
- Records may carry an account identifier (Schwab exports give `individual-xxxx-1234`);
  override it with `--account` if you'd rather it not be there.
- Uninstalling touches neither `~/.tradegit` nor your GitHub repo, and never rewrites
  a file it doesn't own.

Each of these has a test (`TestSecurity`) — they are guarantees, not good intentions.

---

## Troubleshooting

Run `tradegit doctor` first; it checks each item and prints the fix.

| Symptom | Cause / fix |
|---|---|
| `没有检测到已连接的 GitHub 账户` | `gh auth login`, or `export GITHUB_TOKEN=<token with repo scope>` |
| `创建仓库需要 gh CLI 登录` | Install and log in to `gh`, or create the private repo by hand and use `--use-existing` |
| `xxx 当前是 public 仓库` | Flip it to private in GitHub settings, then rerun `init` |
| `TradeGit 尚未初始化` | `tradegit init` |
| Push failed | The record is committed locally; run `tradegit sync` once you're online |
| `pull failed and could not be auto-resolved` | A conflict outside the JSONL files; resolve it by hand in `~/.tradegit/repo` |
| Numbers don't match your broker | Usually missing cash events or a gap in the imported range; check `tradegit list --kind cash` |
| Fewer records than expected after import | Look at `unparsed` and `duplicates` in the `--dry-run` output |
| Index looks stale | Delete `~/.tradegit/cache/index.sqlite`; it rebuilds on the next query |

Multiple journals: switch roots with the `TRADEGIT_HOME` environment variable.

---

## Development

```bash
python3 -m unittest tests.test_tradegit -v
```

The tests cover field normalization, P&L calculation, import parsers, storage dedup
and index refresh, the credential-safety guarantees, and a full CLI end-to-end run against a local bare
repo standing in for GitHub (log → dedup → import → analyze → amend → detect a remote
change and pull → push commits left behind while offline).

```
SKILL.md              Claude / Workbuddy entry point (skill definition)
AGENTS.md             Codex / Workbuddy entry point
README.md / .en.md    Chinese / English docs
install.sh            install / uninstall
scripts/tradegit      CLI launcher
tradegit/             implementation (no third-party dependencies)
  cli.py              command line
  schema.py           record normalization, validation, append-only semantics
  store.py            JSONL storage + derived SQLite index
  sync.py             GitHub connection, remote drift detection, conflict resolution
  analytics.py        FIFO matching and performance metrics
  config.py           configuration and paths
  scaffold.py         files written into the private repo on init
  importers/          ibkr / schwab / generic
reference/            schema / brokers / analysis / storage
tests/                tests and broker sample files
```

---

## License

[MIT](LICENSE) © [rollingSirius](https://github.com/rollingSirius)

Fork it, change it, use it commercially. If you write a parser for another broker, a
PR back would be welcome.

## Disclaimer

This tool exists to **record and summarize** your own trades. It is not investment
advice and is not a recommendation to buy or sell anything. P&L figures are derived
from the data you enter or import and **are not a substitute for your broker's
statements** — use official broker documents for tax and reconciliation purposes. Any
investment decision you make while using this tool, and its outcome, are your own.
