"""P&L engine: FIFO lot matching, round trips, and performance metrics.

Positions are tracked per ``(account, symbol)``. Options match on the OSI
contract symbol, so each strike/expiry is its own position. Shorts are
supported: a fill in the opposite direction of the open position closes
lots FIFO, and any residual quantity flips the position.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Iterable

from .schema import parse_ts, round_money


# ---------------------------------------------------------------------------
# lot matching
# ---------------------------------------------------------------------------

class _Lot:
    __slots__ = ("qty", "price", "ts", "rid", "direction", "fee_per_unit",
                 "multiplier", "record")

    def __init__(self, qty: float, record: dict[str, Any], direction: int):
        self.qty = qty
        self.price = float(record.get("price") or 0.0)
        self.ts = record.get("ts", "")
        self.rid = record.get("id", "")
        self.direction = direction
        self.multiplier = float(record.get("multiplier") or 1.0)
        total_qty = float(record.get("quantity") or 1.0)
        self.fee_per_unit = float(record.get("fees_total") or 0.0) / (total_qty or 1.0)
        self.record = record


def match_fifo(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Walk fills chronologically, producing closed round trips + open lots."""
    trades = sorted(
        (r for r in records if r.get("kind", "trade") == "trade"),
        key=lambda r: (r.get("ts", ""), r.get("id", "")),
    )
    books: dict[tuple[str, str], deque[_Lot]] = defaultdict(deque)
    roundtrips: list[dict[str, Any]] = []

    for record in trades:
        key = (record.get("account", ""), record.get("symbol", ""))
        book = books[key]
        signed = float(record.get("signed_quantity") or 0.0)
        if signed == 0:
            continue
        direction = 1 if signed > 0 else -1
        remaining = abs(signed)
        multiplier = float(record.get("multiplier") or 1.0)
        exit_qty_total = abs(signed)
        exit_fee_per_unit = float(record.get("fees_total") or 0.0) / (exit_qty_total or 1.0)

        # Close against opposing lots first.
        while remaining > 1e-12 and book and book[0].direction != direction:
            lot = book[0]
            matched = min(remaining, lot.qty)
            entry_price, exit_price = lot.price, float(record.get("price") or 0.0)
            gross = (exit_price - entry_price) * matched * multiplier * lot.direction
            fees = matched * (lot.fee_per_unit + exit_fee_per_unit)
            roundtrips.append(_make_roundtrip(
                lot, record, matched, gross, fees, multiplier))
            lot.qty -= matched
            remaining -= matched
            if lot.qty <= 1e-12:
                book.popleft()

        # Anything left opens (or flips) a position.
        if remaining > 1e-12:
            book.append(_Lot(remaining, record, direction))

    open_positions = _summarize_open(books)
    roundtrips.sort(key=lambda t: (t["exit_ts"], t["symbol"]))
    return {"roundtrips": roundtrips, "open_positions": open_positions}


def _make_roundtrip(lot: _Lot, exit_rec: dict[str, Any], qty: float,
                    gross: float, fees: float, multiplier: float) -> dict[str, Any]:
    entry = lot.record
    entry_price, exit_price = lot.price, float(exit_rec.get("price") or 0.0)
    net = gross - fees
    cost_basis = entry_price * qty * multiplier
    hold_days = _days_between(lot.ts, exit_rec.get("ts", ""))
    risk = (entry.get("risk") or {})
    r_multiple = None
    stop = risk.get("stop")
    if stop:
        per_unit_risk = abs(entry_price - float(stop))
        if per_unit_risk > 1e-9:
            r_multiple = round_money(
                (net / (per_unit_risk * qty * multiplier)), 3)
    entry_ccy = (entry.get("currency") or "").upper()
    exit_ccy = (exit_rec.get("currency") or "").upper()
    return {
        "symbol": exit_rec.get("symbol", ""),
        "underlying": (exit_rec.get("option") or {}).get("underlying") or exit_rec.get("symbol"),
        "account": exit_rec.get("account", ""),
        "currency": exit_ccy or entry_ccy or "USD",
        # Entry and exit in different currencies means one of them is mis-recorded.
        "currency_mismatch": bool(entry_ccy and exit_ccy and entry_ccy != exit_ccy) or None,
        "asset_class": exit_rec.get("asset_class", "STK"),
        "direction": "long" if lot.direction > 0 else "short",
        "quantity": round_money(qty, 8),
        "entry_ts": lot.ts,
        "exit_ts": exit_rec.get("ts", ""),
        "entry_price": round_money(entry_price, 6),
        "exit_price": round_money(exit_price, 6),
        "cost_basis": round_money(cost_basis),
        "gross_pnl": round_money(gross),
        "fees": round_money(fees, 4),
        "net_pnl": round_money(net),
        "return_pct": round_money((net / cost_basis * 100.0) if cost_basis else 0.0, 4),
        "hold_days": hold_days,
        "r_multiple": r_multiple,
        "entry_id": lot.rid,
        "exit_id": exit_rec.get("id", ""),
        "strategy": entry.get("strategy") or exit_rec.get("strategy"),
        "setup": entry.get("setup"),
        "thesis": entry.get("thesis"),
        "exit_note": exit_rec.get("thesis") or exit_rec.get("notes"),
        "conviction": entry.get("conviction"),
        "tags": sorted(set((entry.get("tags") or []) + (exit_rec.get("tags") or []))),
    }


