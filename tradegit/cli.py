"""TradeGit command line interface.

Every command accepts ``--json`` and prints a machine-readable object, which
is what the Claude/Codex skill uses. Without it, output is a short human
summary.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import __version__, analytics, importers, scaffold, store, sync
from .config import Config, read_state, write_state
from .schema import ValidationError, iso, normalize, now_iso, parse_ts, to_float


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class CliError(RuntimeError):
    pass


_REL = re.compile(r"^(\d+)\s*([dwmy])$", re.IGNORECASE)


def parse_when(value: str | None) -> str | None:
    """``30d`` / ``3m`` / ``ytd`` / ``mtd`` / ``2026-01-01`` -> ISO timestamp."""
    if not value:
        return None
    text = str(value).strip().lower()
    now = datetime.now(timezone.utc)
    if text in {"today"}:
        return iso(now.replace(hour=0, minute=0, second=0, microsecond=0))
    if text == "ytd":
        return iso(now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0))
    if text == "mtd":
        return iso(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
    if text == "all":
        return None
    match = _REL.match(text)
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        days = {"d": 1, "w": 7, "m": 30, "y": 365}[unit] * amount
        return iso(now - timedelta(days=days))
    return iso(parse_ts(value))


def parse_marks(values: list[str] | None) -> dict[str, float]:
    """``--mark AAPL=213.4 --mark MSFT=500`` (also accepts a comma list)."""
    marks: dict[str, float] = {}
    for chunk in values or []:
        for pair in str(chunk).split(","):
            if "=" not in pair:
                continue
            symbol, _, price = pair.partition("=")
            value = to_float(price, None)
            if value is not None:
                marks[symbol.strip().upper()] = value
    return marks


def parse_fees(value: str | None) -> Any:
    if not value:
        return None
    text = value.strip()
    if text.startswith("{"):
        return json.loads(text)
    return to_float(text, 0.0)


def emit(data: Any, args: argparse.Namespace, human: str = "") -> None:
    if getattr(args, "json", False):
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        print(human or json.dumps(data, ensure_ascii=False, indent=2, default=str))


def require_init(cfg: Config) -> None:
    if not cfg.initialized:
        raise CliError("TradeGit 尚未初始化。先运行：tradegit init")


def filters_from(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "since": parse_when(getattr(args, "since", None)),
        "until": parse_when(getattr(args, "until", None)),
        "symbol": getattr(args, "symbol", None),
        "account": getattr(args, "account", None),
        "broker": getattr(args, "broker", None),
        "strategy": getattr(args, "strategy", None),
        "tag": getattr(args, "tag", None),
    }


def maybe_refresh(cfg: Config, args: argparse.Namespace) -> dict[str, Any]:
    """Check the private repo for changes before a read or a write."""
    if getattr(args, "no_sync", False) or not cfg.initialized:
        return {"checked": False}
    if not cfg.check_remote_on_write:
        return {"checked": False, "reason": "check_remote_on_write disabled"}
    try:
        return sync.ensure_fresh(cfg)
    except sync.SyncError as exc:
        return {"checked": True, "error": str(exc), "in_sync": False}


def push_after_write(cfg: Config, args: argparse.Namespace, message: str) -> dict[str, Any]:
    if getattr(args, "no_push", False):
        return sync.commit_and_push(cfg, message, push=False)
    return sync.commit_and_push(cfg, message)


# ---------------------------------------------------------------------------
# init / status / doctor
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    cfg = Config.load()
    cfg.ensure_dirs()

    if cfg.initialized and not args.force:
        status = sync.check(cfg)
        emit({"already_initialized": True, "repo": cfg.repo_slug, "status": status}, args,
             f"已经初始化过了：{cfg.repo_slug}\n本地副本：{cfg.repo_dir}\n"
             f"{status.get('message', '')}\n（要换仓库请加 --force）")
        return 0

    auth = sync.auth_status()
    if not auth["connected"]:
        raise CliError(
            "没有检测到已连接的 GitHub 账户。任选一种方式连接后重试：\n"
            "  1) gh auth login            （推荐，Claude Code / Codex 环境通常已装 gh）\n"
            "  2) export GITHUB_TOKEN=...  （需要 repo 权限的 Personal Access Token）\n"
            "TradeGit 不会保存你的 token，只调用宿主环境已有的凭证。")

    user = auth["user"]
    slug = args.repo or f"{user}/{args.name}"
    if "/" not in slug:
        slug = f"{user}/{slug}"

    created = False
    if sync.repo_exists(slug):
        if not args.use_existing and not args.force:
            raise CliError(
                f"{slug} 已存在。用 --use-existing 复用它（会克隆现有交易日志），"
                f"或用 --name 换一个仓库名。")
    else:
        sync.create_private_repo(slug, "Private trading journal (TradeGit)")
        created = True

    visibility = sync.visibility(slug)
    if visibility and visibility != "private":
        raise CliError(
            f"{slug} 当前是 {visibility} 仓库。交易日志必须放在私有仓库里——"
            f"请先在 GitHub 上把它改为 private，再重新运行 init。")

    if cfg.repo_dir.exists():
        if not args.force:
            raise CliError(f"{cfg.repo_dir} 已存在。加 --force 会删除它并重新克隆。")
        shutil.rmtree(cfg.repo_dir)
    sync.clone(slug, cfg.repo_dir)

    cfg.repo_slug = slug
    cfg.repo_url = f"https://github.com/{slug}"
    cfg.default_account = args.account or cfg.default_account
    cfg.save()

    written = scaffold.write_repo_scaffold(cfg.repo_dir)
    store.write_manifest(cfg)
    result = sync.commit_and_push(cfg, "chore: initialize trading journal")
    write_state(cfg, {"last_sync": now_iso(), "initialized_at": now_iso()})

    payload = {
        "repo": slug, "url": cfg.repo_url, "created": created,
        "visibility": visibility or "private", "local": str(cfg.repo_dir),
        "scaffold": written, "commit": result, "github_user": user,
        "default_account": cfg.default_account,
    }
    emit(payload, args,
         f"✓ {'已创建' if created else '已连接'}私有仓库 {slug}\n"
         f"  GitHub: {cfg.repo_url}\n"
         f"  本地副本: {cfg.repo_dir}\n"
         f"  默认账户: {cfg.default_account}\n\n"
         f"下一步：tradegit log --symbol AAPL --side BUY --qty 100 --price 213.45 --why \"...\"")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = Config.load()
    auth = sync.auth_status()
    payload: dict[str, Any] = {"version": __version__, "auth": auth,
                               "home": str(cfg.home), "initialized": cfg.initialized}
    if not cfg.initialized:
        emit(payload, args, "未初始化。运行 tradegit init。")
        return 0

    status = sync.check(cfg)
    records = store.load(cfg)
    matched = analytics.match_fifo(records)
    payload.update({
        "repo": cfg.repo_slug, "url": cfg.repo_url, "sync": status,
        "records": len(records),
        "first_ts": records[0]["ts"] if records else None,
        "last_ts": records[-1]["ts"] if records else None,
        "open_positions": len(matched["open_positions"]),
        "closed_roundtrips": len(matched["roundtrips"]),
        "accounts": sorted({r.get("account", "") for r in records}),
    })
    human = (
        f"仓库      {cfg.repo_slug}  ({cfg.repo_url})\n"
        f"本地      {cfg.repo_dir}\n"
        f"同步      {status.get('message')}\n"
        f"  local  {(status.get('local_head') or '')[:8]}   "
        f"remote {(status.get('remote_head') or '')[:8]}\n"
        f"未提交    {len(status.get('uncommitted') or [])} 项\n"
        f"记录      {len(records)} 条"
        + (f"（{records[0]['ts'][:10]} → {records[-1]['ts'][:10]}）" if records else "")
        + f"\n持仓      {len(matched['open_positions'])} 个 · 平仓 "
          f"{len(matched['roundtrips'])} 笔"
    )
    emit(payload, args, human)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Cheap remote-drift check — run this before analysis or a big import."""
    cfg = Config.load()
    require_init(cfg)
    status = sync.check(cfg)
    if args.pull and not status["in_sync"] and status.get("remote_reachable"):
        status["pull"] = sync.pull(cfg)
        status = {**sync.check(cfg), "pull": status["pull"]}
    emit(status, args, status.get("message", ""))
    return 0 if status.get("in_sync") else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = Config.load()
    auth = sync.auth_status()
    checks = [
        ("git", bool(shutil.which("git")), "安装 git"),
        ("gh CLI", auth["gh_cli"], "可选：brew install gh（创建仓库需要）"),
        ("GitHub 已连接", auth["connected"], "gh auth login 或 export GITHUB_TOKEN=..."),
        ("已初始化", cfg.initialized, "tradegit init"),
    ]
    if cfg.initialized:
        status = sync.check(cfg)
        checks.append(("远端可达", bool(status.get("remote_reachable")), "检查网络 / 凭证"))
        checks.append(("已同步", bool(status.get("in_sync")), "tradegit sync"))
        try:
            store.load(cfg)
            checks.append(("日志可解析", True, ""))
        except Exception as exc:  # noqa: BLE001 - surface the parse error verbatim
            checks.append(("日志可解析", False, str(exc)))
    payload = {"ok": all(ok for _, ok, _ in checks),
               "checks": [{"name": n, "ok": ok, "fix": fix} for n, ok, fix in checks],
               "auth": auth, "home": str(cfg.home), "version": __version__}
    human = "\n".join(f"{'✓' if ok else '✗'} {name}" + (f"   → {fix}" if not ok and fix else "")
                      for name, ok, fix in checks)
    emit(payload, args, human)
    return 0 if payload["ok"] else 1


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------

