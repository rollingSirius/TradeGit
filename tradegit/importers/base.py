"""Shared helpers for broker importers.

Broker CSV exports are messy: preamble lines, multi-section files, BOMs,
``$1,234.56`` numbers, ``(12.30)`` negatives, and option symbols in three
different notations. Everything that has to cope with that lives here.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}


def read_rows(path: str | Path) -> list[list[str]]:
    """Read a CSV into raw rows, tolerating BOM and stray blank lines."""
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    return [row for row in csv.reader(io.StringIO(text)) if any(c.strip() for c in row)]


def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name).lower())


def find_header(rows: Sequence[Sequence[str]], required: Iterable[str],
                start: int = 0) -> int | None:
    """Index of the first row that looks like a header for ``required`` cols."""
    wanted = {norm(r) for r in required}
    for index in range(start, len(rows)):
        cells = {norm(c) for c in rows[index]}
        if wanted <= cells:
            return index
    return None


def as_dicts(rows: Sequence[Sequence[str]], header_index: int) -> list[dict[str, str]]:
    header = [str(c).strip() for c in rows[header_index]]
    out = []
    for row in rows[header_index + 1:]:
        if not any(str(c).strip() for c in row):
            continue
        out.append({header[i]: (row[i] if i < len(row) else "")
                    for i in range(len(header))})
    return out


def pick(row: dict[str, str], *names: str, default: str = "") -> str:
    """Fetch a column by any of several spellings, ignoring case/punctuation."""
    index = {norm(k): v for k, v in row.items()}
    for name in names:
        value = index.get(norm(name))
        if value not in (None, ""):
            return str(value).strip()
    return default


# ---------------------------------------------------------------------------
# option symbols
# ---------------------------------------------------------------------------

def to_osi(underlying: str, expiry: str, right: str, strike: float) -> str:
    """Build the 21-char OSI symbol, e.g. ``AAPL  260717C00200000``."""
    root = (underlying or "").upper().strip()[:6].ljust(6)
    y, m, d = expiry.split("-")
    strike_int = int(round(float(strike) * 1000))
    return f"{root}{y[2:]}{m}{d}{right.upper()[0]}{strike_int:08d}"


_DATE_PATTERNS = (
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})$"), lambda m: (m[1], m[2], m[3])),
    (re.compile(r"^(\d{2})/(\d{2})/(\d{4})$"), lambda m: (m[3], m[1], m[2])),
    (re.compile(r"^(\d{4})(\d{2})(\d{2})$"), lambda m: (m[1], m[2], m[3])),
    (re.compile(r"^(\d{1,2})([A-Z]{3})(\d{2})$"),
     lambda m: (f"20{m[3]}", f"{MONTHS[m[2]]:02d}", f"{int(m[1]):02d}")),
    (re.compile(r"^([A-Z]{3})\s+(\d{1,2})\s+(\d{4})$"),
     lambda m: (m[3], f"{MONTHS[m[1]]:02d}", f"{int(m[2]):02d}")),
)


def to_iso_date(value: str) -> str | None:
    """Normalize the date spellings brokers use into ``YYYY-MM-DD``."""
    text = str(value or "").strip().upper().replace("'", "")
    for pattern, extract in _DATE_PATTERNS:
        match = pattern.match(text)
        if match:
            year, month, day = extract(match)
            return f"{year}-{month}-{day}"
    return None


# ``AAPL 07/17/2026 200.00 C``  (Schwab)
_SCHWAB_OPT = re.compile(
    r"^(?P<u>[A-Z.]{1,6})\s+(?P<exp>\d{2}/\d{2}/\d{4})\s+(?P<k>[\d.]+)\s+(?P<r>[CP])",
    re.IGNORECASE)
# ``AAPL 17JUL26 200 C``  (IBKR activity statement)
_IBKR_OPT = re.compile(
    r"^(?P<u>[A-Z.]{1,6})\s+(?P<exp>\d{1,2}[A-Z]{3}\d{2})\s+(?P<k>[\d.]+)\s+(?P<r>[CP])",
    re.IGNORECASE)


def parse_option_symbol(text: str) -> dict[str, Any] | None:
    """Recognize a broker option symbol and return its OSI components."""
    raw = re.sub(r"\s+", " ", str(text or "").strip().upper())
    for pattern in (_SCHWAB_OPT, _IBKR_OPT):
        match = pattern.match(raw)
        if not match:
            continue
        expiry = to_iso_date(match.group("exp"))
        if not expiry:
            continue
        right = match.group("r").upper()
        strike = float(match.group("k"))
        underlying = match.group("u").upper()
        return {
            "symbol": to_osi(underlying, expiry, right, strike),
            "underlying": underlying,
            "expiry": expiry,
            "right": right,
            "strike": strike,
        }
    return None


def combine_datetime(date: str, time: str = "") -> str:
    iso_date = to_iso_date(date) or date
    time = (time or "").strip()
    if not time:
        return f"{iso_date}T00:00:00Z"
    time = time.replace(";", "").strip()
    if len(time) == 6 and time.isdigit():
        time = f"{time[:2]}:{time[2:4]}:{time[4:]}"
    if len(time) == 5:
        time += ":00"
    return f"{iso_date}T{time}Z"


class ImportReport(dict):
    """Result of parsing a broker file: records plus what was skipped."""

    @classmethod
    def make(cls, broker: str, path: str, records: list[dict[str, Any]],
             skipped: list[dict[str, Any]], sections: list[str] | None = None):
        return cls(broker=broker, file=str(path), parsed=len(records),
                   records=records, skipped=skipped, sections=sections or [])