def _days_between(start: str, end: str) -> float:
    try:
        delta = parse_ts(end) - parse_ts(start)
    except Exception:
        return 0.0
    return round(delta.total_seconds() / 86400.0, 3)


def _summarize_open(books: dict[tuple[str, str], deque[_Lot]]) -> list[dict[str, Any]]:
    positions = []
    for (account, symbol), book in books.items():
        lots = [lot for lot in book if lot.qty > 1e-12]
        if not lots:
            continue
        qty = sum(lot.qty for lot in lots)
        direction = lots[0].direction
        cost = sum(lot.qty * lot.price for lot in lots)
        multiplier = lots[0].multiplier
        positions.append({
            "account": account,
            "symbol": symbol,
            "direction": "long" if direction > 0 else "short",
            "quantity": round_money(qty * direction, 8),
            "avg_price": round_money(cost / qty if qty else 0.0, 6),
            "cost_basis": round_money(cost * multiplier),
            "multiplier": multiplier,
            "opened_ts": min(lot.ts for lot in lots),
            "lots": len(lots),
            "asset_class": lots[0].record.get("asset_class", "STK"),
            "currency": (lots[0].record.get("currency") or "USD").upper(),
            "strategy": lots[0].record.get("strategy"),
            "thesis": lots[0].record.get("thesis"),
            "stop": (lots[0].record.get("risk") or {}).get("stop"),
            "target": (lots[0].record.get("risk") or {}).get("target"),
        })
    positions.sort(key=lambda p: -abs(p["cost_basis"]))
    return positions


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------

class CurrencyError(ValueError):
    """Raised when money in different currencies would have to be added up."""


def _currency_free_notes(report: dict[str, Any], metrics: dict[str, Any],
                         trips: list[dict[str, Any]]) -> list[str]:
    """Observations that survive a mixed-currency book — counts and durations."""
    notes = []
    no_stop = [t for t in trips if t.get("r_multiple") is None]
    if len(no_stop) > len(trips) * 0.5:
        notes.append(f"{len(no_stop)}/{len(trips)} 笔交易没有记录止损价，无法计算 R 倍数。")

    losers = [t["hold_days"] for t in trips if t["net_pnl"] < 0]
    winners = [t["hold_days"] for t in trips if t["net_pnl"] > 0]
    if losers and winners:
        avg_l, avg_w = sum(losers) / len(losers), sum(winners) / len(winners)
        if avg_l > avg_w * 1.5:
            notes.append(f"亏损单平均持有 {avg_l:.1f} 天 vs 盈利单 {avg_w:.1f} 天——"
                         f"典型的「截断利润、让亏损奔跑」形态。")

    untagged = sum(1 for t in trips if not t.get("thesis"))
    if untagged:
        notes.append(f"{untagged}/{len(trips)} 笔没有记录交易理由。")

    for bucket in report.get("by_currency") or []:
        notes.append(f"{bucket['currency']}：{bucket['roundtrips']} 笔平仓，"
                     f"已实现 {bucket['realized_pnl']:+,.2f}，"
                     f"胜率 {bucket['win_rate']:.1f}%。")
    return notes


