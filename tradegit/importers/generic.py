"""Fallback importer for CSV/JSON files that are not a known broker export.

Column names are matched loosely (case and punctuation insensitive), so
``Trade Date`` / ``trade_date`` / ``DATE`` all resolve to the timestamp.
Pass ``mapping={"ts": "Executed At", ...}`` to override for an odd file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..schema import to_float
from .base import (ImportReport, as_dicts, combine_datetime, find_header,
                   parse_option_symbol, pick, read_rows)

BROKER = "GENERIC"

ALIASES: dict[str, tuple[str, ...]] = {
    "ts": ("ts", "timestamp", "datetime", "date/time", "trade date", "date",
           "execution time", "filled at", "time"),
    "symbol": ("symbol", "ticker", "instrument", "contract", "security"),
    "side": ("side", "action", "buy/sell", "direction", "type", "transaction type"),
    "quantity": ("quantity", "qty", "shares", "size", "units", "amount of shares"),
    "price": ("price", "trade price", "fill price", "avg price", "average price",
              "executed price", "t. price"),
    "fees": ("fees", "commission", "fees & comm", "comm/fee", "total fees"),
    "currency": ("currency", "ccy"),
    "account": ("account", "account id", "account number"),
    "broker": ("broker", "venue"),
    "asset_class": ("asset class", "asset category", "type", "security type"),
    "strategy": ("strategy", "system", "playbook"),
    "thesis": ("thesis", "reason", "rationale", "why", "note", "notes", "comment"),
    "tags": ("tags", "labels"),
    "stop": ("stop", "stop loss", "stop price"),
    "target": ("target", "take profit", "target price"),
    "conviction": ("conviction", "confidence"),
}


def detect(path: str | Path) -> bool:
    suffix = Path(path).suffix.lower()
    if suffix in {".json", ".jsonl", ".ndjson"}:
        return True
    rows = read_rows(path)
    return bool(rows) and find_header(rows, ["Symbol"]) is not None


def parse(path: str | Path, *, account: str | None = None,
          mapping: dict[str, str] | None = None) -> ImportReport:
    suffix = Path(path).suffix.lower()
    if suffix in {".json", ".jsonl", ".ndjson"}:
        return _parse_json(path, account)
    return _parse_csv(path, account, mapping or {})


def _parse_json(path: str | Path, account: str | None) -> ImportReport:
    text = Path(path).read_text(encoding="utf-8-sig")
    stripped = text.lstrip()
    if stripped.startswith("["):
        items = json.loads(text)
    elif stripped.startswith("{") and "\n" not in stripped.strip():
        items = [json.loads(text)]
    else:
        items = [json.loads(line) for line in text.splitlines() if line.strip()]
    records = []
    for item in items:
        if account:
            item.setdefault("account", account)
        item.setdefault("source", {"kind": "import", "importer": "json",
                                   "file": Path(path).name})
        records.append(item)
    return ImportReport.make("JSON", path, records, [])


def _parse_csv(path: str | Path, account: str | None,
               mapping: dict[str, str]) -> ImportReport:
    rows = read_rows(path)
    header_index = find_header(rows, ["Symbol"])
    if header_index is None:
        header_index = 0
    records, skipped = [], []
    for row in as_dicts(rows, header_index):
        def get(field: str) -> str:
            if field in mapping:
                return pick(row, mapping[field])
            return pick(row, *ALIASES.get(field, (field,)))

        symbol = get("symbol")
        quantity = to_float(get("quantity"), None)
        price = to_float(get("price"), None)
        if not symbol or not quantity or price is None:
            skipped.append({"row": row, "reason": "missing symbol/quantity/price"})
            continue

        side = (get("side") or "").upper()
        if not side:
            side = "BUY" if quantity > 0 else "SELL"
        fees = to_float(get("fees"), 0.0) or 0.0
        record: dict[str, Any] = {
            "kind": "trade",
            "ts": combine_datetime(get("ts")),
            "symbol": symbol,
            "side": side,
            "quantity": abs(quantity),
            "price": abs(price),
            "fees": {"commission": abs(fees)} if fees else {},
            "account": account or get("account") or None,
            "broker": get("broker") or BROKER,
            "currency": get("currency") or "USD",
            "asset_class": get("asset_class") or None,
            "strategy": get("strategy") or None,
            "thesis": get("thesis") or None,
            "tags": get("tags") or None,
            "conviction": to_float(get("conviction"), None),
            "risk": {k: to_float(get(k), None) for k in ("stop", "target")},
            "source": {"kind": "import", "importer": "generic-csv",
                       "file": Path(path).name},
        }
        option = parse_option_symbol(symbol)
        if option:
            record["asset_class"] = "OPT"
            record["symbol"] = option["symbol"]
            record["option"] = {k: v for k, v in option.items() if k != "symbol"}
        record["risk"] = {k: v for k, v in record["risk"].items() if v is not None}
        records.append({k: v for k, v in record.items() if v not in (None, {}, "")})
    return ImportReport.make(BROKER, path, records, skipped)