def _record_from_args(args: argparse.Namespace, cfg: Config) -> list[dict[str, Any]]:
    if args.json_input:
        text = args.json_input
        if text == "-":
            text = sys.stdin.read()
        elif Path(text).expanduser().exists():
            text = Path(text).expanduser().read_text(encoding="utf-8")
        data = json.loads(text)
        raw = data if isinstance(data, list) else [data]
    else:
        raw = [{
            "symbol": args.symbol, "side": args.side, "quantity": args.qty,
            "price": args.price, "ts": args.at or now_iso(),
        }]
    out = []
    for item in raw:
        item = dict(item)
        for key, value in {
            "account": args.account, "broker": args.broker,
            "asset_class": args.asset_class, "currency": args.currency,
            "multiplier": args.multiplier, "fees": parse_fees(args.fees),
            "thesis": args.why, "strategy": args.strategy, "setup": args.setup,
            "notes": args.notes, "emotions": args.emotions, "exit_plan": args.exit_plan,
            "market_context": args.market_context, "review": args.review,
            "conviction": args.conviction, "tags": args.tags,
            "stop": args.stop, "target": args.target,
        }.items():
            if value not in (None, ""):
                item.setdefault(key, value)
        item.setdefault("source", {"kind": "manual"})
        out.append(normalize(item, default_account=cfg.default_account,
                             default_currency=cfg.default_currency))
    return out


