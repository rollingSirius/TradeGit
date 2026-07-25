"""Test suite. Run with: python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT))

from tradegit import analytics, importers, store  # noqa: E402
from tradegit.config import Config  # noqa: E402
from tradegit.schema import ValidationError, normalize, resolve  # noqa: E402


def trade(symbol, side, qty, price, ts, **kw):
    return normalize({"symbol": symbol, "side": side, "quantity": qty,
                      "price": price, "ts": ts, **kw})


# ---------------------------------------------------------------------------


class TestSchema(unittest.TestCase):
    def test_normalizes_and_derives(self):
        record = trade("aapl", "buy", "100", "213.45", "2026-05-04T09:31:12Z",
                       fees=1.0025, stop=205, target=240, tags="tech, earnings",
                       thesis="突破前高")
        self.assertEqual(record["symbol"], "AAPL")
        self.assertEqual(record["side"], "BUY")
        self.assertEqual(record["gross_amount"], 21345.0)
        self.assertEqual(record["signed_quantity"], 100)
        self.assertAlmostEqual(record["net_amount"], -21346.0025, places=4)
        self.assertEqual(record["tags"], ["earnings", "tech"])
        self.assertAlmostEqual(record["risk"]["risk_amount"], 845.0, places=2)
        # (240 - 213.45) / (213.45 - 205)
        self.assertAlmostEqual(record["risk"]["planned_r"], 3.142, places=3)
        self.assertTrue(record["id"].startswith("trd_20260504T093112Z_AAPL_"))

    def test_broker_number_formats(self):
        record = trade("MSFT", "SELL", "40", "$505.20", "06/20/2026",
                       fees="($0.02)")
        self.assertEqual(record["price"], 505.2)
        self.assertEqual(record["fees_total"], 0.02)
        self.assertEqual(record["ts"], "2026-06-20T00:00:00Z")

    def test_option_multiplier_and_osi_parsing(self):
        record = trade("TSLA  260619C00300000", "BUY", 5, 12.4,
                       "2026-05-28T11:00:00Z", asset_class="OPT")
        self.assertEqual(record["multiplier"], 100)
        self.assertEqual(record["gross_amount"], 6200.0)
        self.assertEqual(record["option"]["underlying"], "TSLA")
        self.assertEqual(record["option"]["expiry"], "2026-06-19")
        self.assertEqual(record["option"]["strike"], 300.0)
        self.assertEqual(record["option"]["right"], "C")

    def test_rejects_incomplete_records(self):
        for bad in ({"symbol": "AAPL", "side": "BUY", "quantity": 1},
                    {"symbol": "AAPL", "side": "HOLD", "quantity": 1, "price": 1,
                     "ts": "2026-01-01"},
                    {"side": "BUY", "quantity": 1, "price": 1, "ts": "2026-01-01"}):
            with self.assertRaises(ValidationError):
                normalize({"ts": "2026-01-01T00:00:00Z", **bad})

    def test_dedup_key_is_content_stable(self):
        a = trade("AAPL", "BUY", 100, 213.45, "2026-05-04T09:31:12Z")
        b = trade("AAPL", "BUY", 100, 213.45, "2026-05-04T09:31:12Z", thesis="different")
        self.assertEqual(a["dedup_key"], b["dedup_key"])
        c = trade("AAPL", "BUY", 101, 213.45, "2026-05-04T09:31:12Z")
        self.assertNotEqual(a["dedup_key"], c["dedup_key"])

    def test_resolve_applies_amend_and_void(self):
        first = trade("AAPL", "BUY", 100, 213.45, "2026-05-04T09:31:12Z")
        amended = trade("AAPL", "BUY", 100, 213.50, "2026-05-04T09:31:12Z",
                        supersedes=first["id"])
        second = trade("NVDA", "BUY", 10, 1180.5, "2026-05-20T10:15:00Z")
        void = normalize({"kind": "void", "voids": second["id"],
                          "ts": "2026-05-21T00:00:00Z", "symbol": "NVDA"})
        live = resolve([first, amended, second, void])
        self.assertEqual([r["id"] for r in live], [amended["id"]])
        self.assertEqual(live[0]["price"], 213.50)


class TestAnalytics(unittest.TestCase):
    def test_long_roundtrip_pnl_net_of_fees(self):
        records = [
            trade("AAPL", "BUY", 100, 213.45, "2026-05-04T09:31:12Z", fees=1.0025),
            trade("AAPL", "SELL", 100, 229.80, "2026-06-12T14:02:44Z", fees=1.0031),
        ]
        trips = analytics.match_fifo(records)["roundtrips"]
        self.assertEqual(len(trips), 1)
        self.assertAlmostEqual(trips[0]["net_pnl"], 1632.9944, places=4)
        self.assertEqual(trips[0]["direction"], "long")
        self.assertAlmostEqual(trips[0]["hold_days"], 39.19, places=1)

    def test_short_roundtrip_profits_when_price_falls(self):
        records = [
            trade("GME", "SHORT", 100, 28.50, "2026-06-10T10:15:00Z", fees=1),
            trade("GME", "COVER", 100, 22.10, "2026-06-20T14:00:00Z", fees=1),
        ]
        trips = analytics.match_fifo(records)["roundtrips"]
        self.assertEqual(trips[0]["direction"], "short")
        self.assertAlmostEqual(trips[0]["net_pnl"], 638.0, places=6)

    def test_partial_exits_split_fifo_lots(self):
        records = [
            trade("SPY", "BUY", 100, 500, "2026-01-02T00:00:00Z"),
            trade("SPY", "BUY", 100, 520, "2026-02-02T00:00:00Z"),
            trade("SPY", "SELL", 150, 540, "2026-03-02T00:00:00Z"),
        ]
        result = analytics.match_fifo(records)
        trips = result["roundtrips"]
        self.assertEqual(len(trips), 2)
        self.assertAlmostEqual(trips[0]["net_pnl"], 4000.0)   # 100 @ 500 -> 540
        self.assertAlmostEqual(trips[1]["net_pnl"], 1000.0)   # 50  @ 520 -> 540
        self.assertEqual(len(result["open_positions"]), 1)
        self.assertAlmostEqual(result["open_positions"][0]["quantity"], 50.0)
        self.assertAlmostEqual(result["open_positions"][0]["avg_price"], 520.0)

    def test_position_flip_opens_the_residual_short(self):
        records = [
            trade("F", "BUY", 100, 12, "2026-01-02T00:00:00Z"),
            trade("F", "SELL", 250, 15, "2026-01-09T00:00:00Z"),
        ]
        result = analytics.match_fifo(records)
        self.assertAlmostEqual(result["roundtrips"][0]["net_pnl"], 300.0)
        position = result["open_positions"][0]
        self.assertEqual(position["direction"], "short")
        self.assertAlmostEqual(position["quantity"], -150.0)

    def test_option_pnl_uses_the_contract_multiplier(self):
        records = [
            trade("TSLA  260619C00300000", "BUY", 5, 12.4, "2026-05-28T11:00:00Z",
                  asset_class="OPT", fees=3.25),
            trade("TSLA  260619C00300000", "SELL", 5, 18.9, "2026-06-10T13:20:00Z",
                  asset_class="OPT", fees=3.25),
        ]
        trips = analytics.match_fifo(records)["roundtrips"]
        self.assertAlmostEqual(trips[0]["net_pnl"], 3243.5, places=4)

    def test_r_multiple_from_the_recorded_stop(self):
        records = [
            trade("AAPL", "BUY", 100, 200, "2026-01-02T00:00:00Z", stop=190),
            trade("AAPL", "SELL", 100, 230, "2026-02-02T00:00:00Z"),
        ]
        self.assertAlmostEqual(
            analytics.match_fifo(records)["roundtrips"][0]["r_multiple"], 3.0)

    def test_summary_metrics_and_cash_events(self):
        records = [
            trade("A", "BUY", 10, 100, "2026-01-02T00:00:00Z"),
            trade("A", "SELL", 10, 110, "2026-01-12T00:00:00Z"),   # +100
            trade("B", "BUY", 10, 100, "2026-02-02T00:00:00Z"),
            trade("B", "SELL", 10, 95, "2026-02-12T00:00:00Z"),    # -50
            normalize({"kind": "cash", "cash_type": "DIVIDEND", "amount": 26,
                       "ts": "2026-01-15T00:00:00Z", "symbol": "A"}),
            normalize({"kind": "cash", "cash_type": "DEPOSIT", "amount": 50000,
                       "ts": "2026-01-01T00:00:00Z", "symbol": ""}),
        ]
        result = analytics.summarize(records)
        m = result["metrics"]
        self.assertEqual(m["roundtrips"], 2)
        self.assertEqual(m["win_rate"], 50.0)
        self.assertAlmostEqual(m["realized_pnl"], 50.0)
        self.assertAlmostEqual(m["profit_factor"], 2.0)
        self.assertAlmostEqual(m["dividends"], 26.0)
        # deposits are cash movements, not performance
        self.assertAlmostEqual(m["cash_events_net"], 26.0)
        self.assertAlmostEqual(m["total_pnl"], 76.0)
        self.assertAlmostEqual(m["max_drawdown"], -50.0)
        self.assertEqual(result["by_symbol"][0]["key"], "B")

    def test_unrealized_needs_marks(self):
        records = [trade("AAPL", "BUY", 100, 200, "2026-01-02T00:00:00Z")]
        self.assertIsNone(analytics.summarize(records)["metrics"]["unrealized_pnl"])
        marked = analytics.summarize(records, marks={"AAPL": 220})
        self.assertAlmostEqual(marked["metrics"]["unrealized_pnl"], 2000.0)


class TestImporters(unittest.TestCase):
    def test_ibkr_activity_statement(self):
        parsed = importers.parse(FIXTURES / "ibkr_activity.csv")
        self.assertEqual(parsed["detected_broker"], "ibkr")
        records = [normalize(r) for r in parsed["records"]]
        trades = [r for r in records if r["kind"] == "trade"]
        cash = [r for r in records if r["kind"] == "cash"]

        # ClosedLot and SubTotal rows must not become trades.
        self.assertEqual(len(trades), 6)
        self.assertEqual(sorted({r["symbol"] for r in trades}),
                         ["AAPL", "NVDA", "TSLA  260619C00300000"])

        result = analytics.summarize(records)
        by_symbol = {b["key"]: b["net_pnl"] for b in result["by_symbol"]}
        # Matches IBKR's own Realized P/L column in the fixture.
        self.assertAlmostEqual(by_symbol["AAPL"], 1632.9944, places=4)
        self.assertAlmostEqual(by_symbol["NVDA"], -4114.5, places=4)
        self.assertAlmostEqual(by_symbol["TSLA  260619C00300000"], 3243.5, places=4)

        types = {r["cash_type"] for r in cash}
        self.assertEqual(types, {"DIVIDEND", "TAX", "INTEREST", "FEE", "DEPOSIT"})
        self.assertAlmostEqual(
            next(r["amount"] for r in cash if r["cash_type"] == "DIVIDEND"), 26.0)

    def test_ibkr_flex_query(self):
        parsed = importers.parse(FIXTURES / "ibkr_flex.csv")
        records = [normalize(r) for r in parsed["records"]]
        self.assertEqual(len(records), 4)
        self.assertEqual([r["side"] for r in records],
                         ["BUY", "SELL", "SHORT", "COVER"])
        self.assertEqual(records[0]["ts"], "2026-06-01T09:31:05Z")
        self.assertEqual(records[0]["source"]["external_id"], "90001")
        trips = analytics.match_fifo(records)["roundtrips"]
        pnl = {t["symbol"]: t["net_pnl"] for t in trips}
        self.assertAlmostEqual(pnl["SPY"], 3518.0, places=4)
        self.assertAlmostEqual(pnl["GME"], 638.0, places=4)

    def test_schwab_transactions(self):
        parsed = importers.parse(FIXTURES / "schwab_transactions.csv")
        self.assertEqual(parsed["detected_broker"], "schwab")
        records = [normalize(r) for r in parsed["records"]]
        trades = [r for r in records if r["kind"] == "trade"]
        self.assertEqual(len(trades), 6)
        self.assertEqual(records[0]["account"], "individual-xxxx-1234")

        # 'Journaled Shares' cannot be interpreted automatically.
        self.assertTrue(any("Journaled Shares" in s.get("reason", "")
                            for s in parsed["skipped"]))

        option = next(r for r in trades if r["asset_class"] == "OPT")
        self.assertEqual(option["symbol"], "AAPL  260717C00200000")
        self.assertEqual(option["multiplier"], 100)

        # 'as of' date wins over the posting date
        dividend = next(r for r in records if r.get("cash_type") == "DIVIDEND")
        self.assertEqual(dividend["ts"][:10], "2026-06-12")

        result = analytics.summarize(records)
        by_symbol = {b["key"]: b["net_pnl"] for b in result["by_symbol"]}
        self.assertAlmostEqual(by_symbol["MSFT"], 1407.98, places=2)
        self.assertAlmostEqual(by_symbol["AMD"], -2660.03, places=2)
        self.assertAlmostEqual(by_symbol["AAPL  260717C00200000"], 1297.40, places=2)
        self.assertAlmostEqual(result["metrics"]["realized_pnl"], 45.35, places=2)

    def test_generic_csv_with_journal_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "my-trades.csv"
            path.write_text(
                "Trade Date,Ticker,Action,Shares,Fill Price,Commission,Reason,Stop\n"
                "2026-03-02,NVDA,BUY,10,900.50,1.00,回调到20日线,850\n",
                encoding="utf-8")
            parsed = importers.parse(path)
            record = normalize(parsed["records"][0])
            self.assertEqual(record["symbol"], "NVDA")
            self.assertEqual(record["quantity"], 10)
            self.assertEqual(record["thesis"], "回调到20日线")
            self.assertEqual(record["risk"]["stop"], 850.0)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["TRADEGIT_HOME"] = self.tmp.name
        self.cfg = Config.load()
        self.cfg.ensure_dirs()
        self.cfg.journal_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        os.environ.pop("TRADEGIT_HOME", None)
        self.tmp.cleanup()

    def test_append_partitions_by_month_and_dedupes(self):
        a = trade("AAPL", "BUY", 100, 213.45, "2026-05-04T09:31:12Z")
        b = trade("NVDA", "BUY", 10, 1180.5, "2026-06-20T10:15:00Z")
        first = store.append(self.cfg, [a, b])
        self.assertEqual(len(first["written"]), 2)
        self.assertEqual(
            sorted(p.name for p in store.journal_files(self.cfg)),
            ["2026-05.jsonl", "2026-06.jsonl"])

        second = store.append(self.cfg, [a])
        self.assertEqual(len(second["written"]), 0)
        self.assertEqual(len(second["skipped"]), 1)
        self.assertEqual(len(store.load(self.cfg)), 2)

        manifest = json.loads((self.cfg.repo_dir / "manifest.json").read_text())
        self.assertEqual(manifest["total_records"], 2)

    def test_index_refreshes_when_journal_changes(self):
        store.append(self.cfg, [trade("AAPL", "BUY", 1, 100, "2026-05-04T00:00:00Z")])
        self.assertEqual(len(store.select(self.cfg)), 1)
        store.append(self.cfg, [trade("NVDA", "BUY", 1, 100, "2026-05-05T00:00:00Z")])
        self.assertEqual(len(store.select(self.cfg)), 2)
        self.assertEqual(len(store.select(self.cfg, symbol="nvda")), 1)
        self.assertEqual(len(store.select(self.cfg, since="2026-05-05T00:00:00Z")), 1)


class TestCliEndToEnd(unittest.TestCase):
    """Drives the real CLI against a local bare repo standing in for GitHub."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.home = root / "home"
        cls.remote = root / "remote.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(cls.remote)],
                       check=True, capture_output=True)
        repo = cls.home / "repo"
        repo.mkdir(parents=True)
        for args in (["init", "-b", "main"], ["remote", "add", "origin", str(cls.remote)],
                     ["config", "user.email", "t@example.com"],
                     ["config", "user.name", "T"]):
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
        (repo / "journal").mkdir()
        (repo / "journal" / ".gitkeep").write_text("")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True,
                       capture_output=True)
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo, check=True,
                       capture_output=True)
        (cls.home / "config.json").write_text(json.dumps(
            {"repo_slug": "local/test", "repo_url": str(cls.remote),
             "default_account": "test"}))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_cli(self, *args, expect=0):
        proc = subprocess.run(
            [sys.executable, "-m", "tradegit", *args],
            cwd=ROOT, capture_output=True, text=True,
            env={**os.environ, "TRADEGIT_HOME": str(self.home), "PYTHONPATH": str(ROOT)})
        self.assertEqual(proc.returncode, expect,
                         f"args={args}\nstdout={proc.stdout}\nstderr={proc.stderr}")
        return json.loads(proc.stdout) if "--json" in args else proc.stdout

    def test_01_log_commits_and_pushes(self):
        out = self.run_cli("log", "--symbol", "AAPL", "--side", "BUY", "--qty", "100",
                           "--price", "213.45", "--at", "2026-05-04T09:31:12Z",
                           "--why", "突破前高，量能配合", "--stop", "205",
                           "--tags", "tech,breakout", "--json")
        self.assertEqual(out["written"], 1)
        self.assertTrue(out["push"]["pushed"])
        self.assertEqual(out["records"][0]["thesis"], "突破前高，量能配合")

    def test_02_duplicate_log_is_a_noop(self):
        out = self.run_cli("log", "--symbol", "AAPL", "--side", "BUY", "--qty", "100",
                           "--price", "213.45", "--at", "2026-05-04T09:31:12Z", "--json")
        self.assertEqual(out["written"], 0)
        self.assertEqual(out["skipped"], 1)

    def test_03_import_dry_run_then_write(self):
        preview = self.run_cli("import", "--file", str(FIXTURES / "schwab_transactions.csv"),
                               "--dry-run", "--json")
        self.assertEqual(preview["broker"], "schwab")
        self.assertGreater(preview["new"], 0)
        self.assertEqual(preview.get("written"), None)

        done = self.run_cli("import", "--file", str(FIXTURES / "schwab_transactions.csv"),
                            "--json")
        self.assertEqual(done["written"], preview["new"])

        again = self.run_cli("import", "--file", str(FIXTURES / "schwab_transactions.csv"),
                             "--json")
        self.assertEqual(again["written"], 0)
        self.assertEqual(again["duplicates"], preview["new"])

    def test_04_analyze_and_positions(self):
        out = self.run_cli("analyze", "--json")
        self.assertAlmostEqual(out["metrics"]["realized_pnl"], 45.35, places=2)
        self.assertEqual(out["sync"]["in_sync"], True)

        positions = self.run_cli("positions", "--mark", "AAPL=250", "--json")
        aapl = next(p for p in positions["positions"] if p["symbol"] == "AAPL")
        self.assertAlmostEqual(aapl["unrealized_pnl"], 3655.0, places=2)

    def test_05_amend_supersedes_without_rewriting_history(self):
        listed = self.run_cli("list", "--symbol", "AMD", "--json")
        target = listed["records"][0]["id"]
        out = self.run_cli("amend", target, "--review", "追高买入，没等回调", "--json")
        self.assertEqual(out["updated"]["supersedes"], target)
        after = self.run_cli("list", "--symbol", "AMD", "--json")
        self.assertEqual(len(after["records"]), len(listed["records"]))
        self.assertTrue(any(r.get("review") for r in after["records"]))

    def test_07_remote_change_is_detected_and_pulled(self):
        """Simulate a trade logged from another machine."""
        other = Path(self.tmp.name) / "other"
        subprocess.run(["git", "clone", str(self.remote), str(other)],
                       check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "o@example.com"], cwd=other,
                       check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "O"], cwd=other, check=True,
                       capture_output=True)
        record = trade("TSM", "BUY", 50, 210, "2026-07-01T10:00:00Z")
        target = other / "journal" / "2026" / "2026-07.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        subprocess.run(["git", "add", "-A"], cwd=other, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "other machine"], cwd=other, check=True,
                       capture_output=True)
        subprocess.run(["git", "push"], cwd=other, check=True, capture_output=True)

        status = self.run_cli("check", "--json", expect=1)
        self.assertFalse(status["in_sync"])
        self.assertIn("private repo has changes", status["message"])

        pulled = self.run_cli("check", "--pull", "--json")
        self.assertTrue(pulled["in_sync"])
        listed = self.run_cli("list", "--symbol", "TSM", "--json")
        self.assertEqual(listed["count"], 1)

    def test_08_sql_is_read_only(self):
        rows = self.run_cli("sql", "SELECT symbol, COUNT(*) n FROM trades GROUP BY symbol",
                            "--json")
        self.assertTrue(rows["count"] >= 3)
        self.run_cli("sql", "DELETE FROM trades", "--json", expect=2)

    def test_10_sync_pushes_commits_left_behind_by_no_push(self):
        out = self.run_cli("log", "--symbol", "KO", "--side", "BUY", "--qty", "10",
                           "--price", "70", "--at", "2026-07-20T00:00:00Z",
                           "--no-push", "--json")
        self.assertTrue(out["push"]["committed"])
        self.assertFalse(out["push"]["pushed"])
        # There is nothing new to commit, but there IS something to push.
        synced = self.run_cli("sync", "--json")
        self.assertFalse(synced["push"]["committed"])
        self.assertTrue(synced["push"]["pushed"])
        self.assertTrue(self.run_cli("check", "--json")["in_sync"])

    def test_09_status_reports_the_repo(self):
        out = self.run_cli("status", "--json")
        self.assertTrue(out["initialized"])
        self.assertGreater(out["records"], 5)
        self.assertTrue(out["sync"]["in_sync"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
