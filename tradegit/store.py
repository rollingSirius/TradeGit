"""Journal storage: month-partitioned JSONL plus a derived SQLite index.

Why JSONL and not one big JSON file: appends touch a single line, so git
diffs stay minimal and two machines appending on the same day merge without
conflict. Why a SQLite index: it is stdlib, and it lets the agent run ad-hoc
SQL over the journal without re-parsing every file.

The JSONL files are the source of truth. The index is disposable and is
rebuilt automatically whenever the journal fingerprint changes.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import SCHEMA_VERSION
from .config import Config
from .schema import fees_total, resolve

INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    ts TEXT, day TEXT, month TEXT, year TEXT,
    kind TEXT, account TEXT, broker TEXT, symbol TEXT, underlying TEXT,
    asset_class TEXT, currency TEXT, side TEXT, cash_type TEXT,
    quantity REAL, signed_quantity REAL, price REAL, multiplier REAL,
    gross_amount REAL, fees_total REAL, net_amount REAL, amount REAL,
    strategy TEXT, setup TEXT, thesis TEXT, conviction INTEGER,
    tags TEXT, stop REAL, target REAL, risk_amount REAL,
    source_kind TEXT, external_id TEXT, dedup_key TEXT,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_account ON trades(account);
CREATE INDEX IF NOT EXISTS idx_trades_dedup ON trades(dedup_key);
"""


# ---------------------------------------------------------------------------
# file layout
# ---------------------------------------------------------------------------

def partition_for(ts: str) -> tuple[str, str]:
    """``2026-07-25T…`` -> ``("2026", "2026-07")``."""
    return ts[:4], ts[:7]


def partition_path(cfg: Config, ts: str) -> Path:
    year, month = partition_for(ts)
    return cfg.journal_dir / year / f"{month}.jsonl"


def journal_files(cfg: Config) -> list[Path]:
    if not cfg.journal_dir.exists():
        return []
    return sorted(cfg.journal_dir.glob("*/*.jsonl"))


def fingerprint(cfg: Config) -> str:
    parts = [f"v{SCHEMA_VERSION}"]
    for path in journal_files(cfg):
        stat = path.stat()
        parts.append(f"{path.name}:{stat.st_size}:{int(stat.st_mtime_ns)}")
    return "|".join(parts)


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

def iter_raw(cfg: Config) -> Iterator[dict[str, Any]]:
    """Yield every record as written, including superseded/void entries."""
    for path in journal_files(cfg):
        with path.open(encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{lineno} is not valid JSON: {exc}") from exc


def load(cfg: Config) -> list[dict[str, Any]]:
    """Current view of the journal, sorted by timestamp."""
    return resolve(iter_raw(cfg))


def existing_dedup_keys(cfg: Config) -> set[str]:
    return {r.get("dedup_key") for r in iter_raw(cfg) if r.get("dedup_key")}


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------

def append(cfg: Config, records: Iterable[dict[str, Any]], *,
           skip_duplicates: bool = True) -> dict[str, Any]:
    """Append normalized records, grouped by month partition.

    Returns ``{"written": [...], "skipped": [...], "files": [...]}``.
    """
    records = list(records)
    known = existing_dedup_keys(cfg) if skip_duplicates else set()
    by_file: dict[Path, list[dict[str, Any]]] = {}
    written: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for record in records:
        key = record.get("dedup_key")
        if skip_duplicates and key and key in known:
            skipped.append(record)
            continue
        if key:
            known.add(key)
        by_file.setdefault(partition_path(cfg, record["ts"]), []).append(record)
        written.append(record)

    for path, batch in by_file.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for record in batch:
                fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    if written:
        write_manifest(cfg)
    return {"written": written, "skipped": skipped, "files": sorted(by_file)}


def write_manifest(cfg: Config) -> dict[str, Any]:
    """Repo-level manifest so a human (or another tool) can see the shape."""
    files = journal_files(cfg)
    per_file = []
    total = 0
    for path in files:
        with path.open(encoding="utf-8") as fh:
            count = sum(1 for line in fh if line.strip())
        total += count
        per_file.append({"path": str(path.relative_to(cfg.repo_dir)), "records": count})
    live = load(cfg)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "format": "jsonl",
        "partitioning": "journal/<YYYY>/<YYYY-MM>.jsonl",
        "total_records": total,
        "live_records": len(live),
        "first_ts": live[0]["ts"] if live else None,
        "last_ts": live[-1]["ts"] if live else None,
        "accounts": sorted({r.get("account", "") for r in live if r.get("account")}),
        "brokers": sorted({r.get("broker", "") for r in live if r.get("broker")}),
        "files": per_file,
    }
    path = cfg.repo_dir / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return manifest