def cmd_log(args: argparse.Namespace) -> int:
    cfg = Config.load()
    require_init(cfg)
    if not args.json_input and not (args.symbol and args.side and args.qty and args.price):
        raise CliError("需要 --symbol --side --qty --price，或用 --json 传入结构化记录。")

    refresh = maybe_refresh(cfg, args)
    records = _record_from_args(args, cfg)

    if args.dry_run:
        emit({"dry_run": True, "records": records, "sync": refresh}, args,
             json.dumps(records, ensure_ascii=False, indent=2))
        return 0

    result = store.append(cfg, records, skip_duplicates=not args.allow_duplicates)
    if not result["written"]:
        emit({"written": 0, "skipped": len(result["skipped"]), "sync": refresh}, args,
             "这笔交易已经记录过了（重复），没有写入。加 --allow-duplicates 可强制写入。")
        return 0

    summary = ", ".join(
        f"{r.get('side', r.get('cash_type', ''))} {r.get('quantity', r.get('amount', ''))} "
        f"{r['symbol']}" for r in result["written"][:3])
    if len(result["written"]) > 3:
        summary += f" 等 {len(result['written'])} 条"
    push = push_after_write(cfg, args, f"log: {summary}")

    payload = {"written": len(result["written"]), "skipped": len(result["skipped"]),
               "records": result["written"], "push": push, "sync": refresh}
    ids = "\n".join(f"  {r['id']}" for r in result["written"])
    emit(payload, args,
         f"✓ 已记录 {len(result['written'])} 条：{summary}\n{ids}\n"
         + ("✓ 已同步到 " + cfg.repo_slug if push.get("pushed")
            else "⚠ " + str(push.get("message", "未推送"))))
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    cfg = Config.load()
    require_init(cfg)
    path = Path(args.file).expanduser()
    if not path.exists():
        raise CliError(f"文件不存在：{path}")

    refresh = maybe_refresh(cfg, args)
    parsed = importers.parse(path, broker=args.broker, account=args.account)

    normalized, errors = [], []
    for item in parsed["records"]:
        try:
            item.setdefault("source", {}).setdefault("file", path.name)
            normalized.append(normalize(item, default_account=cfg.default_account,
                                        default_currency=cfg.default_currency))
        except (ValidationError, ValueError) as exc:
            errors.append({"record": item, "error": str(exc)})

    known = store.existing_dedup_keys(cfg)
    new = [r for r in normalized if r["dedup_key"] not in known]
    duplicates = len(normalized) - len(new)

    preview = {
        "file": str(path), "broker": parsed.get("detected_broker"),
        "parsed": len(parsed["records"]), "normalized": len(normalized),
        "new": len(new), "duplicates": duplicates,
        "unparsed": parsed["skipped"][:20], "errors": errors[:20],
        "ignored": len(parsed.get("ignored") or []),
        "sections": parsed.get("sections", []),
        "date_range": [min(r["ts"] for r in new), max(r["ts"] for r in new)] if new else None,
        "symbols": sorted({r["symbol"] for r in new if r.get("symbol")})[:50],
        "sync": refresh,
    }

    if args.dry_run:
        preview["sample"] = new[:5]
        preview["dry_run"] = True
        emit(preview, args, _import_human(preview, dry=True))
        return 0

    if not new:
        emit({**preview, "written": 0}, args, _import_human(preview, dry=False))
        return 0

    if args.keep_source:
        cfg.imports_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, cfg.imports_dir / f"{now_iso().replace(':', '')}-{path.name}")

    result = store.append(cfg, new, skip_duplicates=True)
    push = push_after_write(
        cfg, args,
        f"import: {len(result['written'])} records from {parsed.get('detected_broker')} "
        f"({path.name})")
    payload = {**preview, "written": len(result["written"]), "push": push}
    emit(payload, args, _import_human(payload, dry=False))
    return 0