def _money_metrics(trips: list[dict[str, Any]], cash_records: list[dict[str, Any]],
                   positions: list[dict[str, Any]], marks: dict[str, float]
                   ) -> dict[str, Any]:
    """Metrics denominated in money. Only valid for one currency at a time."""
    wins = [t for t in trips if t["net_pnl"] > 0]
    losses = [t for t in trips if t["net_pnl"] < 0]
    gross_profit = sum(t["net_pnl"] for t in wins)
    gross_loss = -sum(t["net_pnl"] for t in losses)
    realized = sum(t["net_pnl"] for t in trips)
    total_fees = sum(t["fees"] for t in trips)

    cash = _cash_summary(cash_records)
    unrealized, marked = _unrealized(positions, marks)

    equity = _equity_curve(trips)
    return {
        "metrics": {
        "roundtrips": len(trips),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(trips) - len(wins) - len(losses),
        "win_rate": round_money(len(wins) / len(trips) * 100.0, 2) if trips else 0.0,
        "realized_pnl": round_money(realized),
        "gross_profit": round_money(gross_profit),
        "gross_loss": round_money(gross_loss),
        "fees_paid": round_money(total_fees),
        "avg_win": round_money(gross_profit / len(wins), 2) if wins else 0.0,
        "avg_loss": round_money(-gross_loss / len(losses), 2) if losses else 0.0,
        # Over wins/losses specifically: min() across all trades reports the
        # smallest *win* as the "largest loss" when nothing lost money.
        "largest_win": round_money(max((t["net_pnl"] for t in wins), default=0.0)),
        "largest_loss": round_money(min((t["net_pnl"] for t in losses), default=0.0)),
        "profit_factor": round_money(gross_profit / gross_loss, 3) if gross_loss else None,
        "expectancy": round_money(realized / len(trips), 2) if trips else 0.0,
        "payoff_ratio": round_money(
            (gross_profit / len(wins)) / (gross_loss / len(losses)), 3)
            if wins and losses else None,
        "avg_hold_days": round_money(
            sum(t["hold_days"] for t in trips) / len(trips), 2) if trips else 0.0,
        "avg_r_multiple": _avg_r(trips),
        "max_drawdown": equity["max_drawdown"],
        "max_drawdown_pct_of_peak": equity["max_drawdown_pct"],
        "best_streak": equity["best_streak"],
        "worst_streak": equity["worst_streak"],
        "cash_events_net": cash["net"],
        "dividends": cash["dividends"],
        "interest": cash["interest"],
        "account_fees": cash["fees"],
        "unrealized_pnl": unrealized,
        "total_pnl": round_money(realized + cash["net"] + (unrealized or 0.0)),
        },
        "equity_curve": equity["points"],
        "open_positions": marked,
    }


# Metrics that are pure counts or ratios of counts, and therefore stay
# meaningful even when the trades span several currencies.
COUNT_METRICS = ("roundtrips", "wins", "losses", "breakeven", "win_rate",
                 "avg_hold_days", "best_streak", "worst_streak")


def _convert(trip: dict[str, Any], rate: float) -> dict[str, Any]:
    """A copy of the round trip with money fields expressed in the base currency."""
    converted = dict(trip)
    for field in ("gross_pnl", "fees", "net_pnl", "cost_basis"):
        converted[field] = round_money(trip[field] * rate)
    converted["fx_rate"] = rate
    return converted


