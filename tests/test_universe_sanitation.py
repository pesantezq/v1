"""
Tests for portfolio_automation/universe_sanitation.py.

Covers:
  - Empty inputs degrade safely (empty payload, no error)
  - Each input source contributes distinct tickers
  - Ranking puts multi-source tickers above single-source tickers
  - Top-N cap is honoured deterministically
  - Cadence builders produce distinct lookback windows
"""
from __future__ import annotations

import csv
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_automation.universe_sanitation import (
    _TOP_N,
    _aggregate_universe,
    _load_sector,
    _rank_candidates,
    build_top100_daily,
    build_top100_monthly,
    build_top100_weekly,
    run_universe_sanitation,
)


class TestLoadSectorEtfNormalization(unittest.TestCase):
    """_load_sector must normalize funds so the sector: rationale tag isn't
    contaminated by FMP's issuer sector ("Financial Services / Asset Management").
    """

    def _write_profile(self, root, ticker, sector, *, is_etf=False, is_fund=False):
        p = root / "data" / "fmp_cache" / f"profile_stable_{ticker}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"data": [{
            "symbol": ticker, "sector": sector, "isEtf": is_etf, "isFund": is_fund,
        }]}))

    def test_broad_etf_buckets_as_etf_index(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_profile(root, "QQQ", "Financial Services", is_etf=True)
            self.assertEqual(_load_sector(root, "QQQ"), "ETF/Index")

    def test_sector_spdr_maps_to_exposure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_profile(root, "XLE", "Financial Services", is_etf=True)
            self.assertEqual(_load_sector(root, "XLE"), "Energy")

    def test_equity_sector_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_profile(root, "NVDA", "Technology")
            self.assertEqual(_load_sector(root, "NVDA"), "Technology")

    def test_missing_profile_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(_load_sector(Path(td), "NOPE"), "Unknown")


def _write_config(root: Path, static_watchlist: list[str]) -> None:
    cfg = {"watchlist_scanner": {"watchlist": static_watchlist}}
    (root / "config.json").write_text(json.dumps(cfg))


def _write_extended_active(root: Path, rows: list[dict]) -> None:
    db = root / "data" / "portfolio.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE extended_watchlist (
            symbol TEXT PRIMARY KEY,
            is_active INTEGER NOT NULL DEFAULT 1,
            promoted_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            last_reinforced TEXT NOT NULL,
            theme_name TEXT NOT NULL,
            theme_names TEXT NOT NULL DEFAULT '[]',
            theme_confidence REAL NOT NULL,
            mention_count INTEGER NOT NULL DEFAULT 1,
            scan_count INTEGER NOT NULL DEFAULT 0,
            alert_count INTEGER NOT NULL DEFAULT 0,
            outcome TEXT NOT NULL DEFAULT 'none',
            drop_reason TEXT
        )
    """)
    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        conn.execute(
            "INSERT INTO extended_watchlist "
            "(symbol, is_active, promoted_at, expires_at, last_reinforced, "
            "theme_name, theme_confidence) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (r["symbol"], 1, now, now, now, r.get("theme_name", "x"),
             float(r.get("theme_confidence", 0.8)))
        )
    conn.commit()
    conn.close()


def _write_theme_candidates(root: Path, candidates: list[dict]) -> None:
    p = root / "outputs" / "latest" / "watch_candidates.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(candidates))


def _write_fmp_top100(root: Path, symbols: list[str]) -> None:
    p = root / "data" / "fmp_cache" / "top100_watchlist.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "watchlist_source": "fmp",
        "candidates": [
            {"symbol": s, "score": 0.5, "watchlist_source": "fmp"} for s in symbols
        ],
    }))


def _write_signal_outcomes(root: Path, rows: list[dict]) -> None:
    p = root / "outputs" / "performance" / "signal_outcomes.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    cols = [
        "ticker", "signal_time", "outcome_return_1d", "direction_correct_1d",
    ]
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


class TestEmptyInputs(unittest.TestCase):
    def test_no_config_no_crash(self):
        with tempfile.TemporaryDirectory() as td:
            r = run_universe_sanitation(root=Path(td), cadence="daily")
            self.assertEqual(r["status"], "ok")
            self.assertEqual(r["total_distinct_tickers"], 0)
            self.assertEqual(r["top_count"], 0)


class TestSourceContribution(unittest.TestCase):
    def test_static_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, ["AAPL", "MSFT"])
            by = _aggregate_universe(root, lookback_days=1)
            self.assertEqual(set(by.keys()), {"AAPL", "MSFT"})
            for sym, rec in by.items():
                self.assertEqual(rec["sources"], ["static"])

    def test_all_sources_distinct_tickers(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, ["AAPL"])
            _write_extended_active(root, [{"symbol": "CRWD", "theme_confidence": 0.85}])
            _write_theme_candidates(root, [{"ticker": "NVDA", "confidence": 0.9}])
            _write_fmp_top100(root, ["AMD"])
            _write_signal_outcomes(root, [
                {"ticker": "TSLA",
                 "signal_time": datetime.now(timezone.utc).isoformat(),
                 "outcome_return_1d": "0.02", "direction_correct_1d": "1"},
            ])
            by = _aggregate_universe(root, lookback_days=1)
            self.assertEqual(set(by.keys()), {"AAPL", "CRWD", "NVDA", "AMD", "TSLA"})

    def test_overlap_ticker_aggregates_sources(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, ["NVDA"])
            _write_fmp_top100(root, ["NVDA"])
            _write_theme_candidates(root, [{"ticker": "NVDA", "confidence": 0.92}])
            by = _aggregate_universe(root, lookback_days=1)
            self.assertEqual(len(by), 1)
            srcs = set(by["NVDA"]["sources"])
            self.assertIn("static", srcs)
            self.assertIn("fmp_top100", srcs)
            self.assertIn("theme_candidate", srcs)
            self.assertEqual(by["NVDA"]["theme_confidence"], 0.92)


class TestRanking(unittest.TestCase):
    def test_multi_source_outranks_single_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, ["NVDA", "ZZZ"])  # ZZZ is static-only
            _write_fmp_top100(root, ["NVDA"])
            _write_theme_candidates(root, [{"ticker": "NVDA", "confidence": 0.9}])
            by = _aggregate_universe(root, lookback_days=1)
            ranked = _rank_candidates(by, root)
            self.assertEqual(ranked[0]["symbol"], "NVDA")
            self.assertEqual(ranked[1]["symbol"], "ZZZ")

    def test_top_n_cap_enforced(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # Create 150 static tickers
            syms = [f"T{i:03d}" for i in range(150)]
            _write_config(root, syms)
            by = _aggregate_universe(root, lookback_days=1)
            ranked = _rank_candidates(by, root)
            self.assertEqual(len(ranked), _TOP_N)
            # rank field populated
            self.assertEqual(ranked[0]["rank"], 1)
            self.assertEqual(ranked[-1]["rank"], _TOP_N)


class TestCadenceBuilders(unittest.TestCase):
    def test_daily_lookback_is_1(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, ["AAPL"])
            payload = build_top100_daily(root)
            self.assertEqual(payload["cadence"], "daily")
            self.assertEqual(payload["lookback_days"], 1)

    def test_weekly_lookback_is_7(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, ["AAPL"])
            payload = build_top100_weekly(root)
            self.assertEqual(payload["cadence"], "weekly")
            self.assertEqual(payload["lookback_days"], 7)

    def test_monthly_lookback_is_30(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, ["AAPL"])
            payload = build_top100_monthly(root)
            self.assertEqual(payload["cadence"], "monthly")
            self.assertEqual(payload["lookback_days"], 30)


class TestArtifactWriting(unittest.TestCase):
    def test_run_writes_both_json_and_md(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, ["AAPL", "MSFT"])
            r = run_universe_sanitation(root=root, cadence="daily", write_files=True)
            self.assertEqual(r["status"], "ok")
            self.assertTrue((root / "outputs" / "latest" / "top100_daily.json").exists())
            self.assertTrue((root / "outputs" / "latest" / "top100_daily.md").exists())

    def test_unknown_cadence_returns_error(self):
        with tempfile.TemporaryDirectory() as td:
            r = run_universe_sanitation(root=Path(td), cadence="hourly")
            self.assertEqual(r["status"], "error")
            self.assertIn("unknown_cadence", r["error"])


class TestRationaleEnrichment(unittest.TestCase):
    """Each top100 row carries reason + rationale_tags + contributing_signals."""

    def test_row_has_required_rationale_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, ["AAPL"])
            payload = build_top100_daily(root)
            row = payload["candidates"][0]
            self.assertIn("reason", row)
            self.assertIn("rationale_tags", row)
            self.assertIn("contributing_signals", row)
            self.assertIsInstance(row["rationale_tags"], list)
            self.assertIsInstance(row["contributing_signals"], dict)

    def test_net_new_discovery_tag_when_theme_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, ["AAPL"])  # AAPL is static-only
            _write_theme_candidates(root, [
                {"ticker": "CRWD", "confidence": 0.85, "themes": ["Cybersecurity"]},
            ])
            payload = build_top100_daily(root)
            crwd = next(r for r in payload["candidates"] if r["symbol"] == "CRWD")
            self.assertIn("net_new_discovery", crwd["rationale_tags"])
            self.assertNotIn("net_new_discovery",
                             next(r for r in payload["candidates"] if r["symbol"] == "AAPL")["rationale_tags"])

    def test_multi_source_confluence_tag(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, ["NVDA"])
            _write_fmp_top100(root, ["NVDA"])
            _write_theme_candidates(root, [{"ticker": "NVDA", "confidence": 0.9, "themes": ["AI Infrastructure"]}])
            payload = build_top100_daily(root)
            nvda = next(r for r in payload["candidates"] if r["symbol"] == "NVDA")
            self.assertIn("multi_source_confluence", nvda["rationale_tags"])
            self.assertIn("high_theme_confidence", nvda["rationale_tags"])
            # contributing_signals carries the theme name
            self.assertIn("theme_candidate", nvda["contributing_signals"])
            self.assertIn("AI Infrastructure", nvda["contributing_signals"]["theme_candidate"])


class TestSignalOutcomesLookback(unittest.TestCase):
    """Lookback must filter older signals out of the recent_signal source."""

    def test_old_signals_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, [])
            old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
            recent_ts = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
            _write_signal_outcomes(root, [
                {"ticker": "OLD", "signal_time": old_ts,
                 "outcome_return_1d": "0.01", "direction_correct_1d": "1"},
                {"ticker": "NEW", "signal_time": recent_ts,
                 "outcome_return_1d": "0.02", "direction_correct_1d": "1"},
            ])
            # daily lookback (1 day) → only NEW counts
            by_daily = _aggregate_universe(root, lookback_days=1)
            self.assertIn("NEW", by_daily)
            self.assertNotIn("OLD", by_daily)
            # weekly lookback (7 days) → still excludes OLD (10 days old)
            by_weekly = _aggregate_universe(root, lookback_days=7)
            self.assertNotIn("OLD", by_weekly)
            # monthly lookback (30 days) → OLD now counts
            by_monthly = _aggregate_universe(root, lookback_days=30)
            self.assertIn("OLD", by_monthly)


if __name__ == "__main__":
    unittest.main(verbosity=2)
