import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from opportunity_ranker import RankedOpportunity, FactorBreakdown
from promotion_engine import promote_candidates, _COMPOUNDER_RS_MIN


def _opp(symbol, score, rs_score=80.0, events=None, reasons=None, rank=1):
    fb = FactorBreakdown(
        momentum=60.0,
        relative_strength=rs_score,
        volume_confirmation=60.0,
        volatility_sanity=80.0,
    )
    return RankedOpportunity(
        symbol=symbol,
        total_score=score,
        factor_breakdown=fb,
        reasons=reasons or [],
        events=events or [],
        rank=rank,
    )


class TestPromotionEngineConstants(unittest.TestCase):
    def test_compounder_rs_min_is_75(self):
        self.assertEqual(_COMPOUNDER_RS_MIN, 75.0)


class TestScoreFiltering(unittest.TestCase):
    def test_default_min_score_excludes_weak_candidates(self):
        # Default min_score is now 45 — score of 40 should not promote
        weak = _opp("WEAK", score=40.0)
        self.assertEqual(promote_candidates([weak]), [])

    def test_candidate_above_default_min_score_is_promoted(self):
        above = _opp("ABOVE", score=50.0)
        self.assertEqual(len(promote_candidates([above])), 1)

    def test_min_score_boundary_at_45(self):
        at_floor = _opp("AT", score=45.0)
        just_below = _opp("BELOW", score=44.9)
        self.assertEqual(len(promote_candidates([at_floor])), 1)
        self.assertEqual(promote_candidates([just_below]), [])

    def test_no_candidates_returns_empty_list(self):
        self.assertEqual(promote_candidates([]), [])


class TestLabelAssignment(unittest.TestCase):
    def test_compounder_label_requires_rs_at_least_75(self):
        # RS = 72 (below new threshold of 75) → not compounder.
        # BREAKOUT_PROXY is not a momentum event either, so label → watchlist.
        mid_rs = _opp("MID", score=60.0, rs_score=72.0, events=["BREAKOUT_PROXY"])
        promoted = promote_candidates([mid_rs])
        self.assertNotEqual(promoted[0].label, "compounder")

    def test_compounder_label_with_rs_above_75(self):
        strong_rs = _opp("HIGH", score=65.0, rs_score=80.0, events=["BREAKOUT_PROXY"])
        promoted = promote_candidates([strong_rs])
        self.assertEqual(promoted[0].label, "compounder")

    def test_compounder_requires_breakout_event(self):
        # RS is strong but no BREAKOUT_PROXY → not compounder
        no_breakout = _opp("NOBE", score=60.0, rs_score=90.0, events=[])
        promoted = promote_candidates([no_breakout])
        self.assertNotEqual(promoted[0].label, "compounder")

    def test_momentum_label_on_strong_move_up(self):
        mover = _opp("MOVE", score=55.0, events=["STRONG_MOVE_UP"])
        promoted = promote_candidates([mover])
        self.assertEqual(promoted[0].label, "momentum")

    def test_momentum_label_on_volume_spike(self):
        spike = _opp("SPKE", score=55.0, events=["VOLUME_SPIKE"])
        promoted = promote_candidates([spike])
        self.assertEqual(promoted[0].label, "momentum")

    def test_watchlist_when_no_decisive_events(self):
        quiet = _opp("QUIET", score=55.0, events=[])
        promoted = promote_candidates([quiet])
        self.assertEqual(promoted[0].label, "watchlist")


class TestTopNCapping(unittest.TestCase):
    def test_default_top_n_caps_at_15(self):
        # Generate 20 candidates above the score floor
        candidates = [_opp(f"S{i}", score=100.0 - i, rank=i + 1) for i in range(20)]
        promoted = promote_candidates(candidates)
        self.assertLessEqual(len(promoted), 15)

    def test_explicit_top_n_override_is_respected(self):
        candidates = [_opp(f"S{i}", score=100.0 - i, rank=i + 1) for i in range(20)]
        promoted = promote_candidates(candidates, config={"top_n": 5, "min_score": 0.0})
        self.assertLessEqual(len(promoted), 5)

    def test_explicit_min_score_zero_allows_low_scorers(self):
        low = _opp("LOW", score=5.0)
        promoted = promote_candidates([low], config={"min_score": 0.0})
        self.assertEqual(len(promoted), 1)


class TestEdgeCases(unittest.TestCase):
    def test_score_exactly_at_floor_is_included(self):
        at_floor = _opp("EXACT", score=45.0)
        self.assertEqual(len(promote_candidates([at_floor])), 1)

    def test_weak_signal_multi_candidate_only_top_quality_passes(self):
        strong = _opp("STR", score=80.0, rs_score=85.0, events=["BREAKOUT_PROXY"])
        too_weak = _opp("WK", score=30.0)
        promoted = promote_candidates([strong, too_weak])
        symbols = {p.symbol for p in promoted}
        self.assertIn("STR", symbols)
        self.assertNotIn("WK", symbols)


class TestConfigurableCompoundersRsMin(unittest.TestCase):
    def test_lower_rs_min_allows_borderline_rs_to_become_compounder(self):
        # RS=68 is below the default floor of 75 → watchlist with defaults.
        # With compounder_rs_min=65 it qualifies.
        borderline = _opp("BORD", score=60.0, rs_score=68.0, events=["BREAKOUT_PROXY"])
        default_result = promote_candidates([borderline])
        self.assertNotEqual(default_result[0].label, "compounder")

        relaxed_result = promote_candidates([borderline], config={"compounder_rs_min": 65.0})
        self.assertEqual(relaxed_result[0].label, "compounder")

    def test_higher_rs_min_blocks_borderline_rs_from_compounder(self):
        # RS=80 qualifies with the default floor (75) but not with floor=85.
        borderline = _opp("HIGH", score=65.0, rs_score=80.0, events=["BREAKOUT_PROXY"])
        default_result = promote_candidates([borderline])
        self.assertEqual(default_result[0].label, "compounder")

        strict_result = promote_candidates([borderline], config={"compounder_rs_min": 85.0})
        self.assertNotEqual(strict_result[0].label, "compounder")


if __name__ == "__main__":
    unittest.main(verbosity=2)
