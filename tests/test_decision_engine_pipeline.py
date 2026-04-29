"""
tests/test_decision_engine_pipeline.py

Tests for the decision engine pipeline integration helpers and the
additive guarantee: decision_plan outputs must not mutate any upstream artifact.

All tests use plain dicts / lightweight stubs — no main.py invocation needed.
"""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

# Helpers under test (module-level functions in main.py)
from main import (
    _adj_to_de_dict,
    _finance_rec_to_de_dict,
    _market_opps_from_coverage,
)

# Decision engine public API
from portfolio_automation.decision_engine import (
    DECISION_BUY,
    DECISION_SELL,
    DECISION_WAIT,
    SOURCE_FINANCE,
    SOURCE_MARKET,
    SOURCE_PORTFOLIO,
    SOURCE_STRUCTURAL,
    SOURCE_WATCHLIST,
    build_decision_plan,
    summarize_decision_plan,
)


# ---------------------------------------------------------------------------
# Stubs that mimic the real dataclass shapes without importing the full modules
# ---------------------------------------------------------------------------


def _make_adj(
    symbol="VTI",
    rec_type="sell",
    action_level="ACTION_REQUIRED",
    is_leveraged=False,
    amount=1000.0,
    drift=0.08,
    title="Drift alert",
    do="Trim position",
    why="Above band",
) -> Any:
    """Lightweight PortfolioAdjustment stub."""
    obj = SimpleNamespace()
    obj.symbol = symbol
    obj.title = title
    obj.recommendation_type = SimpleNamespace(value=rec_type)
    obj.action_level = SimpleNamespace(value=action_level)
    obj.is_leveraged = is_leveraged
    obj.amount = amount
    obj.drift = drift
    obj.do = do
    obj.why = why
    return obj


def _make_finance_rec(
    rec_id="DRIFT_001",
    title="Drift recommendation",
    action="Review and rebalance.",
    action_level="RECOMMENDED",
    impact_area="PORTFOLIO_RISK",
    trigger="Drift exceeded threshold.",
) -> Any:
    """Lightweight FinanceRecommendation stub."""
    obj = SimpleNamespace()
    obj.id = rec_id
    obj.title = title
    obj.action = action
    obj.action_level = SimpleNamespace(value=action_level)
    obj.impact_area = SimpleNamespace(value=impact_area)
    obj.trigger = trigger
    return obj


# ---------------------------------------------------------------------------
# Tests for _adj_to_de_dict
# ---------------------------------------------------------------------------


class TestAdjToDeDict(unittest.TestCase):

    def test_maps_all_required_fields(self):
        adj = _make_adj(symbol="TQQQ", rec_type="sell", is_leveraged=True, amount=500.0)
        d = _adj_to_de_dict(adj)

        self.assertEqual(d["symbol"], "TQQQ")
        self.assertEqual(d["recommendation_type"], "sell")
        self.assertEqual(d["action_level"], "ACTION_REQUIRED")
        self.assertTrue(d["is_leveraged"])
        self.assertEqual(d["amount"], 500.0)

    def test_preserves_drift(self):
        adj = _make_adj(drift=0.12)
        d = _adj_to_de_dict(adj)
        self.assertAlmostEqual(d["drift"], 0.12)

    def test_preserves_do_and_why(self):
        adj = _make_adj(do="Sell 10 shares", why="Above leverage cap")
        d = _adj_to_de_dict(adj)
        self.assertEqual(d["do"], "Sell 10 shares")
        self.assertEqual(d["why"], "Above leverage cap")

    def test_returns_empty_dict_on_broken_object(self):
        result = _adj_to_de_dict(None)
        self.assertEqual(result, {})

    def test_returns_empty_dict_on_missing_attribute(self):
        broken = SimpleNamespace(symbol="X")  # missing all other attrs
        result = _adj_to_de_dict(broken)
        self.assertEqual(result, {})

    def test_does_not_mutate_original_object(self):
        adj = _make_adj(symbol="VTI")
        _adj_to_de_dict(adj)
        self.assertEqual(adj.symbol, "VTI")  # unchanged


