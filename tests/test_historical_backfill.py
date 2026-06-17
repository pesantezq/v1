"""
Tests for portfolio_automation/historical_backfill.py.

Covers:
  - build_universe combines static + extended_watchlist + top100 sources
  - is_archive_fresh respects mtime + max_age_days
  - Fresh archives skipped on subsequent runs
  - Budget-exhausted client triggers skipped_budget
  - Stub FMP client integration (per-ticker rows persisted to HISTORICAL namespace)
  - Per-ticker errors don't poison the run
  - Status artifact written to outputs/latest/
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_automation.historical_backfill import (
    archive_path_for,
    build_universe,
    is_archive_fresh,
    run_historical_backfill,
)


class _StubFMPClient:
    """Minimal stand-in for FMPClient — used so tests don't hit FMP."""

    def __init__(self, *, per_symbol_rows=None, fail_symbols=None,
                 over_budget=False):
        self._rows = per_symbol_rows or {}
        self._fail = set(fail_symbols or [])

        class _Counter:
            def __init__(self, exceed): self._x = exceed
            def would_exceed(self, budget):
                return self._x

        self._counter = _Counter(over_budget)
        self._budget = 500

    def get_historical_prices(self, symbol, years=5):
        if symbol in self._fail:
            raise RuntimeError(f"simulated fail for {symbol}")
        return self._rows.get(symbol, [])


def _write_config(root: Path, static_watchlist: list[str]) -> None:
    (root / "config.json").write_text(json.dumps({
        "watchlist_scanner": {"watchlist": static_watchlist},
        "api_limits": {"fmp_daily_calls_budget": 500},
    }))


def _write_universe_lists(root: Path, *, broad: list[str], sector: list[str]) -> None:
    (root / "config").mkdir(parents=True, exist_ok=True)
    lines = ["broad_market_etfs: [" + ", ".join(broad) + "]",
             "sector_etfs: [" + ", ".join(sector) + "]"]
    (root / "config" / "universe_lists.yaml").write_text("\n".join(lines) + "\n")


