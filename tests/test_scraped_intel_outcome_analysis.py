"""
Tests for scraped_intel outcome-linked comparison analytics.

Covers:
  TestSnapshotPersistence     — save/load comparison_snapshots CRUD
  TestOutcomeSlots            — pending slots created per window
  TestJoinLogic               — get_resolved_comparison_outcomes join
  TestBucketAggregation       — bucket functions + compute_bucket_analysis
  TestSchemaIntegrity         — table columns present
  TestNoContamination         — comparison tables don't touch scraped_records /
                                soft_signals / watchlist_alert_outcomes
  TestEvaluatePendingOutcomes — price-lookup + resolve logic
  TestOutcomeAnalysisReports  — JSON + MD writers
  TestRunOutcomeAnalysis      — end-to-end entry point
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from scraped_intel.store import ScrapedIntelStore
from scraped_intel.outcome_analysis import (
    _confidence_delta_bucket,
    _label_return,
    _signal_delta_bucket,
    _source_count_bucket,
    _top_feature_bucket,
    compute_bucket_analysis,
    build_analysis_report,
    write_outcome_analysis_json,
    write_outcome_analysis_md,
    evaluate_pending_comparison_outcomes,
    run_outcome_analysis,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_row_dict(
    symbol: str = "AAPL",
    signal_delta: float = 0.05,
    confidence_delta: float = 0.04,
    soft_composite: float = 0.42,
    scraped_confidence: float = 0.70,
    source_count: int = 2,
    evidence_count: int = 8,
    top_features: list | None = None,
    soft_signals_available: bool = True,
    baseline_signal_score: float = 0.60,
    enriched_signal_score: float = 0.65,
    baseline_confidence_score: float = 0.55,
    enriched_confidence_score: float = 0.59,
    baseline_rank: int = 3,
    enriched_rank: int = 1,
    rank_change: int = 2,
) -> dict:
    if top_features is None:
        top_features = [
            {"feature": "scraped_confidence",  "value": 0.70, "weight": 0.40, "contribution": 0.28},
            {"feature": "recency_score",        "value": 0.60, "weight": 0.30, "contribution": 0.18},
            {"feature": "theme_alignment_score","value": 0.50, "weight": 0.20, "contribution": 0.10},
        ]
    return {
        "symbol":                   symbol,
        "baseline_signal_score":    baseline_signal_score,
        "enriched_signal_score":    enriched_signal_score,
        "signal_delta":             signal_delta,
        "baseline_confidence_score": baseline_confidence_score,
        "enriched_confidence_score": enriched_confidence_score,
        "confidence_delta":         confidence_delta,
        "baseline_rank":            baseline_rank,
        "enriched_rank":            enriched_rank,
        "rank_change":              rank_change,
        "soft_composite":           soft_composite,
        "top_features":             top_features,
        "source_count":             source_count,
        "evidence_count":           evidence_count,
        "scraped_confidence":       scraped_confidence,
        "soft_signals_available":   soft_signals_available,
    }


def _make_store(tmp_dir: Path) -> ScrapedIntelStore:
    return ScrapedIntelStore(db_path=tmp_dir / "test.db")


# ---------------------------------------------------------------------------
# TestSnapshotPersistence
# ---------------------------------------------------------------------------

class TestSnapshotPersistence(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = _make_store(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_save_and_load_single_snapshot(self):
        rd = _make_row_dict("NVDA", signal_delta=0.07)
        row_id = self.store.save_comparison_snapshot(rd, "2026-04-01")
        self.assertIsInstance(row_id, int)
        self.assertGreater(row_id, 0)

        rows = self.store.load_comparison_snapshots(symbol="NVDA")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["symbol"], "NVDA")
        self.assertAlmostEqual(r["signal_delta"], 0.07)
        self.assertEqual(r["as_of_date"], "2026-04-01")

    def test_upsert_overwrites_existing_row(self):
        rd = _make_row_dict("AMD", signal_delta=0.03)
        id1 = self.store.save_comparison_snapshot(rd, "2026-04-01")
        rd["signal_delta"] = 0.08
        id2 = self.store.save_comparison_snapshot(rd, "2026-04-01")
        # Same (symbol, as_of_date) → same row id
        self.assertEqual(id1, id2)
        rows = self.store.load_comparison_snapshots(symbol="AMD")
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["signal_delta"], 0.08)

    def test_top_features_round_trips_as_list(self):
        rd = _make_row_dict("MSFT")
        self.store.save_comparison_snapshot(rd, "2026-04-02")
        rows = self.store.load_comparison_snapshots(symbol="MSFT")
        feats = rows[0]["top_features"]
        self.assertIsInstance(feats, list)
        self.assertEqual(feats[0]["feature"], "scraped_confidence")

    def test_soft_signals_available_bool_round_trip(self):
        for flag in [True, False]:
            sym = "AAPL" if flag else "GOOG"
            rd = _make_row_dict(sym, soft_signals_available=flag)
            self.store.save_comparison_snapshot(rd, "2026-04-01")
            rows = self.store.load_comparison_snapshots(symbol=sym)
            self.assertIs(rows[0]["soft_signals_available"], flag)

    def test_symbol_normalised_to_uppercase(self):
        rd = _make_row_dict("tsla")
        self.store.save_comparison_snapshot(rd, "2026-04-01")
        rows = self.store.load_comparison_snapshots(symbol="TSLA")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "TSLA")

    def test_since_date_filter(self):
        for sym, dt in [("AAPL", "2026-03-01"), ("MSFT", "2026-04-01")]:
            self.store.save_comparison_snapshot(_make_row_dict(sym), dt)
        rows = self.store.load_comparison_snapshots(since_date="2026-04-01")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "MSFT")

    def test_save_batch_returns_correct_count(self):
        dicts = [_make_row_dict(s) for s in ["A", "B", "C"]]
        ids = self.store.save_comparison_snapshots(dicts, "2026-04-01", windows=[1])
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(set(ids)), 3)  # all distinct


# ---------------------------------------------------------------------------
# TestOutcomeSlots
# ---------------------------------------------------------------------------

class TestOutcomeSlots(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = _make_store(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def _count_pending(self, symbol: str, window: int) -> int:
        conn = sqlite3.connect(str(self.tmp / "test.db"))
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM comparison_outcomes "
                "WHERE symbol=? AND window_days=? AND outcome_status='pending'",
                (symbol, window),
            ).fetchone()[0]
        finally:
            conn.close()

    def test_default_windows_created(self):
        rd = _make_row_dict("NVDA")
        self.store.save_comparison_snapshots([rd], "2026-04-01")
        for w in [1, 5, 20]:
            self.assertEqual(self._count_pending("NVDA", w), 1, f"window {w}")

    def test_custom_windows(self):
        rd = _make_row_dict("AMD")
        self.store.save_comparison_snapshots([rd], "2026-04-01", windows=[3, 7])
        self.assertEqual(self._count_pending("AMD", 3), 1)
        self.assertEqual(self._count_pending("AMD", 7), 1)
        self.assertEqual(self._count_pending("AMD", 1), 0)  # not requested

    def test_duplicate_save_does_not_duplicate_slots(self):
        rd = _make_row_dict("AAPL")
        self.store.save_comparison_snapshots([rd], "2026-04-01", windows=[1])
        self.store.save_comparison_snapshots([rd], "2026-04-01", windows=[1])
        self.assertEqual(self._count_pending("AAPL", 1), 1)

    def test_pending_slots_returned_by_get_pending(self):
        rds = [_make_row_dict(s) for s in ["X", "Y"]]
        self.store.save_comparison_snapshots(rds, "2026-04-01", windows=[5])
        pending = self.store.get_pending_comparison_outcomes()
        symbols = {r["symbol"] for r in pending}
        self.assertIn("X", symbols)
        self.assertIn("Y", symbols)
        self.assertTrue(all(r["window_days"] == 5 for r in pending))


# ---------------------------------------------------------------------------
# TestJoinLogic
# ---------------------------------------------------------------------------

class TestJoinLogic(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = _make_store(self.tmp)
        # Plant one snapshot with windows [1, 5]
        rd = _make_row_dict("COIN", signal_delta=0.04, source_count=3)
        ids = self.store.save_comparison_snapshots([rd], "2026-03-10", windows=[1, 5])
        self._snapshot_id = ids[0]
        # Resolve the 1d window manually
        pending = self.store.get_pending_comparison_outcomes()
        for p in pending:
            if p["window_days"] == 1:
                self.store.resolve_comparison_outcome(
                    p["id"],
                    baseline_price=100.0,
                    outcome_price=102.5,
                    return_pct=2.5,
                    outcome_label="positive",
                )

    def tearDown(self):
        self._tmp.cleanup()

    def test_resolved_row_joins_snapshot_fields(self):
        rows = self.store.get_resolved_comparison_outcomes()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["symbol"], "COIN")
        self.assertAlmostEqual(r["signal_delta"], 0.04)
        self.assertEqual(r["source_count"], 3)
        self.assertAlmostEqual(r["return_pct"], 2.5)
        self.assertEqual(r["outcome_label"], "positive")

    def test_window_filter(self):
        rows_1d = self.store.get_resolved_comparison_outcomes(window_days=1)
        rows_5d = self.store.get_resolved_comparison_outcomes(window_days=5)
        self.assertEqual(len(rows_1d), 1)
        self.assertEqual(len(rows_5d), 0)  # not yet resolved

    def test_since_date_filter(self):
        rows_after = self.store.get_resolved_comparison_outcomes(since_date="2026-03-11")
        self.assertEqual(len(rows_after), 0)
        rows_on = self.store.get_resolved_comparison_outcomes(since_date="2026-03-10")
        self.assertEqual(len(rows_on), 1)

    def test_top_features_is_list_in_joined_row(self):
        rows = self.store.get_resolved_comparison_outcomes()
        self.assertIsInstance(rows[0]["top_features"], list)

    def test_soft_signals_available_bool_in_joined_row(self):
        rows = self.store.get_resolved_comparison_outcomes()
        self.assertIsInstance(rows[0]["soft_signals_available"], bool)

    def test_pending_row_not_included_in_resolved(self):
        # The 5d slot is still pending.
        rows = self.store.get_resolved_comparison_outcomes()
        windows_found = {r["window_days"] for r in rows}
        self.assertNotIn(5, windows_found)


# ---------------------------------------------------------------------------
# TestBucketAggregation
# ---------------------------------------------------------------------------

class TestBucketAggregation(unittest.TestCase):

    # ── bucket helper functions ────────────────────────────────────────────

    def test_signal_delta_none_at_zero(self):
        self.assertEqual(_signal_delta_bucket(0.0), "none")

    def test_signal_delta_none_at_negative(self):
        self.assertEqual(_signal_delta_bucket(-0.01), "none")

    def test_signal_delta_small(self):
        self.assertEqual(_signal_delta_bucket(0.01), "small(0-2%)")

    def test_signal_delta_medium(self):
        self.assertEqual(_signal_delta_bucket(0.04), "medium(2-6%)")

    def test_signal_delta_large(self):
        self.assertEqual(_signal_delta_bucket(0.09), "large(6%+)")

    def test_confidence_delta_bucket_mirrors_signal(self):
        self.assertEqual(_confidence_delta_bucket(0.03), _signal_delta_bucket(0.03))

    def test_top_feature_returns_first_feature_name(self):
        feats = [{"feature": "recency_score", "value": 0.8, "weight": 0.3, "contribution": 0.24}]
        self.assertEqual(_top_feature_bucket(feats), "recency_score")

    def test_top_feature_empty_list_returns_none(self):
        self.assertEqual(_top_feature_bucket([]), "none")

    def test_top_feature_none_input_returns_none(self):
        self.assertEqual(_top_feature_bucket(None), "none")

    def test_source_count_zero(self):
        self.assertEqual(_source_count_bucket(0), "0")

    def test_source_count_one(self):
        self.assertEqual(_source_count_bucket(1), "1")

    def test_source_count_two_to_three(self):
        self.assertEqual(_source_count_bucket(2), "2-3")
        self.assertEqual(_source_count_bucket(3), "2-3")

    def test_source_count_four_plus(self):
        self.assertEqual(_source_count_bucket(4), "4+")
        self.assertEqual(_source_count_bucket(10), "4+")

    def test_label_return_positive(self):
        self.assertEqual(_label_return(2.5), "positive")

    def test_label_return_negative(self):
        self.assertEqual(_label_return(-1.5), "negative")

    def test_label_return_flat(self):
        self.assertEqual(_label_return(0.5), "flat")
        self.assertEqual(_label_return(0.0), "flat")

    # ── compute_bucket_analysis ────────────────────────────────────────────

    def _make_resolved_row(
        self, symbol, signal_delta, confidence_delta, source_count,
        top_features, return_pct
    ):
        return {
            "symbol":              symbol,
            "signal_delta":        signal_delta,
            "confidence_delta":    confidence_delta,
            "source_count":        source_count,
            "top_features":        top_features,
            "return_pct":          return_pct,
            "soft_signals_available": True,
        }

    def test_empty_input_returns_empty_analysis(self):
        result = compute_bucket_analysis([])
        self.assertEqual(result["totals"]["count"], 0)
        self.assertEqual(result["by_signal_delta"], {})

    def test_totals_count(self):
        rows = [
            self._make_resolved_row("A", 0.05, 0.03, 2, [{"feature": "recency_score"}], 1.2),
            self._make_resolved_row("B", 0.01, 0.01, 0, [], -0.5),
        ]
        result = compute_bucket_analysis(rows)
        self.assertEqual(result["totals"]["count"], 2)
        self.assertEqual(result["totals"]["resolved_count"], 2)

    def test_avg_return_per_bucket(self):
        rows = [
            self._make_resolved_row("A", 0.05, 0.03, 1, [{"feature": "recency_score"}], 2.0),
            self._make_resolved_row("B", 0.05, 0.03, 1, [{"feature": "recency_score"}], 4.0),
        ]
        result = compute_bucket_analysis(rows)
        sig_buckets = result["by_signal_delta"]
        # Both rows have signal_delta=0.05 → medium bucket
        self.assertIn("medium(2-6%)", sig_buckets)
        self.assertAlmostEqual(sig_buckets["medium(2-6%)"]["avg_return"], 3.0)

    def test_win_rate_calculation(self):
        rows = [
            self._make_resolved_row("A", 0.05, 0.0, 1, [], 2.0),   # win
            self._make_resolved_row("B", 0.05, 0.0, 1, [], -2.0),  # loss
            self._make_resolved_row("C", 0.05, 0.0, 1, [], 1.5),   # win
        ]
        result = compute_bucket_analysis(rows)
        bucket = result["by_signal_delta"]["medium(2-6%)"]
        self.assertAlmostEqual(bucket["win_rate"], 2 / 3, places=4)

    def test_rows_without_return_pct_counted_but_not_resolved(self):
        rows = [
            self._make_resolved_row("A", 0.05, 0.0, 1, [], 1.5),
            {**self._make_resolved_row("B", 0.05, 0.0, 1, [], 0.0), "return_pct": None},
        ]
        result = compute_bucket_analysis(rows)
        self.assertEqual(result["totals"]["count"], 2)
        self.assertEqual(result["totals"]["resolved_count"], 1)

    def test_top_feature_bucket_grouping(self):
        rows = [
            self._make_resolved_row("A", 0.05, 0.0, 1, [{"feature": "recency_score"}], 1.0),
            self._make_resolved_row("B", 0.05, 0.0, 1, [{"feature": "scraped_confidence"}], 2.0),
            self._make_resolved_row("C", 0.05, 0.0, 1, [{"feature": "recency_score"}], 3.0),
        ]
        result = compute_bucket_analysis(rows)
        feats = result["by_top_feature"]
        self.assertIn("recency_score", feats)
        self.assertIn("scraped_confidence", feats)
        self.assertEqual(feats["recency_score"]["count"], 2)
        self.assertEqual(feats["scraped_confidence"]["count"], 1)

    def test_source_count_bucket_grouping(self):
        rows = [
            self._make_resolved_row("A", 0.0, 0.0, 0, [], -1.0),
            self._make_resolved_row("B", 0.0, 0.0, 1, [], 0.5),
            self._make_resolved_row("C", 0.0, 0.0, 5, [], 2.0),
        ]
        result = compute_bucket_analysis(rows)
        src = result["by_source_count"]
        self.assertIn("0", src)
        self.assertIn("1", src)
        self.assertIn("4+", src)

    def test_build_analysis_report_structure(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        store = _make_store(tmp)
        rd = _make_row_dict("AAPL", signal_delta=0.05)
        ids = store.save_comparison_snapshots([rd], "2026-03-10", windows=[1])
        pending = store.get_pending_comparison_outcomes()
        store.resolve_comparison_outcome(
            pending[0]["id"],
            baseline_price=100.0, outcome_price=103.0,
            return_pct=3.0, outcome_label="positive",
        )
        report = build_analysis_report(store, windows=[1])
        self.assertIn("generated_at", report)
        self.assertIn("by_window", report)
        self.assertIn(1, report["by_window"])
        self.assertIn("overall_totals", report)
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# TestSchemaIntegrity
# ---------------------------------------------------------------------------

class TestSchemaIntegrity(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "test.db"
        ScrapedIntelStore(db_path=self.db_path)

    def tearDown(self):
        self._tmp.cleanup()

    def _columns(self, table: str) -> set:
        conn = sqlite3.connect(str(self.db_path))
        try:
            return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        finally:
            conn.close()

    def test_comparison_snapshots_columns(self):
        cols = self._columns("comparison_snapshots")
        for expected in [
            "id", "symbol", "as_of_date",
            "baseline_signal_score", "enriched_signal_score", "signal_delta",
            "baseline_confidence_score", "enriched_confidence_score", "confidence_delta",
            "baseline_rank", "enriched_rank", "rank_change",
            "soft_composite", "top_features",
            "source_count", "evidence_count", "scraped_confidence",
            "soft_signals_available", "recorded_at",
        ]:
            self.assertIn(expected, cols, f"Missing column: {expected}")

    def test_comparison_outcomes_columns(self):
        cols = self._columns("comparison_outcomes")
        for expected in [
            "id", "snapshot_id", "symbol", "as_of_date", "window_days",
            "baseline_price", "outcome_price", "return_pct", "outcome_label",
            "evaluated_at", "outcome_status",
        ]:
            self.assertIn(expected, cols, f"Missing column: {expected}")

    def test_existing_scraped_tables_still_present(self):
        for table in ["scraped_records", "soft_signals"]:
            cols = self._columns(table)
            self.assertGreater(len(cols), 0, f"Table {table} missing or empty")

    def test_unique_constraint_symbol_as_of_date(self):
        """Saving the same (symbol, as_of_date) twice should not raise."""
        store = ScrapedIntelStore(db_path=self.db_path)
        rd = _make_row_dict("TSLA")
        id1 = store.save_comparison_snapshot(rd, "2026-04-01")
        id2 = store.save_comparison_snapshot(rd, "2026-04-01")
        self.assertEqual(id1, id2)

    def test_unique_constraint_snapshot_window(self):
        """Slot creation for the same (snapshot_id, window) is idempotent."""
        store = ScrapedIntelStore(db_path=self.db_path)
        rd = _make_row_dict("META")
        ids = store.save_comparison_snapshots([rd], "2026-04-01", windows=[5])
        # Calling again with same date should not raise
        store.save_comparison_snapshots([rd], "2026-04-01", windows=[5])
        pending = store.get_pending_comparison_outcomes()
        # Should still be exactly one slot for window=5
        meta_5d = [p for p in pending if p["symbol"] == "META" and p["window_days"] == 5]
        self.assertEqual(len(meta_5d), 1)


# ---------------------------------------------------------------------------
# TestNoContamination
# ---------------------------------------------------------------------------

class TestNoContamination(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "test.db"
        self.store = ScrapedIntelStore(db_path=self.db_path)

    def tearDown(self):
        self._tmp.cleanup()

    def _table_count(self, table: str) -> int:
        conn = sqlite3.connect(str(self.db_path))
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()

    def test_saving_snapshots_does_not_write_scraped_records(self):
        rd = _make_row_dict("PLTR")
        self.store.save_comparison_snapshots([rd], "2026-04-01")
        self.assertEqual(self._table_count("scraped_records"), 0)

    def test_saving_snapshots_does_not_write_soft_signals(self):
        rd = _make_row_dict("PLTR")
        self.store.save_comparison_snapshots([rd], "2026-04-01")
        self.assertEqual(self._table_count("soft_signals"), 0)

    def test_resolving_outcome_does_not_write_soft_signals(self):
        rd = _make_row_dict("AMD")
        self.store.save_comparison_snapshots([rd], "2026-04-01", windows=[1])
        pending = self.store.get_pending_comparison_outcomes()
        self.store.resolve_comparison_outcome(
            pending[0]["id"],
            baseline_price=50.0, outcome_price=51.0,
            return_pct=2.0, outcome_label="positive",
        )
        self.assertEqual(self._table_count("soft_signals"), 0)

    def test_no_watchlist_alert_outcomes_written(self):
        """comparison_snapshots must not touch watchlist_alert_outcomes."""
        rd = _make_row_dict("NVDA")
        self.store.save_comparison_snapshots([rd], "2026-04-01")
        # Table exists but should be empty
        try:
            count = self._table_count("watchlist_alert_outcomes")
            self.assertEqual(count, 0)
        except sqlite3.OperationalError:
            pass  # Table not present in this DB — also fine


# ---------------------------------------------------------------------------
# TestEvaluatePendingOutcomes
# ---------------------------------------------------------------------------

class TestEvaluatePendingOutcomes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "test.db"
        self.cache_dir = self.tmp / "cache"
        self.cache_dir.mkdir()
        self.store = ScrapedIntelStore(db_path=self.db_path)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_price_cache(self, symbol: str, entries: dict[str, float]):
        """Write a minimal TIME_SERIES_DAILY cache file."""
        ts = {
            day: {"4. close": str(price), "5. volume": "1000000"}
            for day, price in entries.items()
        }
        cache_file = self.cache_dir / f"daily_{symbol}.json"
        cache_file.write_text(
            json.dumps({"Time Series (Daily)": ts}),
            encoding="utf-8",
        )

    def test_resolves_slot_when_price_data_available(self):
        # Surface date: 2026-03-01, window: 1d
        rd = _make_row_dict("AAPL")
        self.store.save_comparison_snapshots([rd], "2026-03-01", windows=[1])
        # Write prices for the surfaced date and the next day
        self._write_price_cache("AAPL", {
            "2026-03-01": 150.0,
            "2026-03-02": 153.0,
        })
        from datetime import datetime
        result = evaluate_pending_comparison_outcomes(
            db_path=self.db_path,
            cache_dir=self.cache_dir,
            as_of=datetime(2026, 3, 10),
        )
        self.assertEqual(result["resolved_count"], 1)
        self.assertEqual(result["skipped_count"], 0)
        resolved = self.store.get_resolved_comparison_outcomes(window_days=1)
        self.assertEqual(len(resolved), 1)
        self.assertAlmostEqual(resolved[0]["return_pct"], 2.0, places=2)
        self.assertEqual(resolved[0]["outcome_label"], "positive")

    def test_skips_when_target_date_in_future(self):
        rd = _make_row_dict("MSFT")
        # Surface tomorrow → target is in the future relative to as_of=today
        from datetime import datetime
        today = date.today()
        tomorrow_str = (today + timedelta(days=1)).isoformat()
        self.store.save_comparison_snapshots([rd], tomorrow_str, windows=[1])
        result = evaluate_pending_comparison_outcomes(
            db_path=self.db_path,
            cache_dir=self.cache_dir,
            as_of=datetime.now(),
        )
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["resolved_count"], 0)

    def test_skips_when_no_cache_file(self):
        rd = _make_row_dict("XYZ")
        self.store.save_comparison_snapshots([rd], "2026-03-01", windows=[1])
        from datetime import datetime
        result = evaluate_pending_comparison_outcomes(
            db_path=self.db_path,
            cache_dir=self.cache_dir,
            as_of=datetime(2026, 3, 10),
        )
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["resolved_count"], 0)


# ---------------------------------------------------------------------------
# TestOutcomeAnalysisReports
# ---------------------------------------------------------------------------

class TestOutcomeAnalysisReports(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = _make_store(self.tmp)
        # Plant a resolved outcome.
        rd = _make_row_dict("NVDA", signal_delta=0.06, source_count=2)
        self.store.save_comparison_snapshots([rd], "2026-03-10", windows=[1])
        pending = self.store.get_pending_comparison_outcomes()
        self.store.resolve_comparison_outcome(
            pending[0]["id"],
            baseline_price=100.0, outcome_price=105.0,
            return_pct=5.0, outcome_label="positive",
        )
        self.report = build_analysis_report(self.store, windows=[1])

    def tearDown(self):
        self._tmp.cleanup()

    def test_json_file_created(self):
        path = write_outcome_analysis_json(self.report, self.tmp)
        self.assertTrue(path.exists())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("by_window", data)

    def test_json_windows_serialised_as_strings(self):
        path = write_outcome_analysis_json(self.report, self.tmp)
        data = json.loads(path.read_text(encoding="utf-8"))
        # JSON doesn't allow int keys; they must be strings.
        for key in data["by_window"]:
            self.assertIsInstance(key, str)

    def test_json_contains_overall_totals(self):
        path = write_outcome_analysis_json(self.report, self.tmp)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("overall_totals", data)
        self.assertIn("resolved_count", data["overall_totals"])

    def test_md_file_created(self):
        path = write_outcome_analysis_md(self.report, self.tmp)
        self.assertTrue(path.exists())

    def test_md_contains_heading(self):
        path = write_outcome_analysis_md(self.report, self.tmp)
        content = path.read_text(encoding="utf-8")
        self.assertIn("Outcome Analysis", content)

    def test_md_contains_window_section(self):
        path = write_outcome_analysis_md(self.report, self.tmp)
        content = path.read_text(encoding="utf-8")
        self.assertIn("1-Day Return Window", content)

    def test_md_contains_bucket_table_headers(self):
        path = write_outcome_analysis_md(self.report, self.tmp)
        content = path.read_text(encoding="utf-8")
        self.assertIn("By Signal Delta Bucket", content)
        self.assertIn("By Source Count Bucket", content)

    def test_md_disclaimer_present(self):
        path = write_outcome_analysis_md(self.report, self.tmp)
        content = path.read_text(encoding="utf-8")
        self.assertIn("shadow mode", content)


# ---------------------------------------------------------------------------
# TestRunOutcomeAnalysis
# ---------------------------------------------------------------------------

class TestRunOutcomeAnalysis(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "test.db"
        self.out_dir = self.tmp / "out"

    def tearDown(self):
        self._tmp.cleanup()

    def test_runs_without_errors_on_empty_db(self):
        report = run_outcome_analysis(
            db_path=self.db_path,
            output_dir=self.out_dir,
            config={"comparison_outcome_windows": [1]},
            evaluate_first=False,
        )
        self.assertIn("overall_totals", report)
        self.assertEqual(report["overall_totals"]["count"], 0)

    def test_output_files_written(self):
        run_outcome_analysis(
            db_path=self.db_path,
            output_dir=self.out_dir,
            config={"comparison_outcome_windows": [1]},
            evaluate_first=False,
        )
        self.assertTrue((self.out_dir / "scraped_intel_outcome_analysis.json").exists())
        self.assertTrue((self.out_dir / "scraped_intel_outcome_analysis.md").exists())

    def test_report_includes_windows_from_config(self):
        report = run_outcome_analysis(
            db_path=self.db_path,
            output_dir=self.out_dir,
            config={"comparison_outcome_windows": [1, 5]},
            evaluate_first=False,
        )
        self.assertEqual(report["windows"], [1, 5])

    def test_eval_summary_present_in_report(self):
        # evaluate_first=True but no pending rows → still returns a summary
        report = run_outcome_analysis(
            db_path=self.db_path,
            output_dir=self.out_dir,
            config={"comparison_outcome_windows": [1]},
            evaluate_first=True,
            cache_dir=self.tmp / "empty_cache",
        )
        self.assertIn("eval_summary", report)

    def test_does_not_contaminate_scraped_records(self):
        run_outcome_analysis(
            db_path=self.db_path,
            output_dir=self.out_dir,
            config={},
            evaluate_first=False,
        )
        store = ScrapedIntelStore(db_path=self.db_path)
        self.assertEqual(len(store.load_records("AAPL")), 0)


if __name__ == "__main__":
    unittest.main()
