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
    _W_FMP_TOP100,
    _W_RECENT_HITRATE,
    _W_SOURCES,
    _W_THEME_CONF,
    _aggregate_universe,
    _diagnose_ranking,
    _load_sector,
    _rank_candidates,
    build_top100_daily,
    build_top100_monthly,
    build_top100_weekly,
    run_universe_sanitation,
)

_SCORE_WEIGHTS = {
    "sources_presence": _W_SOURCES,
    "theme_confidence": _W_THEME_CONF,
    "recent_hit_rate": _W_RECENT_HITRATE,
    "fmp_top100_presence": _W_FMP_TOP100,
}


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


class TestRankingDiagnostics(unittest.TestCase):
    """WS9 fix (2026-07-28): `_diagnose_ranking` must detect a degenerate
    ranking WITHOUT changing any candidate's score/rank/order."""

    def test_empty_candidates_not_degenerate(self):
        diag = _diagnose_ranking([], _SCORE_WEIGHTS)
        self.assertFalse(diag["degenerate_ranking"])
        self.assertEqual(diag["candidate_count"], 0)

    def test_discriminative_ranking_not_degenerate(self):
        cands = [
            {"symbol": "AAA", "score": 0.9, "sources": ["static", "fmp_top100", "theme_candidate"],
             "theme_confidence_max": 0.9, "recent_hit_rate_1d": 0.8},
            {"symbol": "BBB", "score": 0.5, "sources": ["static"],
             "theme_confidence_max": 0.2, "recent_hit_rate_1d": 0.3},
            {"symbol": "CCC", "score": 0.1, "sources": ["theme_candidate"],
             "theme_confidence_max": 0.0, "recent_hit_rate_1d": None},
        ]
        diag = _diagnose_ranking(cands, _SCORE_WEIGHTS)
        self.assertFalse(diag["degenerate_ranking"])
        self.assertFalse(diag["zero_variance"])
        self.assertEqual(diag["zero_information_terms"], [])
        self.assertEqual(diag["distinct_score_count"], 3)

    def test_zero_variance_flagged(self):
        cands = [
            {"symbol": s, "score": 0.16, "sources": ["static"], "theme_confidence_max": 0.0,
             "recent_hit_rate_1d": None}
            for s in ("AAA", "BBB", "CCC")
        ]
        diag = _diagnose_ranking(cands, _SCORE_WEIGHTS)
        self.assertTrue(diag["zero_variance"])
        self.assertTrue(diag["degenerate_ranking"])
        self.assertEqual(diag["largest_tie_fraction"], 1.0)

    def test_majority_tie_bucket_flagged_with_real_ws9_shape(self):
        """Mirrors the real production shape: 17/31 tied at 0.16, the rest
        spread across distinct scores — not zero variance overall, but a
        dominant tie bucket that falls back to alphabetical order."""
        tied = [
            {"symbol": s, "score": 0.16, "sources": ["recent_signal", "static"],
             "theme_confidence_max": 0.0, "recent_hit_rate_1d": None}
            for s in ("MARA", "AAPL", "TSLA", "ZEBRA")  # deliberately unsorted input
        ]
        distinct = [
            {"symbol": "NVDA", "score": 0.9, "sources": ["static", "fmp_top100", "theme_candidate"],
             "theme_confidence_max": 0.9, "recent_hit_rate_1d": 0.7},
            {"symbol": "MSFT", "score": 0.5, "sources": ["static", "fmp_top100"],
             "theme_confidence_max": 0.3, "recent_hit_rate_1d": 0.4},
        ]
        cands = tied + distinct  # 4/6 = 67% tie bucket
        diag = _diagnose_ranking(cands, _SCORE_WEIGHTS)
        self.assertFalse(diag["zero_variance"])
        self.assertTrue(diag["degenerate_ranking"])
        self.assertEqual(diag["largest_tie_group_size"], 4)
        self.assertAlmostEqual(diag["largest_tie_fraction"], 4 / 6, places=3)
        self.assertTrue(diag["alphabetical_tiebreak_detected"])

    def test_zero_information_term_flagged_even_without_tie_majority(self):
        """recent_hit_rate is unresolved (None) for every candidate this run,
        while scores otherwise differ enough to avoid a majority tie — the
        term itself must still be flagged as zero-information."""
        cands = [
            {"symbol": "AAA", "score": 0.40, "sources": ["static"],
             "theme_confidence_max": 0.0, "recent_hit_rate_1d": None},
            {"symbol": "BBB", "score": 0.30, "sources": ["theme_candidate"],
             "theme_confidence_max": 0.5, "recent_hit_rate_1d": None},
            {"symbol": "CCC", "score": 0.10, "sources": ["fmp_top100"],
             "theme_confidence_max": 0.0, "recent_hit_rate_1d": None},
        ]
        diag = _diagnose_ranking(cands, _SCORE_WEIGHTS)
        self.assertIn("recent_hit_rate", diag["zero_information_terms"])
        self.assertTrue(diag["degenerate_ranking"])

    def test_small_universe_flag(self):
        cands = [{"symbol": "AAA", "score": 0.9, "sources": ["static"],
                   "theme_confidence_max": 0.0, "recent_hit_rate_1d": None}]
        diag = _diagnose_ranking(cands, _SCORE_WEIGHTS)
        self.assertTrue(diag["small_universe"])