def _import_human(payload: dict[str, Any], *, dry: bool) -> str:
    lines = [
        f"文件      {payload['file']}",
        f"识别为    {payload['broker']}",
        f"解析      {payload['parsed']} 条记录（{payload['normalized']} 条通过校验）"
        + (f"，另有 {payload['ignored']} 行是券商自己的配对/小计，已按设计忽略"
           if payload.get("ignored") else ""),
        f"新增      {payload['new']} 条（重复 {payload['duplicates']} 条已跳过）",
    ]
    if payload.get("date_range"):
        lines.append(f"时间范围  {payload['date_range'][0][:10]} → {payload['date_range'][1][:10]}")
    if payload.get("symbols"):
        lines.append(f"标的      {', '.join(payload['symbols'][:12])}"
                     + (" …" if len(payload["symbols"]) > 12 else ""))
    if payload.get("unparsed"):
        lines.append(f"⚠ {len(payload['unparsed'])} 行无法解析（需要人工确认）：")
        for item in payload["unparsed"][:5]:
            lines.append(f"    - {item.get('reason', '')}")
    if payload.get("errors"):
        lines.append(f"⚠ {len(payload['errors'])} 条记录校验失败")
    if dry:
        lines.append("\n（--dry-run，未写入。确认无误后去掉 --dry-run 再跑一次）")
    elif payload.get("written"):
        lines.append(f"\n✓ 已写入 {payload['written']} 条"
                     + ("并同步到远端" if payload.get("push", {}).get("pushed") else "（未推送）"))
    else:
        lines.append("\n没有新记录需要写入。")
    return "\n".join(lines)


