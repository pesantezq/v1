"""
Tests for policy_evaluator.outcome_attributor and outcome_writer.

Coverage:
  TestLoadPortfolioSnapshots   — SQLite loading, grouping, missing DB
  TestParseRunDate             — run_id date parsing
  TestFindValueAtOrAfter       — nearest-snapshot lookup with gap tolerance
  TestForwardReturnCalculation — forward return formula and horizon mapping
  TestMfeMaeCalculation        — MFE / MAE edge cases
  TestAttributeSingle          — event-to-outcome alignment
  TestAttributeOutcomes        — batch attribution
  TestBucketAggregation        — BucketOutcome accumulation and metrics
  TestConfidenceTierBuckets    — confidence tier comparisons
  TestDegradedModeBuckets      — degraded vs normal comparisons
  TestRegimeBuckets            — regime-specific comparisons
  TestScoreQuintile            — score quintile aggregation
  TestNotableItems             — wins / misses selection
  TestSparseData               — missing / insufficient data handling
  TestBackwardCompatibility    — older history records missing fields
  TestReadOnly                 — attribution never mutates original records
  TestOutcomeWriter            — JSON / MD output, dry-run, empty result
  TestRoundTrip                — end-to-end integration test
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from typing import List, Tuple

from policy_evaluator.outcome_attributor import (
    ADVERSE_THRESHOLD,
    POSITIVE_RETURN_THRESHOLD,
    SMALL_SAMPLE_WARNING,
    STRONG_WIN_THRESHOLD,
    AttributedRecord,
    BucketOutcome,
    OutcomeResult,
    _accumulate_bucket,
    _aggregate_by_score_quintile,
    _attribute_single,
    _compute_forward_returns,
    _confidence_calibration_summary,
    _confidence_tier,
    _find_value_at_or_after,
    _finalize_bucket,
    _notable_items,
    _parse_date_from_run_id,
    _score_quintile_label,
    attribute_outcomes,
    load_portfolio_snapshots,
    run_outcome_attribution,
)
from policy_evaluator.outcome_writer import (
    build_outcome_memo,
    write_outcome_reports,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _snapshots(*pairs: Tuple[str, float]) -> List[Tuple[date, float]]:
    """Build a sorted snapshot list from (YYYY-MM-DD, value) string pairs."""
    return [(date.fromisoformat(d), v) for d, v in sorted(pairs)]


def _rec(
    run_id="2026-01-10_daily",
    rec_id="drift_SPY",
    action_level="Recommended",
    confidence=70,
    score=60,
    raw_score=65,
    impact_area="Drift",
    priority=60,
    degraded_mode=False,
    data_mode="live",
    drawdown_regime="normal",
    **kwargs,
) -> dict:
    """Build a minimal recommendation history record."""
    return dict(
        run_id=run_id,
        timestamp=f"{run_id[:10]}T09:00:00",
        rec_id=rec_id,
        rec_base_id=rec_id,
        action_level=action_level,
        confidence=confidence,
        score=score,
        raw_score=raw_score,
        impact_area=impact_area,
        priority=priority,
        degraded_mode=degraded_mode,
        data_mode=data_mode,
        drawdown_regime=drawdown_regime,
        regime=drawdown_regime,
        **kwargs,
    )


def _write_history(path: Path, records: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _make_db(path: Path, rows: List[Tuple[str, float, str]]) -> None:
    """Create a minimal portfolio.db with a snapshots table."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            total_value REAL,
            cash REAL,
            max_drift REAL,
            drawdown_regime TEXT,
            recorded_at TEXT NOT NULL
        )"""
    )
    for run_id, value, recorded_at in rows:
        conn.execute(
            "INSERT INTO snapshots (run_id, total_value, recorded_at) VALUES (?,?,?)",
            (run_id, value, recorded_at),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# TestLoadPortfolioSnapshots
# ---------------------------------------------------------------------------

class TestLoadPortfolioSnapshots(unittest.TestCase):
    def test_returns_empty_if_db_missing(self):
        snaps = load_portfolio_snapshots(Path("/nonexistent/portfolio.db"))
        self.assertEqual(snaps, [])

    def test_loads_single_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "p.db"
            _make_db(db, [("2026-01-10_daily", 100000.0, "2026-01-10T10:00:00")])
            snaps = load_portfolio_snapshots(db)
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0][0], date(2026, 1, 10))
        self.assertAlmostEqual(snaps[0][1], 100000.0)

    def test_groups_multiple_runs_same_day_keeps_last(self):
        """When daily + weekly both run on the same date, keep the last value."""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "p.db"
            _make_db(db, [
                ("2026-01-10_daily", 99000.0, "2026-01-10T08:00:00"),
                ("2026-01-10_weekly", 100000.0, "2026-01-10T09:00:00"),
            ])
            snaps = load_portfolio_snapshots(db)
        # One entry per date (last wins)
        self.assertEqual(len(snaps), 1)
        self.assertAlmostEqual(snaps[0][1], 100000.0)

    def test_sorted_ascending_by_date(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "p.db"
            _make_db(db, [
                ("2026-01-15_daily", 105000.0, "2026-01-15T09:00:00"),
                ("2026-01-10_daily", 100000.0, "2026-01-10T09:00:00"),
                ("2026-01-12_daily", 102000.0, "2026-01-12T09:00:00"),
            ])
            snaps = load_portfolio_snapshots(db)
        dates = [s[0] for s in snaps]
        self.assertEqual(dates, sorted(dates))

    def test_skips_null_total_value(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "p.db"
            _make_db(db, [
                ("2026-01-10_daily", None, "2026-01-10T09:00:00"),
                ("2026-01-11_daily", 100000.0, "2026-01-11T09:00:00"),
            ])
            snaps = load_portfolio_snapshots(db)
        self.assertEqual(len(snaps), 1)

    def test_skips_zero_total_value(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "p.db"
            _make_db(db, [
                ("2026-01-10_daily", 0.0, "2026-01-10T09:00:00"),
                ("2026-01-11_daily", 100000.0, "2026-01-11T09:00:00"),
            ])
            snaps = load_portfolio_snapshots(db)
        self.assertEqual(len(snaps), 1)


# ---------------------------------------------------------------------------
# TestParseRunDate
# ---------------------------------------------------------------------------

class TestParseRunDate(unittest.TestCase):
    def test_standard_daily(self):
        self.assertEqual(_parse_date_from_run_id("2026-04-16_daily"), date(2026, 4, 16))

    def test_standard_weekly(self):
        self.assertEqual(_parse_date_from_run_id("2026-01-01_weekly"), date(2026, 1, 1))

    def test_standard_monthly(self):
        self.assertEqual(_parse_date_from_run_id("2025-12-31_monthly"), date(2025, 12, 31))

    def test_returns_none_for_unknown_format(self):
        self.assertIsNone(_parse_date_from_run_id("unknown"))

    def test_returns_none_for_empty(self):
        self.assertIsNone(_parse_date_from_run_id(""))


# ---------------------------------------------------------------------------
# TestFindValueAtOrAfter
# ---------------------------------------------------------------------------

class TestFindValueAtOrAfter(unittest.TestCase):
    def setUp(self):
        self.snaps = _snapshots(
            ("2026-01-10", 100_000),
            ("2026-01-15", 102_000),
            ("2026-01-20", 104_000),
        )

    def test_exact_match(self):
        val, d = _find_value_at_or_after(self.snaps, date(2026, 1, 10))
        self.assertAlmostEqual(val, 100_000)
        self.assertEqual(d, date(2026, 1, 10))

    def test_nearest_after(self):
        """Target Jan 11 — nearest snapshot is Jan 15 (within 3-day gap? No: gap=4)."""
        val, d = _find_value_at_or_after(self.snaps, date(2026, 1, 11), max_gap_days=5)
        self.assertAlmostEqual(val, 102_000)

    def test_returns_none_when_gap_exceeded(self):
        """Jan 11 with max_gap=2 — next snapshot is Jan 15, gap=4 → None."""
        val, d = _find_value_at_or_after(self.snaps, date(2026, 1, 11), max_gap_days=2)
        self.assertIsNone(val)
        self.assertIsNone(d)

    def test_returns_none_for_empty_list(self):
        val, d = _find_value_at_or_after([], date(2026, 1, 10))
        self.assertIsNone(val)

    def test_returns_none_when_all_before_target(self):
        val, d = _find_value_at_or_after(self.snaps, date(2026, 2, 1))
        self.assertIsNone(val)


# ---------------------------------------------------------------------------
# TestForwardReturnCalculation
# ---------------------------------------------------------------------------

class TestForwardReturnCalculation(unittest.TestCase):
    """Tests for _compute_forward_returns."""

    def setUp(self):
        # Portfolio: goes up 1% each 5 days
        self.snaps = _snapshots(
            ("2026-01-10", 100_000),  # T
            ("2026-01-11", 100_200),  # T+1d
            ("2026-01-13", 100_400),  # T+3d
            ("2026-01-15", 100_600),  # T+5d
            ("2026-01-20", 101_000),  # T+10d
        )
        self.run_date = date(2026, 1, 10)
        self.value_at_t = 100_000.0

    def test_all_horizons_present(self):
        fwd, mfe, mae = _compute_forward_returns(
            self.value_at_t, self.snaps, self.run_date, horizons=(1, 3, 5, 10)
        )
        self.assertIsNotNone(fwd[1])
        self.assertIsNotNone(fwd[3])
        self.assertIsNotNone(fwd[5])
        self.assertIsNotNone(fwd[10])

    def test_forward_return_formula(self):
        """forward_return(1d) = (100_200 - 100_000) / 100_000 = 0.002"""
        fwd, _, _ = _compute_forward_returns(
            self.value_at_t, self.snaps, self.run_date, horizons=(1,)
        )
        self.assertAlmostEqual(fwd[1], 0.002, places=5)

    def test_forward_return_5d(self):
        """forward_return(5d) = (100_600 - 100_000) / 100_000 = 0.006"""
        fwd, _, _ = _compute_forward_returns(
            self.value_at_t, self.snaps, self.run_date, horizons=(5,)
        )
        self.assertAlmostEqual(fwd[5], 0.006, places=5)

    def test_null_when_no_forward_snapshot(self):
        """No snapshot exists at T+100d."""
        fwd, _, _ = _compute_forward_returns(
            self.value_at_t, self.snaps, self.run_date, horizons=(100,)
        )
        self.assertIsNone(fwd[100])

    def test_mfe_positive_when_all_gains(self):
        _, mfe, mae = _compute_forward_returns(
            self.value_at_t, self.snaps, self.run_date
        )
        self.assertIsNotNone(mfe)
        self.assertGreater(mfe, 0)

    def test_mae_zero_when_all_gains(self):
        """MAE = min(0, min_return) — when all returns positive, MAE = 0."""
        _, mfe, mae = _compute_forward_returns(
            self.value_at_t, self.snaps, self.run_date
        )
        self.assertEqual(mae, 0.0)

    def test_returns_none_when_no_snapshots(self):
        fwd, mfe, mae = _compute_forward_returns(100_000.0, [], date(2026, 1, 10))
        for h in (1, 3, 5, 10):
            self.assertIsNone(fwd[h])
        self.assertIsNone(mfe)
        self.assertIsNone(mae)


# ---------------------------------------------------------------------------
# TestMfeMaeCalculation
# ---------------------------------------------------------------------------

class TestMfeMaeCalculation(unittest.TestCase):
    def test_mae_captures_worst_loss(self):
        """Portfolio drops at all horizons — MAE should be the worst (most negative)."""
        snaps = _snapshots(
            ("2026-01-10", 100_000),
            ("2026-01-11", 99_000),   # -1%
            ("2026-01-13", 98_500),   # -1.5%
            ("2026-01-15", 98_000),   # -2%
            ("2026-01-20", 97_000),   # -3%  ← worst
        )
        _, mfe, mae = _compute_forward_returns(100_000.0, snaps, date(2026, 1, 10))
        self.assertEqual(mfe, 0.0)
        self.assertAlmostEqual(mae, -0.03, places=5)  # worst is -3%

    def test_mfe_captures_best_gain(self):
        snaps = _snapshots(
            ("2026-01-10", 100_000),
            ("2026-01-11", 101_000),   # +1%
            ("2026-01-13", 103_000),   # +3% ← best
            ("2026-01-15", 102_000),   # +2%
            ("2026-01-20", 101_500),   # +1.5%
        )
        _, mfe, mae = _compute_forward_returns(100_000.0, snaps, date(2026, 1, 10))
        self.assertAlmostEqual(mfe, 0.03, places=5)
        self.assertEqual(mae, 0.0)

    def test_mixed_gains_and_losses(self):
        snaps = _snapshots(
            ("2026-01-10", 100_000),
            ("2026-01-11", 101_000),   # +1%
            ("2026-01-13", 99_000),    # -1%
            ("2026-01-15", 102_000),   # +2%
            ("2026-01-20", 98_000),    # -2%
        )
        _, mfe, mae = _compute_forward_returns(100_000.0, snaps, date(2026, 1, 10))
        self.assertAlmostEqual(mfe, 0.02, places=5)
        self.assertAlmostEqual(mae, -0.02, places=5)

    def test_mfe_mae_null_when_no_snapshots(self):
        _, mfe, mae = _compute_forward_returns(100_000.0, [], date(2026, 1, 10))
        self.assertIsNone(mfe)
        self.assertIsNone(mae)

    def test_single_return_case(self):
        snaps = _snapshots(
            ("2026-01-10", 100_000),
            ("2026-01-11", 101_000),
        )
        _, mfe, mae = _compute_forward_returns(100_000.0, snaps, date(2026, 1, 10))
        self.assertAlmostEqual(mfe, 0.01, places=5)
        self.assertEqual(mae, 0.0)


# ---------------------------------------------------------------------------
# TestAttributeSingle
# ---------------------------------------------------------------------------

class TestAttributeSingle(unittest.TestCase):
    def _good_snaps(self) -> List[Tuple[date, float]]:
        return _snapshots(
            ("2026-01-10", 100_000),
            ("2026-01-11", 101_000),
            ("2026-01-13", 102_000),
            ("2026-01-15", 103_000),
            ("2026-01-20", 105_000),
        )

    def test_attributable_when_snapshot_present(self):
        rec = _rec(run_id="2026-01-10_daily")
        ar = _attribute_single(rec, self._good_snaps())
        self.assertTrue(ar.attributable)
        self.assertEqual(ar.attribution_note, "ok")

    def test_portfolio_value_at_t_is_set(self):
        rec = _rec(run_id="2026-01-10_daily")
        ar = _attribute_single(rec, self._good_snaps())
        self.assertAlmostEqual(ar.portfolio_value_at_t, 100_000)

    def test_forward_returns_computed(self):
        rec = _rec(run_id="2026-01-10_daily")
        ar = _attribute_single(rec, self._good_snaps())
        self.assertIsNotNone(ar.forward_return_1d)
        self.assertIsNotNone(ar.forward_return_5d)

    def test_not_attributable_when_no_snapshot(self):
        rec = _rec(run_id="2026-03-01_daily")  # no snapshot around this date
        ar = _attribute_single(rec, self._good_snaps())
        self.assertFalse(ar.attributable)
        self.assertIn("no snapshot", ar.attribution_note)

    def test_missing_t_day_snapshot_outside_gap_is_not_attributable(self):
        rec = _rec(run_id="2026-01-10_daily")
        sparse_snaps = _snapshots(
            ("2026-01-14", 100_500),
            ("2026-01-20", 101_000),
        )
        ar = _attribute_single(rec, sparse_snaps)
        self.assertFalse(ar.attributable)
        self.assertIn("no snapshot within", ar.attribution_note)

    def test_not_attributable_bad_run_id(self):
        rec = _rec(run_id="unknown_run")
        ar = _attribute_single(rec, self._good_snaps())
        self.assertFalse(ar.attributable)
        self.assertIn("run_date", ar.attribution_note)

    def test_confidence_tier_mapped(self):
        for conf, expected in [(30, "low"), (65, "medium"), (90, "high")]:
            rec = _rec(run_id="2026-01-10_daily", confidence=conf)
            ar = _attribute_single(rec, self._good_snaps())
            self.assertEqual(ar.confidence_tier, expected)

    def test_degraded_mode_preserved(self):
        rec = _rec(run_id="2026-01-10_daily", degraded_mode=True)
        ar = _attribute_single(rec, self._good_snaps())
        self.assertTrue(ar.degraded_mode)

    def test_drawdown_regime_preserved(self):
        rec = _rec(run_id="2026-01-10_daily", drawdown_regime="aggressive")
        ar = _attribute_single(rec, self._good_snaps())
        self.assertEqual(ar.drawdown_regime, "aggressive")

    def test_hit_at_primary_horizon_positive(self):
        rec = _rec(run_id="2026-01-10_daily")
        ar = _attribute_single(rec, self._good_snaps())
        # All returns positive in _good_snaps
        self.assertTrue(ar.hit_at_primary_horizon())

    def test_hit_at_primary_horizon_none_when_no_5d(self):
        # Only T snapshot, no forward data
        snaps = _snapshots(("2026-01-10", 100_000))
        rec = _rec(run_id="2026-01-10_daily")
        ar = _attribute_single(rec, snaps)
        # No forward snapshot at 5d
        self.assertIsNone(ar.hit_at_primary_horizon())


# ---------------------------------------------------------------------------
# TestAttributeOutcomes
# ---------------------------------------------------------------------------

class TestAttributeOutcomes(unittest.TestCase):
    def test_returns_same_count_as_input(self):
        snaps = _snapshots(("2026-01-10", 100_000), ("2026-01-15", 102_000))
        records = [_rec(run_id="2026-01-10_daily") for _ in range(3)]
        attributed = attribute_outcomes(records, snaps)
        self.assertEqual(len(attributed), 3)

    def test_empty_records_returns_empty(self):
        attributed = attribute_outcomes([], [])
        self.assertEqual(attributed, [])

    def test_all_attributable_when_snapshots_cover_window(self):
        snaps = _snapshots(
            ("2026-01-10", 100_000),
            ("2026-01-11", 101_000),
            ("2026-01-15", 103_000),
        )
        records = [_rec(run_id="2026-01-10_daily")]
        attributed = attribute_outcomes(records, snaps)
        self.assertTrue(attributed[0].attributable)

    def test_none_attributable_when_no_snapshots(self):
        records = [_rec(run_id="2026-01-10_daily")]
        attributed = attribute_outcomes(records, [])
        self.assertFalse(attributed[0].attributable)

    def test_original_records_not_mutated(self):
        """Attribution must never mutate the input records."""
        rec = _rec(run_id="2026-01-10_daily")
        original_keys = set(rec.keys())
        snaps = _snapshots(("2026-01-10", 100_000), ("2026-01-15", 102_000))
        attribute_outcomes([rec], snaps)
        self.assertEqual(set(rec.keys()), original_keys)

    def test_multiple_runs_same_day_use_last_recorded_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "p.db"
            _make_db(db, [
                ("2026-01-10_daily", 100_000, "2026-01-10T08:00:00"),
                ("2026-01-10_weekly", 101_000, "2026-01-10T16:00:00"),
                ("2026-01-15_daily", 103_000, "2026-01-15T09:00:00"),
            ])
            snaps = load_portfolio_snapshots(db)
        attributed = attribute_outcomes(
            [
                _rec(run_id="2026-01-10_daily", rec_id="rec_a"),
                _rec(run_id="2026-01-10_weekly", rec_id="rec_b"),
            ],
            snaps,
        )
        self.assertEqual(len(attributed), 2)
        self.assertTrue(all(ar.attributable for ar in attributed))
        self.assertTrue(all(ar.portfolio_value_at_t == 101_000 for ar in attributed))


# ---------------------------------------------------------------------------
# TestBucketAggregation
# ---------------------------------------------------------------------------

class TestBucketAggregation(unittest.TestCase):
    def _make_ar(self, forward_5d: float, attributable: bool = True) -> AttributedRecord:
        return AttributedRecord(
            rec_id="test", rec_base_id="test", run_id="2026-01-10_daily",
            run_date="2026-01-10", action_level="Recommended",
            confidence=70, confidence_tier="medium", score=60, raw_score=65,
            impact_area="Drift", priority=60, degraded_mode=False,
            data_mode="live", drawdown_regime="normal",
            portfolio_value_at_t=100_000,
            forward_return_1d=forward_5d,
            forward_return_3d=forward_5d,
            forward_return_5d=forward_5d if attributable else None,
            forward_return_10d=forward_5d,
            mfe=max(0.0, forward_5d),
            mae=min(0.0, forward_5d),
            attributable=attributable,
            attribution_note="ok" if attributable else "no forward",
        )

    def test_count_increments(self):
        bucket = BucketOutcome(label="test")
        _accumulate_bucket(bucket, self._make_ar(0.01))
        self.assertEqual(bucket.count, 1)
        self.assertEqual(bucket.attributable_count, 1)

    def test_non_attributable_not_counted_in_attr(self):
        bucket = BucketOutcome(label="test")
        _accumulate_bucket(bucket, self._make_ar(0.0, attributable=False))
        self.assertEqual(bucket.count, 1)
        self.assertEqual(bucket.attributable_count, 0)

    def test_hit_count_positive_return(self):
        bucket = BucketOutcome(label="test")
        _accumulate_bucket(bucket, self._make_ar(0.01))  # +1% → hit
        _accumulate_bucket(bucket, self._make_ar(-0.01))  # -1% → miss
        self.assertEqual(bucket.hit_count, 1)

    def test_strong_win_threshold(self):
        bucket = BucketOutcome(label="test")
        _accumulate_bucket(bucket, self._make_ar(STRONG_WIN_THRESHOLD + 0.001))
        _accumulate_bucket(bucket, self._make_ar(STRONG_WIN_THRESHOLD - 0.001))
        self.assertEqual(bucket.strong_win_count, 1)

    def test_adverse_threshold(self):
        bucket = BucketOutcome(label="test")
        _accumulate_bucket(bucket, self._make_ar(ADVERSE_THRESHOLD - 0.001))  # adverse
        _accumulate_bucket(bucket, self._make_ar(ADVERSE_THRESHOLD + 0.001))  # not adverse
        self.assertEqual(bucket.adverse_count, 1)

    def test_hit_rate_computed(self):
        bucket = BucketOutcome(label="test")
        _accumulate_bucket(bucket, self._make_ar(0.01))   # hit
        _accumulate_bucket(bucket, self._make_ar(0.01))   # hit
        _accumulate_bucket(bucket, self._make_ar(-0.01))  # miss
        _finalize_bucket(bucket)
        self.assertAlmostEqual(bucket.hit_rate(), 2 / 3, places=4)

    def test_hit_rate_none_when_no_5d_data(self):
        bucket = BucketOutcome(label="test")
        _accumulate_bucket(bucket, self._make_ar(0.0, attributable=False))
        self.assertIsNone(bucket.hit_rate())

    def test_small_sample_flag(self):
        bucket = BucketOutcome(label="test")
        for _ in range(SMALL_SAMPLE_WARNING - 1):
            _accumulate_bucket(bucket, self._make_ar(0.01))
        _finalize_bucket(bucket)
        self.assertTrue(bucket.small_sample)

    def test_not_small_sample_when_sufficient(self):
        bucket = BucketOutcome(label="test")
        for _ in range(SMALL_SAMPLE_WARNING + 1):
            _accumulate_bucket(bucket, self._make_ar(0.01))
        _finalize_bucket(bucket)
        self.assertFalse(bucket.small_sample)


# ---------------------------------------------------------------------------
# TestConfidenceTierBuckets
# ---------------------------------------------------------------------------

class TestConfidenceTierBuckets(unittest.TestCase):
    def test_confidence_tier_boundaries(self):
        self.assertEqual(_confidence_tier(0), "low")
        self.assertEqual(_confidence_tier(50), "low")
        self.assertEqual(_confidence_tier(51), "medium")
        self.assertEqual(_confidence_tier(80), "medium")
        self.assertEqual(_confidence_tier(81), "high")
        self.assertEqual(_confidence_tier(100), "high")

    def test_by_confidence_tier_in_outcome_result(self):
        snaps = _snapshots(
            ("2026-01-10", 100_000),
            ("2026-01-15", 105_000),   # +5% over 5 days
        )
        records = [
            _rec(run_id="2026-01-10_daily", confidence=30),  # low
            _rec(run_id="2026-01-10_daily", confidence=70),  # medium
            _rec(run_id="2026-01-10_daily", confidence=90),  # high
        ]
        with tempfile.TemporaryDirectory() as td:
            hist = Path(td) / "history.jsonl"
            db = Path(td) / "portfolio.db"
            _write_history(hist, records)
            _make_db(db, [
                ("2026-01-10_daily", 100_000, "2026-01-10T09:00:00"),
                ("2026-01-15_daily", 105_000, "2026-01-15T09:00:00"),
            ])
            result = run_outcome_attribution(history_path=hist, db_path=db)

        self.assertIn("low", result.by_confidence_tier)
        self.assertIn("medium", result.by_confidence_tier)
        self.assertIn("high", result.by_confidence_tier)

    def test_confidence_calibration_monotonicity_summary(self):
        summary = _confidence_calibration_summary({
            "low": {
                "hit_rate": 0.25,
                "avg_forward_return_5d": -0.01,
                "small_sample": False,
            },
            "medium": {
                "hit_rate": 0.50,
                "avg_forward_return_5d": 0.01,
                "small_sample": False,
            },
            "high": {
                "hit_rate": 0.75,
                "avg_forward_return_5d": 0.03,
                "small_sample": False,
            },
        })
        self.assertTrue(summary["monotonicity"]["hit_rate_monotonic"])
        self.assertTrue(summary["monotonicity"]["avg_return_5d_monotonic"])
        self.assertTrue(summary["monotonicity"]["overall"])

    def test_small_sample_bucket_warning_surfaces_in_calibration_notes(self):
        with tempfile.TemporaryDirectory() as td:
            hist = Path(td) / "history.jsonl"
            db = Path(td) / "portfolio.db"
            records = [
                _rec(run_id="2026-01-10_daily", confidence=30),
                _rec(run_id="2026-01-11_daily", confidence=70),
                _rec(run_id="2026-01-12_daily", confidence=90),
            ]
            _write_history(hist, records)
            _make_db(db, [
                ("2026-01-10_daily", 100_000, "2026-01-10T09:00:00"),
                ("2026-01-11_daily", 100_500, "2026-01-11T09:00:00"),
                ("2026-01-12_daily", 101_000, "2026-01-12T09:00:00"),
                ("2026-01-15_daily", 101_500, "2026-01-15T09:00:00"),
                ("2026-01-16_daily", 102_000, "2026-01-16T09:00:00"),
                ("2026-01-17_daily", 102_500, "2026-01-17T09:00:00"),
            ])
            result = run_outcome_attribution(history_path=hist, db_path=db)

        self.assertTrue(result.by_confidence_tier["low"]["small_sample"])
        self.assertTrue(any("small samples" in note for note in result.confidence_calibration["notes"]))


# ---------------------------------------------------------------------------
# TestDegradedModeBuckets
# ---------------------------------------------------------------------------

class TestDegradedModeBuckets(unittest.TestCase):
    def test_normal_vs_degraded_separate_buckets(self):
        snaps = _snapshots(
            ("2026-01-10", 100_000),
            ("2026-01-15", 103_000),
        )
        records = [
            _rec(run_id="2026-01-10_daily", degraded_mode=False),
            _rec(run_id="2026-01-10_daily", degraded_mode=True),
        ]
        with tempfile.TemporaryDirectory() as td:
            hist = Path(td) / "history.jsonl"
            db = Path(td) / "p.db"
            _write_history(hist, records)
            _make_db(db, [
                ("2026-01-10_daily", 100_000, "2026-01-10T09:00:00"),
                ("2026-01-15_daily", 103_000, "2026-01-15T09:00:00"),
            ])
            result = run_outcome_attribution(history_path=hist, db_path=db)

        self.assertIn("normal", result.by_degraded_mode)
        self.assertIn("degraded", result.by_degraded_mode)
        self.assertEqual(result.by_degraded_mode["normal"]["count"], 1)
        self.assertEqual(result.by_degraded_mode["degraded"]["count"], 1)


# ---------------------------------------------------------------------------
# TestRegimeBuckets
# ---------------------------------------------------------------------------

class TestRegimeBuckets(unittest.TestCase):
    def test_regime_split(self):
        with tempfile.TemporaryDirectory() as td:
            hist = Path(td) / "history.jsonl"
            db = Path(td) / "p.db"
            records = [
                _rec(run_id="2026-01-10_daily", drawdown_regime="normal"),
                _rec(run_id="2026-01-10_daily", drawdown_regime="aggressive"),
            ]
            _write_history(hist, records)
            _make_db(db, [
                ("2026-01-10_daily", 100_000, "2026-01-10T09:00:00"),
                ("2026-01-15_daily", 103_000, "2026-01-15T09:00:00"),
            ])
            result = run_outcome_attribution(history_path=hist, db_path=db)

        self.assertIn("normal", result.by_regime)
        self.assertIn("aggressive", result.by_regime)


# ---------------------------------------------------------------------------
# TestScoreQuintile
# ---------------------------------------------------------------------------

class TestScoreQuintile(unittest.TestCase):
    def test_quintile_boundaries(self):
        self.assertEqual(_score_quintile_label(0), "0-20")
        self.assertEqual(_score_quintile_label(20), "0-20")
        self.assertEqual(_score_quintile_label(21), "21-40")
        self.assertEqual(_score_quintile_label(40), "21-40")
        self.assertEqual(_score_quintile_label(41), "41-60")
        self.assertEqual(_score_quintile_label(60), "41-60")
        self.assertEqual(_score_quintile_label(61), "61-80")
        self.assertEqual(_score_quintile_label(80), "61-80")
        self.assertEqual(_score_quintile_label(81), "81-100")
        self.assertEqual(_score_quintile_label(100), "81-100")

    def test_aggregate_by_score_quintile_returns_list(self):
        snaps = _snapshots(
            ("2026-01-10", 100_000),
            ("2026-01-15", 102_000),
        )
        recs = [_rec(run_id="2026-01-10_daily", score=s) for s in [10, 30, 55, 70, 90]]
        attributed = attribute_outcomes(recs, snaps)
        result = _aggregate_by_score_quintile(attributed)
        self.assertIsInstance(result, list)
        # All 5 quintiles should be present (one rec per quintile)
        labels = [b["label"] for b in result]
        self.assertEqual(len(labels), 5)

    def test_empty_quintiles_omitted(self):
        snaps = _snapshots(("2026-01-10", 100_000), ("2026-01-15", 102_000))
        recs = [_rec(run_id="2026-01-10_daily", score=90)]  # only top quintile
        attributed = attribute_outcomes(recs, snaps)
        result = _aggregate_by_score_quintile(attributed)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["label"], "81-100")


# ---------------------------------------------------------------------------
# TestNotableItems
# ---------------------------------------------------------------------------

class TestNotableItems(unittest.TestCase):
    def _make_ar_with_5d(self, r5d: float) -> AttributedRecord:
        return AttributedRecord(
            rec_id="r", rec_base_id="r", run_id="2026-01-10_daily",
            run_date="2026-01-10", action_level="Recommended",
            confidence=70, confidence_tier="medium", score=60, raw_score=65,
            impact_area="Drift", priority=60, degraded_mode=False,
            data_mode="live", drawdown_regime="normal",
            portfolio_value_at_t=100_000,
            forward_return_1d=r5d, forward_return_3d=r5d,
            forward_return_5d=r5d, forward_return_10d=r5d,
            mfe=max(0.0, r5d), mae=min(0.0, r5d),
            attributable=True, attribution_note="ok",
        )

    def test_wins_ordered_best_first(self):
        attributed = [self._make_ar_with_5d(r) for r in [0.01, 0.05, 0.02]]
        wins, _ = _notable_items(attributed, n=2)
        self.assertAlmostEqual(wins[0]["forward_return_5d"], 0.05, places=5)

    def test_misses_ordered_worst_first(self):
        attributed = [self._make_ar_with_5d(r) for r in [-0.01, -0.05, -0.02]]
        _, misses = _notable_items(attributed, n=2)
        self.assertAlmostEqual(misses[0]["forward_return_5d"], -0.05, places=5)

    def test_empty_when_no_5d_data(self):
        ar = AttributedRecord(
            rec_id="r", rec_base_id="r", run_id="2026-01-10_daily",
            run_date="2026-01-10", action_level="Recommended",
            confidence=70, confidence_tier="medium", score=60, raw_score=65,
            impact_area="Drift", priority=60, degraded_mode=False,
            data_mode="live", drawdown_regime="normal",
            portfolio_value_at_t=None,
            forward_return_1d=None, forward_return_3d=None,
            forward_return_5d=None, forward_return_10d=None,
            mfe=None, mae=None,
            attributable=False, attribution_note="no snapshot",
        )
        wins, misses = _notable_items([ar])
        self.assertEqual(wins, [])
        self.assertEqual(misses, [])


# ---------------------------------------------------------------------------
# TestSparseData
# ---------------------------------------------------------------------------

class TestSparseData(unittest.TestCase):
    def test_missing_history_file(self):
        result = run_outcome_attribution(
            history_path=Path("/nonexistent/history.jsonl"),
            db_path=Path("/nonexistent/p.db"),
        )
        self.assertEqual(result.total_records, 0)
        self.assertEqual(result.attributable_records, 0)
        self.assertTrue(len(result.data_quality_notes) > 0)

    def test_missing_db_file(self):
        with tempfile.TemporaryDirectory() as td:
            hist = Path(td) / "history.jsonl"
            _write_history(hist, [_rec()])
            result = run_outcome_attribution(
                history_path=hist,
                db_path=Path(td) / "nonexistent.db",
            )
        self.assertEqual(result.total_records, 1)
        self.assertEqual(result.attributable_records, 0)
        self.assertTrue(any("absent" in n or "empty" in n for n in result.data_quality_notes))

    def test_empty_history_file(self):
        with tempfile.TemporaryDirectory() as td:
            hist = Path(td) / "history.jsonl"
            hist.write_text("")
            result = run_outcome_attribution(
                history_path=hist,
                db_path=Path(td) / "nonexistent.db",
            )
        self.assertEqual(result.total_records, 0)

    def test_no_forward_snapshots_beyond_last_date(self):
        """All recs are after the last snapshot — zero attributable."""
        with tempfile.TemporaryDirectory() as td:
            hist = Path(td) / "history.jsonl"
            db = Path(td) / "p.db"
            _write_history(hist, [_rec(run_id="2026-12-01_daily")])
            _make_db(db, [("2026-01-01_daily", 100_000, "2026-01-01T09:00:00")])
            result = run_outcome_attribution(history_path=hist, db_path=db)
        self.assertEqual(result.attributable_records, 0)

    def test_single_snapshot_no_forward_returns(self):
        """Exactly one snapshot — no forward returns possible."""
        with tempfile.TemporaryDirectory() as td:
            hist = Path(td) / "history.jsonl"
            db = Path(td) / "p.db"
            _write_history(hist, [_rec(run_id="2026-01-10_daily")])
            _make_db(db, [("2026-01-10_daily", 100_000, "2026-01-10T09:00:00")])
            result = run_outcome_attribution(history_path=hist, db_path=db)
        # attributable=False because no forward snapshots
        self.assertEqual(result.attributable_records, 0)

    def test_coverage_rate_null_when_no_records(self):
        with tempfile.TemporaryDirectory() as td:
            hist = Path(td) / "h.jsonl"
            hist.write_text("")
            result = run_outcome_attribution(history_path=hist)
        self.assertIsNone(result.coverage_rate)

    def test_data_quality_note_added_for_low_coverage(self):
        """When fewer than 50% of records are attributable, a note is added."""
        with tempfile.TemporaryDirectory() as td:
            hist = Path(td) / "h.jsonl"
            db = Path(td) / "p.db"
            # One rec in 2026, snapshot only in 2025 → zero coverage
            _write_history(hist, [_rec(run_id="2026-06-01_daily")])
            _make_db(db, [("2025-01-01_daily", 50_000, "2025-01-01T00:00:00")])
            result = run_outcome_attribution(history_path=hist, db_path=db)
        # Should have a coverage note
        notes_text = " ".join(result.data_quality_notes)
        self.assertTrue(len(result.data_quality_notes) > 0)

    def test_sparse_weekly_aliasing_is_reported(self):
        with tempfile.TemporaryDirectory() as td:
            hist = Path(td) / "h.jsonl"
            db = Path(td) / "p.db"
            _write_history(hist, [_rec(run_id="2026-01-10_daily")])
            _make_db(db, [
                ("2026-01-10_daily", 100_000, "2026-01-10T09:00:00"),
                ("2026-01-14_weekly", 101_000, "2026-01-14T09:00:00"),
            ])
            result = run_outcome_attribution(history_path=hist, db_path=db)
        self.assertEqual(result.sample_quality, "sparse_weekly")
        self.assertEqual(result.aliasing_notes["count_1d_3d_same_snapshot"], 1)
        self.assertEqual(result.aliasing_notes["count_3d_5d_same_snapshot"], 0)
        self.assertEqual(result.coverage_by_horizon["count_1d"], 1)
        self.assertEqual(result.coverage_by_horizon["count_3d"], 1)


# ---------------------------------------------------------------------------
# TestBackwardCompatibility
# ---------------------------------------------------------------------------

class TestBackwardCompatibility(unittest.TestCase):
    """Older recommendation history rows may be missing newer fields."""

    def test_missing_rec_base_id_falls_back_to_rec_id(self):
        rec = {"run_id": "2026-01-10_daily", "rec_id": "drift_SPY"}  # no rec_base_id
        snaps = _snapshots(("2026-01-10", 100_000), ("2026-01-15", 102_000))
        ar = _attribute_single(rec, snaps)
        self.assertEqual(ar.rec_base_id, "drift_SPY")

    def test_missing_confidence_defaults_to_100(self):
        rec = {"run_id": "2026-01-10_daily", "rec_id": "x"}  # no confidence
        snaps = _snapshots(("2026-01-10", 100_000))
        ar = _attribute_single(rec, snaps)
        self.assertEqual(ar.confidence, 100)
        self.assertEqual(ar.confidence_tier, "high")  # 100 → high

    def test_missing_score_defaults_to_0(self):
        rec = {"run_id": "2026-01-10_daily", "rec_id": "x"}
        snaps = _snapshots(("2026-01-10", 100_000))
        ar = _attribute_single(rec, snaps)
        self.assertEqual(ar.score, 0)

    def test_missing_degraded_mode_defaults_false(self):
        rec = {"run_id": "2026-01-10_daily", "rec_id": "x"}
        snaps = _snapshots(("2026-01-10", 100_000))
        ar = _attribute_single(rec, snaps)
        self.assertFalse(ar.degraded_mode)

    def test_minimal_record_does_not_crash(self):
        rec = {"run_id": "2026-01-10_daily"}  # absolute minimum
        snaps = _snapshots(("2026-01-10", 100_000), ("2026-01-15", 102_000))
        ar = _attribute_single(rec, snaps)
        self.assertIsNotNone(ar)

    def test_extra_unknown_fields_ignored(self):
        rec = _rec(run_id="2026-01-10_daily", unknown_field="value", another=42)
        snaps = _snapshots(("2026-01-10", 100_000), ("2026-01-15", 102_000))
        ar = _attribute_single(rec, snaps)
        self.assertTrue(ar.attributable)

    def test_bad_jsonl_lines_skipped_in_run_outcome_attribution(self):
        with tempfile.TemporaryDirectory() as td:
            hist = Path(td) / "h.jsonl"
            db = Path(td) / "p.db"
            hist.write_text(
                '{"run_id": "2026-01-10_daily", "rec_id": "drift_SPY"}\n'
                'NOT VALID JSON\n'
                '{"run_id": "2026-01-10_daily", "rec_id": "leverage_warn"}\n'
            )
            _make_db(db, [
                ("2026-01-10_daily", 100_000, "2026-01-10T09:00:00"),
                ("2026-01-15_daily", 102_000, "2026-01-15T09:00:00"),
            ])
            result = run_outcome_attribution(history_path=hist, db_path=db)
        # 2 valid records, 1 bad line skipped
        self.assertEqual(result.total_records, 2)


# ---------------------------------------------------------------------------
# TestReadOnly
# ---------------------------------------------------------------------------

class TestReadOnly(unittest.TestCase):
    """Attribution must never mutate the source history file or records."""

    def test_history_file_not_modified(self):
        with tempfile.TemporaryDirectory() as td:
            hist = Path(td) / "h.jsonl"
            db = Path(td) / "p.db"
            records = [_rec(run_id="2026-01-10_daily")]
            _write_history(hist, records)
            original_content = hist.read_text()
            _make_db(db, [
                ("2026-01-10_daily", 100_000, "2026-01-10T09:00:00"),
                ("2026-01-15_daily", 103_000, "2026-01-15T09:00:00"),
            ])
            run_outcome_attribution(history_path=hist, db_path=db)
            after_content = hist.read_text()
        self.assertEqual(original_content, after_content)

    def test_input_dicts_not_mutated(self):
        recs = [_rec(run_id="2026-01-10_daily") for _ in range(5)]
        original_copies = [dict(r) for r in recs]
        snaps = _snapshots(("2026-01-10", 100_000), ("2026-01-15", 102_000))
        attribute_outcomes(recs, snaps)
        for orig, after in zip(original_copies, recs):
            self.assertEqual(orig, after)


# ---------------------------------------------------------------------------
# TestAdditionalGroupings
# ---------------------------------------------------------------------------

class TestAdditionalGroupings(unittest.TestCase):
    def test_additional_groupings_are_populated(self):
        with tempfile.TemporaryDirectory() as td:
            hist = Path(td) / "h.jsonl"
            db = Path(td) / "p.db"
            _write_history(hist, [
                _rec(
                    run_id="2026-01-10_daily",
                    impact_area="Drift",
                    priority=75,
                    score=88,
                    action_level="Recommended",
                ),
                _rec(
                    run_id="2026-01-11_daily",
                    impact_area="Liquidity",
                    priority=25,
                    score=42,
                    action_level="Monitor",
                ),
            ])
            _make_db(db, [
                ("2026-01-10_daily", 100_000, "2026-01-10T09:00:00"),
                ("2026-01-11_daily", 100_500, "2026-01-11T09:00:00"),
                ("2026-01-15_daily", 101_500, "2026-01-15T09:00:00"),
                ("2026-01-16_daily", 102_000, "2026-01-16T09:00:00"),
            ])
            result = run_outcome_attribution(history_path=hist, db_path=db)

        self.assertIn("Drift", result.by_impact_area)
        self.assertIn("Liquidity", result.by_impact_area)
        self.assertIn("67-100", result.by_priority_bucket)
        self.assertIn("0-33", result.by_priority_bucket)
        self.assertIn("81-90", [bucket["label"] for bucket in result.by_score_decile])


# ---------------------------------------------------------------------------
# TestOutcomeWriter
# ---------------------------------------------------------------------------

class TestOutcomeWriter(unittest.TestCase):
    def _empty_result(self) -> OutcomeResult:
        return OutcomeResult(
            generated_at="2026-01-15T09:00:00",
            history_path="outputs/policy/recommendation_history.jsonl",
            db_path="data/portfolio.db",
        )

    def test_write_json_and_md(self):
        result = self._empty_result()
        with tempfile.TemporaryDirectory() as td:
            ok = write_outcome_reports(result, policy_dir=Path(td))
            self.assertTrue(ok)
            json_file = Path(td) / "recommendation_outcomes.json"
            md_file = Path(td) / "recommendation_outcomes.md"
            self.assertTrue(json_file.exists())
            self.assertTrue(md_file.exists())
            data = json.loads(json_file.read_text())
            self.assertIn("attribution_method", data)

    def test_json_is_valid(self):
        with tempfile.TemporaryDirectory() as td:
            hist = Path(td) / "h.jsonl"
            db = Path(td) / "p.db"
            _write_history(hist, [
                _rec(
                    run_id="2026-01-10_daily",
                    impact_area="Drift",
                    priority=75,
                    score=88,
                    action_level="Recommended",
                )
            ])
            _make_db(db, [
                ("2026-01-10_daily", 100_000, "2026-01-10T09:00:00"),
                ("2026-01-15_daily", 103_000, "2026-01-15T09:00:00"),
            ])
            result = run_outcome_attribution(history_path=hist, db_path=db)
            out = Path(td) / "reports"
            write_outcome_reports(result, policy_dir=out)
            data = json.loads((out / "recommendation_outcomes.json").read_text())
        self.assertIn("coverage", data)
        self.assertIn("coverage_by_horizon", data)
        self.assertIn("aliasing_notes", data)
        self.assertIn("sample_quality", data)
        self.assertIn("outcome_data_gaps", data)
        self.assertIn("overall", data)
        self.assertIn("by_confidence_tier", data)
        self.assertIn("confidence_calibration", data)
        self.assertIn("by_degraded_mode", data)
        self.assertIn("by_regime", data)
        self.assertIn("by_drawdown_regime", data)
        self.assertIn("by_impact_area", data)
        self.assertIn("by_priority_bucket", data)
        self.assertIn("by_score_decile", data)
        self.assertIn("outcome_thresholds", data)

    def test_dry_run_no_files_written(self):
        result = self._empty_result()
        with tempfile.TemporaryDirectory() as td:
            write_outcome_reports(result, policy_dir=Path(td), dry_run=True)
            files = list(Path(td).iterdir())
        self.assertEqual(files, [])

    def test_md_contains_attribution_method(self):
        with tempfile.TemporaryDirectory() as td:
            hist = Path(td) / "h.jsonl"
            db = Path(td) / "p.db"
            _write_history(hist, [_rec(run_id="2026-01-10_daily")])
            _make_db(db, [
                ("2026-01-10_daily", 100_000, "2026-01-10T09:00:00"),
                ("2026-01-15_daily", 103_000, "2026-01-15T09:00:00"),
            ])
            result = run_outcome_attribution(history_path=hist, db_path=db)
            write_outcome_reports(result, policy_dir=Path(td))
            md_text = (Path(td) / "recommendation_outcomes.md").read_text()
        self.assertIn("Attribution method", md_text)
        self.assertIn("Option A", md_text)
        self.assertIn("portfolio-level attribution", md_text)
        self.assertIn("Coverage by Horizon", md_text)

    def test_memo_empty_result(self):
        result = self._empty_result()
        memo = build_outcome_memo(result)
        self.assertIn("0", memo)

    def test_memo_with_attributable_records(self):
        with tempfile.TemporaryDirectory() as td:
            hist = Path(td) / "h.jsonl"
            db = Path(td) / "p.db"
            recs = [_rec(run_id=f"2026-01-{10+i:02d}_daily") for i in range(6)]
            _write_history(hist, recs)
            _make_db(db, [(f"2026-01-{10+i:02d}_daily", 100_000 + i * 500, f"2026-01-{10+i:02d}T09:00:00") for i in range(16)])
            result = run_outcome_attribution(history_path=hist, db_path=db)
            memo = build_outcome_memo(result)
        self.assertIn("attributed", memo)


# ---------------------------------------------------------------------------
# TestRoundTrip
# ---------------------------------------------------------------------------

class TestRoundTrip(unittest.TestCase):
    """End-to-end integration: build history, snapshots, run attribution, verify outputs."""

    def test_full_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            hist = Path(td) / "policy" / "recommendation_history.jsonl"
            db = Path(td) / "portfolio.db"

            # Two runs, 5 recs each, spanning 3 weeks
            run1_recs = [
                _rec(run_id="2026-01-05_daily", rec_id="drift_SPY", action_level="Recommended",
                     confidence=75, score=65, drawdown_regime="normal"),
                _rec(run_id="2026-01-05_daily", rec_id="emergency_fund", action_level="Action Required",
                     confidence=90, score=80, drawdown_regime="normal", degraded_mode=False),
                _rec(run_id="2026-01-05_daily", rec_id="leverage_warn", action_level="Monitor",
                     confidence=50, score=45, drawdown_regime="modest", degraded_mode=True),
                _rec(run_id="2026-01-12_daily", rec_id="savings_rate", action_level="FYI",
                     confidence=60, score=35, drawdown_regime="normal"),
                _rec(run_id="2026-01-12_daily", rec_id="drift_QQQ", action_level="Recommended",
                     confidence=80, score=70, drawdown_regime="modest"),
            ]
            _write_history(hist, run1_recs)

            # Portfolio snapshots: rising trend
            _make_db(db, [
                ("2026-01-05_daily", 100_000, "2026-01-05T09:00:00"),
                ("2026-01-06_daily", 100_500, "2026-01-06T09:00:00"),
                ("2026-01-08_daily", 101_200, "2026-01-08T09:00:00"),
                ("2026-01-10_daily", 101_800, "2026-01-10T09:00:00"),
                ("2026-01-12_daily", 102_500, "2026-01-12T09:00:00"),
                ("2026-01-15_daily", 103_200, "2026-01-15T09:00:00"),
                ("2026-01-17_daily", 103_800, "2026-01-17T09:00:00"),
                ("2026-01-19_daily", 104_200, "2026-01-19T09:00:00"),
                ("2026-01-22_daily", 104_900, "2026-01-22T09:00:00"),
            ])

            result = run_outcome_attribution(history_path=hist, db_path=db)

        # Basic checks
        self.assertEqual(result.total_records, 5)
        self.assertGreater(result.attributable_records, 0)
        self.assertIsNotNone(result.coverage_rate)

        # All attributed recs should have positive forward returns (rising portfolio)
        if result.hit_rate_overall is not None:
            self.assertGreater(result.hit_rate_overall, 0.0)

        # Breakdown keys should exist
        self.assertTrue(len(result.by_confidence_tier) > 0)
        self.assertTrue(len(result.by_degraded_mode) > 0)
        self.assertTrue(len(result.by_regime) > 0)
        self.assertTrue(len(result.by_action_level) > 0)

        # to_dict should serialize cleanly
        d = result.to_dict()
        serialized = json.dumps(d)
        self.assertIn("attribution_method", serialized)
        self.assertIn("option_a_portfolio_proxy", serialized)

        # Report write
        with tempfile.TemporaryDirectory() as out_td:
            ok = write_outcome_reports(result, policy_dir=Path(out_td))
            self.assertTrue(ok)
            json_data = json.loads((Path(out_td) / "recommendation_outcomes.json").read_text())
            md_text = (Path(out_td) / "recommendation_outcomes.md").read_text()

        self.assertEqual(json_data["attribution_method"], "option_a_portfolio_proxy")
        self.assertIn("# Recommendation Outcome Attribution Report", md_text)
        self.assertIn("Option A", md_text)
        self.assertIn("Outcome Thresholds", md_text)


if __name__ == "__main__":
    unittest.main()