class TestRealisticResolutionTiming(unittest.TestCase):
    """WS9-F4 fix: the pre-existing fixture unrealistically pre-populated a
    12-hour-old signal with an already-resolved `outcome_return_1d`, which
    masked the real defect. In production, resolution takes >= 1 full trading
    day, so a signal inside a 1-day lookback window is genuinely unresolved.
    This test reflects that realistic timing and proves `recent_hit_rate`
    reads as structurally zero-information at `lookback_days=1` as a result —
    not merely that a stale fixture happens to trip a flag."""

    def _write_recent_unresolved_signals(self, root: Path, tickers: list[str]) -> None:
        """Signals from 12 hours ago (well inside a 1-day lookback) with NO
        outcome_return_1d populated — the realistic production state, since
        resolution requires >= 1 full trading day to elapse."""
        recent_ts = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
        _write_signal_outcomes(root, [
            {"ticker": t, "signal_time": recent_ts,
             "outcome_return_1d": "", "direction_correct_1d": ""}
            for t in tickers
        ])

    def test_recent_signal_within_1d_lookback_has_no_resolved_hit_rate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, [])
            self._write_recent_unresolved_signals(root, ["NEWCO"])
            by_daily = _aggregate_universe(root, lookback_days=1)
            self.assertIn("NEWCO", by_daily)
            sig = by_daily["NEWCO"]["signal"]
            self.assertEqual(sig["resolved_1d"], 0)
            ranked = _rank_candidates(by_daily, root)
            row = next(r for r in ranked if r["symbol"] == "NEWCO")
            self.assertIsNone(row["recent_hit_rate_1d"])

    def test_daily_build_under_realistic_timing_flags_zero_info_recent_hit_rate(self):
        """End-to-end: build_top100_daily with multiple candidates whose only
        recent signals are realistically unresolved must flag recent_hit_rate
        as zero-information in ranking_diagnostics — this is the exact defect
        that a 12h-old-but-already-resolved fixture would have hidden."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, ["AAPL", "MSFT"])
            self._write_recent_unresolved_signals(root, ["AAPL", "MSFT", "NEWCO"])
            payload = build_top100_daily(root)
            for row in payload["candidates"]:
                self.assertIsNone(row["recent_hit_rate_1d"])
            diag = payload["ranking_diagnostics"]
            self.assertIn("recent_hit_rate", diag["zero_information_terms"])

    def test_longer_lookback_lets_older_resolved_signal_inform_hit_rate(self):
        """Contrast case: at lookback_days=30, a signal old enough to have
        actually resolved DOES carry a real hit-rate — confirming the defect
        is specific to the 1-day/unresolved-timing interaction, not a general
        bug in the hit-rate computation."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, [])
            old_resolved_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
            _write_signal_outcomes(root, [
                {"ticker": "OLDCO", "signal_time": old_resolved_ts,
                 "outcome_return_1d": "0.02", "direction_correct_1d": "1"},
            ])
            by_monthly = _aggregate_universe(root, lookback_days=30)
            ranked = _rank_candidates(by_monthly, root)
            row = next(r for r in ranked if r["symbol"] == "OLDCO")
            self.assertEqual(row["recent_hit_rate_1d"], 1.0)


class TestRankingOutputUnchangedByDiagnostics(unittest.TestCase):
    """Regression guard for the WS9 fix's core constraint: diagnostics are
    additive only. Adding `ranking_diagnostics` must not alter `candidates`,
    scores, or rank order."""

    def test_candidates_identical_with_and_without_diagnostics_field_present(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, ["NVDA", "ZZZ"])
            _write_fmp_top100(root, ["NVDA"])
            _write_theme_candidates(root, [{"ticker": "NVDA", "confidence": 0.9}])
            by = _aggregate_universe(root, lookback_days=1)
            ranked_a = _rank_candidates(by, root)
            ranked_b = _rank_candidates(by, root)
            # _rank_candidates itself (the actual ranking function) is
            # untouched by the diagnostics addition — deterministic rerun
            # produces byte-identical output.
            self.assertEqual(ranked_a, ranked_b)
            # ranking_diagnostics computed from the ranked output does not
            # feed back into or mutate it.
            diag = _diagnose_ranking(ranked_a, _SCORE_WEIGHTS)
            self.assertEqual(ranked_a, ranked_b)
            self.assertIn("degenerate_ranking", diag)


if __name__ == "__main__":
    unittest.main(verbosity=2)