def cmd_amend(args: argparse.Namespace) -> int:
    """Append a corrected copy of a record (the original stays in git history)."""
    cfg = Config.load()
    require_init(cfg)
    maybe_refresh(cfg, args)
    records = {r["id"]: r for r in store.load(cfg)}
    original = records.get(args.id)
    if not original:
        raise CliError(f"找不到记录 {args.id}")

    patch: dict[str, Any] = json.loads(args.set) if args.set else {}
    for key, value in {
        "thesis": args.why, "strategy": args.strategy, "setup": args.setup,
        "notes": args.notes, "review": args.review, "mistake": args.mistake,
        "emotions": args.emotions, "tags": args.tags, "conviction": args.conviction,
        "price": args.price, "quantity": args.qty, "stop": args.stop, "target": args.target,
    }.items():
        if value not in (None, ""):
            patch[key] = value

    if not patch:
        raise CliError("没有要修改的字段。用 --why/--review/--tags… 或 --set '{\"字段\":值}'。")

    merged = {k: v for k, v in original.items()
              if k not in {"id", "hash", "dedup_key", "updated_at", "supersedes"}}
    merged.update(patch)
    merged["supersedes"] = original["id"]
    merged["created_at"] = original.get("created_at")
    updated = normalize(merged, default_account=cfg.default_account,
                        default_currency=cfg.default_currency)

    result = store.append(cfg, [updated], skip_duplicates=False)
    push = push_after_write(cfg, args, f"amend: {original['id']}")
    emit({"original": original, "updated": updated, "push": push,
          "written": len(result["written"])}, args,
         f"✓ 已更新 {original['id']} → {updated['id']}\n"
         f"  修改字段：{', '.join(patch)}")
    return 0


def cmd_void(args: argparse.Namespace) -> int:
    cfg = Config.load()
    require_init(cfg)
    maybe_refresh(cfg, args)
    records = {r["id"]: r for r in store.load(cfg)}
    if args.id not in records:
        raise CliError(f"找不到记录 {args.id}")
    target = records[args.id]
    record = normalize({"kind": "void", "voids": args.id, "ts": now_iso(),
                        "symbol": target.get("symbol", ""), "notes": args.reason,
                        "account": target.get("account")},
                       default_account=cfg.default_account)
    store.append(cfg, [record], skip_duplicates=False)
    push = push_after_write(cfg, args, f"void: {args.id}")
    emit({"voided": args.id, "record": record, "push": push}, args,
         f"✓ 已作废 {args.id}（原记录仍保留在 git 历史中）")
    return 0


# ---------------------------------------------------------------------------
# reading / analysis
# ---------------------------------------------------------------------------

def cmd_list(args: argparse.Namespace) -> int:
    cfg = Config.load()
    require_init(cfg)
    maybe_refresh(cfg, args)
    records = store.select(cfg, **filters_from(args), kind=args.kind, limit=args.limit)
    if args.limit and len(records) == args.limit:
        records = records[-args.limit:]
    rows = []
    for r in records:
        if r.get("kind") == "cash":
            rows.append(f"{r['ts'][:16]}  {r['cash_type']:<10} {r.get('symbol', ''):<8} "
                        f"{r['amount']:>12,.2f}  {r.get('notes', '')[:40]}")
        else:
            rows.append(f"{r['ts'][:16]}  {r['side']:<6} {r['symbol']:<14} "
                        f"{r['quantity']:>10,.4g} @ {r['price']:>10,.4f}  "
                        f"{(r.get('thesis') or '')[:44]}")
    emit({"count": len(records), "records": records}, args,
         "\n".join(rows) or "没有匹配的记录。")
    return 0


def cmd_positions(args: argparse.Namespace) -> int:
    cfg = Config.load()
    require_init(cfg)
    maybe_refresh(cfg, args)
    records = store.select(cfg, **filters_from(args))
    marks = parse_marks(args.mark)
    result = analytics.summarize(records, marks=marks)
    positions = result["open_positions"]
    rows = [f"{p['symbol']:<16} {p['direction']:<6} {p['quantity']:>12,.4g} "
            f"@ {p['avg_price']:>10,.4f}  成本 {p['cost_basis']:>12,.2f}"
            + (f"  现价 {p['mark']:>9,.4f}  浮动 {p['unrealized_pnl']:>+11,.2f}"
               if p.get("mark") is not None else "")
            for p in positions]
    emit({"positions": positions,
          "unrealized_pnl": result["metrics"]["unrealized_pnl"]}, args,
         "\n".join(rows) or "当前没有持仓。")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    cfg = Config.load()
    require_init(cfg)
    refresh = maybe_refresh(cfg, args)
    records = store.select(cfg, **filters_from(args))
    result = analytics.summarize(records, marks=parse_marks(args.mark))
    result["sync"] = refresh
    result["notes"] = analytics.review_prompts(result)

    if args.group_by:
        key = {"symbol": "by_symbol", "month": "by_month", "strategy": "by_strategy",
               "tag": "by_tag", "direction": "by_direction"}[args.group_by]
        result["grouped"] = result[key]

    if not args.json:
        result.pop("roundtrips", None)
    emit(result, args, _analyze_human(result, args))
    return 0


