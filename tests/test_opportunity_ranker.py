import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from opportunity_ranker import rank_opportunities, _DEFAULT_WEIGHTS


def _sr(symbol, pct_change_1d=2.0, pct_from_year_high=-5.0, rel_volume=2.0, day_range_pct=1.5, volume=None):
    return SimpleNamespace(
        symbol=symbol,
        has_price=True,
        pct_change_1d=pct_change_1d,
        pct_from_year_high=pct_from_year_high,
        rel_volume=rel_volume,
        volume=volume,
        day_range_pct=day_range_pct,
    )


class TestDefaultWeights(unittest.TestCase):
    def test_momentum_weight_is_30pct(self):
        self.assertAlmostEqual(_DEFAULT_WEIGHTS["momentum"], 0.30, places=2)

    def test_rs_weight_is_30pct(self):
        self.assertAlmostEqual(_DEFAULT_WEIGHTS["relative_strength"], 0.30, places=2)

    def test_volume_weight_is_30pct(self):
        self.assertAlmostEqual(_DEFAULT_WEIGHTS["volume_confirmation"], 0.30, places=2)

    def test_volatility_weight_is_10pct(self):
        self.assertAlmostEqual(_DEFAULT_WEIGHTS["volatility_sanity"], 0.10, places=2)

    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(_DEFAULT_WEIGHTS.values()), 1.0, places=6)


class TestRankingBehavior(unittest.TestCase):
    def test_empty_scan_results_returns_empty_list(self):
        self.assertEqual(rank_opportunities([], events=[]), [])

    def test_no_price_result_is_excluded(self):
        no_price = SimpleNamespace(
            symbol="NOPX", has_price=False,
            pct_change_1d=5.0, pct_from_year_high=0.0,
            rel_volume=3.0, day_range_pct=1.0, volume=None,
        )
        self.assertEqual(rank_opportunities([no_price], events=[]), [])

    def test_ranking_order_descending_by_score(self):
        strong = _sr("STRONG", pct_change_1d=5.0, rel_volume=3.0, pct_from_year_high=-1.0)
        weak = _sr("WEAK", pct_change_1d=0.3, rel_volume=0.4, pct_from_year_high=-18.0)
        results = rank_opportunities([weak, strong], events=[])
        self.assertEqual(results[0].symbol, "STRONG")
        self.assertEqual(results[0].rank, 1)
        self.assertEqual(results[1].rank, 2)

    def test_min_score_filter_excludes_low_scorers(self):
        weak = _sr("WEAK", pct_change_1d=0.1, rel_volume=0.4, pct_from_year_high=-19.0)
        results = rank_opportunities([weak], events=[], config={"min_score": 50.0})
        self.assertEqual(results, [])

    def test_negative_momentum_contributes_zero_pts(self):
        sr = _sr("NEG", pct_change_1d=-3.0, rel_volume=3.0, pct_from_year_high=-2.0)
        results = rank_opportunities([sr], events=[])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].factor_breakdown.momentum, 0.0)

    def test_total_score_in_valid_range(self):
        perfect = _sr("PERF", pct_change_1d=5.0, rel_volume=3.0, pct_from_year_high=0.0, day_range_pct=1.0)
        results = rank_opportunities([perfect], events=[])
        self.assertGreaterEqual(results[0].total_score, 0.0)
        self.assertLessEqual(results[0].total_score, 100.0)


