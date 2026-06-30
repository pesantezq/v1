"""Phase 10 — governance proposal hardening (observe-only).

Overlay power classes (1-6), complete evidence cards, and dedup / expiration /
supersession / conflict detection. Higher-power overlays require stronger
evidence; nothing self-activates; production stays human-gated.

TDD: written before portfolio_automation/proposal_evidence.py existed.
"""
from __future__ import annotations

import portfolio_automation.proposal_evidence as pe


_NOW = "2026-06-30T00:00:00+00:00"


def _card(**over):
    base = dict(
        proposal_id="prop_1", proposal_type="crowd_context_change",
        hypothesis="confirmed attention precedes continuation",
        affected_component="GOOG", proposed_effect="annotate crowd_context",
        power_class=1, simulation_result={"delta": 0.0}, baseline_comparison={},
        oos_status="validated", sample_size=60, cost_adjusted_result=0.01,
        regime_stability=0.8, risk_impact="low", max_production_impact="annotation",
        failure_conditions=["evidence stale"], rollback_plan="delete overlay entry",
        expiration="2026-07-30T00:00:00+00:00", evidence_freshness="fresh",
        source_experiment_ids=["exp_001"], now=_NOW,
    )
    base.update(over)
    return pe.evidence_card(**base)


# ---------------------------------------------------------------------------
# Power classes + escalating evidence
# ---------------------------------------------------------------------------


def test_six_overlay_power_classes():
    assert pe.OVERLAY_POWER_CLASSES[1] == "explanation_only"
    assert pe.OVERLAY_POWER_CLASSES[6] == "decision_override"
    assert set(pe.OVERLAY_POWER_CLASSES) == {1, 2, 3, 4, 5, 6}


def test_required_evidence_escalates_with_power():
    low = pe.required_evidence(1)
    high = pe.required_evidence(6)
    assert high["min_oos_sample"] > low["min_oos_sample"]
    assert high["min_regime_stability"] >= low["min_regime_stability"]
    assert low["human_approval"] is True and high["human_approval"] is True


def test_high_power_overlay_needs_strong_evidence():
    weak = _card(power_class=5, sample_size=20, regime_stability=0.2, oos_status="inconclusive")
    g = pe.gate_proposal(weak)
    assert g["eligible_for_review"] is False
    assert "insufficient_evidence_for_power_class" in g["reasons"]
    # same evidence is fine for a class-1 explanation overlay
    ok = _card(power_class=1, sample_size=20, regime_stability=0.2, oos_status="inconclusive")
    assert pe.gate_proposal(ok)["eligible_for_review"] is True


def test_card_has_all_fields_and_stays_pending():
    c = _card()
    for f in ("proposal_id", "power_class", "oos_status", "sample_size",
              "cost_adjusted_result", "regime_stability", "max_production_impact",
              "failure_conditions", "rollback_plan", "expiration",
              "evidence_freshness", "source_experiment_ids", "approval_status"):
        assert f in c
    assert c["approval_status"] == "pending"   # never self-approves


# ---------------------------------------------------------------------------
# Dedup / expiration / supersession / conflict
# ---------------------------------------------------------------------------


def test_dedupe_collapses_same_component_and_effect():
    a = _card(proposal_id="p1", affected_component="GOOG", proposed_effect="annotate X")
    b = _card(proposal_id="p2", affected_component="GOOG", proposed_effect="annotate X")
    c = _card(proposal_id="p3", affected_component="MSFT", proposed_effect="annotate X")
    out = pe.dedupe_proposals([a, b, c])
    assert len(out) == 2  # GOOG/annotate-X collapsed


def test_expiration():
    assert pe.is_expired(_card(expiration="2026-06-29T00:00:00+00:00"), now=_NOW) is True
    assert pe.is_expired(_card(expiration="2026-07-30T00:00:00+00:00"), now=_NOW) is False


def test_conflict_detection_contradictory_effects():
    a = _card(proposal_id="p1", affected_component="GOOG", proposed_effect="raise")
    b = _card(proposal_id="p2", affected_component="GOOG", proposed_effect="lower")
    conflicts = pe.detect_conflicts([a, b])
    assert any({"p1", "p2"} == set(c["proposal_ids"]) for c in conflicts)


def test_stale_evidence_blocks_eligibility():
    g = pe.gate_proposal(_card(power_class=1, evidence_freshness="stale"))
    assert g["eligible_for_review"] is False
    assert "stale_evidence" in g["reasons"]
