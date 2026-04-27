from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from watchlist_scanner.performance_feedback import (
    build_final_rank_performance,
    build_portfolio_fit_performance,
    build_theme_alignment_performance,
    build_theme_type_performance,
)
from watchlist_scanner.state import WatchlistStateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row(
    *,
    ticker: str = "AMD",
    signal_score: float = 0.80,
    confidence_score: float = 0.85,
    theme_alignment_score: float | None = None,
    theme_top_name: str | None = None,
    theme_type: str | None = None,
    portfolio_fit_score: float | None = None,
    portfolio_fit_label: str | None = None,
    final_rank_score: float | None = None,
    augmented_signal_score: float | None = None,
    outcome_return_3d: float | None = None,
    outcome_success_3d: int | None = None,
    direction_correct_3d: int | None = None,
) -> dict:
    return {
        "ticker": ticker,
        "signal_score": signal_score,
        "confidence_score": confidence_score,
        "theme_alignment_score": theme_alignment_score,
        "theme_top_name": theme_top_name,
        "theme_type": theme_type,
        "portfolio_fit_score": portfolio_fit_score,
        "portfolio_fit_label": portfolio_fit_label,
        "final_rank_score": final_rank_score,
        "augmented_signal_score": augmented_signal_score,
        "outcome_return_3d": outcome_return_3d,
        "outcome_success_3d": outcome_success_3d,
        "direction_correct_3d": direction_correct_3d,
    }


def _resolved(
    *,
    theme_alignment_score: float | None = None,
    theme_type: str | None = None,
    portfolio_fit_score: float | None = None,
    portfolio_fit_label: str | None = None,
    final_rank_score: float | None = None,
    outcome_return_3d: float = 2.0,
    outcome_success_3d: int = 1,
    direction_correct_3d: int = 1,
) -> dict:
    return _row(
        theme_alignment_score=theme_alignment_score,
        theme_type=theme_type,
        portfolio_fit_score=portfolio_fit_score,
        portfolio_fit_label=portfolio_fit_label,
        final_rank_score=final_rank_score,
        outcome_return_3d=outcome_return_3d,
        outcome_success_3d=outcome_success_3d,
        direction_correct_3d=direction_correct_3d,
    )


# ---------------------------------------------------------------------------
# TestThemeAlignmentPerformance
# ---------------------------------------------------------------------------

class TestThemeAlignmentPerformance(unittest.TestCase):

    def test_empty_rows_returns_zero_counts(self):
        result = build_theme_alignment_performance([])
        self.assertEqual(result["total"], 0)
        for bucket in ("none", "weak", "moderate", "strong"):
            self.assertEqual(result["buckets"][bucket]["count"], 0)

    def test_none_score_goes_to_none_bucket(self):
        rows = [_row(theme_alignment_score=None)]
        result = build_theme_alignment_performance(rows)
        self.assertEqual(result["buckets"]["none"]["count"], 1)
        self.assertEqual(result["buckets"]["weak"]["count"], 0)

    def test_zero_score_goes_to_none_bucket(self):
        rows = [_row(theme_alignment_score=0.0)]
        result = build_theme_alignment_performance(rows)
        self.assertEqual(result["buckets"]["none"]["count"], 1)

    def test_weak_bucket(self):
        rows = [_row(theme_alignment_score=0.15)]
        result = build_theme_alignment_performance(rows)
        self.assertEqual(result["buckets"]["weak"]["count"], 1)

    def test_moderate_bucket(self):
        rows = [_row(theme_alignment_score=0.50)]
        result = build_theme_alignment_performance(rows)
        self.assertEqual(result["buckets"]["moderate"]["count"], 1)

    def test_strong_bucket(self):
        rows = [_row(theme_alignment_score=0.85)]
        result = build_theme_alignment_performance(rows)
        self.assertEqual(result["buckets"]["strong"]["count"], 1)

    def test_hit_rate_computed_correctly(self):
        rows = [
            _resolved(theme_alignment_score=0.80, outcome_success_3d=1),
            _resolved(theme_alignment_score=0.75, outcome_success_3d=0),
        ]
        result = build_theme_alignment_performance(rows)
        self.assertEqual(result["buckets"]["strong"]["resolved"], 2)
        self.assertAlmostEqual(result["buckets"]["strong"]["hit_rate"], 0.5)

    def test_no_resolved_returns_none_metrics(self):
        rows = [_row(theme_alignment_score=0.80)]
        result = build_theme_alignment_performance(rows)
        self.assertIsNone(result["buckets"]["strong"]["avg_return"])
        self.assertIsNone(result["buckets"]["strong"]["hit_rate"])

    def test_low_sample_warning_set_when_below_threshold(self):
        rows = [_resolved(theme_alignment_score=0.80)]
        result = build_theme_alignment_performance(rows)
        self.assertTrue(result["buckets"]["strong"]["low_sample_warning"])

    def test_no_division_by_zero_with_empty_bucket(self):
        rows = [_row(theme_alignment_score=0.0)]
        result = build_theme_alignment_performance(rows)
        self.assertIsNone(result["buckets"]["strong"]["avg_return"])

    def test_total_count_matches_input(self):
        rows = [
            _row(theme_alignment_score=0.1),
            _row(theme_alignment_score=0.5),
            _row(theme_alignment_score=0.9),
            _row(theme_alignment_score=None),
        ]
        result = build_theme_alignment_performance(rows)
        self.assertEqual(result["total"], 4)
        total_in_buckets = sum(b["count"] for b in result["buckets"].values())
        self.assertEqual(total_in_buckets, 4)


