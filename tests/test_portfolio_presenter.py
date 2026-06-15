"""Phase 2C tests: portfolio presenter (view-model). Display-layer only — crowd is
subordinate context and never changes a pick's action."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui_v2.data import portfolio_presenter as P


def _crowd(label, present=True, severity="green", composite=0.3, conf=0.7,
           reasons=None, warnings=None):
    return {"present": present, "label": label, "severity": severity,
            "composite": composite, "confidence": conf,
            "top_reasons": reasons or [], "lines": ["ctx line"], "warnings": warnings or [],
            "enabled_sources": ["stock_grades"], "disabled_sources": [], "data_freshness": 0.9}


class TestScoring(unittest.TestCase):
    def test_confidence_pct_normalizes(self):
        self.assertEqual(P.confidence_pct(0.8), 80)
        self.assertEqual(P.confidence_pct(80), 80)
        self.assertEqual(P.confidence_pct(None), 0)
        self.assertEqual(P.confidence_pct(0.05), 5)   # fraction scaled
        self.assertEqual(P.confidence_pct(150), 100)  # clamped

    def test_conviction_bands(self):
        self.assertEqual(P.conviction_band(85), "High")
        self.assertEqual(P.conviction_band(50), "Medium")
        self.assertEqual(P.conviction_band(10), "Low")


class TestAdvisoryPicks(unittest.TestCase):
    def test_crowd_present_supportive_buy_agrees(self):
        d = {"ticker": "INTC", "action": "BUY", "confidence": 0.8, "rationale": "drift gap"}
        picks = P.build_advisory_picks([d], {"INTC": _crowd("Supportive")}, {})
        p = picks[0]
        self.assertEqual(p["action"], "BUY")            # unchanged
        self.assertEqual(p["crowd_agreement"], "Agree")
        self.assertFalse(p["crowd_disagrees"])
        self.assertEqual(p["conviction"], "High")

    def test_crowd_disagreement_surfaced_not_suppressed(self):
        d = {"ticker": "AMD", "action": "BUY", "confidence": 0.7}
        picks = P.build_advisory_picks([d], {"AMD": _crowd("Caution", severity="yellow", composite=-0.4)}, {})
        p = picks[0]
        self.assertEqual(p["crowd_agreement"], "Disagree")
        self.assertTrue(p["crowd_disagrees"])
        self.assertEqual(p["action"], "BUY")            # pick NOT suppressed/flipped by crowd

    def test_crowd_absent_honest_fallback(self):
        d = {"ticker": "XYZ", "action": "HOLD", "confidence": 0.5}
        picks = P.build_advisory_picks([d], {}, {})
        p = picks[0]
        self.assertEqual(p["crowd_agreement"], "Inconclusive")
        self.assertFalse(p["crowd_present"])
        self.assertIn("unavailable", p["crowd_row"].lower())

    def test_held_vs_candidate_portfolio_row(self):
        held = P.build_advisory_picks([{"ticker": "QQQ", "action": "HOLD"}], {},
                                      {"QQQ": {"symbol": "QQQ", "normalized_allocation_pct": 54.2}})
        self.assertIn("Held", held[0]["portfolio_row"])
        cand = P.build_advisory_picks([{"ticker": "KLAC", "action": "BUY"}], {}, {})
        self.assertIn("Candidate", cand[0]["portfolio_row"])


class TestCrowdOverlay(unittest.TestCase):
    def test_agreement_counts_and_coverage(self):
        picks = [
            {"crowd_present": True, "crowd_agreement": "Agree"},
            {"crowd_present": True, "crowd_agreement": "Disagree"},
            {"crowd_present": False, "crowd_agreement": "Inconclusive"},
        ]
        ov = P.build_crowd_overlay(picks, {"available": True, "enabled_categories": ["news", "analyst"],
                                           "social_disabled": True})
        self.assertEqual(ov["agree"], 1)
        self.assertEqual(ov["disagree"], 1)
        self.assertEqual(ov["inconclusive"], 1)
        self.assertEqual(ov["active_sources"], 2)
        self.assertEqual(ov["coverage_pct"], 67)  # 2/3
        self.assertEqual(len(ov["legend"]), 3)


class TestObserveOnlyInvariant(unittest.TestCase):
    def test_presenter_never_emits_trade_action_of_its_own(self):
        # The presenter only echoes the decision's action; it must not invent one.
        d = {"ticker": "AAA", "action": "WAIT", "confidence": 0.9}
        # even with a strongly supportive crowd, action stays WAIT
        picks = P.build_advisory_picks([d], {"AAA": _crowd("Supportive", composite=0.9)}, {})
        self.assertEqual(picks[0]["action"], "WAIT")
        # WAIT is neutral -> crowd cannot manufacture agreement into a buy/sell
        self.assertEqual(picks[0]["crowd_agreement"], "Inconclusive")

    def test_view_model_assembles_without_crowd(self):
        vm = P.build_view_model(
            decisions=[{"ticker": "KLAC", "action": "BUY", "confidence": 0.8}],
            crowd_by_symbol={}, crowd_status={"available": False},
            holdings=[], risk_delta={"portfolio_value": 1000, "concentration": {}},
            cash_summary={"cash_available": 50, "current_cash_pct": 0.05, "target_cash_pct": 0.05},
            portfolio_value=1000)
        self.assertEqual(vm["advisory_count"], 1)
        self.assertEqual(len(vm["summary_cards"]), 4)
        self.assertEqual(len(vm["why_these_picks"]), 4)
        self.assertEqual(vm["crowd_overlay"]["coverage_pct"], 0)


if __name__ == "__main__":
    unittest.main()