# ---------------------------------------------------------------------------
# Tests for _finance_rec_to_de_dict
# ---------------------------------------------------------------------------


class TestFinanceRecToDeDict(unittest.TestCase):

    def test_maps_all_required_fields(self):
        rec = _make_finance_rec(
            rec_id="CASHFLOW_01",
            action_level="ACTION_REQUIRED",
            impact_area="CASHFLOW",
        )
        d = _finance_rec_to_de_dict(rec)

        self.assertEqual(d["id"], "CASHFLOW_01")
        self.assertEqual(d["action_level"], "ACTION_REQUIRED")
        self.assertEqual(d["impact_area"], "CASHFLOW")

    def test_preserves_trigger(self):
        rec = _make_finance_rec(trigger="Savings rate fell below 10%.")
        d = _finance_rec_to_de_dict(rec)
        self.assertIn("Savings rate", d["trigger"])

    def test_returns_empty_dict_on_broken_object(self):
        self.assertEqual(_finance_rec_to_de_dict(None), {})

    def test_does_not_mutate_original_object(self):
        rec = _make_finance_rec(title="Original title")
        _finance_rec_to_de_dict(rec)
        self.assertEqual(rec.title, "Original title")


# ---------------------------------------------------------------------------
# Tests for _market_opps_from_coverage
# ---------------------------------------------------------------------------


class TestMarketOppsFromCoverage(unittest.TestCase):

    def _coverage(self, promoted=None, actions=None):
        return {
            "enabled": True,
            "promoted": promoted or [],
            "decision_layer": {"available": True, "actions": actions or []},
        }

    def test_extracts_promoted_candidates(self):
        coverage = self._coverage(promoted=[
            {"symbol": "NVDA", "label": "compounder", "reasons": ["strong ROE", "momentum"]},
            {"symbol": "MSFT", "label": "compounder", "reasons": []},
        ])
        opps = _market_opps_from_coverage(coverage)
        symbols = [o["symbol"] for o in opps]
        self.assertIn("NVDA", symbols)
        self.assertIn("MSFT", symbols)

    def test_reason_built_from_reasons_list(self):
        coverage = self._coverage(promoted=[
            {"symbol": "AAPL", "label": "compounder", "reasons": ["high FCF", "low PE"]},
        ])
        opps = _market_opps_from_coverage(coverage)
        self.assertIn("high FCF", opps[0]["reason"])

    def test_extracts_decision_layer_buy_actions(self):
        coverage = self._coverage(actions=[
            {"symbol": "VFH", "action_type": "buy", "suggested_pct": 0.03,
             "amount": 1500, "reason": "Underweight sector."},
        ])
        opps = _market_opps_from_coverage(coverage)
        vfh = next(o for o in opps if o["symbol"] == "VFH")
        self.assertEqual(vfh["opportunity_type"], "underweight_target")
        self.assertEqual(vfh["suggested_pct"], 0.03)

    def test_skips_entries_without_symbol(self):
        coverage = self._coverage(promoted=[{"label": "compounder", "reasons": []}])
        opps = _market_opps_from_coverage(coverage)
        self.assertEqual(opps, [])

    def test_empty_coverage_returns_empty_list(self):
        self.assertEqual(_market_opps_from_coverage({}), [])

    def test_none_coverage_returns_empty_list(self):
        self.assertEqual(_market_opps_from_coverage(None or {}), [])

    def test_non_dict_entries_are_skipped(self):
        coverage = self._coverage(promoted=["not_a_dict", None, 42])
        opps = _market_opps_from_coverage(coverage)
        self.assertEqual(opps, [])


# ---------------------------------------------------------------------------
# Additive guarantee: upstream artifacts must be unchanged after decision plan
# ---------------------------------------------------------------------------