# ---------------------------------------------------------------------------
# TestPortfolioFitPerformance
# ---------------------------------------------------------------------------

class TestPortfolioFitPerformance(unittest.TestCase):

    def test_empty_rows(self):
        result = build_portfolio_fit_performance([])
        self.assertEqual(result["total"], 0)

    def test_poor_bucket(self):
        rows = [_row(portfolio_fit_score=0.20)]
        result = build_portfolio_fit_performance(rows)
        self.assertEqual(result["buckets"]["poor"]["count"], 1)

    def test_neutral_bucket(self):
        rows = [_row(portfolio_fit_score=0.45)]
        result = build_portfolio_fit_performance(rows)
        self.assertEqual(result["buckets"]["neutral"]["count"], 1)

    def test_good_bucket(self):
        rows = [_row(portfolio_fit_score=0.65)]
        result = build_portfolio_fit_performance(rows)
        self.assertEqual(result["buckets"]["good"]["count"], 1)

    def test_strong_bucket(self):
        rows = [_row(portfolio_fit_score=0.80)]
        result = build_portfolio_fit_performance(rows)
        self.assertEqual(result["buckets"]["strong"]["count"], 1)

    def test_label_fallback_when_score_absent(self):
        rows = [_row(portfolio_fit_score=None, portfolio_fit_label="good")]
        result = build_portfolio_fit_performance(rows)
        self.assertEqual(result["buckets"]["good"]["count"], 1)

    def test_neutral_default_when_no_score_or_label(self):
        rows = [_row(portfolio_fit_score=None, portfolio_fit_label=None)]
        result = build_portfolio_fit_performance(rows)
        self.assertEqual(result["buckets"]["neutral"]["count"], 1)

    def test_hit_rate_computed(self):
        rows = [
            _resolved(portfolio_fit_score=0.80, outcome_success_3d=1),
            _resolved(portfolio_fit_score=0.78, outcome_success_3d=1),
            _resolved(portfolio_fit_score=0.76, outcome_success_3d=0),
        ]
        result = build_portfolio_fit_performance(rows)
        strong = result["buckets"]["strong"]
        self.assertEqual(strong["resolved"], 3)
        self.assertAlmostEqual(strong["hit_rate"], round(2 / 3, 3))

    def test_no_division_by_zero_empty_bucket(self):
        rows = [_row(portfolio_fit_score=0.30)]
        result = build_portfolio_fit_performance(rows)
        self.assertIsNone(result["buckets"]["strong"]["avg_return"])

    def test_total_accounts_for_all_rows(self):
        rows = [
            _row(portfolio_fit_score=0.20),
            _row(portfolio_fit_score=0.45),
            _row(portfolio_fit_score=0.65),
            _row(portfolio_fit_score=0.85),
        ]
        result = build_portfolio_fit_performance(rows)
        self.assertEqual(result["total"], 4)
        self.assertEqual(sum(b["count"] for b in result["buckets"].values()), 4)


# ---------------------------------------------------------------------------
# TestFinalRankPerformance
# ---------------------------------------------------------------------------

