"""Interactive Brokers importer.

Handles the two exports IBKR users actually have:

* **Activity Statement CSV** — the multi-section file from
  *Performance & Reports → Statements → Activity*, where every row starts
  with its section name (``Trades``, ``Dividends``, ``Interest``, ``Fees``…).
* **Flex Query CSV** — the flat, one-header file from
  *Performance & Reports → Flex Queries* (Trades / Trade Confirmations).

Both are parsed into the same journal records. IBKR's own ``ClosedLot`` rows
are skipped: TradeGit does its own FIFO matching, and keeping them would
double-count every close.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..schema import to_float
from .base import (ImportReport, as_dicts, combine_datetime, find_header, norm,
                   parse_option_symbol, pick, read_rows, to_iso_date)

BROKER = "IBKR"

ASSET_CLASSES = {
    "stocks": "STK", "stk": "STK", "etfs": "ETF",
    "equityandindexoptions": "OPT", "opt": "OPT", "options": "OPT",
    "futures": "FUT", "fut": "FUT", "futuresoptions": "OPT", "fop": "OPT",
    "forex": "FX", "cash": "FX", "cfd": "OTHER",
    "bonds": "BOND", "bond": "BOND", "cryptocurrencies": "CRYPTO", "crypto": "CRYPTO",
    "mutualfunds": "FUND", "fund": "FUND",
}

CASH_SECTIONS = {
    "dividends": "DIVIDEND",
    "withholdingtax": "TAX",
    "interest": "INTEREST",
    "fees": "FEE",
    "otherfees": "FEE",
    "depositswithdrawals": None,          # sign decides deposit vs withdrawal
    "brokerinterestreceived": "INTEREST",
    "brokerinterestpaid": "INTEREST",
}


def detect(path: str | Path) -> bool:
    rows = read_rows(path)[:40]
    if not rows:
        return False
    if any(len(r) > 1 and r[1].strip() == "Header" for r in rows[:20]):
        return True
    header = {norm(c) for c in rows[0]}
    return {"symbol"} <= header and bool(
        {"tradeprice", "ibcommission", "buysell", "clientaccountid"} & header)


def parse(path: str | Path, *, account: str | None = None) -> ImportReport:
    rows = read_rows(path)
    if not rows:
        return ImportReport.make(BROKER, path, [], [])
    if any(len(r) > 1 and r[1].strip() == "Header" for r in rows[:20]):
        return _parse_activity_statement(rows, path, account)
    return _parse_flex(rows, path, account)


# ---------------------------------------------------------------------------
# Activity Statement (multi-section)
# ---------------------------------------------------------------------------

def _parse_activity_statement(rows: list[list[str]], path: str | Path,
                              account: str | None) -> ImportReport:
    sections: dict[str, dict[str, Any]] = {}
    for row in rows:
        if len(row) < 3:
            continue
        name, row_type = row[0].strip(), row[1].strip()
        bucket = sections.setdefault(name, {"header": None, "data": []})
        if row_type == "Header":
            bucket["header"] = [c.strip() for c in row[2:]]
        elif row_type == "Data" and bucket["header"] is not None:
            values = row[2:]
            bucket["data"].append({
                bucket["header"][i]: (values[i] if i < len(values) else "")
                for i in range(len(bucket["header"]))
            })

    records: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    ignored: list[dict[str, Any]] = []
    for row in sections.get("Trades", {}).get("data", []):
        discriminator = norm(pick(row, "DataDiscriminator"))
        if discriminator and discriminator not in {"order", "trade", "execution"}:
            # IBKR's own lot matching / subtotals. TradeGit does its own FIFO
            # pairing, so keeping these would double-count every close.
            ignored.append({"row": row, "reason": f"ignored {discriminator} row"})
            continue
        record = _trade_from_statement(row, account)
        if record:
            records.append(record)
        else:
            skipped.append({"row": row, "reason": "missing symbol/quantity/price"})

    for name, bucket in sections.items():
        cash_type = CASH_SECTIONS.get(norm(name), "__missing__")
        if cash_type == "__missing__":
            continue
        for row in bucket["data"]:
            record = _cash_from_statement(row, cash_type, account)
            if record:
                records.append(record)

    report = ImportReport.make(BROKER, path, records, skipped, sorted(sections))
    report["ignored"] = ignored
    return report


def _trade_from_statement(row: dict[str, str], account: str | None) -> dict[str, Any] | None:
    discriminator = pick(row, "DataDiscriminator")
    if discriminator and norm(discriminator) not in {"order", "trade", "execution"}:
        return None  # ClosedLot / SubTotal / Total rows
    symbol = pick(row, "Symbol")
    quantity = to_float(pick(row, "Quantity"), None)
    price = to_float(pick(row, "T. Price", "TPrice", "Price"), None)
    if not symbol or quantity in (None, 0) or price is None:
        return None

    when = pick(row, "Date/Time", "DateTime", "Date")
    date_part, _, time_part = when.partition(",")
    ts = combine_datetime(date_part.strip(), time_part.strip())

    asset = ASSET_CLASSES.get(norm(pick(row, "Asset Category", "AssetClass")), "STK")
    code = {c.strip().upper() for c in pick(row, "Code").replace(";", ",").split(",")}
    side = _side(quantity, code)

    commission = abs(to_float(pick(row, "Comm/Fee", "Comm in USD", "Commission"), 0.0) or 0.0)
    record: dict[str, Any] = {
        "kind": "trade",
        "ts": ts,
        "broker": BROKER,
        "account": account or pick(row, "Account", "ClientAccountID") or None,
        "symbol": symbol,
        "asset_class": asset,
        "currency": pick(row, "Currency", "CurrencyPrimary", default="USD"),
        "side": side,
        "quantity": abs(quantity),
        "price": price,
        "fees": {"commission": commission} if commission else {},
        "source": {"kind": "import", "importer": "ibkr-activity", "broker": BROKER,
                   "code": pick(row, "Code") or None},
    }
    if asset == "OPT":
        _apply_option(record, symbol)
    return {k: v for k, v in record.items() if v is not None}


def _cash_from_statement(row: dict[str, str], cash_type: str | None,
                         account: str | None) -> dict[str, Any] | None:
    amount = to_float(pick(row, "Amount"), None)
    date = to_iso_date(pick(row, "Date", "Settle Date", "SettleDate", "Report Date"))
    if amount is None or amount == 0 or not date:
        return None
    description = pick(row, "Description")
    if norm(description) in {"total", "totalinusd"}:
        return None
    resolved = cash_type or ("DEPOSIT" if amount > 0 else "WITHDRAWAL")
    return {
        "kind": "cash",
        "ts": f"{date}T00:00:00Z",
        "broker": BROKER,
        "account": account or pick(row, "Account", "ClientAccountID") or None,
        "cash_type": resolved,
        "amount": amount,
        "currency": pick(row, "Currency", default="USD"),
        "symbol": _symbol_from_description(description),
        "notes": description,
        "source": {"kind": "import", "importer": "ibkr-activity", "broker": BROKER},
    }


_DIVIDEND_SYMBOL = re.compile(r"^([A-Z][A-Z.]{0,5})\s*\(")


def _symbol_from_description(description: str) -> str:
    """IBKR writes dividends as ``AAPL(US0378331005) Cash Dividend …``.

    Only that exact shape yields a symbol — a looser rule turns
    ``Market data subscription`` into a holding called MARKET.
    """
    match = _DIVIDEND_SYMBOL.match(str(description or "").strip())
    return match.group(1) if match else ""


# ---------------------------------------------------------------------------
# Flex Query (flat)
# ---------------------------------------------------------------------------

def _parse_flex(rows: list[list[str]], path: str | Path,
                account: str | None) -> ImportReport:
    header_index = find_header(rows, ["Symbol"]) or 0
    records, skipped = [], []
    for row in as_dicts(rows, header_index):
        symbol = pick(row, "Symbol")
        quantity = to_float(pick(row, "Quantity"), None)
        price = to_float(pick(row, "TradePrice", "Price"), None)
        if not symbol or quantity in (None, 0) or price is None:
            skipped.append({"row": row, "reason": "missing symbol/quantity/price"})
            continue

        when = pick(row, "DateTime", "Date/Time")
        if when:
            date_part, _, time_part = when.replace(",", ";").partition(";")
            ts = combine_datetime(date_part.strip(), time_part.strip())
        else:
            ts = combine_datetime(pick(row, "TradeDate", "Date"), pick(row, "TradeTime"))

        asset = ASSET_CLASSES.get(norm(pick(row, "AssetClass", "Asset Category")), "STK")
        buy_sell = pick(row, "Buy/Sell", "BuySell").upper()
        open_close = pick(row, "OpenCloseIndicator", "Open/CloseIndicator").upper()
        signed = abs(quantity) * (-1 if buy_sell.startswith("S") else 1)
        side = _side(signed, {open_close} if open_close else set())

        commission = abs(to_float(pick(row, "IBCommission", "Commission"), 0.0) or 0.0)
        taxes = abs(to_float(pick(row, "Taxes"), 0.0) or 0.0)
        fees = {}
        if commission:
            fees["commission"] = commission
        if taxes:
            fees["tax"] = taxes

        record: dict[str, Any] = {
            "kind": "trade",
            "ts": ts,
            "broker": BROKER,
            "account": account or pick(row, "ClientAccountID", "AccountId") or None,
            "symbol": symbol,
            "asset_class": asset,
            "currency": pick(row, "CurrencyPrimary", "Currency", default="USD"),
            "side": side,
            "quantity": abs(quantity),
            "price": price,
            "multiplier": to_float(pick(row, "Multiplier"), None),
            "fees": fees,
            "source": {
                "kind": "import", "importer": "ibkr-flex", "broker": BROKER,
                "external_id": pick(row, "TransactionID", "TradeID", "IBExecID") or None,
                "order_id": pick(row, "IBOrderID", "OrderID") or None,
            },
        }
        if asset == "OPT":
            _apply_option(record, symbol, row)
        records.append({k: v for k, v in record.items() if v is not None})
    return ImportReport.make(BROKER, path, records, skipped)


# ---------------------------------------------------------------------------
# shared
# ---------------------------------------------------------------------------

def _side(signed_qty: float, code: set[str]) -> str:
    """Map quantity sign plus IBKR's open/close code to a journal side."""
    if signed_qty > 0:
        return "COVER" if "C" in code and "O" not in code else "BUY"
    return "SHORT" if "O" in code and "C" not in code else "SELL"


def _apply_option(record: dict[str, Any], symbol: str, row: dict[str, str] | None = None) -> None:
    parsed = parse_option_symbol(symbol)
    option: dict[str, Any] = {}
    if parsed:
        record["symbol"] = parsed["symbol"]
        option = {k: v for k, v in parsed.items() if k != "symbol"}
    if row:
        expiry = to_iso_date(pick(row, "Expiry", "Expiration"))
        strike = to_float(pick(row, "Strike"), None)
        right = pick(row, "Put/Call", "PutCall", "Right")
        underlying = pick(row, "UnderlyingSymbol", "Underlying")
        option.update({k: v for k, v in {
            "expiry": expiry, "strike": strike,
            "right": right[:1].upper() if right else None,
            "underlying": underlying or None,
        }.items() if v})
    if option:
        record["option"] = option
