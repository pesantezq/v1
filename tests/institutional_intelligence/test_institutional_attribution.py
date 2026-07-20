"""Phase 14 tests — daily snapshot + quant-feedback attribution (additive).

Asserts institutional artifacts are frozen into the immutable snapshot, the
decision-time context captures institutional fields, and quant-feedback exposes
the new by_institutional_* dimensions additively without touching protected
win-rate semantics.
"""

from __future__ import annotations

from portfolio_automation import daily_input_snapshot as dis
from portfolio_automation import decision_context_capture as dcc
from portfolio_automation import quant_feedback as qf


def test_snapshot_declares_institutional_inputs():
    keys = {s.key for s in dis.INPUT_SOURCES}
    assert "institutional_intelligence" in keys
    assert "institutional_consensus" in keys
    # frozen as a reference (crowd kind), quarterly-tolerant staleness
    inst = next(s for s in dis.INPUT_SOURCES if s.key == "institutional_intelligence")
    assert inst.stale_after_hours >= 720.0


def test_quant_feedback_has_institutional_dimensions():
    for dim in ("by_institutional_state", "by_institutional_freshness_band",
                "by_institutional_strategy_fit", "by_institutional_crowding_band",
                "by_institutional_manager_archetype"):
        assert dim in qf._DIMENSIONS
    # Protected dimensions unchanged.
    assert qf._DIMENSIONS["by_regime"] == "regime_at_decision"
    assert qf._DIMENSIONS["by_action"] == "action"


def test_capture_populates_institutional_fields():
    plan = {"decisions": [{"symbol": "BE", "decision": "BUY", "confidence": 0.8},
                          {"symbol": "XYZ", "decision": "WAIT"}]}
    institutional = {"BE": {"state": "moderate_accumulation", "freshness_band": "fresh",
                            "strategy_fit_band": "high", "crowding_band": "low",
                            "dominant_archetype": "value"}}
    recs = dcc.capture_decision_context(plan, run_id="r1", now="2026-05-15T00:00:00Z",
                                        institutional=institutional)
    be = next(r for r in recs if r["symbol"] == "BE")
    assert be["institutional_state_at_decision"] == "moderate_accumulation"
    assert be["institutional_freshness_band_at_decision"] == "fresh"
    assert be["institutional_manager_archetype_at_decision"] == "value"
    # A symbol without institutional data -> None (never fabricated).
    xyz = next(r for r in recs if r["symbol"] == "XYZ")
    assert xyz["institutional_state_at_decision"] is None


def test_capture_without_institutional_is_backward_compatible():
    # Omitting the new param leaves fields None; existing fields intact.
    plan = {"decisions": [{"symbol": "BE", "decision": "BUY", "confidence": 0.8}]}
    rec = dcc.capture_decision_context(plan, run_id="r1", now="2026-05-15T00:00:00Z")[0]
    assert rec["institutional_state_at_decision"] is None
    assert rec["regime_at_decision"] is None or "regime_at_decision" in rec
    assert rec["action"] == "BUY"


def test_banding_helpers():
    assert dcc._band_from_age(10) == "fresh"
    assert dcc._band_from_age(60) == "recent"
    assert dcc._band_from_age(200) == "stale"
    assert dcc._band_from_age(None) is None
    assert dcc._fit_band(0.8) == "high"
    assert dcc._fit_band(0.6) == "medium"
    assert dcc._fit_band(0.4) == "low"
    assert dcc._crowding_band(0.7) == "high"
    assert dcc._crowding_band(0.4) == "medium"
    assert dcc._crowding_band(0.1) == "low"


def test_attribution_buckets_institutional_state():
    # Additive attribution: outcomes grouped by institutional state.
    contexts = [
        {"symbol": "A", "action": "BUY", "institutional_state_at_decision": "moderate_accumulation",
         "resolved": True},
        {"symbol": "B", "action": "BUY", "institutional_state_at_decision": "moderate_accumulation",
         "resolved": True},
        {"symbol": "C", "action": "BUY", "institutional_state_at_decision": None, "resolved": True},
    ]
    # outcome_map is {symbol: return_pct} in percent units (neutral band ±1%).
    outcomes = {"A": 3.0, "B": -2.0, "C": 1.5}
    buckets = qf.attribute_outcomes(contexts, outcomes,
                                    dimension="institutional_state_at_decision")
    assert "moderate_accumulation" in buckets
    assert buckets["moderate_accumulation"]["n_samples"] == 2
    # missing dimension routes to "unknown" bucket (honest, not dropped)
    assert "unknown" in buckets
