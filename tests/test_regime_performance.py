import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from watchlist_scanner.cache_manager import CacheManager
from watchlist_scanner.performance_feedback import (
    build_regime_performance_summary,
    generate_regime_performance_reports,
    record_scan_signals,
)
from watchlist_scanner.state import WatchlistStateStore


class TestRegimePerformance(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "portfolio.db"
        self.store = WatchlistStateStore(self.db_path)
        self.cache = CacheManager(self.root / "cache")

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _scan_result() -> dict:
        row = {
            "ticker": "AMD",
            "watchlist_source": "static",
            "signal_score": 0.80,
            "confidence_score": 0.90,
            "effective_score": 0.72,
            "conviction_score": 0.68,
            "conviction_band": "normal",
            "normalized_allocation": 0.01,
            "price": 100.0,
            "data_mode": "live",
            "notification_status": "alerted",
        }
        return {
            "generated_at": "2026-04-10T12:00:00",
            "data_mode": "live",
            "degraded_mode": False,
            "market_regime": {
                "regime_label": "risk_on",
                "regime_confidence": 0.72,
                "regime_data_quality": "partial",
            },
            "results": [dict(row)],
            "alerts": [dict(row)],
            "scan_summary": {},
        }

    def test_regime_tagging_correctness(self):
        record_scan_signals(self._scan_result(), db_path=self.db_path)
        rows = self.store.list_signal_feedback(limit=10)
        self.assertEqual(rows[0]["regime_label"], "risk_on")
        self.assertAlmostEqual(rows[0]["regime_confidence"], 0.72)
        self.assertEqual(rows[0]["regime_data_quality"], "partial")

    def test_regime_aggregation_accuracy(self):
        self.store.record_signal_feedback(
            signal_key="NVDA|static|2026-04-01T12:00:00",
            ticker="NVDA",
            signal_time="2026-04-01T12:00:00",
            signal_score=0.90,
            confidence_score=0.92,
            effective_score=0.83,
            conviction_score=0.85,
            conviction_band="high_conviction",
            normalized_allocation=0.02,
            price_at_signal=100.0,
            degraded_mode=False,
            regime_label="risk_on",
            regime_confidence=0.75,
            regime_data_quality="full",
        )
        self.store.resolve_signal_feedback(
            1,
            window_days=3,
            outcome_price=105.0,
            return_pct=5.0,
            outcome_success=True,
            direction_correct=True,
            evaluated_at="2026-04-04T00:00:00",
        )
        self.store.record_signal_feedback(
            signal_key="XLU|static|2026-04-02T12:00:00",
            ticker="XLU",
            signal_time="2026-04-02T12:00:00",
            signal_score=0.65,
            confidence_score=0.70,
            effective_score=0.46,
            conviction_score=0.32,
            conviction_band="observe",
            normalized_allocation=0.00,
            price_at_signal=100.0,
            degraded_mode=True,
            regime_label="risk_off",
            regime_confidence=0.68,
            regime_data_quality="degraded",
        )
        self.store.resolve_signal_feedback(
            2,
            window_days=3,
            outcome_price=97.0,
            return_pct=-3.0,
            outcome_success=False,
            direction_correct=False,
            evaluated_at="2026-04-05T00:00:00",
        )

        summary = build_regime_performance_summary(self.store.list_signal_feedback(limit=10))
        self.assertEqual(summary["by_regime"]["risk_on"]["total_signals"], 1)
        self.assertEqual(summary["by_regime"]["risk_on"]["best_conviction_band"], "high_conviction")
        self.assertAlmostEqual(summary["by_regime"]["risk_on"]["avg_return_pct"], 5.0)
        self.assertAlmostEqual(summary["by_regime"]["risk_off"]["avg_return_pct"], -3.0)

    # ---------------------------------------------------------------------
    # B4 correction: `by_regime` covers only rows RESOLVED at the primary
    # window, so a regime label observed but not yet matured is
    # indistinguishable from one never observed at all. Live 2026-07-28: 108
    # risk_off rows existed (2026-07-25..27) with ZERO resolved at the 3d
    # window, and the coverage assessor reported "never observed". The census
    # is additive instrumentation that makes the two absences separable.
    # ---------------------------------------------------------------------

    @staticmethod
    def _row(regime_label, *, resolved_3d=True, return_pct=1.0, quality="full"):
        row = {
            "regime_label": regime_label,
            "regime_confidence": 0.7,
            "regime_data_quality": quality,
            "conviction_band": "normal",
            "conviction_score": 0.5,
            "signal_score": 0.4,
            "normalized_allocation": 0.01,
            "signal_time": "2026-07-27T09:00:00",
            "degraded_mode": False,
        }
        if resolved_3d:
            row["outcome_return_3d"] = return_pct
            row["outcome_success_3d"] = return_pct > 0
        return row

    def test_regime_census_counts_observed_and_resolved_per_label(self):
        rows = (
            [self._row("neutral") for _ in range(5)]
            + [self._row("risk_off", resolved_3d=False) for _ in range(3)]
        )
        summary = build_regime_performance_summary(rows, primary_window_days=3)
        census = summary["regime_census"]
        self.assertEqual(census["primary_window_days"], 3)
        self.assertEqual(census["observed"]["neutral"], {"observed": 5, "resolved": 5})
        self.assertEqual(census["observed"]["risk_off"], {"observed": 3, "resolved": 0})

    def test_regime_census_includes_labels_absent_from_by_regime(self):
        # The whole point: a label with zero resolved rows is missing from
        # by_regime but MUST still appear in the census.
        rows = [self._row("neutral")] + [self._row("risk_off", resolved_3d=False)]
        summary = build_regime_performance_summary(rows, primary_window_days=3)
        self.assertNotIn("risk_off", summary["by_regime"])
        self.assertIn("risk_off", summary["regime_census"]["observed"])

    def test_regime_reports_are_written_without_affecting_rows(self):
        record_scan_signals(self._scan_result(), db_path=self.db_path)
        self.store.resolve_signal_feedback(
            1,
            window_days=3,
            outcome_price=104.0,
            return_pct=4.0,
            outcome_success=True,
            direction_correct=True,
            evaluated_at="2026-04-13T00:00:00",
        )
        before = self.store.list_signal_feedback(limit=10)
        report = generate_regime_performance_reports(
            db_path=self.db_path,
            output_dir=self.root / "outputs" / "regime",
        )
        after = self.store.list_signal_feedback(limit=10)

        json_path = Path(report["paths"]["json_path"])
        md_path = Path(report["paths"]["markdown_path"])
        self.assertTrue(json_path.exists())
        self.assertTrue(md_path.exists())
        self.assertEqual(before[0]["outcome_return_3d"], after[0]["outcome_return_3d"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestRegimeDrawdownBasis(unittest.TestCase):
    """`drawdown_pct` is a SIGNAL-LEVEL summed path, not a portfolio drawdown.

    _regime_drawdown_pct walks the cumulative SUM of every per-signal return in
    time order, so N overlapping same-day positions are added as though they were
    N sequential unit-size trades on the same capital. Live 2026-08-03: neutral
    reported drawdown_pct 826.68 next to avg_return_pct 0.223, and the same path's
    terminal value was +498% — prima facie not a capital-relative equity curve.
    The math and docstring are honest; the CONTRACT is not: the field ships as a
    bare `drawdown_pct` and regime_coverage re-exports it verbatim into an
    artifact the daily skill consumes, so a reader either dismisses it as a bug or
    escalates it as a catastrophe.
    """

    @staticmethod
    def _row(regime_label, day, return_pct):
        return {
            "regime_label": regime_label, "regime_confidence": 0.8,
            "regime_data_quality": "full", "conviction_band": "normal",
            "conviction_score": 0.5, "signal_score": 0.4,
            "normalized_allocation": 0.01, "degraded_mode": False,
            "signal_time": f"2026-07-{day:02d}T09:00:00",
            "outcome_return_3d": return_pct,
            "outcome_success_3d": return_pct > 0,
        }

    def _summary(self):
        # 3 days, 10 same-day signals each. Per-DAY moves: +1, -4, +1.
        rows = []
        for _ in range(10):
            rows.append(self._row("neutral", 1, 1.0))
            rows.append(self._row("neutral", 2, -4.0))
            rows.append(self._row("neutral", 3, 1.0))
        return build_regime_performance_summary(rows, primary_window_days=3)["by_regime"]["neutral"]

    def test_summed_signal_path_is_labelled_as_a_proxy(self):
        m = self._summary()
        self.assertEqual(m["drawdown_basis"], "cumulative_signal_path_proxy")

    def test_daily_path_drawdown_is_published_and_far_smaller(self):
        m = self._summary()
        # summed path: 10x(+1) then 10x(-4) -> peak +10, trough -30 => dd 40
        self.assertAlmostEqual(m["drawdown_pct"], 40.0, places=2)
        # equal-weight per-DAY path: +1, -4, +1 -> peak +1, trough -3 => dd 4
        self.assertAlmostEqual(m["daily_path_drawdown_pct"], 4.0, places=2)
        self.assertLess(m["daily_path_drawdown_pct"], m["drawdown_pct"])

    def test_daily_path_is_none_when_too_few_days(self):
        rows = [self._row("neutral", 1, 1.0) for _ in range(5)]
        m = build_regime_performance_summary(rows, primary_window_days=3)["by_regime"]["neutral"]
        self.assertIsNone(m["daily_path_drawdown_pct"])
