"""Charles Schwab importer.

Targets the *Accounts → History → Export* CSV (also named
``Individual_XXXX_Transactions_YYYYMMDD.csv``), which looks like::

    "Transactions  for account ...231 as of 07/25/2026"

    "Date","Action","Symbol","Description","Quantity","Price","Fees & Comm","Amount"
    "07/24/2026","Buy","AAPL","APPLE INC","100","$213.45","$0.00","-$21,345.00"

Dates may carry an ``as of`` suffix (``07/22/2026 as of 07/21/2026``); the
``as of`` date is the one the trade actually happened on, so that is what
gets journaled.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..schema import to_float
from .base import (ImportReport, as_dicts, find_header, norm, parse_option_symbol,
                   pick, read_rows, to_iso_date)

BROKER = "SCHWAB"

SIDE_ACTIONS = {
    "buy": "BUY",
    "buytoopen": "BUY",
    "reinvestshares": "BUY",
    "sell": "SELL",
    "selltoclose": "SELL",
    "sellshort": "SHORT",
    "selltoopen": "SHORT",
    "buytocover": "COVER",
    "buytoclose": "COVER",
}

CASH_ACTIONS = {
    "qualifieddividend": "DIVIDEND",
    "cashdividend": "DIVIDEND",
    "specialdividend": "DIVIDEND",
    "nonqualifieddiv": "DIVIDEND",
    "pryrcashdiv": "DIVIDEND",
    "reinvestdividend": "DIVIDEND",
    "qualdivreinvest": "DIVIDEND",
    "longtermcapgain": "DIVIDEND",
    "shorttermcapgain": "DIVIDEND",
    "bankinterest": "INTEREST",
    "creditinterest": "INTEREST",
    "margininterest": "INTEREST",
    "foreigntaxpaid": "TAX",
    "nrataxadj": "TAX",
    "adrmgmtfee": "FEE",
    "servicefee": "FEE",
    "foreigntransactionfee": "FEE",
    "moneylinktransfer": None,
    "moneylinkdeposit": "DEPOSIT",
    "wirereceived": "DEPOSIT",
    "wiresent": "WITHDRAWAL",
    "fundsreceived": "DEPOSIT",
    "journal": None,
    "cashinlieu": "ADJUSTMENT",
    "misccashentry": "ADJUSTMENT",
}

# Actions that change a position but whose direction cannot be inferred safely.
NEEDS_REVIEW = {"assigned", "exercised", "stocksplit", "namechange",
                "journaledshares", "internaltransfer", "securitytransfer"}

HEADER_COLS = ["Date", "Action", "Symbol", "Quantity", "Amount"]
_AS_OF = re.compile(r"as\s+of\s+(\d{2}/\d{2}/\d{4})", re.IGNORECASE)


def detect(path: str | Path) -> bool:
    rows = read_rows(path)[:20]
    if not rows:
        return False
    if find_header(rows, HEADER_COLS) is not None:
        return True
    joined = " ".join(" ".join(r) for r in rows[:3]).lower()
    return "transactions" in joined and "for account" in joined


def parse(path: str | Path, *, account: str | None = None) -> ImportReport:
    rows = read_rows(path)
    header_index = find_header(rows, HEADER_COLS)
    if header_index is None:
        header_index = find_header(rows, ["Date", "Action", "Symbol"])
    if header_index is None:
        return ImportReport.make(BROKER, path, [], [{"reason": "no Schwab header row found"}])

    account_name = account or _account_from_preamble(rows[:header_index])
    records: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for row in as_dicts(rows, header_index):
        action = pick(row, "Action")
        date = _trade_date(pick(row, "Date"))
        if not date or not action:
            continue  # totals / footer rows
        key = norm(action)
        base = {
            "ts": f"{date}T00:00:00Z",
            "broker": BROKER,
            "account": account_name,
            "currency": "USD",
            "source": {"kind": "import", "importer": "schwab", "broker": BROKER,
                       "action": action},
        }

        if key in SIDE_ACTIONS:
            record = _trade(row, base, SIDE_ACTIONS[key])
        elif key == "expired":
            record = _expired(row, base)
        elif key in CASH_ACTIONS:
            record = _cash(row, base, CASH_ACTIONS[key])
        elif key in NEEDS_REVIEW:
            skipped.append({"row": row, "reason": f"'{action}' needs a manual decision"})
            continue
        else:
            record = _cash(row, base, "OTHER") if to_float(pick(row, "Amount"), 0.0) else None
            if record is None:
                skipped.append({"row": row, "reason": f"unrecognized action '{action}'"})
                continue
        if record:
            records.append(record)
        else:
            skipped.append({"row": row, "reason": "incomplete row"})

    return ImportReport.make(BROKER, path, records, skipped)


def _account_from_preamble(rows: list[list[str]]) -> str | None:
    text = " ".join(" ".join(r) for r in rows)
    match = re.search(r"for account\s+(.+?)\s+as of", text, re.IGNORECASE)
    if not match:
        return None
    return re.sub(r"\s+", "-", match.group(1).strip()).strip("-").lower() or None


def _trade_date(value: str) -> str | None:
    """``07/22/2026 as of 07/21/2026`` -> the as-of (actual trade) date."""
    match = _AS_OF.search(value or "")
    return to_iso_date(match.group(1) if match else str(value or "").strip())


def _trade(row: dict[str, str], base: dict[str, Any], side: str) -> dict[str, Any] | None:
    symbol = pick(row, "Symbol")
    quantity = to_float(pick(row, "Quantity"), None)
    price = to_float(pick(row, "Price"), None)
    if not symbol or not quantity:
        return None
    if price is None:
        amount = to_float(pick(row, "Amount"), None)
        if amount is None:
            return None
        price = abs(amount) / abs(quantity)  # e.g. Reinvest Shares has no price column

    fees = to_float(pick(row, "Fees & Comm", "Fees&Comm", "Fees"), 0.0) or 0.0
    record: dict[str, Any] = {
        **base,
        "kind": "trade",
        "symbol": symbol,
        "side": side,
        "quantity": abs(quantity),
        "price": abs(price),
        "fees": {"commission": abs(fees)} if fees else {},
        "notes": pick(row, "Description") or None,
    }
    option = parse_option_symbol(symbol)
    if option:
        record["asset_class"] = "OPT"
        record["symbol"] = option["symbol"]
        record["option"] = {k: v for k, v in option.items() if k != "symbol"}
    else:
        record["asset_class"] = "STK"
    return {k: v for k, v in record.items() if v is not None}


def _expired(row: dict[str, str], base: dict[str, Any]) -> dict[str, Any] | None:
    """Option expiry: close the position at zero. Direction is inferred."""
    quantity = to_float(pick(row, "Quantity"), None)
    symbol = pick(row, "Symbol")
    if not symbol or not quantity:
        return None
    side = "SELL" if quantity > 0 else "COVER"
    record = _trade({**row, "Price": "0", "Quantity": str(abs(quantity))}, base, side)
    if record:
        record["source"] = {**record["source"], "inferred_side": True}
        record["notes"] = (record.get("notes") or "") + " [expired worthless]"
    return record


def _cash(row: dict[str, str], base: dict[str, Any],
          cash_type: str | None) -> dict[str, Any] | None:
    amount = to_float(pick(row, "Amount"), None)
    if amount is None or amount == 0:
        return None
    resolved = cash_type or ("DEPOSIT" if amount > 0 else "WITHDRAWAL")
    fees = to_float(pick(row, "Fees & Comm", "Fees&Comm", "Fees"), 0.0) or 0.0
    return {
        **base,
        "kind": "cash",
        "cash_type": resolved,
        "amount": amount,
        "symbol": pick(row, "Symbol"),
        "fees": {"commission": abs(fees)} if fees else {},
        "notes": pick(row, "Description") or None,
    }