def _write_extended_active(root: Path, symbols: list[str]) -> None:
    db_path = root / "data" / "portfolio.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE extended_watchlist (
            symbol TEXT PRIMARY KEY,
            is_active INTEGER NOT NULL DEFAULT 1,
            promoted_at TEXT,
            expires_at TEXT,
            last_reinforced TEXT,
            theme_name TEXT,
            theme_confidence REAL
        )
    """)
    for s in symbols:
        conn.execute(
            "INSERT INTO extended_watchlist (symbol, is_active, promoted_at, "
            "expires_at, last_reinforced, theme_name, theme_confidence) "
            "VALUES (?, 1, ?, ?, ?, ?, ?)",
            (s, "x", "x", "x", "t", 0.85),
        )
    conn.commit()
    conn.close()


class TestUniverseBuilding(unittest.TestCase):
    def test_static_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, ["AAPL", "MSFT"])
            self.assertEqual(build_universe(root), ["AAPL", "MSFT"])

    def test_union_static_and_extended(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, ["AAPL"])
            _write_extended_active(root, ["CRWD", "PANW"])
            uni = build_universe(root)
            self.assertEqual(set(uni), {"AAPL", "CRWD", "PANW"})

    def test_dedup_across_sources(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, ["NVDA"])
            _write_extended_active(root, ["NVDA"])  # duplicate
            self.assertEqual(build_universe(root), ["NVDA"])

    def test_no_config_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(build_universe(Path(td)), [])

    def test_includes_simulation_price_universe(self):
        """The backfill universe must cover the simulation suite's price universe
        (broad/sector ETFs declared in config/universe_lists.yaml). Regression for
        the 2026-06-17 missing_price_history:XLI walk-forward warning — XLI is
        requested by a sim tactic but was never in the watchlist/top100 universe,
        so its 5y archive was never fetched."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, ["AAPL"])
            _write_universe_lists(root, broad=["SPY"], sector=["XLI", "XLF"])
            uni = build_universe(root)
            self.assertIn("XLI", uni)   # the symbol that was dropping
            self.assertIn("XLF", uni)
            self.assertIn("SPY", uni)
            self.assertIn("AAPL", uni)  # watchlist source still honored

    def test_universe_lists_absent_is_failsafe(self):
        """No universe_lists.yaml → behavior unchanged (watchlist-only)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, ["AAPL", "MSFT"])
            self.assertEqual(build_universe(root), ["AAPL", "MSFT"])


class TestArchiveFreshness(unittest.TestCase):
    def test_missing_archive_not_fresh(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(is_archive_fresh(Path(td) / "missing.json"))

    def test_recent_archive_is_fresh(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.json"
            p.write_text("{}")
            self.assertTrue(is_archive_fresh(p, max_age_days=7))

    def test_aged_archive_not_fresh(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.json"
            p.write_text("{}")
            # Backdate the mtime by 10 days
            ten_days_ago = time.time() - 10 * 86400
            os.utime(p, (ten_days_ago, ten_days_ago))
            self.assertFalse(is_archive_fresh(p, max_age_days=7))


class TestBackfillRun(unittest.TestCase):
    def test_writes_archives_and_status(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, ["AAPL", "MSFT"])
            fmp = _StubFMPClient(per_symbol_rows={
                "AAPL": [{"date": "2026-05-28", "close": 310.85}],
                "MSFT": [{"date": "2026-05-28", "close": 412.50}],
            })
            r = run_historical_backfill(root=root, fmp_client=fmp)
            self.assertEqual(r["fetched"], 2)
            self.assertEqual(r["errored"], 0)
            for sym in ("AAPL", "MSFT"):
                p = archive_path_for(root, sym)
                self.assertTrue(p.exists(), f"archive missing for {sym}")
                d = json.loads(p.read_text())
                self.assertEqual(d["symbol"], sym)
                self.assertEqual(d["row_count"], 1)
            # Status artifact
            status = root / "outputs" / "latest" / "historical_backfill_status.json"
            self.assertTrue(status.exists())

    def test_fresh_archives_are_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, ["AAPL"])
            fmp = _StubFMPClient(per_symbol_rows={
                "AAPL": [{"date": "2026-05-28", "close": 310.85}],
            })
            # Pre-create a fresh archive
            p = archive_path_for(root, "AAPL")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"symbol": "AAPL", "row_count": 1, "rows": []}))

            r = run_historical_backfill(root=root, fmp_client=fmp)
            self.assertEqual(r["fetched"], 0)
            self.assertEqual(r["skipped_fresh"], 1)

    def test_force_bypasses_freshness(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, ["AAPL"])
            fmp = _StubFMPClient(per_symbol_rows={
                "AAPL": [{"date": "2026-05-28", "close": 310.85}],
            })
            p = archive_path_for(root, "AAPL")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"symbol": "AAPL", "row_count": 1, "rows": []}))
            r = run_historical_backfill(root=root, fmp_client=fmp, force=True)
            self.assertEqual(r["fetched"], 1)

    def test_over_budget_client_marks_skipped_budget(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, ["AAPL"])
            fmp = _StubFMPClient(over_budget=True)
            r = run_historical_backfill(root=root, fmp_client=fmp)
            self.assertEqual(r["fetched"], 0)
            self.assertEqual(r["skipped_budget"], 1)
            # No archive written
            self.assertFalse(archive_path_for(root, "AAPL").exists())

    def test_budget_exhaustion_surfaced_not_generic_empty(self):
        """FMP budget exhaustion must read as 'budget_exhausted', not a generic
        empty history — so a spent quota is never mistaken for 'no data exists'."""
        from portfolio_automation.historical_backfill import _budget_exhausted, _fetch_one

        # Over-budget client returns [] for an uncached symbol (the real client's
        # behaviour). _fetch_one must label it budget_exhausted, not empty.
        over = _StubFMPClient(over_budget=True)
        self.assertTrue(_budget_exhausted(over))
        sym, rows, err = _fetch_one(over, "XLB", 5)
        self.assertEqual(err, "budget_exhausted")
        self.assertIsNone(rows)

        # A genuinely-empty history (budget fine) keeps the distinct generic label.
        ok = _StubFMPClient(over_budget=False)
        self.assertFalse(_budget_exhausted(ok))
        sym2, rows2, err2 = _fetch_one(ok, "XLB", 5)
        self.assertEqual(err2, "fmp_returned_empty")
        self.assertEqual(rows2, [])

    def test_per_ticker_error_isolated(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, ["AAPL", "BROKEN", "MSFT"])
            fmp = _StubFMPClient(
                per_symbol_rows={
                    "AAPL": [{"date": "x", "close": 1}],
                    "MSFT": [{"date": "x", "close": 2}],
                },
                fail_symbols={"BROKEN"},
            )
            r = run_historical_backfill(root=root, fmp_client=fmp)
            self.assertEqual(r["fetched"], 2)
            self.assertEqual(r["errored"], 1)
            self.assertTrue(archive_path_for(root, "AAPL").exists())
            self.assertTrue(archive_path_for(root, "MSFT").exists())
            self.assertFalse(archive_path_for(root, "BROKEN").exists())

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, ["AAPL"])
            r = run_historical_backfill(root=root, dry_run=True, fmp_client=None)
            self.assertEqual(r["fetched"], 0)
            self.assertFalse(archive_path_for(root, "AAPL").exists())
            # Status not written in dry_run
            self.assertFalse((root / "outputs" / "latest" / "historical_backfill_status.json").exists())

    def test_max_tickers_caps_universe(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, ["AAA", "BBB", "CCC", "DDD"])
            fmp = _StubFMPClient(per_symbol_rows={
                "AAA": [{"x": 1}], "BBB": [{"x": 1}],
                "CCC": [{"x": 1}], "DDD": [{"x": 1}],
            })
            r = run_historical_backfill(root=root, fmp_client=fmp, max_tickers=2)
            self.assertEqual(r["universe_size"], 2)
            self.assertEqual(r["fetched"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