# ---------------------------------------------------------------------------
# derived SQLite index
# ---------------------------------------------------------------------------

def _row(record: dict[str, Any]) -> tuple:
    risk = record.get("risk") or {}
    source = record.get("source") or {}
    option = record.get("option") or {}
    ts = record.get("ts", "")
    return (
        record.get("id"), ts, ts[:10], ts[:7], ts[:4],
        record.get("kind", "trade"), record.get("account"), record.get("broker"),
        record.get("symbol"), option.get("underlying") or record.get("symbol"),
        record.get("asset_class"), record.get("currency"),
        record.get("side"), record.get("cash_type"),
        record.get("quantity"), record.get("signed_quantity"),
        record.get("price"), record.get("multiplier"),
        record.get("gross_amount"),
        record.get("fees_total", fees_total(record)),
        record.get("net_amount"), record.get("amount"),
        record.get("strategy"), record.get("setup"), record.get("thesis"),
        record.get("conviction"),
        ",".join(record.get("tags") or []),
        risk.get("stop"), risk.get("target"), risk.get("risk_amount"),
        source.get("kind"), source.get("external_id"), record.get("dedup_key"),
        json.dumps(record, ensure_ascii=False, sort_keys=True),
    )


def connect(cfg: Config, *, refresh: bool = True) -> sqlite3.Connection:
    """Open the index, rebuilding it if the journal changed underneath."""
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.index_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(INDEX_SCHEMA)
    if not refresh:
        return conn
    current = fingerprint(cfg)
    row = conn.execute("SELECT value FROM meta WHERE key='fingerprint'").fetchone()
    if row and row["value"] == current:
        return conn
    conn.execute("DELETE FROM trades")
    conn.executemany(
        "INSERT OR REPLACE INTO trades VALUES (" + ",".join(["?"] * 34) + ")",
        [_row(r) for r in load(cfg)],
    )
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('fingerprint', ?)", (current,))
    conn.commit()
    return conn


def query(cfg: Config, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    conn = connect(cfg)
    try:
        return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]
    finally:
        conn.close()


def select(cfg: Config, *, since: str | None = None, until: str | None = None,
           symbol: str | None = None, account: str | None = None,
           broker: str | None = None, strategy: str | None = None,
           tag: str | None = None, kind: str | None = None,
           limit: int | None = None) -> list[dict[str, Any]]:
    """Filtered records, newest last. Returns full JSON records."""
    clauses, params = [], []
    if since:
        clauses.append("ts >= ?"); params.append(since)
    if until:
        clauses.append("ts <= ?"); params.append(until if len(until) > 10 else until + "T23:59:59Z")
    if symbol:
        clauses.append("(symbol = ? OR underlying = ?)")
        params.extend([symbol.upper(), symbol.upper()])
    if account:
        clauses.append("account = ?"); params.append(account)
    if broker:
        clauses.append("broker = ?"); params.append(broker.upper())
    if strategy:
        clauses.append("strategy = ?"); params.append(strategy)
    if tag:
        clauses.append("(',' || tags || ',') LIKE ?"); params.append(f"%,{tag.lower()},%")
    if kind:
        clauses.append("kind = ?"); params.append(kind)
    sql = "SELECT raw FROM trades"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY ts ASC, id ASC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [json.loads(row["raw"]) for row in query(cfg, sql, params)]
