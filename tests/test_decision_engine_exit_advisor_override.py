"""
P4.2 — Exit Advisor → decision_engine TRIM source.

When the Exit Advisor flags a held position, the unified decision_engine
maps the recommendation to a downgrade:
  EXIT_FULL    → SELL
  EXIT_HALF    → TRIM (new closed-set decision)
  TIGHTEN_STOP → HOLD
  HOLD         → no change

Rules:
  - SELL decisions are authoritative — never downgraded by exit_advisor.
  - Override only applies to BUY / SCALE / HOLD decisions.
  - WAIT / AVOID are conservative-by-default; left alone.
  - Records annotated with exit_advisor_override metadata + risk flag.
  - Plan-missing / symbol-missing / unknown-recommendation paths are no-ops.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from portfolio_automation.decision_engine import (  # noqa: E402
    DECISION_BUY,
    DECISION_HOLD,
    DECISION_SCALE,
    DECISION_SELL,
    DECISION_TRIM,
    DECISION_WAIT,
    apply_exit_advisor_override,
)


def _decision(symbol: str = "AAPL", decision: str = DECISION_BUY) -> dict:
    return {
        "symbol": symbol,
        "decision": decision,
        "source": "watchlist",
        "confidence_score": 0.80,
        "risk_flags": [],
    }


def _plan(symbol: str, recommendation: str, drawdown_pct: float = 0.05) -> dict:
    return {
        "observe_only": True,
        "schema_version": "1",
        "summary_line": f"{recommendation} {symbol}",
        "by_position": [
            {
                "symbol": symbol,
                "recommendation": recommendation,
                "drawdown_from_peak_pct": drawdown_pct,
                "reason": f"synthetic {recommendation}",
            }
        ],
    }


class TestExitAdvisorOverride(unittest.TestCase):

    # ---- Fallback paths ----------------------------------------------------

    def test_no_plan_returns_record_unchanged(self):
        rec = _decision()
        out = apply_exit_advisor_override(rec, None)
        self.assertEqual(out["decision"], DECISION_BUY)
        self.assertNotIn("exit_advisor_override", out)

    def test_empty_plan_returns_record_unchanged(self):
        rec = _decision()
        out = apply_exit_advisor_override(rec, {})
        self.assertEqual(out["decision"], DECISION_BUY)

    def test_symbol_not_in_plan_returns_unchanged(self):
        rec = _decision("AAPL")
        out = apply_exit_advisor_override(rec, _plan("MSFT", "EXIT_FULL"))
        self.assertEqual(out["decision"], DECISION_BUY)

    def test_malformed_plan_returns_unchanged(self):
        rec = _decision()
        out = apply_exit_advisor_override(rec, {"by_position": "not a list"})
        self.assertEqual(out["decision"], DECISION_BUY)

    def test_hold_recommendation_no_change(self):
        # Exit advisor says HOLD = "this position is fine"
        rec = _decision()
        out = apply_exit_advisor_override(rec, _plan("AAPL", "HOLD"))
        self.assertEqual(out["decision"], DECISION_BUY)

    def test_unknown_recommendation_no_change(self):
        rec = _decision()
        out = apply_exit_advisor_override(rec, _plan("AAPL", "ZZZ_UNKNOWN"))
        self.assertEqual(out["decision"], DECISION_BUY)

    # ---- EXIT_FULL → SELL --------------------------------------------------

    def test_exit_full_overrides_buy_to_sell(self):
        out = apply_exit_advisor_override(_decision("AAPL", DECISION_BUY), _plan("AAPL", "EXIT_FULL"))
        self.assertEqual(out["decision"], DECISION_SELL)

    def test_exit_full_overrides_scale_to_sell(self):
        out = apply_exit_advisor_override(_decision("AAPL", DECISION_SCALE), _plan("AAPL", "EXIT_FULL"))
        self.assertEqual(out["decision"], DECISION_SELL)

    def test_exit_full_overrides_hold_to_sell(self):
        out = apply_exit_advisor_override(_decision("AAPL", DECISION_HOLD), _plan("AAPL", "EXIT_FULL"))
        self.assertEqual(out["decision"], DECISION_SELL)

    # ---- EXIT_HALF → TRIM --------------------------------------------------

    def test_exit_half_overrides_buy_to_trim(self):
        out = apply_exit_advisor_override(_decision("AAPL", DECISION_BUY), _plan("AAPL", "EXIT_HALF"))
        self.assertEqual(out["decision"], DECISION_TRIM)

    def test_exit_half_overrides_scale_to_trim(self):
        out = apply_exit_advisor_override(_decision("AAPL", DECISION_SCALE), _plan("AAPL", "EXIT_HALF"))
        self.assertEqual(out["decision"], DECISION_TRIM)

    def test_exit_half_overrides_hold_to_trim(self):
        out = apply_exit_advisor_override(_decision("AAPL", DECISION_HOLD), _plan("AAPL", "EXIT_HALF"))
        self.assertEqual(out["decision"], DECISION_TRIM)

    # ---- TIGHTEN_STOP → HOLD ----------------------------------------------

    def test_tighten_stop_overrides_buy_to_hold(self):
        out = apply_exit_advisor_override(_decision("AAPL", DECISION_BUY), _plan("AAPL", "TIGHTEN_STOP"))
        self.assertEqual(out["decision"], DECISION_HOLD)

    def test_tighten_stop_overrides_scale_to_hold(self):
        out = apply_exit_advisor_override(_decision("AAPL", DECISION_SCALE), _plan("AAPL", "TIGHTEN_STOP"))
        self.assertEqual(out["decision"], DECISION_HOLD)

    def test_tighten_stop_does_not_upgrade_hold(self):
        # HOLD + TIGHTEN_STOP → HOLD (no change, no false annotation)
        out = apply_exit_advisor_override(_decision("AAPL", DECISION_HOLD), _plan("AAPL", "TIGHTEN_STOP"))
        self.assertEqual(out["decision"], DECISION_HOLD)
        # No override annotation when result == input
        self.assertNotIn("exit_advisor_override", out)

    # ---- Authoritative SELL preserved --------------------------------------

    def test_sell_decision_never_downgraded_by_exit_half(self):
        out = apply_exit_advisor_override(_decision("AAPL", DECISION_SELL), _plan("AAPL", "EXIT_HALF"))
        self.assertEqual(out["decision"], DECISION_SELL)
        self.assertNotIn("exit_advisor_override", out)

    def test_sell_decision_never_downgraded_by_tighten_stop(self):
        out = apply_exit_advisor_override(_decision("AAPL", DECISION_SELL), _plan("AAPL", "TIGHTEN_STOP"))
        self.assertEqual(out["decision"], DECISION_SELL)

    def test_sell_passthrough_on_exit_full(self):
        # SELL + EXIT_FULL: still SELL (no annotation since result == input)
        out = apply_exit_advisor_override(_decision("AAPL", DECISION_SELL), _plan("AAPL", "EXIT_FULL"))
        self.assertEqual(out["decision"], DECISION_SELL)
        self.assertNotIn("exit_advisor_override", out)

    # ---- Conservative decisions left alone ---------------------------------

    def test_wait_decision_left_alone(self):
        out = apply_exit_advisor_override(_decision("AAPL", DECISION_WAIT), _plan("AAPL", "EXIT_FULL"))
        # WAIT means "don't act on this opportunity" — exit advisor scope is
        # currently-held positions, so WAIT decisions are not exit candidates.
        self.assertEqual(out["decision"], DECISION_WAIT)

    # ---- Observability -----------------------------------------------------

    def test_override_annotation_recorded(self):
        out = apply_exit_advisor_override(_decision("AAPL", DECISION_BUY), _plan("AAPL", "EXIT_HALF"))
        meta = out["exit_advisor_override"]
        self.assertEqual(meta["from"], DECISION_BUY)
        self.assertEqual(meta["to"], DECISION_TRIM)
        self.assertEqual(meta["recommendation"], "EXIT_HALF")
        self.assertIn("reason", meta)

    def test_risk_flag_added_on_override(self):
        out = apply_exit_advisor_override(_decision("AAPL", DECISION_BUY), _plan("AAPL", "EXIT_FULL"))
        self.assertIn("exit_advisor_triggered", out["risk_flags"])

    def test_does_not_mutate_input(self):
        rec = _decision("AAPL", DECISION_BUY)
        rec_before = dict(rec)
        rec_before["risk_flags"] = list(rec["risk_flags"])
        apply_exit_advisor_override(rec, _plan("AAPL", "EXIT_FULL"))
        self.assertEqual(rec["decision"], rec_before["decision"])
        self.assertEqual(rec["risk_flags"], rec_before["risk_flags"])


class TestBuildDecisionPlanThreadsExitAdvisor(unittest.TestCase):
    """Smoke-level integration: build_decision_plan accepts exit_advisor_plan."""

    def test_build_decision_plan_accepts_exit_advisor_kwarg(self):
        from portfolio_automation.decision_engine import build_decision_plan
        plan = build_decision_plan(
            structural_violations=[],
            portfolio_adjustments=[],
            watchlist_signals=[],
            market_opportunities=[],
            finance_recommendations=[],
            portfolio_context={},
            exit_advisor_plan=None,
        )
        self.assertIsInstance(plan, list)


if __name__ == "__main__":
    unittest.main(verbosity=2)