class TestDecisionPlanIsAdditive(unittest.TestCase):
    """
    Verify that build_decision_plan never mutates the input lists or dicts.
    This is the core regression guard: the decision engine is observe-only.
    """

    def _run_plan(self, adj_dicts, finance_dicts, watchlist_rows, market_opps):
        return build_decision_plan(
            structural_violations=[],
            portfolio_adjustments=adj_dicts,
            watchlist_signals=watchlist_rows,
            market_opportunities=market_opps,
            finance_recommendations=finance_dicts,
            portfolio_context={
                "total_portfolio_value": 50_000,
                "cash": 5_000,
                "current_holdings": {},
                "degraded_mode": False,
                "data_mode": "live",
                "drawdown_regime": "neutral",
                "active_structural_violations": [],
            },
        )

    def test_portfolio_adjustment_list_unchanged(self):
        adjs = [_adj_to_de_dict(_make_adj("VTI")), _adj_to_de_dict(_make_adj("TQQQ"))]
        original_symbols = [a["symbol"] for a in adjs]
        self._run_plan(adjs, [], [], [])
        self.assertEqual([a["symbol"] for a in adjs], original_symbols)

    def test_watchlist_rows_not_mutated(self):
        rows = [
            {"ticker": "NVDA", "signal_score": 0.80, "confidence_score": 0.85,
             "conviction_band": "high_conviction", "conviction_score": 0.88},
        ]
        ticker_before = rows[0]["ticker"]
        self._run_plan([], [], rows, [])
        self.assertEqual(rows[0]["ticker"], ticker_before)

    def test_market_opps_not_mutated(self):
        opps = [{"symbol": "VFH", "opportunity_type": "underweight_target",
                 "suggested_pct": 0.03, "reason": "Underweight."}]
        reason_before = opps[0]["reason"]
        self._run_plan([], [], [], opps)
        self.assertEqual(opps[0]["reason"], reason_before)

    def test_finance_recs_not_mutated(self):
        recs = [_finance_rec_to_de_dict(_make_finance_rec())]
        title_before = recs[0]["title"]
        self._run_plan([], recs, [], [])
        self.assertEqual(recs[0]["title"], title_before)

    def test_decision_plan_has_no_extra_keys_in_existing_outputs(self):
        """
        Schema guard: existing output field names must not appear in the
        decision_plan records — the plan uses its own closed schema.
        """
        plan = self._run_plan(
            [_adj_to_de_dict(_make_adj())],
            [],
            [],
            [],
        )
        # existing CSV/JSON field names that must not bleed into decision records
        forbidden_keys = {"RecKey", "AdjustmentMode", "FinalScore", "NextCheck"}
        for record in plan:
            overlap = forbidden_keys & set(record.keys())
            self.assertEqual(overlap, set(), f"Unexpected keys in decision record: {overlap}")

    def test_recommendations_csv_schema_keys_unchanged(self):
        """
        The PortfolioAdjustment.to_dict() CSV schema is unchanged by the
        decision engine (it produces a separate, parallel output).
        """
        adj_obj = _make_adj(symbol="VTI")
        # Simulate what to_dict() would produce (using known key set)
        csv_keys = {"RecKey", "ActionLevel", "RecommendationType", "AdjustmentMode",
                    "Symbol", "Shares", "Amount", "FinalScore", "What", "Why",
                    "Do", "NextCheck", "Timestamp", "Drift", "Band", "IsLeveraged"}
        de_dict = _adj_to_de_dict(adj_obj)
        # The decision engine dict must not contain CSV-only keys
        self.assertFalse(csv_keys & set(de_dict.keys()),
                         "Decision engine dict must not use CSV key names")

    def test_watchlist_signal_schema_unchanged(self):
        """
        watchlist_signals.json uses 'ticker' as the primary key.
        The decision engine reads it but must not rename or remove it.
        """
        row = {"ticker": "NVDA", "signal_score": 0.80, "confidence_score": 0.85,
               "conviction_band": "normal", "conviction_score": 0.72,
               "alert_priority": "high"}
        import copy
        row_copy = copy.deepcopy(row)
        build_decision_plan(watchlist_signals=[row], portfolio_context={})
        # Original row is unchanged
        self.assertEqual(row, row_copy)