def _analyze_human(result: dict[str, Any], args: argparse.Namespace) -> str:
    m = result["metrics"]
    period = result["period"]
    lines = [
        f"区间      {(period['from'] or '—')[:10]} → {(period['to'] or '—')[:10]}"
        f"   {period['records']} 条记录",
        f"已实现    {m['realized_pnl']:+,.2f}   （{m['roundtrips']} 笔平仓，"
        f"{m['wins']} 胜 / {m['losses']} 负，胜率 {m['win_rate']:.1f}%）",
        f"总盈亏    {m['total_pnl']:+,.2f}   股息 {m['dividends']:+,.2f} · "
        f"利息 {m['interest']:+,.2f} · 手续费 {m['fees_paid']:,.2f}",
        f"均盈/均亏 {m['avg_win']:+,.2f} / {m['avg_loss']:+,.2f}"
        f"   盈亏因子 {m['profit_factor'] if m['profit_factor'] is not None else '—'}"
        f"   期望 {m['expectancy']:+,.2f}",
        f"最大单笔  盈 {m['largest_win']:+,.2f} / 亏 {m['largest_loss']:+,.2f}"
        f"   最大回撤 {m['max_drawdown']:,.2f}",
        f"持有天数  平均 {m['avg_hold_days']:.1f}"
        + (f"   平均 R {m['avg_r_multiple']}" if m["avg_r_multiple"] is not None else ""),
    ]
    if m["unrealized_pnl"] is not None:
        lines.append(f"浮动盈亏  {m['unrealized_pnl']:+,.2f}")

    grouped = result.get("grouped")
    if grouped:
        lines.append(f"\n按{args.group_by}分组（亏损在前）：")
        for bucket in grouped[:15]:
            lines.append(f"  {bucket['key'][:22]:<22} {bucket['net_pnl']:>+12,.2f}   "
                         f"{bucket['roundtrips']:>3} 笔  胜率 {bucket['win_rate']:>5.1f}%")

    if result.get("worst"):
        lines.append("\n亏损最重的交易：")
        for trip in result["worst"][:5]:
            if trip["net_pnl"] >= 0:
                break
            lines.append(f"  {trip['exit_ts'][:10]}  {trip['symbol']:<14} "
                         f"{trip['net_pnl']:>+12,.2f}  {trip['return_pct']:>+7.2f}%  "
                         f"持有 {trip['hold_days']:.1f} 天")

    if result.get("notes"):
        lines.append("\n复盘观察：")
        lines.extend(f"  · {n}" for n in result["notes"])
    return "\n".join(lines)


def cmd_roundtrips(args: argparse.Namespace) -> int:
    cfg = Config.load()
    require_init(cfg)
    maybe_refresh(cfg, args)
    records = store.select(cfg, **filters_from(args))
    trips = analytics.match_fifo(records)["roundtrips"]
    if args.sort == "pnl":
        trips.sort(key=lambda t: t["net_pnl"])
    elif args.sort == "return":
        trips.sort(key=lambda t: t["return_pct"])
    if args.limit:
        trips = trips[:args.limit]
    rows = [f"{t['exit_ts'][:10]}  {t['symbol']:<14} {t['direction']:<5} "
            f"{t['quantity']:>8,.4g}  {t['entry_price']:>9,.4f} → {t['exit_price']:>9,.4f}  "
            f"{t['net_pnl']:>+12,.2f}  {t['return_pct']:>+7.2f}%  {t['hold_days']:>6.1f}d"
            for t in trips]
    emit({"count": len(trips), "roundtrips": trips}, args,
         "\n".join(rows) or "没有平仓交易。")
    return 0