def summarize(records: list[dict[str, Any]], *, marks: dict[str, float] | None = None,
              fx: dict[str, float] | None = None,
              base_currency: str | None = None) -> dict[str, Any]:
    """Full performance report over an already-filtered record set.

    Money in different currencies is never silently added up. With a single
    currency this behaves as you'd expect. With several:

    * given ``fx`` rates (``{"HKD": 0.128}``, meaning 1 HKD = 0.128 base),
      everything is converted into ``base_currency`` and reported as one number;
    * without rates, every money metric at the top level is ``None`` and the
      real numbers live in ``by_currency`` — a wrong total is worse than no
      total in a P&L tool.
    """
    matched = match_fifo(records)
    trips = matched["roundtrips"]
    positions = matched["open_positions"]
    marks = {k.upper(): float(v) for k, v in (marks or {}).items()}
    rates = {k.upper(): float(v) for k, v in (fx or {}).items()}
    cash_records = [r for r in records if r.get("kind") == "cash"]

    currencies = sorted({t["currency"] for t in trips}
                        | {(r.get("currency") or "").upper() for r in cash_records}
                        | {p["currency"] for p in positions}) or []
    currencies = [c for c in currencies if c]

    period = {
        "from": records[0]["ts"] if records else None,
        "to": records[-1]["ts"] if records else None,
        "records": len(records),
    }

    def block(subset_trips, subset_cash, subset_positions):
        return _money_metrics(subset_trips, subset_cash, subset_positions, marks)

    by_currency = []
    for code in currencies:
        part = block([t for t in trips if t["currency"] == code],
                     [r for r in cash_records if (r.get("currency") or "").upper() == code],
                     [p for p in positions if p["currency"] == code])
        by_currency.append({"currency": code, **part["metrics"],
                            "equity_curve": part["equity_curve"]})

    mixed = len(currencies) > 1
    base = (base_currency or ("" if mixed else (currencies[0] if currencies else "")))
    base = base.upper() if base else None
    if mixed and rates and not base:
        base = "USD"

    if not mixed:
        computed = block(trips, cash_records, positions)
        metrics = {**computed["metrics"], "currency": base, "currency_mixed": False}
        equity_curve = computed["equity_curve"]
        marked = computed["open_positions"]
        scaled = trips
    elif rates:
        missing = [c for c in currencies if c != base and c not in rates]
        if missing:
            raise CurrencyError(
                f"这些币种缺少汇率：{', '.join(missing)}。"
                f"用 --fx {missing[0]}=<1 {missing[0]} 折合多少 {base}> 补上，"
                f"或不传 --fx 以按币种分别查看。")
        scaled = [_convert(t, 1.0 if t["currency"] == base else rates[t["currency"]])
                  for t in trips]
        scaled_cash = []
        for record in cash_records:
            code = (record.get("currency") or "").upper()
            rate = 1.0 if code == base else rates.get(code, 1.0)
            scaled_cash.append({**record,
                                "net_amount": (record.get("net_amount") or 0.0) * rate})
        computed = block(scaled, scaled_cash, positions)
        metrics = {**computed["metrics"], "currency": base, "currency_mixed": True,
                   "fx_rates": {**rates, base: 1.0}}
        equity_curve = computed["equity_curve"]
        marked = computed["open_positions"]
    else:
        # Mixed currencies, no rates: report counts, refuse to invent a total.
        counts = block(trips, [], [])["metrics"]
        metrics = {k: (counts[k] if k in COUNT_METRICS else None) for k in counts}
        metrics.update({
            "currency": None,
            "currency_mixed": True,
            "currencies": currencies,
            "note": ("交易涉及多个币种，金额类指标无法合并。"
                     "请看 by_currency，或传 --fx 折算到统一币种。"),
        })
        equity_curve = []
        marked = positions
        scaled = trips

    return {
        "period": period,
        "currencies": currencies,
        "base_currency": base,
        "metrics": metrics,
        "by_currency": by_currency,
        "equity_curve": equity_curve,
        "roundtrips": trips,
        "open_positions": marked,
        # Grouped over base-converted trips when rates were supplied, so a
        # month or strategy bucket spanning currencies still adds up.
        "by_symbol": group_by(scaled, "symbol"),
        "by_month": group_by(scaled, lambda t: t["exit_ts"][:7]),
        "by_strategy": group_by(scaled, lambda t: t.get("strategy") or "(unset)"),
        "by_direction": group_by(scaled, "direction"),
        "by_tag": _group_by_tag(scaled),
        "worst": sorted(scaled, key=lambda t: t["net_pnl"])[:10],
        "best": sorted(scaled, key=lambda t: -t["net_pnl"])[:10],
    }


def _avg_r(trips: list[dict[str, Any]]) -> float | None:
    values = [t["r_multiple"] for t in trips if t.get("r_multiple") is not None]
    return round_money(sum(values) / len(values), 3) if values else None