class TestVolumeGatedMomentum(unittest.TestCase):
    def test_high_volume_move_outscores_thin_volume_same_pct(self):
        high_vol = _sr("HVOL", pct_change_1d=4.0, rel_volume=3.0, pct_from_year_high=-3.0)
        thin_vol = _sr("TVOL", pct_change_1d=4.0, rel_volume=0.3, pct_from_year_high=-3.0)
        results = rank_opportunities([high_vol, thin_vol], events=[])
        scores = {r.symbol: r.total_score for r in results}
        self.assertGreater(scores["HVOL"], scores["TVOL"])

    def test_below_average_volume_discounts_momentum_factor(self):
        thin = _sr("THIN", pct_change_1d=5.0, rel_volume=0.3, pct_from_year_high=-5.0)
        results = rank_opportunities([thin], events=[])
        self.assertEqual(len(results), 1)
        # rel_volume=0.3 → vol_adj=max(0.2, 0.3)=0.3 → momentum_raw=100*0.3=30
        self.assertAlmostEqual(results[0].factor_breakdown.momentum, 30.0, places=1)

    def test_above_average_volume_no_momentum_penalty(self):
        normal = _sr("NORM", pct_change_1d=5.0, rel_volume=1.5, pct_from_year_high=-5.0)
        results = rank_opportunities([normal], events=[])
        self.assertEqual(len(results), 1)
        # rel_volume >= 1.0 → no penalty → momentum_raw = 100
        self.assertAlmostEqual(results[0].factor_breakdown.momentum, 100.0, places=1)

    def test_volume_at_boundary_exactly_1x_no_penalty(self):
        boundary = _sr("BNDRY", pct_change_1d=4.0, rel_volume=1.0, pct_from_year_high=-5.0)
        results = rank_opportunities([boundary], events=[])
        # pct=4 → 4/5*100=80; rel_volume=1.0 → no penalty
        self.assertAlmostEqual(results[0].factor_breakdown.momentum, 80.0, places=1)

    def test_light_volume_reason_string_included(self):
        thin = _sr("THIN2", pct_change_1d=3.0, rel_volume=0.5, pct_from_year_high=-5.0)
        results = rank_opportunities([thin], events=[])
        reasons_joined = " ".join(results[0].reasons)
        self.assertIn("light volume", reasons_joined)

    def test_missing_rel_volume_no_penalty_applied(self):
        no_vol = _sr("NOVOL", pct_change_1d=5.0, rel_volume=None, pct_from_year_high=-5.0)
        results = rank_opportunities([no_vol], events=[])
        # No rel_volume → no penalty → momentum_raw = 100
        self.assertAlmostEqual(results[0].factor_breakdown.momentum, 100.0, places=1)

    def test_very_low_volume_floor_is_02(self):
        # rel_volume=0.05 → vol_adj=max(0.2, 0.05)=0.2 → momentum_raw=100*0.2=20
        tiny_vol = _sr("TINY", pct_change_1d=5.0, rel_volume=0.05, pct_from_year_high=-5.0)
        results = rank_opportunities([tiny_vol], events=[])
        self.assertAlmostEqual(results[0].factor_breakdown.momentum, 20.0, places=1)

    def test_half_volume_penalty_is_proportional(self):
        # rel_volume=0.5 → vol_adj=max(0.2, 0.5)=0.5 → no change from old behaviour at 0.5
        half_vol = _sr("HALF", pct_change_1d=5.0, rel_volume=0.5, pct_from_year_high=-5.0)
        results = rank_opportunities([half_vol], events=[])
        self.assertAlmostEqual(results[0].factor_breakdown.momentum, 50.0, places=1)


class TestVolatilitySanity(unittest.TestCase):
    def test_low_range_scores_perfect(self):
        clean = _sr("CLEAN", pct_change_1d=2.0, rel_volume=1.0, pct_from_year_high=-5.0, day_range_pct=1.5)
        results = rank_opportunities([clean], events=[])
        self.assertAlmostEqual(results[0].factor_breakdown.volatility_sanity, 100.0, places=1)

    def test_range_above_12pct_scores_zero(self):
        wild = _sr("WILD", pct_change_1d=2.0, rel_volume=1.0, pct_from_year_high=-5.0, day_range_pct=13.0)
        results = rank_opportunities([wild], events=[])
        self.assertAlmostEqual(results[0].factor_breakdown.volatility_sanity, 0.0, places=1)

    def test_range_at_old_cliff_now_gets_nonzero_score(self):
        # 10.5% range: old code → 0 pts (above old 10% cliff); new code → non-zero
        borderline = _sr("BORD", pct_change_1d=2.0, rel_volume=1.0, pct_from_year_high=-5.0, day_range_pct=10.5)
        results = rank_opportunities([borderline], events=[])
        self.assertGreater(results[0].factor_breakdown.volatility_sanity, 0.0)

    def test_range_exactly_at_12pct_scores_zero(self):
        at_cliff = _sr("CLIFF", pct_change_1d=2.0, rel_volume=1.0, pct_from_year_high=-5.0, day_range_pct=12.0)
        results = rank_opportunities([at_cliff], events=[])
        self.assertAlmostEqual(results[0].factor_breakdown.volatility_sanity, 0.0, places=1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