def cmd_sql(args: argparse.Namespace) -> int:
    """Ad-hoc SQL over the derived index — the escape hatch for odd questions."""
    cfg = Config.load()
    require_init(cfg)
    maybe_refresh(cfg, args)
    lowered = args.query.strip().lower()
    if not lowered.startswith(("select", "with", "pragma", "explain")):
        raise CliError("只允许只读查询（SELECT / WITH / PRAGMA / EXPLAIN）。")
    rows = store.query(cfg, args.query)
    for row in rows:
        row.pop("raw", None)
    if rows and not args.json:
        headers = list(rows[0])
        widths = [max(len(h), max(len(str(r[h])) for r in rows)) for h in headers]
        human = "  ".join(h.ljust(w) for h, w in zip(headers, widths)) + "\n"
        human += "\n".join("  ".join(str(r[h]).ljust(w) for h, w in zip(headers, widths))
                           for r in rows[:200])
    else:
        human = "没有结果。"
    emit({"rows": rows, "count": len(rows)}, args, human)
    return 0


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------

def cmd_sync(args: argparse.Namespace) -> int:
    cfg = Config.load()
    require_init(cfg)
    result: dict[str, Any] = {}
    if not args.push_only:
        result["pull"] = sync.pull(cfg)
    if not args.pull_only:
        store.write_manifest(cfg)
        result["push"] = sync.commit_and_push(cfg, args.message or "sync: journal update")
    result["status"] = sync.check(cfg)
    state = read_state(cfg)
    state["last_sync"] = now_iso()
    write_state(cfg, state)
    emit(result, args,
         f"{'拉取：变更已合并' if result.get('pull', {}).get('changed') else '拉取：无变更'}\n"
         f"推送：{result.get('push', {}).get('message') or ('已推送' if result.get('push', {}).get('pushed') else '无需提交')}\n"
         f"{result['status'].get('message')}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    cfg = Config.load()
    if args.set:
        for pair in args.set:
            key, _, value = pair.partition("=")
            key = key.strip()
            if not hasattr(cfg, key):
                raise CliError(f"未知配置项：{key}")
            current = getattr(cfg, key)
            if isinstance(current, bool):
                setattr(cfg, key, value.strip().lower() in {"1", "true", "yes", "on"})
            elif isinstance(current, int) and not isinstance(current, bool):
                setattr(cfg, key, int(value))
            else:
                setattr(cfg, key, value.strip())
        cfg.save()
    from dataclasses import asdict
    data = asdict(cfg)
    emit(data, args, "\n".join(f"{k:<24} {v}" for k, v in data.items()))
    return 0


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------

def _add_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--since", help="30d / 3m / 1y / ytd / mtd / 2026-01-01")
    parser.add_argument("--until", help="截止时间，同样支持相对写法")
    parser.add_argument("--symbol", "-s", help="标的（期权会同时匹配 underlying）")
    parser.add_argument("--account")
    parser.add_argument("--broker")
    parser.add_argument("--strategy")
    parser.add_argument("--tag")


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--no-sync", action="store_true",
                        help="跳过远端变动检查（离线时用）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tradegit", description="GitHub 私有仓库交易日志")
    parser.add_argument("--version", action="version", version=f"tradegit {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="连接 GitHub 并创建私有交易日志仓库")
    p.add_argument("--repo", help="owner/name；默认 <你的账号>/trading-journal")
    p.add_argument("--name", default="trading-journal", help="仓库名（默认 trading-journal）")
    p.add_argument("--account", help="默认账户名")
    p.add_argument("--use-existing", action="store_true", help="复用已存在的仓库")
    p.add_argument("--force", action="store_true", help="删除本地副本并重新克隆")
    _add_common(p)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("status", help="本地与私有仓库的同步状态")
    _add_common(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("check", help="检查私有仓库是否有本地没有的变动")
    p.add_argument("--pull", action="store_true", help="发现变动就直接拉取")
    _add_common(p)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("doctor", help="环境自检")
    _add_common(p)
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("log", help="记录一笔交易")
    p.add_argument("--symbol", "-s")
    p.add_argument("--side", help="BUY / SELL / SHORT / COVER")
    p.add_argument("--qty", "--quantity", dest="qty")
    p.add_argument("--price")
    p.add_argument("--at", "--ts", dest="at", help="成交时间，默认现在")
    p.add_argument("--why", "--thesis", dest="why", help="交易理由")
    p.add_argument("--strategy")
    p.add_argument("--setup")
    p.add_argument("--stop")
    p.add_argument("--target")
    p.add_argument("--conviction", type=int, help="1-5")
    p.add_argument("--tags")
    p.add_argument("--notes")
    p.add_argument("--emotions")
    p.add_argument("--exit-plan", dest="exit_plan")
    p.add_argument("--market-context", dest="market_context")
    p.add_argument("--review")
    p.add_argument("--fees", help="数字或 JSON，如 '{\"commission\":1,\"regulatory\":0.02}'")
    p.add_argument("--account")
    p.add_argument("--broker")
    p.add_argument("--asset-class", dest="asset_class", help="STK/ETF/OPT/FUT/FX/CRYPTO")
    p.add_argument("--currency")
    p.add_argument("--multiplier")
    p.add_argument("--json-input", "--record", dest="json_input",
                   help="JSON 对象/数组，或文件路径，或 - 表示 stdin")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-push", action="store_true")
    p.add_argument("--allow-duplicates", action="store_true")
    _add_common(p)
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("import", help="导入券商流水（IBKR / 嘉信 / 通用 CSV / JSON）")
    p.add_argument("--file", "-f", required=True)
    p.add_argument("--broker", help="ibkr / schwab / generic；默认自动识别")
    p.add_argument("--account")
    p.add_argument("--dry-run", action="store_true", help="只预览，不写入（建议先跑一次）")
    p.add_argument("--no-push", action="store_true")
    p.add_argument("--keep-source", action="store_true", help="把原始文件留一份到本地")
    _add_common(p)
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("amend", help="更正一条记录（追加新版本，不改历史）")
    p.add_argument("id")
    p.add_argument("--set", help="JSON 补丁")
    p.add_argument("--why", "--thesis", dest="why")
    p.add_argument("--review", help="事后复盘")
    p.add_argument("--mistake")
    p.add_argument("--strategy")
    p.add_argument("--setup")
    p.add_argument("--notes")
    p.add_argument("--emotions")
    p.add_argument("--tags")
    p.add_argument("--conviction", type=int)
    p.add_argument("--price")
    p.add_argument("--qty", dest="qty")
    p.add_argument("--stop")
    p.add_argument("--target")
    p.add_argument("--no-push", action="store_true")
    _add_common(p)
    p.set_defaults(func=cmd_amend)

    p = sub.add_parser("void", help="作废一条记录")
    p.add_argument("id")
    p.add_argument("--reason")
    p.add_argument("--no-push", action="store_true")
    _add_common(p)
    p.set_defaults(func=cmd_void)

    p = sub.add_parser("list", help="列出记录")
    _add_filters(p)
    p.add_argument("--kind", choices=["trade", "cash"])
    p.add_argument("--limit", type=int, default=50)
    _add_common(p)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("positions", help="当前持仓（FIFO）")
    _add_filters(p)
    p.add_argument("--mark", action="append", help="AAPL=213.4，可重复，用于算浮动盈亏")
    _add_common(p)
    p.set_defaults(func=cmd_positions)

    p = sub.add_parser("analyze", help="盈亏与复盘分析")
    _add_filters(p)
    p.add_argument("--group-by", choices=["symbol", "month", "strategy", "tag", "direction"])
    p.add_argument("--mark", action="append")
    _add_common(p)
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("roundtrips", help="平仓明细（FIFO 配对）")
    _add_filters(p)
    p.add_argument("--sort", choices=["time", "pnl", "return"], default="time")
    p.add_argument("--limit", type=int)
    _add_common(p)
    p.set_defaults(func=cmd_roundtrips)

    p = sub.add_parser("sql", help="对本地索引跑只读 SQL（表名 trades）")
    p.add_argument("query")
    _add_common(p)
    p.set_defaults(func=cmd_sql)

    p = sub.add_parser("sync", help="与私有仓库同步")
    p.add_argument("--pull-only", action="store_true")
    p.add_argument("--push-only", action="store_true")
    p.add_argument("--message", "-m")
    _add_common(p)
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("config", help="查看/修改配置")
    p.add_argument("--set", action="append", metavar="KEY=VALUE")
    _add_common(p)
    p.set_defaults(func=cmd_config)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (CliError, sync.SyncError, ValidationError, ValueError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"error": str(exc), "command": args.command},
                             ensure_ascii=False, indent=2))
        else:
            print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