class TestFinalRankPerformance(unittest.TestCase):

    def test_no_scored_rows_returns_empty_quartiles(self):
        rows = [_row(final_rank_score=None)]
        result = build_final_rank_performance(rows)
        self.assertEqual(result["scored"], 0)
        self.assertEqual(result["quartiles"], {})

    def test_empty_rows(self):
        result = build_final_rank_performance([])
        self.assertEqual(result["scored"], 0)

    def test_q1_has_highest_scores(self):
        rows = [
            _resolved(final_rank_score=0.9),
            _resolved(final_rank_score=0.8),
            _resolved(final_rank_score=0.5),
            _resolved(final_rank_score=0.2),
        ]
        result = build_final_rank_performance(rows)
        q1 = result["quartiles"]["Q1"]
        q4 = result["quartiles"]["Q4"]
        self.assertGreater(q1["avg_final_rank_score"], q4["avg_final_rank_score"])

    def test_quartile_counts_sum_to_total(self):
        rows = [_resolved(final_rank_score=float(i) / 10) for i in range(1, 9)]
        result = build_final_rank_performance(rows)
        total = sum(q["count"] for q in result["quartiles"].values())
        self.assertEqual(total, 8)

    def test_direction_correct_rate_computed(self):
        rows = [
            _resolved(final_rank_score=0.9, direction_correct_3d=1),
            _resolved(final_rank_score=0.85, direction_correct_3d=0),
            _resolved(final_rank_score=0.3, direction_correct_3d=1),
            _resolved(final_rank_score=0.1, direction_correct_3d=0),
        ]
        result = build_final_rank_performance(rows)
        self.assertIsNotNone(result["quartiles"]["Q1"]["direction_correct_rate"])

    def test_no_division_by_zero_unresolved(self):
        rows = [_row(final_rank_score=0.5, outcome_return_3d=None)]
        result = build_final_rank_performance(rows)
        self.assertIsNone(result["quartiles"]["Q1"]["avg_return"])

    def test_low_sample_warning_on_small_bucket(self):
        rows = [_resolved(final_rank_score=0.9)]
        result = build_final_rank_performance(rows)
        self.assertTrue(result["quartiles"]["Q1"]["low_sample_warning"])

    def test_scored_field_excludes_none_rank(self):
        rows = [
            _row(final_rank_score=0.8),
            _row(final_rank_score=None),
            _row(final_rank_score=0.6),
        ]
        result = build_final_rank_performance(rows)
        self.assertEqual(result["scored"], 2)
        self.assertEqual(result["total"], 3)


# ---------------------------------------------------------------------------
# TestThemeTypePerformance
# ---------------------------------------------------------------------------

class TestThemeTypePerformance(unittest.TestCase):

    def test_empty_rows(self):
        result = build_theme_type_performance([])
        self.assertEqual(result["total"], 0)
        for t in ("classified", "emerging", "none"):
            self.assertEqual(result["by_type"][t]["count"], 0)

    def test_classified_bucket(self):
        rows = [_row(theme_type="classified")]
        result = build_theme_type_performance(rows)
        self.assertEqual(result["by_type"]["classified"]["count"], 1)

    def test_emerging_bucket(self):
        rows = [_row(theme_type="emerging")]
        result = build_theme_type_performance(rows)
        self.assertEqual(result["by_type"]["emerging"]["count"], 1)

    def test_none_bucket_for_missing_type(self):
        rows = [_row(theme_type=None)]
        result = build_theme_type_performance(rows)
        self.assertEqual(result["by_type"]["none"]["count"], 1)

    def test_unknown_type_goes_to_none_bucket(self):
        rows = [_row(theme_type="sector_rotation")]
        result = build_theme_type_performance(rows)
        self.assertEqual(result["by_type"]["none"]["count"], 1)

    def test_hit_rate_classified_vs_emerging(self):
        rows = [
            _resolved(theme_type="classified", outcome_success_3d=1),
            _resolved(theme_type="classified", outcome_success_3d=1),
            _resolved(theme_type="emerging", outcome_success_3d=0),
        ]
        result = build_theme_type_performance(rows)
        self.assertEqual(result["by_type"]["classified"]["hit_rate"], 1.0)
        self.assertEqual(result["by_type"]["emerging"]["hit_rate"], 0.0)

    def test_no_division_by_zero_empty_type(self):
        rows = [_resolved(theme_type="classified")]
        result = build_theme_type_performance(rows)
        self.assertIsNone(result["by_type"]["emerging"]["avg_return"])

    def test_total_correct(self):
        rows = [
            _row(theme_type="classified"),
            _row(theme_type="emerging"),
            _row(theme_type=None),
        ]
        result = build_theme_type_performance(rows)
        self.assertEqual(result["total"], 3)


# ---------------------------------------------------------------------------
# TestStateStoreEnrichmentColumns
# ---------------------------------------------------------------------------

