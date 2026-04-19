"""
GUI Insights — Read-Only Tests
================================
Covers five areas:

A. Confidence insight generation
B. Rotation insight generation
C. Execution insight generation
D. Data trust insight generation
E. Read-only guarantee

All tests exercise gui_insights.generate_insights() directly.
No Streamlit import is needed — correctness is verified at the data level.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from gui_insights import InsightCard, generate_insights


# ---------------------------------------------------------------------------
# Shared fixture builders
# ---------------------------------------------------------------------------

def _minimal_calibration(**overrides) -> dict:
    base: dict = {
        "observe_only": True,
        "status": "healthy",
        "sample_summary": {
            "low_matched": 4, "medium_matched": 6,
            "high_matched": 5, "total_matched": 15,
        },
        "low_win_rate": 0.25,
        "medium_win_rate": 0.55,
        "high_win_rate": 0.80,
        "band_order_valid": True,
        "strongest_band": "high",
        "weakest_band": "low",
        "recommendation": "Observe only.",
        "recommendation_reason": "Insufficient data.",
    }
    base.update(overrides)
    return base


def _minimal_execution(**overrides) -> dict:
    base: dict = {
        "generated_at": "2026-04-17T08:00:00",
        "total_events": 10,
        "matched_events": 8,
        "match_rate": 0.80,
        "by_action": [
            {
                "action": "BUY",
                "total_events": 8,
                "matched_events": 6,
                "win_rate": 0.67,
                "avg_gain": 0.030,
                "avg_loss": -0.015,
                "risk_reward": 2.0,
                "expectancy": 0.015,
                "avg_exit_quality": None,
            },
        ],
        "by_confidence_band": [
            {"name": "low",    "total_entries": 3, "attributable": 3,
             "win_rate": 0.33, "small_sample": True},
            {"name": "medium", "total_entries": 4, "attributable": 4,
             "win_rate": 0.50, "small_sample": True},
            {"name": "high",   "total_entries": 4, "attributable": 4,
             "win_rate": 0.75, "small_sample": True},
        ],
        "confidence_calibration": _minimal_calibration(),
    }
    base.update(overrides)
    return base


def _minimal_pa(**overrides) -> dict:
    base: dict = {
        "generated_at": "2026-04-17T08:00:00",
        "metrics": {
            "total_entries": 15,
            "attributable_entries": 12,
            "coverage_rate": 0.80,
            "win_rate": 0.60,
            "avg_gain": 0.030,
            "avg_loss": -0.018,
            "risk_reward": 1.67,
            "expectancy": 0.012,
        },
        "execution": _minimal_execution(),
        "data_quality_notes": [],
    }
    base.update(overrides)
    return base


def _rotation_event(
    strategy: str = "momentum",
    triggered: bool = True,
    actual_margin: float = 8.0,
    required_margin: float = 5.0,
    outcome_resolved: bool = False,
    forward_return_5d: float | None = None,
    degraded_mode: bool = False,
) -> dict:
    return {
        "event_id": "TSLA_run_001",
        "strategy_type": strategy,
        "rotation_triggered": triggered,
        "actual_margin": actual_margin,
        "required_margin": required_margin,
        "outcome_resolved": outcome_resolved,
        "forward_return_5d": forward_return_5d,
        "degraded_mode": degraded_mode,
        "challenger_is_breakout": False,
    }


# ---------------------------------------------------------------------------
# A. Confidence insight generation
# ---------------------------------------------------------------------------

class TestConfidenceInsight(unittest.TestCase):

    def _get_confidence(self, pa, rot_events=None) -> InsightCard:
        cards = generate_insights(pa, rot_events or [])
        return next(c for c in cards if c.category == "Confidence")

    def test_healthy_calibration_produces_healthy(self):
        pa = _minimal_pa()
        card = self._get_confidence(pa)
        self.assertEqual(card.status, "Healthy")

    def test_healthy_calibration_trust_reflects_sample_size(self):
        pa = _minimal_pa()
        card = self._get_confidence(pa)
        # total_matched=15 → medium trust (>=10 but <20)
        self.assertEqual(card.trust, "medium")

    def test_insufficient_total_matched_produces_insufficient_data(self):
        cal = _minimal_calibration(sample_summary={"total_matched": 3})
        ex = _minimal_execution(confidence_calibration=cal)
        pa = _minimal_pa(execution=ex)
        card = self._get_confidence(pa)
        self.assertEqual(card.status, "Insufficient Data")
        self.assertEqual(card.trust, "low")

    def test_weak_separation_produces_watch(self):
        cal = _minimal_calibration(status="weak_separation", band_order_valid=None)
        ex = _minimal_execution(confidence_calibration=cal)
        pa = _minimal_pa(execution=ex)
        card = self._get_confidence(pa)
        self.assertEqual(card.status, "Watch")

    def test_inverted_band_order_produces_investigate(self):
        cal = _minimal_calibration(band_order_valid=False)
        ex = _minimal_execution(confidence_calibration=cal)
        pa = _minimal_pa(execution=ex)
        card = self._get_confidence(pa)
        self.assertEqual(card.status, "Investigate")

    def test_no_execution_block_produces_insufficient_data(self):
        pa = _minimal_pa(execution=None)
        card = self._get_confidence(pa)
        self.assertEqual(card.status, "Insufficient Data")

    def test_empty_pa_produces_insufficient_data(self):
        card = self._get_confidence({})
        self.assertEqual(card.status, "Insufficient Data")

    def test_high_sample_size_produces_high_trust(self):
        cal = _minimal_calibration(
            sample_summary={"total_matched": 25},
        )
        ex = _minimal_execution(confidence_calibration=cal)
        pa = _minimal_pa(execution=ex)
        card = self._get_confidence(pa)
        self.assertEqual(card.trust, "high")


# ---------------------------------------------------------------------------
# B. Rotation insight generation
# ---------------------------------------------------------------------------

class TestRotationInsight(unittest.TestCase):

    def _get_rotation(self, rot_events) -> InsightCard:
        cards = generate_insights({}, rot_events)
        return next(c for c in cards if c.category == "Rotation")

    def test_no_rotation_events_produces_insufficient_data(self):
        card = self._get_rotation([])
        self.assertEqual(card.status, "Insufficient Data")

    def test_thin_data_fewer_than_5_produces_insufficient_data(self):
        events = [_rotation_event() for _ in range(3)]
        card = self._get_rotation(events)
        self.assertEqual(card.status, "Insufficient Data")

    def test_no_resolved_outcomes_produces_insufficient_data(self):
        events = [_rotation_event(outcome_resolved=False) for _ in range(8)]
        card = self._get_rotation(events)
        self.assertEqual(card.status, "Insufficient Data")

    def test_near_threshold_dominance_produces_watch(self):
        # actual_margin < required * 1.25: 3.0 < 5.0 * 1.25 = 6.25 → near-threshold
        near = [
            _rotation_event(actual_margin=3.0, required_margin=5.0,
                            outcome_resolved=True, forward_return_5d=0.01)
            for _ in range(6)
        ]
        # margin well above: 12 > 6.25 → not near-threshold
        strong = [
            _rotation_event(actual_margin=12.0, required_margin=5.0,
                            outcome_resolved=True, forward_return_5d=0.02)
            for _ in range(2)
        ]
        card = self._get_rotation(near + strong)
        self.assertIn(card.status, ("Watch", "Investigate"))

    def test_near_threshold_with_negative_returns_produces_investigate(self):
        events = [
            _rotation_event(actual_margin=3.0, required_margin=5.0,
                            outcome_resolved=True, forward_return_5d=-0.04)
            for _ in range(8)
        ]
        card = self._get_rotation(events)
        self.assertEqual(card.status, "Investigate")

    def test_healthy_rotation_pattern(self):
        events = [
            _rotation_event(actual_margin=10.0, required_margin=5.0,
                            outcome_resolved=True, forward_return_5d=0.03)
            for _ in range(10)
        ]
        card = self._get_rotation(events)
        self.assertEqual(card.status, "Healthy")

    def test_low_win_rate_produces_investigate(self):
        # all triggered, resolved, losing — should trigger win_rate < 0.4
        events = [
            _rotation_event(actual_margin=10.0, required_margin=5.0,
                            outcome_resolved=True, forward_return_5d=-0.03)
            for _ in range(8)
        ]
        card = self._get_rotation(events)
        self.assertEqual(card.status, "Investigate")


# ---------------------------------------------------------------------------
# C. Execution insight generation
# ---------------------------------------------------------------------------

class TestExecutionInsight(unittest.TestCase):

    def _get_execution(self, pa) -> InsightCard:
        cards = generate_insights(pa, [])
        return next(c for c in cards if c.category == "Execution")

    def test_no_execution_block_produces_insufficient_data(self):
        pa = _minimal_pa(execution=None)
        card = self._get_execution(pa)
        self.assertEqual(card.status, "Insufficient Data")

    def test_empty_pa_produces_insufficient_data(self):
        card = self._get_execution({})
        self.assertEqual(card.status, "Insufficient Data")

    def test_low_match_rate_below_30pct_produces_insufficient_data(self):
        ex = _minimal_execution(match_rate=0.25, total_events=10, matched_events=2)
        pa = _minimal_pa(execution=ex)
        card = self._get_execution(pa)
        self.assertEqual(card.status, "Insufficient Data")

    def test_medium_match_rate_produces_watch(self):
        ex = _minimal_execution(match_rate=0.40, total_events=10, matched_events=4)
        pa = _minimal_pa(execution=ex)
        card = self._get_execution(pa)
        self.assertEqual(card.status, "Watch")

    def test_positive_buy_expectancy_produces_healthy(self):
        ex = _minimal_execution(
            match_rate=0.80,
            by_action=[{
                "action": "BUY", "total_events": 8, "matched_events": 6,
                "win_rate": 0.67, "expectancy": 0.020,
                "avg_gain": 0.03, "avg_loss": -0.01,
                "risk_reward": 3.0, "avg_exit_quality": None,
            }],
        )
        pa = _minimal_pa(execution=ex)
        card = self._get_execution(pa)
        self.assertEqual(card.status, "Healthy")

    def test_low_buy_win_rate_produces_watch(self):
        ex = _minimal_execution(
            match_rate=0.80,
            by_action=[{
                "action": "BUY", "total_events": 8, "matched_events": 5,
                "win_rate": 0.30, "expectancy": -0.005,
                "avg_gain": 0.01, "avg_loss": -0.02,
                "risk_reward": 0.5, "avg_exit_quality": None,
            }],
        )
        pa = _minimal_pa(execution=ex)
        card = self._get_execution(pa)
        self.assertEqual(card.status, "Watch")

    def test_few_events_total_produces_insufficient_data(self):
        ex = _minimal_execution(total_events=2, matched_events=2, match_rate=1.0)
        pa = _minimal_pa(execution=ex)
        card = self._get_execution(pa)
        self.assertEqual(card.status, "Insufficient Data")


# ---------------------------------------------------------------------------
# D. Data trust insight generation
# ---------------------------------------------------------------------------

class TestDataTrustInsight(unittest.TestCase):

    def _get_data_trust(self, pa) -> InsightCard:
        cards = generate_insights(pa, [])
        return next(c for c in cards if c.category == "Data Trust")

    def test_missing_pa_produces_insufficient_data(self):
        card = self._get_data_trust(None)
        self.assertEqual(card.status, "Insufficient Data")

    def test_empty_pa_produces_insufficient_data(self):
        card = self._get_data_trust({})
        self.assertEqual(card.status, "Insufficient Data")

    def test_zero_entries_produces_insufficient_data(self):
        pa = _minimal_pa(metrics={"total_entries": 0, "coverage_rate": 0.0})
        card = self._get_data_trust(pa)
        self.assertEqual(card.status, "Insufficient Data")

    def test_small_entry_count_produces_insufficient_data(self):
        pa = _minimal_pa(metrics={"total_entries": 3, "coverage_rate": 0.70})
        card = self._get_data_trust(pa)
        self.assertEqual(card.status, "Insufficient Data")

    def test_low_coverage_produces_watch(self):
        pa = _minimal_pa(
            metrics={"total_entries": 12, "coverage_rate": 0.35},
            execution=None,
        )
        card = self._get_data_trust(pa)
        self.assertEqual(card.status, "Watch")

    def test_all_small_sample_bands_produces_watch(self):
        ex = _minimal_execution(by_confidence_band=[
            {"name": "low",    "total_entries": 1, "attributable": 1,
             "win_rate": 0.0,  "small_sample": True},
            {"name": "medium", "total_entries": 2, "attributable": 2,
             "win_rate": 0.5,  "small_sample": True},
            {"name": "high",   "total_entries": 2, "attributable": 2,
             "win_rate": 1.0,  "small_sample": True},
        ])
        pa = _minimal_pa(
            metrics={"total_entries": 12, "coverage_rate": 0.75},
            execution=ex,
        )
        card = self._get_data_trust(pa)
        self.assertEqual(card.status, "Watch")

    def test_sufficient_data_produces_healthy(self):
        ex = _minimal_execution(by_confidence_band=[
            {"name": "low",    "total_entries": 6,  "attributable": 6,
             "win_rate": 0.33, "small_sample": False},
            {"name": "medium", "total_entries": 8,  "attributable": 8,
             "win_rate": 0.55, "small_sample": False},
            {"name": "high",   "total_entries": 10, "attributable": 10,
             "win_rate": 0.80, "small_sample": False},
        ])
        pa = _minimal_pa(
            metrics={"total_entries": 24, "coverage_rate": 0.80},
            execution=ex,
        )
        card = self._get_data_trust(pa)
        self.assertEqual(card.status, "Healthy")


# ---------------------------------------------------------------------------
# E. Read-only guarantee
# ---------------------------------------------------------------------------

class TestReadOnlyGuarantee(unittest.TestCase):

    def test_always_returns_exactly_four_cards(self):
        for pa, rot in [
            (None, None),
            ({}, []),
            (_minimal_pa(), []),
            (_minimal_pa(), [_rotation_event() for _ in range(6)]),
        ]:
            with self.subTest(pa=bool(pa), rot=bool(rot)):
                cards = generate_insights(pa, rot)
                self.assertEqual(len(cards), 4)

    def test_all_cards_are_insightcard_instances(self):
        cards = generate_insights(_minimal_pa(), [])
        for card in cards:
            self.assertIsInstance(card, InsightCard)

    def test_pa_dict_not_mutated(self):
        import copy
        pa = _minimal_pa()
        original = copy.deepcopy(pa)
        generate_insights(pa, [])
        self.assertEqual(pa, original)

    def test_rot_events_list_not_mutated(self):
        events = [_rotation_event() for _ in range(5)]
        original_len = len(events)
        original_first = dict(events[0])
        generate_insights({}, events)
        self.assertEqual(len(events), original_len)
        self.assertEqual(events[0], original_first)

    def test_categories_cover_all_four_required(self):
        cards = generate_insights(_minimal_pa(), [])
        categories = {c.category for c in cards}
        self.assertSetEqual(categories, {"Confidence", "Rotation", "Execution", "Data Trust"})

    def test_status_values_are_valid(self):
        valid = {"Healthy", "Watch", "Investigate", "Insufficient Data"}
        cards = generate_insights(_minimal_pa(), [_rotation_event() for _ in range(6)])
        for card in cards:
            self.assertIn(card.status, valid, f"Invalid status '{card.status}' for {card.category}")

    def test_trust_values_are_valid(self):
        valid = {"low", "medium", "high"}
        cards = generate_insights(_minimal_pa(), [])
        for card in cards:
            self.assertIn(card.trust, valid, f"Invalid trust '{card.trust}' for {card.category}")

    def test_no_backend_imports_in_source(self):
        src = Path(__file__).parent.parent / "gui_insights.py"
        text = src.read_text(encoding="utf-8")
        forbidden = [
            "exit_engine",
            "portfolio_decision_engine",
            "allocation_engine",
            "replacement_gap_momentum",
            "DEFAULT_THRESHOLDS",
        ]
        for token in forbidden:
            self.assertNotIn(token, text, f"gui_insights.py must not reference '{token}'")

    def test_generate_insights_does_not_raise_on_malformed_pa(self):
        malformed_pa = {
            "metrics": {"total_entries": "not_a_number", "coverage_rate": None},
            "execution": {"match_rate": "bad", "total_events": None},
        }
        try:
            cards = generate_insights(malformed_pa, [])
            self.assertEqual(len(cards), 4)
        except Exception as exc:
            self.fail(f"generate_insights raised unexpectedly: {exc}")


if __name__ == "__main__":
    unittest.main()