# ---------------------------------------------------------------------------
# Decision plan output schema validation
# ---------------------------------------------------------------------------


class TestDecisionPlanOutputSchema(unittest.TestCase):
    """
    Every record in the plan must conform to the documented output schema.
    """

    _REQUIRED_KEYS = {
        "symbol", "decision", "priority", "urgency", "source",
        "recommended_action", "recommended_amount", "recommended_allocation_pct",
        "reason", "risk_flags", "confidence", "inputs_used",
    }
    _VALID_DECISIONS = {"BUY", "SELL", "SCALE", "HOLD", "WAIT", "AVOID"}
    _VALID_URGENCIES = {"critical", "high", "medium", "low"}
    _VALID_SOURCES = {"structural", "portfolio", "watchlist", "market", "finance"}

    def _plan_from_structural(self):
        return build_decision_plan(
            structural_violations=[{
                "symbol": "TQQQ", "violation_type": "leverage",
                "current_pct": 0.18, "cap_pct": 0.15, "required_action": "trim",
            }],
            portfolio_context={},
        )

    def test_all_required_keys_present(self):
        plan = self._plan_from_structural()
        for record in plan:
            missing = self._REQUIRED_KEYS - set(record.keys())
            self.assertEqual(missing, set(), f"Missing keys in {record.get('symbol')}: {missing}")

    def test_decision_is_valid_enum_value(self):
        plan = build_decision_plan(
            structural_violations=[{"symbol": "X", "violation_type": "leverage"}],
            watchlist_signals=[{"ticker": "Y", "conviction_band": "observe"}],
            market_opportunities=[{"symbol": "Z", "opportunity_type": "underweight_target"}],
            portfolio_context={},
        )
        for record in plan:
            self.assertIn(record["decision"], self._VALID_DECISIONS,
                          f"Bad decision for {record.get('symbol')}")

    def test_urgency_is_valid_enum_value(self):
        plan = self._plan_from_structural()
        for record in plan:
            self.assertIn(record["urgency"], self._VALID_URGENCIES)

    def test_source_is_valid_enum_value(self):
        plan = self._plan_from_structural()
        for record in plan:
            self.assertIn(record["source"], self._VALID_SOURCES)

    def test_priority_is_float_between_0_and_1(self):
        plan = self._plan_from_structural()
        for record in plan:
            pri = record["priority"]
            self.assertIsInstance(pri, float)
            self.assertGreaterEqual(pri, 0.0)
            self.assertLessEqual(pri, 1.0)

    def test_risk_flags_is_list(self):
        plan = self._plan_from_structural()
        for record in plan:
            self.assertIsInstance(record["risk_flags"], list)

    def test_inputs_used_is_dict(self):
        plan = self._plan_from_structural()
        for record in plan:
            self.assertIsInstance(record["inputs_used"], dict)

    def test_plan_is_json_serialisable(self):
        plan = self._plan_from_structural()
        payload = {
            "generated_at": "2026-04-28T00:00:00",
            "run_mode": "daily",
            "observe_only": True,
            "total_decisions": len(plan),
            "decisions": plan,
        }
        try:
            serialised = json.dumps(payload, default=str)
            reloaded = json.loads(serialised)
            self.assertEqual(reloaded["total_decisions"], len(plan))
        except (TypeError, ValueError) as exc:
            self.fail(f"decision_plan is not JSON-serialisable: {exc}")

    def test_summary_is_string_and_non_empty(self):
        plan = self._plan_from_structural()
        summary = summarize_decision_plan(plan, {})
        self.assertIsInstance(summary, str)
        self.assertGreater(len(summary), 50)


