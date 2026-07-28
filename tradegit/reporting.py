"""Deterministic Markdown and PDF reports for TradeGit analysis output."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def money(value: Any, currency: str | None = None, *, signed: bool = True) -> str:
    if value is None:
        return "-"
    text = f"{value:+,.2f}" if signed else f"{value:,.2f}"
    return f"{text} {currency}" if currency else text


def ratio(value: Any, suffix: str = "%") -> str:
    return "-" if value is None else f"{value:.1f}{suffix}"


def render_markdown(report: dict[str, Any], *, since: str | None = None,
                    until: str | None = None,
                    generated_at: str | None = None) -> str:
    """Render a self-contained, factual review report."""
    generated_at = generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    period = report["period"]
    metrics = report["metrics"]
    currency = metrics.get("currency")
    period_from = (period.get("from") or since or "-")[:10]
    period_to = (period.get("to") or until or "-")[:10]
    title = f"TradeGit Review Report ({period_from} to {period_to})"

    lines = [
        f"# {title}",
        "",
        f"Generated: {generated_at}",
        "",
        "TradeGit keeps the facts, preserves the reasoning, "
        "and helps you review behavior without turning the journal into investment advice.",
        "",
        "## Summary",
        "",
    ]

    if metrics.get("currency_mixed") and currency is None:
        lines += [
            f"- Period: {period_from} to {period_to}",
            f"- Records reviewed: {period.get('records', 0)}",
            f"- Closed round trips: {metrics.get('roundtrips', 0)}",
            f"- Win rate: {ratio(metrics.get('win_rate'))}",
            f"- Currencies: {', '.join(report.get('currencies') or [])}",
            "- Money metrics are not added across currencies. Use --fx and --base-currency "
            "to generate a converted total.",
            "",
            "### By Currency",
            "",
            table(
                ["Currency", "Round trips", "Realized P&L", "Total P&L", "Win rate", "Profit factor"],
                [[b["currency"], b["roundtrips"], money(b["realized_pnl"], b["currency"]),
                  money(b["total_pnl"], b["currency"]), ratio(b["win_rate"]),
                  b["profit_factor"] if b["profit_factor"] is not None else "-"]
                 for b in report.get("by_currency") or []],
            ),
            "",
        ]
    else:
        if metrics.get("fx_rates"):
            lines.append(
                "FX rates: " + ", ".join(f"{k}={v}" for k, v in sorted(metrics["fx_rates"].items())))
            lines.append("")
        lines += [
            table(
                ["Metric", "Value"],
                [
                    ["Period", f"{period_from} to {period_to}"],
                    ["Records reviewed", period.get("records", 0)],
                    ["Closed round trips", metrics.get("roundtrips", 0)],
                    ["Win rate", ratio(metrics.get("win_rate"))],
                    ["Realized P&L", money(metrics.get("realized_pnl"), currency)],
                    ["Total P&L", money(metrics.get("total_pnl"), currency)],
                    ["Profit factor", metrics.get("profit_factor") if metrics.get("profit_factor") is not None else "-"],
                    ["Expectancy", money(metrics.get("expectancy"), currency)],
                    ["Average win", money(metrics.get("avg_win"), currency)],
                    ["Average loss", money(metrics.get("avg_loss"), currency)],
                    ["Max drawdown", money(metrics.get("max_drawdown"), currency)],
                    ["Average hold", f"{metrics.get('avg_hold_days', 0):.1f} days"],
                    ["Average R", metrics.get("avg_r_multiple") if metrics.get("avg_r_multiple") is not None else "-"],
                ],
            ),
            "",
        ]

    notes = report.get("notes") or []
    lines += ["## Review Observations", ""]
    lines += [f"- {note}" for note in notes] if notes else ["- No closed round trips in this period."]
    lines.append("")

    lines += ["## Worst Closed Trades", ""]
    worst = [t for t in report.get("worst") or [] if t.get("net_pnl", 0) < 0]
    lines += [
        table(
            ["Exit", "Symbol", "Direction", "P&L", "Return", "Hold", "Entry thesis"],
            [[t["exit_ts"][:10], t["symbol"], t["direction"], money(t["net_pnl"], t.get("currency")),
              f"{t['return_pct']:+.2f}%", f"{t['hold_days']:.1f}d", t.get("thesis") or "-"]
             for t in worst[:10]],
        ) if worst else "No losing closed trades in this period.",
        "",
    ]

    lines += ["## Best Closed Trades", ""]
    best = [t for t in report.get("best") or [] if t.get("net_pnl", 0) > 0]
    lines += [
        table(
            ["Exit", "Symbol", "Direction", "P&L", "Return", "Hold", "Entry thesis"],
            [[t["exit_ts"][:10], t["symbol"], t["direction"], money(t["net_pnl"], t.get("currency")),
              f"{t['return_pct']:+.2f}%", f"{t['hold_days']:.1f}d", t.get("thesis") or "-"]
             for t in best[:10]],
        ) if best else "No winning closed trades in this period.",
        "",
    ]

    lines += ["## P&L By Symbol", ""]
    lines += [
        table(
            ["Symbol", "P&L", "Round trips", "Win rate", "Profit factor"],
            [[b["key"], money(b.get("net_pnl"), b.get("currency")), b["roundtrips"],
              ratio(b["win_rate"]), b["profit_factor"] if b["profit_factor"] is not None else "-"]
             for b in (report.get("by_symbol") or [])[:15]],
        ) if report.get("by_symbol") else "No symbol buckets in this period.",
        "",
    ]

    lines += ["## P&L By Month", ""]
    lines += [
        table(
            ["Month", "P&L", "Round trips", "Win rate"],
            [[b["key"], money(b.get("net_pnl"), b.get("currency")), b["roundtrips"], ratio(b["win_rate"])]
             for b in (report.get("by_month") or [])],
        ) if report.get("by_month") else "No monthly buckets in this period.",
        "",
    ]

    positions = report.get("open_positions") or []
    lines += ["## Open Positions", ""]
    lines += [
        table(
            ["Symbol", "Direction", "Quantity", "Avg price", "Cost basis", "Thesis"],
            [[p["symbol"], p["direction"], p["quantity"], p["avg_price"],
              money(p["cost_basis"], p.get("currency"), signed=False), p.get("thesis") or "-"]
             for p in positions[:15]],
        ) if positions else "No open positions in this period.",
        "",
    ]

    lines += [
        "## Method",
        "",
        "- Closed trades are matched with FIFO lot accounting.",
        "- P&L is net of recorded fees; dividends, interest, tax and account fees are separated.",
        "- Deposits and withdrawals are cash movements, not performance.",
        "- Unrealized P&L is included only when marks are provided with --mark.",
        "- This report is a factual journal review, not investment advice.",
        "",
    ]
    return "\n".join(lines)


def table(headers: list[Any], rows: list[list[Any]]) -> str:
    def cell(value: Any) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    out = ["| " + " | ".join(cell(h) for h in headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    out.extend("| " + " | ".join(cell(v) for v in row) + " |" for row in rows)
    return "\n".join(out)


def write_pdf(markdown: str, path: Path) -> None:
    """Write a simple Unicode PDF without third-party dependencies."""
    lines = markdown_to_text(markdown)
    pages = paginate(lines, width=76, height=48)
    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{6 + i * 2} 0 R" for i in range(len(pages)))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("ascii"))
    objects.append(
        b"<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light "
        b"/Encoding /UniGB-UCS2-H /DescendantFonts [4 0 R] >>"
    )
    objects.append(
        b"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light "
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 2 >> "
        b"/FontDescriptor 5 0 R >>"
    )
    objects.append(
        b"<< /Type /FontDescriptor /FontName /STSong-Light /Flags 4 "
        b"/FontBBox [0 -200 1000 900] /ItalicAngle 0 /Ascent 800 "
        b"/Descent -200 /CapHeight 700 /StemV 80 >>"
    )

    for page in pages:
        stream = page_stream(page)
        content_obj = len(objects) + 2
        page_obj = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_obj} 0 R >>"
        ).encode("ascii")
        objects.append(page_obj)
        objects.append(
            b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n"
            + stream + b"\nendstream"
        )

    write_pdf_objects(objects, path)


def markdown_to_text(markdown: str) -> list[str]:
    lines = []
    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.startswith("|"):
            parts = [p.strip() for p in line.strip("|").split("|")]
            if parts and all(set(p) <= {"-"} for p in parts):
                continue
            line = "  ".join(parts)
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"^\-\s+", "- ", line)
        line = re.sub(r"\*\*(.*?)\*\*", r"\1", line)
        lines.append(line)
    return lines


def paginate(lines: list[str], *, width: int, height: int) -> list[list[str]]:
    wrapped: list[str] = []
    for line in lines:
        wrapped.extend(wrap_display(line, width) or [""])
    pages = [wrapped[i:i + height] for i in range(0, len(wrapped), height)]
    return pages or [["No report content."]]


def wrap_display(text: str, width: int) -> list[str]:
    out: list[str] = []
    current = ""
    current_width = 0
    for chunk in re.split(r"(\s+)", text):
        chunk_width = display_width(chunk)
        if current and current_width + chunk_width > width:
            out.append(current.rstrip())
            current = chunk.lstrip()
            current_width = display_width(current)
        else:
            current += chunk
            current_width += chunk_width
    while display_width(current) > width:
        cut = 0
        seen = 0
        for i, ch in enumerate(current):
            seen += char_width(ch)
            if seen > width:
                break
            cut = i + 1
        out.append(current[:cut])
        current = current[cut:]
    if current or not out:
        out.append(current.rstrip())
    return out


def display_width(text: str) -> int:
    return sum(char_width(ch) for ch in text)


def char_width(ch: str) -> int:
    return 2 if unicodedata.east_asian_width(ch) in {"F", "W"} else 1


def page_stream(lines: list[str]) -> bytes:
    parts = ["BT", "/F1 10 Tf", "50 790 Td", "14 TL"]
    for line in lines:
        parts.append(f"<{line.encode('utf-16-be').hex()}> Tj")
        parts.append("T*")
    parts.append("ET")
    return "\n".join(parts).encode("ascii")


def write_pdf_objects(objects: list[bytes], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{number} 0 obj\n".encode("ascii"))
        data.extend(obj)
        data.extend(b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    data.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    data.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    path.write_bytes(bytes(data))