def _cash_summary(records: list[dict[str, Any]]) -> dict[str, float]:
    buckets = defaultdict(float)
    for record in records:
        if record.get("kind") != "cash":
            continue
        buckets[record.get("cash_type", "OTHER")] += float(record.get("net_amount") or 0.0)
    external = {"DEPOSIT", "WITHDRAWAL"}
    net = sum(v for k, v in buckets.items() if k not in external)
    return {
        "net": round_money(net),
        "dividends": round_money(buckets.get("DIVIDEND", 0.0)),
        "interest": round_money(buckets.get("INTEREST", 0.0)),
        "fees": round_money(buckets.get("FEE", 0.0) + buckets.get("TAX", 0.0)),
        "deposits": round_money(buckets.get("DEPOSIT", 0.0)),
        "withdrawals": round_money(buckets.get("WITHDRAWAL", 0.0)),
    }


def _unrealized(positions: list[dict[str, Any]], marks: dict[str, float]
                ) -> tuple[float | None, list[dict[str, Any]]]:
    if not positions:
        return (0.0, [])
    total = 0.0
    any_marked = False
    out = []
    for position in positions:
        enriched = dict(position)
        mark = marks.get(position["symbol"].upper())
        if mark is not None:
            any_marked = True
            qty = position["quantity"]
            pnl = (mark - position["avg_price"]) * qty * position["multiplier"]
            enriched["mark"] = mark
            enriched["unrealized_pnl"] = round_money(pnl)
            enriched["market_value"] = round_money(mark * qty * position["multiplier"])
            enriched["return_pct"] = round_money(
                pnl / abs(position["cost_basis"]) * 100.0 if position["cost_basis"] else 0.0, 3)
            total += pnl
        out.append(enriched)
    return (round_money(total) if any_marked else None, out)


def _equity_curve(trips: list[dict[str, Any]]) -> dict[str, Any]:
    points, cumulative, peak, max_dd = [], 0.0, 0.0, 0.0
    streak = best_streak = worst_streak = 0
    for trip in trips:
        cumulative += trip["net_pnl"]
        peak = max(peak, cumulative)
        max_dd = min(max_dd, cumulative - peak)
        points.append({
            "ts": trip["exit_ts"], "symbol": trip["symbol"],
            "pnl": round_money(trip["net_pnl"]), "cumulative": round_money(cumulative),
        })
        if trip["net_pnl"] > 0:
            streak = streak + 1 if streak > 0 else 1
            best_streak = max(best_streak, streak)
        elif trip["net_pnl"] < 0:
            streak = streak - 1 if streak < 0 else -1
            worst_streak = min(worst_streak, streak)
    return {
        "points": points,
        "max_drawdown": round_money(max_dd),
        "max_drawdown_pct": round_money(abs(max_dd) / peak * 100.0, 2) if peak > 0 else None,
        "best_streak": best_streak,
        "worst_streak": abs(worst_streak),
    }


def group_by(trips: list[dict[str, Any]], key: Any) -> list[dict[str, Any]]:
    getter = key if callable(key) else (lambda t: t.get(key) or "(unset)")
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trip in trips:
        buckets[str(getter(trip))].append(trip)
    return sorted(
        (_bucket_stats(name, items) for name, items in buckets.items()),
        key=_bucket_sort_key,
    )


def _bucket_sort_key(bucket: dict[str, Any]) -> tuple[int, float]:
    """Losses first; buckets with no comparable total sort last."""
    total = bucket.get("net_pnl")
    return (1, 0.0) if total is None else (0, total)