class TestStateStoreEnrichmentColumns(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "portfolio.db"
        self.store = WatchlistStateStore(self.db_path)

    def tearDown(self):
        self.store = None
        self.tmp.cleanup()

    def test_new_columns_stored_and_retrieved(self):
        created = self.store.record_signal_feedback(
            signal_key="NVDA|static|2026-04-01T12:00:00",
            ticker="NVDA",
            signal_time="2026-04-01T12:00:00",
            signal_score=0.85,
            confidence_score=0.90,
            price_at_signal=100.0,
            theme_alignment_score=0.72,
            theme_top_name="AI Infrastructure",
            theme_type="classified",
            portfolio_fit_score=0.68,
            portfolio_fit_label="good",
            final_rank_score=0.77,
            augmented_signal_score=0.88,
        )
        self.assertIsNotNone(created)
        self.assertAlmostEqual(float(created["theme_alignment_score"]), 0.72)
        self.assertEqual(created["theme_top_name"], "AI Infrastructure")
        self.assertEqual(created["theme_type"], "classified")
        self.assertAlmostEqual(float(created["portfolio_fit_score"]), 0.68)
        self.assertEqual(created["portfolio_fit_label"], "good")
        self.assertAlmostEqual(float(created["final_rank_score"]), 0.77)
        self.assertAlmostEqual(float(created["augmented_signal_score"]), 0.88)

    def test_null_enrichment_fields_default_to_none(self):
        created = self.store.record_signal_feedback(
            signal_key="TSLA|static|2026-04-02T12:00:00",
            ticker="TSLA",
            signal_time="2026-04-02T12:00:00",
            price_at_signal=200.0,
        )
        self.assertIsNotNone(created)
        self.assertIsNone(created["theme_alignment_score"])
        self.assertIsNone(created["portfolio_fit_score"])
        self.assertIsNone(created["final_rank_score"])

    def test_list_signal_feedback_includes_enrichment_columns(self):
        self.store.record_signal_feedback(
            signal_key="AMD|static|2026-04-03T12:00:00",
            ticker="AMD",
            signal_time="2026-04-03T12:00:00",
            price_at_signal=150.0,
            theme_alignment_score=0.55,
            portfolio_fit_score=0.62,
            final_rank_score=0.70,
        )
        rows = self.store.list_signal_feedback(limit=10)
        self.assertEqual(len(rows), 1)
        self.assertIn("theme_alignment_score", rows[0])
        self.assertIn("portfolio_fit_score", rows[0])
        self.assertIn("final_rank_score", rows[0])

    def test_existing_rows_without_enrichment_columns_safe(self):
        import sqlite3
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT INTO watchlist_signal_feedback (signal_key, ticker, signal_time) VALUES (?, ?, ?)",
            ("AMZN|static|2026-01-01T00:00:00", "AMZN", "2026-01-01T00:00:00"),
        )
        conn.commit()
        conn.close()
        rows = self.store.list_signal_feedback(limit=10)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].get("theme_alignment_score"))
        self.assertIsNone(rows[0].get("portfolio_fit_score"))

    def test_duplicate_signal_key_ignored(self):
        key = "MSFT|static|2026-04-04T12:00:00"
        self.store.record_signal_feedback(
            signal_key=key, ticker="MSFT", signal_time="2026-04-04T12:00:00",
            theme_alignment_score=0.60,
        )
        result2 = self.store.record_signal_feedback(
            signal_key=key, ticker="MSFT", signal_time="2026-04-04T12:00:00",
            theme_alignment_score=0.99,
        )
        rows = self.store.list_signal_feedback(limit=10)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(float(rows[0]["theme_alignment_score"]), 0.60)


# ---------------------------------------------------------------------------
# TestBucketStatEdgeCases
# ---------------------------------------------------------------------------

class TestBucketStatEdgeCases(unittest.TestCase):

    def test_avg_return_rounds_to_3_decimals(self):
        rows = [
            _resolved(theme_alignment_score=0.80, outcome_return_3d=1.111),
            _resolved(theme_alignment_score=0.75, outcome_return_3d=2.222),
        ]
        result = build_theme_alignment_performance(rows)
        avg = result["buckets"]["strong"]["avg_return"]
        self.assertEqual(avg, round((1.111 + 2.222) / 2, 3))

    def test_single_row_not_divide_by_zero(self):
        rows = [_resolved(final_rank_score=0.70)]
        result = build_final_rank_performance(rows)
        self.assertEqual(result["scored"], 1)

    def test_all_unresolved_no_crash(self):
        rows = [_row(portfolio_fit_score=0.80) for _ in range(5)]
        result = build_portfolio_fit_performance(rows)
        self.assertIsNone(result["buckets"]["strong"]["hit_rate"])

    def test_small_sample_warning_exactly_at_threshold(self):
        rows = [_resolved(theme_type="classified") for _ in range(10)]
        result = build_theme_type_performance(rows)
        self.assertFalse(result["by_type"]["classified"]["low_sample_warning"])

    def test_small_sample_warning_below_threshold(self):
        rows = [_resolved(theme_type="classified") for _ in range(9)]
        result = build_theme_type_performance(rows)
        self.assertTrue(result["by_type"]["classified"]["low_sample_warning"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