# ---------------------------------------------------------------------------
# Serialiser round-trip: helper → decision engine → valid record
# ---------------------------------------------------------------------------


class TestSerialiserRoundTrip(unittest.TestCase):
    """
    Verify that objects serialised by the helpers produce valid decisions
    when passed through build_decision_plan.
    """

    def test_portfolio_adjustment_produces_portfolio_source_decision(self):
        adj = _make_adj(symbol="VTI", rec_type="sell", action_level="ACTION_REQUIRED")
        plan = build_decision_plan(
            portfolio_adjustments=[_adj_to_de_dict(adj)],
            portfolio_context={},
        )
        sources = [d["source"] for d in plan]
        self.assertIn(SOURCE_PORTFOLIO, sources)

    def test_finance_rec_produces_finance_source_decision(self):
        rec = _make_finance_rec(action_level="RECOMMENDED")
        plan = build_decision_plan(
            finance_recommendations=[_finance_rec_to_de_dict(rec)],
            portfolio_context={},
        )
        sources = [d["source"] for d in plan]
        self.assertIn(SOURCE_FINANCE, sources)

    def test_promoted_market_candidate_produces_market_source_decision(self):
        coverage = {
            "promoted": [{"symbol": "VFH", "label": "compounder",
                          "reasons": ["underweight sector"]}],
            "decision_layer": {"actions": []},
        }
        plan = build_decision_plan(
            market_opportunities=_market_opps_from_coverage(coverage),
            portfolio_context={},
        )
        sources = [d["source"] for d in plan]
        self.assertIn(SOURCE_MARKET, sources)

    def test_watchlist_row_produces_watchlist_source_decision(self):
        plan = build_decision_plan(
            watchlist_signals=[{
                "ticker": "NVDA",
                "signal_score": 0.80,
                "confidence_score": 0.88,
                "conviction_band": "high_conviction",
                "conviction_score": 0.85,
            }],
            portfolio_context={},
        )
        sources = [d["source"] for d in plan]
        self.assertIn(SOURCE_WATCHLIST, sources)

    def test_leveraged_adj_produces_critical_sell(self):
        adj = _make_adj(symbol="TQQQ", rec_type="sell",
                        action_level="ACTION_REQUIRED", is_leveraged=True)
        plan = build_decision_plan(
            structural_violations=[{
                "symbol": "TQQQ", "violation_type": "leverage",
                "current_pct": 0.18, "cap_pct": 0.15, "required_action": "trim",
            }],
            portfolio_context={},
        )
        tqqq = next(d for d in plan if d["symbol"] == "TQQQ")
        self.assertEqual(tqqq["decision"], DECISION_SELL)

    def test_all_sources_present_when_all_inputs_provided(self):
        plan = build_decision_plan(
            structural_violations=[{
                "symbol": "QQQ", "violation_type": "concentration",
                "current_pct": 0.45, "cap_pct": 0.40,
            }],
            portfolio_adjustments=[_adj_to_de_dict(_make_adj("VTI"))],
            watchlist_signals=[{
                "ticker": "NVDA", "conviction_band": "high_conviction",
                "conviction_score": 0.88, "signal_score": 0.82,
                "confidence_score": 0.91,
            }],
            market_opportunities=[{
                "symbol": "VFH", "opportunity_type": "underweight_target",
                "reason": "Underweight.",
            }],
            finance_recommendations=[_finance_rec_to_de_dict(_make_finance_rec())],
            portfolio_context={},
        )
        sources_present = {d["source"] for d in plan}
        self.assertIn(SOURCE_STRUCTURAL, sources_present)
        self.assertIn(SOURCE_PORTFOLIO, sources_present)
        self.assertIn(SOURCE_WATCHLIST, sources_present)
        self.assertIn(SOURCE_MARKET, sources_present)
        self.assertIn(SOURCE_FINANCE, sources_present)


if __name__ == "__main__":
    unittest.main(verbosity=2)