def _group_by_tag(trips: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trip in trips:
        for tag in trip.get("tags") or ["(untagged)"]:
            buckets[tag].append(trip)
    return sorted((_bucket_stats(n, i) for n, i in buckets.items()),
                  key=_bucket_sort_key)


def _bucket_stats(name: str, trips: list[dict[str, Any]]) -> dict[str, Any]:
    net = sum(t["net_pnl"] for t in trips)
    wins = [t for t in trips if t["net_pnl"] > 0]
    losses = [t for t in trips if t["net_pnl"] < 0]
    gross_loss = -sum(t["net_pnl"] for t in losses)
    codes = sorted({t.get("currency") for t in trips if t.get("currency")})
    if len(codes) > 1:
        # A month or tag bucket can span currencies; a total would be fiction.
        return {
            "key": name, "roundtrips": len(trips), "wins": len(wins),
            "losses": len(losses),
            "win_rate": round_money(len(wins) / len(trips) * 100.0, 2) if trips else 0.0,
            "net_pnl": None, "avg_pnl": None, "profit_factor": None,
            "fees": None, "volume": None, "currencies": codes,
        }
    return {
        "key": name,
        "currency": codes[0] if codes else None,
        "roundtrips": len(trips),
        "net_pnl": round_money(net),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round_money(len(wins) / len(trips) * 100.0, 2) if trips else 0.0,
        "avg_pnl": round_money(net / len(trips), 2) if trips else 0.0,
        "profit_factor": round_money(sum(t["net_pnl"] for t in wins) / gross_loss, 3)
                         if gross_loss else None,
        "fees": round_money(sum(t["fees"] for t in trips), 2),
        "volume": round_money(sum(t["cost_basis"] for t in trips), 2),
    }


def review_prompts(report: dict[str, Any]) -> list[str]:
    """Concrete things worth asking about, derived from the numbers.

    These are observations for the user to reflect on, not advice.
    """
    notes: list[str] = []
    metrics = report["metrics"]
    trips = report["roundtrips"]
    if not trips:
        return ["No closed round trips in this period."]

    if metrics.get("currency_mixed") and metrics.get("currency") is None:
        codes = "、".join(report.get("currencies") or [])
        notes.append(
            f"这段时间的交易涉及 {codes} 多个币种，金额无法直接合并——"
            f"下面只给出与币种无关的观察。要看合计，请传 --fx 折算到统一币种。")
        return notes + _currency_free_notes(report, metrics, trips)

    if metrics["losses"] and metrics["avg_loss"] and metrics["avg_win"]:
        ratio = abs(metrics["avg_win"] / metrics["avg_loss"])
        if ratio < 1:
            notes.append(
                f"平均盈利 {metrics['avg_win']:.2f} < 平均亏损 {abs(metrics['avg_loss']):.2f}"
                f"（盈亏比 {ratio:.2f}），胜率 {metrics['win_rate']:.1f}% 需要高于 "
                f"{100 / (1 + ratio):.1f}% 才能打平。")

    worst = report["worst"][0] if report["worst"] else None
    if worst and metrics["realized_pnl"] and worst["net_pnl"] < 0:
        share = abs(worst["net_pnl"]) / max(sum(abs(t["net_pnl"]) for t in trips), 1e-9)
        notes.append(
            f"最大单笔亏损 {worst['symbol']} {worst['net_pnl']:.2f}"
            f"（{worst['entry_ts'][:10]} → {worst['exit_ts'][:10]}，"
            f"持有 {worst['hold_days']:.1f} 天），占全部盈亏绝对值的 {share * 100:.1f}%。")

    no_stop = [t for t in trips if t.get("r_multiple") is None]
    if len(no_stop) > len(trips) * 0.5:
        notes.append(f"{len(no_stop)}/{len(trips)} 笔交易没有记录止损价，无法计算 R 倍数——"
                     f"下单时补上 --stop 会让复盘更有意义。")

    losers_held = [t["hold_days"] for t in trips if t["net_pnl"] < 0]
    winners_held = [t["hold_days"] for t in trips if t["net_pnl"] > 0]
    if losers_held and winners_held:
        avg_l = sum(losers_held) / len(losers_held)
        avg_w = sum(winners_held) / len(winners_held)
        if avg_l > avg_w * 1.5:
            notes.append(f"亏损单平均持有 {avg_l:.1f} 天 vs 盈利单 {avg_w:.1f} 天——"
                         f"典型的「截断利润、让亏损奔跑」形态。")

    worst_bucket = report["by_symbol"][0] if report["by_symbol"] else None
    if worst_bucket and worst_bucket["net_pnl"] < 0:
        notes.append(f"亏损最集中的标的：{worst_bucket['key']}，"
                     f"{worst_bucket['roundtrips']} 笔合计 {worst_bucket['net_pnl']:.2f}。")

    untagged = sum(1 for t in trips if not t.get("thesis"))
    if untagged:
        notes.append(f"{untagged}/{len(trips)} 笔没有记录交易理由，复盘时无法判断是"
                     f"「计划内的亏损」还是「执行错误」。")

    if metrics["fees_paid"] and metrics["realized_pnl"]:
        pct = abs(metrics["fees_paid"] / metrics["realized_pnl"]) * 100
        if pct > 15:
            notes.append(f"手续费 {metrics['fees_paid']:.2f} 相当于已实现盈亏的 {pct:.0f}%。")

    return notes
