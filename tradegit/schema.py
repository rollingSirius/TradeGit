"""Trade record schema: normalization, validation, identity.

A journal record is a flat JSON object. Two kinds exist:

* ``kind="trade"`` — an execution (fill). Drives position and P&L math.
* ``kind="cash"``  — a non-trade cash event (dividend, interest, fee, tax,
  deposit, withdrawal). Ignoring these makes realized P&L wrong, so broker
  importers keep them.

Records are append-only. A correction is a new record carrying
``supersedes: <id>``; the loader applies the latest one and drops the
superseded original. ``kind="void"`` deletes a record logically.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from . import SCHEMA_VERSION

KINDS = ("trade", "cash", "void")
SIDES = ("BUY", "SELL", "SHORT", "COVER")
ASSET_CLASSES = ("STK", "ETF", "OPT", "FUT", "FX", "CRYPTO", "BOND", "FUND", "OTHER")
CASH_TYPES = (
    "DIVIDEND", "INTEREST", "FEE", "TAX", "DEPOSIT", "WITHDRAWAL",
    "REBATE", "ADJUSTMENT", "OTHER",
)
OPENING_SIDES = ("BUY", "SHORT")

# BUY/COVER increase the position, SELL/SHORT decrease it.
SIDE_SIGN = {"BUY": 1, "COVER": 1, "SELL": -1, "SHORT": -1}

FEE_KEYS = ("commission", "regulatory", "exchange", "clearing", "tax", "other")

_ID_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class ValidationError(ValueError):
    """Raised when a record cannot be normalized into the schema."""


# ---------------------------------------------------------------------------
# time helpers
# ---------------------------------------------------------------------------

_TS_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y%m%d;%H%M%S",      # IBKR Flex
    "%Y%m%d %H%M%S",
    "%Y%m%d",
    "%Y-%m-%d",
    "%m/%d/%Y %H:%M:%S",  # Schwab / US exports
    "%m/%d/%Y",
)


def parse_ts(value: Any) -> datetime:
    """Parse a timestamp from the many shapes brokers emit. Returns UTC."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
    else:
        raw = str(value or "").strip()
        if not raw:
            raise ValidationError("timestamp is required")
        text = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            dt = None  # type: ignore[assignment]
            for fmt in _TS_FORMATS:
                try:
                    dt = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    continue
            if dt is None:
                raise ValidationError(f"unrecognized timestamp: {raw!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return iso(datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# numeric helpers
# ---------------------------------------------------------------------------

def to_float(value: Any, default: float | None = 0.0) -> float | None:
    """Coerce broker-formatted numbers: ``$1,234.56``, ``(12.30)``, ``--``."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text in {"--", "-", "N/A", "n/a", "null"}:
        return default
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace(",", "").replace("$", "").replace("%", "").strip()
    if text.startswith("+"):
        text = text[1:]
    try:
        number = float(text)
    except ValueError:
        return default
    return -number if negative else number


def round_money(value: float, digits: int = 6) -> float:
    return round(value + 0.0, digits)


# ---------------------------------------------------------------------------
# normalization
# ---------------------------------------------------------------------------

def normalize_fees(raw: Any) -> dict[str, float]:
    """Accept a scalar, a dict, or nothing; always return a keyed breakdown."""
    fees: dict[str, float] = {}
    if raw is None:
        return fees
    if isinstance(raw, (int, float, str)):
        amount = to_float(raw, 0.0) or 0.0
        if amount:
            fees["commission"] = abs(round_money(amount, 4))
        return fees
    if isinstance(raw, dict):
        for key, value in raw.items():
            amount = to_float(value, 0.0) or 0.0
            if amount:
                fees[str(key)] = abs(round_money(amount, 4))
    return fees


def fees_total(record: dict[str, Any]) -> float:
    return round_money(sum(float(v) for v in (record.get("fees") or {}).values()), 4)


def _slug(text: str, limit: int = 24) -> str:
    return _ID_SAFE.sub("-", str(text or "").strip()).strip("-")[:limit] or "x"


def dedup_key(record: dict[str, Any]) -> str:
    """Stable identity for a record, used to skip re-imports.

    Prefers the broker's own execution id; otherwise hashes the economic
    content of the fill so that re-running the same export is a no-op.
    """
    source = record.get("source") or {}
    external = source.get("external_id")
    if external:
        return f"ext:{source.get('broker') or record.get('broker') or ''}:{external}"
    parts = [
        record.get("kind", "trade"),
        record.get("account", ""),
        record.get("broker", ""),
        record.get("symbol", ""),
        record.get("side") or record.get("cash_type") or "",
        record.get("ts", ""),
        f"{to_float(record.get('quantity'), 0.0):.8f}",
        f"{to_float(record.get('price'), 0.0):.8f}",
        f"{to_float(record.get('amount'), 0.0):.8f}",
    ]
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return f"content:{digest[:32]}"


def make_id(record: dict[str, Any]) -> str:
    ts = record.get("ts", "")
    stamp = re.sub(r"[-:]", "", ts).replace("Z", "Z")
    digest = hashlib.sha256(dedup_key(record).encode()).hexdigest()[:8]
    label = _slug(record.get("symbol") or record.get("cash_type") or "cash", 16)
    prefix = "csh" if record.get("kind") == "cash" else "trd"
    return f"{prefix}_{stamp}_{label}_{digest}"


def record_hash(record: dict[str, Any]) -> str:
    payload = {k: v for k, v in record.items() if k not in {"hash", "updated_at"}}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()[:32]


def default_multiplier(asset_class: str, given: Any = None) -> float:
    explicit = to_float(given, None)
    if explicit:
        return float(explicit)
    return 100.0 if asset_class == "OPT" else 1.0


def normalize(raw: dict[str, Any], *, default_account: str = "main",
              default_currency: str = "USD") -> dict[str, Any]:
    """Turn loose user/importer input into a canonical journal record."""
    record: dict[str, Any] = {}
    src = dict(raw)

    kind = str(src.pop("kind", "trade") or "trade").lower()
    if kind not in KINDS:
        raise ValidationError(f"kind must be one of {KINDS}, got {kind!r}")

    ts_value = src.pop("ts", None) or src.pop("timestamp", None) or src.pop("date", None)
    record["schema_version"] = int(src.pop("schema_version", SCHEMA_VERSION))
    record["kind"] = kind
    record["ts"] = iso(parse_ts(ts_value))
    record["account"] = str(src.pop("account", None) or default_account)
    record["broker"] = str(src.pop("broker", "") or "").upper() or "MANUAL"
    record["currency"] = str(src.pop("currency", None) or default_currency).upper()

    symbol = str(src.pop("symbol", "") or "").strip().upper()
    record["symbol"] = symbol

    if kind == "void":
        target = src.pop("voids", None)
        if not target:
            raise ValidationError("kind=void requires 'voids' (record id)")
        record["voids"] = str(target)
    elif kind == "cash":
        cash_type = str(src.pop("cash_type", "OTHER") or "OTHER").upper()
        if cash_type not in CASH_TYPES:
            cash_type = "OTHER"
        record["cash_type"] = cash_type
        amount = to_float(src.pop("amount", None), None)
        if amount is None:
            raise ValidationError("kind=cash requires 'amount'")
        record["amount"] = round_money(amount)
        record["fees"] = normalize_fees(src.pop("fees", None))
        record["net_amount"] = round_money(amount - fees_total(record))
    else:
        if not symbol:
            raise ValidationError("symbol is required for a trade")
        side = str(src.pop("side", "") or "").strip().upper()
        if side in {"BOT", "BUY TO OPEN", "BUY_TO_OPEN"}:
            side = "BUY"
        elif side in {"SLD", "SELL TO CLOSE", "SELL_TO_CLOSE"}:
            side = "SELL"
        elif side in {"SELL SHORT", "SELL TO OPEN", "SHORT SELL"}:
            side = "SHORT"
        elif side in {"BUY TO COVER", "BUY TO CLOSE", "COVER SHORT"}:
            side = "COVER"
        if side not in SIDES:
            raise ValidationError(f"side must be one of {SIDES}, got {side!r}")
        record["side"] = side

        asset_class = str(src.pop("asset_class", "") or "").strip().upper() or "STK"
        if asset_class not in ASSET_CLASSES:
            asset_class = "OTHER"
        record["asset_class"] = asset_class

        quantity = to_float(src.pop("quantity", None) or src.pop("qty", None), None)
        if quantity is None:
            raise ValidationError("quantity is required for a trade")
        quantity = abs(quantity)
        if quantity <= 0:
            raise ValidationError("quantity must be greater than zero")
        record["quantity"] = round_money(quantity, 8)

        price = to_float(src.pop("price", None), None)
        if price is None:
            raise ValidationError("price is required for a trade")
        record["price"] = round_money(abs(price), 8)

        record["multiplier"] = default_multiplier(asset_class, src.pop("multiplier", None))
        record["fees"] = normalize_fees(src.pop("fees", None))

        gross = record["quantity"] * record["price"] * record["multiplier"]
        record["gross_amount"] = round_money(gross)
        record["fees_total"] = fees_total(record)
        # Cash impact: buying spends cash (negative), selling brings it in.
        direction = -1 if record["side"] in ("BUY", "COVER") else 1
        record["net_amount"] = round_money(direction * gross - record["fees_total"])
        record["signed_quantity"] = round_money(
            SIDE_SIGN[record["side"]] * record["quantity"], 8)

        if asset_class == "OPT":
            record["option"] = _normalize_option(src, symbol)

    # --- journal fields (the part that makes this a journal, not a ledger) --
    for key in ("thesis", "strategy", "setup", "notes", "emotions", "market_context",
                "exit_plan", "review", "mistake"):
        value = src.pop(key, None)
        if value not in (None, ""):
            record[key] = str(value)

    conviction = to_float(src.pop("conviction", None), None)
    if conviction is not None:
        record["conviction"] = max(1, min(5, int(round(conviction))))

    tags = src.pop("tags", None)
    if tags:
        if isinstance(tags, str):
            tags = [t.strip() for t in re.split(r"[,\s]+", tags) if t.strip()]
        record["tags"] = sorted({str(t).strip().lower() for t in tags if str(t).strip()})

    risk = _normalize_risk(src.pop("risk", None), src, record)
    if risk:
        record["risk"] = risk

    links = src.pop("links", None)
    if links:
        record["links"] = [str(x) for x in (links if isinstance(links, list) else [links])]

    source = src.pop("source", None) or {}
    if not isinstance(source, dict):
        source = {"kind": str(source)}
    source.setdefault("kind", "manual")
    record["source"] = {k: v for k, v in source.items() if v not in (None, "")}

    supersedes = src.pop("supersedes", None)
    if supersedes:
        record["supersedes"] = str(supersedes)

    # Keep anything we did not recognize rather than silently dropping it.
    extras = {k: v for k, v in src.items()
              if k not in {"id", "created_at", "updated_at", "hash"} and v not in (None, "")}
    if extras:
        record["extra"] = extras

    record["id"] = str(raw.get("id") or make_id(record))
    record["dedup_key"] = dedup_key(record)
    record["created_at"] = str(raw.get("created_at") or now_iso())
    record["updated_at"] = now_iso()
    record["hash"] = record_hash(record)
    return record


def _normalize_option(src: dict[str, Any], symbol: str) -> dict[str, Any]:
    option = dict(src.pop("option", None) or {})
    for key in ("underlying", "expiry", "strike", "right"):
        if key in src:
            option.setdefault(key, src.pop(key))
    if not option.get("underlying") and symbol:
        option["underlying"] = _parse_osi(symbol).get("underlying", symbol)
    parsed = _parse_osi(symbol)
    for key, value in parsed.items():
        option.setdefault(key, value)
    if option.get("right"):
        right = str(option["right"]).upper()
        option["right"] = "C" if right.startswith("C") else "P"
    if option.get("strike") is not None:
        option["strike"] = to_float(option["strike"], None)
    return {k: v for k, v in option.items() if v not in (None, "")}


_OSI = re.compile(r"^(?P<u>[A-Z.]{1,6})\s*(?P<y>\d{2})(?P<m>\d{2})(?P<d>\d{2})"
                  r"(?P<r>[CP])(?P<s>\d{8})$")


def _parse_osi(symbol: str) -> dict[str, Any]:
    """Parse an OSI option symbol, e.g. ``AAPL  260116C00200000``."""
    match = _OSI.match(re.sub(r"\s+", "", symbol or ""))
    if not match:
        return {}
    return {
        "underlying": match.group("u"),
        "expiry": f"20{match.group('y')}-{match.group('m')}-{match.group('d')}",
        "right": match.group("r"),
        "strike": int(match.group("s")) / 1000.0,
    }


def _normalize_risk(raw: Any, src: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    risk = dict(raw or {})
    for key in ("stop", "target", "risk_amount", "planned_r", "position_pct"):
        if key in src:
            risk.setdefault(key, src.pop(key))
    risk = {k: to_float(v, None) for k, v in risk.items()}
    risk = {k: v for k, v in risk.items() if v is not None}
    entry = record.get("price")
    if risk.get("stop") and entry and record.get("side") in OPENING_SIDES:
        per_share = abs(entry - risk["stop"])
        if per_share > 0:
            risk.setdefault("risk_amount", round_money(
                per_share * record["quantity"] * record.get("multiplier", 1)))
            if risk.get("target"):
                risk.setdefault("planned_r", round_money(
                    abs(risk["target"] - entry) / per_share, 3))
    return risk


# ---------------------------------------------------------------------------
# resolution of append-only edits
# ---------------------------------------------------------------------------

def resolve(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse an append-only stream into the current view of the journal.

    Later records win over the ids they supersede; ``kind="void"`` removes.
    """
    alive: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    seen: set[str] = set()
    voided: set[str] = set()

    for record in records:
        if record.get("kind") == "void":
            voided.add(str(record.get("voids")))
            continue
        rid = str(record.get("id"))
        target = record.get("supersedes")
        # An amendment that leaves the economics untouched keeps the original
        # id, so a record never supersedes itself out of existence.
        if target and str(target) != rid:
            alive.pop(str(target), None)
        alive[rid] = record
        if rid not in seen:
            seen.add(rid)
            order.append(rid)

    live = [alive[rid] for rid in order if rid in alive and rid not in voided]
    live.sort(key=lambda r: (r.get("ts", ""), r.get("id", "")))
    return live
