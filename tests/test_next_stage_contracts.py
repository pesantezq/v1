"""Phase 1 — next-stage artifact contracts + record schemas.

Asserts the observe-only / no-trade invariants and that every declared artifact
is registered in artifact_registry.yaml. No producers exist yet (Phase 1).
"""
from __future__ import annotations

import portfolio_automation.artifact_registry as ar
from portfolio_automation.data_governance import OutputNamespace
from portfolio_automation.next_stage import contracts as C


# ---------------------------------------------------------------------------
# Envelope invariants
# ---------------------------------------------------------------------------


def test_envelope_forces_observe_only_and_no_trade():
    env = C.observe_only_envelope("2026-06-09T00:00:00",
                                  observe_only=False, no_trade=False, foo="bar")
    assert env["observe_only"] is True   # cannot be overridden
    assert env["no_trade"] is True
    assert env["foo"] == "bar"
    assert env["schema_version"] == C.SCHEMA_VERSION
    assert env["generated_at"] == "2026-06-09T00:00:00"


# ---------------------------------------------------------------------------
# Artifact contract set
# ---------------------------------------------------------------------------


def test_every_contract_has_valid_namespace_and_write_mode():
    for fn, c in C.NEW_ARTIFACTS.items():
        assert isinstance(c.namespace, OutputNamespace)
        assert c.write_mode in ("replace_latest", "append"), fn
        # jsonl ⇒ append; json/md ⇒ replace_latest
        if fn.endswith(".jsonl"):
            assert c.write_mode == "append", fn
        else:
            assert c.write_mode == "replace_latest", fn


def test_no_contract_targets_decision_plan_or_official_recommendation():
    # The single source of truth is never a next-stage write target.
    for fn, c in C.NEW_ARTIFACTS.items():
        assert "decision_plan" not in fn
        assert c.path != "outputs/latest/decision_plan.json"


def test_research_artifacts_live_in_sandbox():
    sandbox_expected = {
        "universe_scan_candidates.json", "opportunity_radar.json",
        "private_ipo_watchlist.json", "theme_candidates.json",
        "market_opportunity_prompts.json", "market_opportunity_review_cards.json",
        "opportunity_approval_queue.json", "shadow_opportunity_tracking.json",
        "shadow_portfolios.json", "strategy_comparison.json",
        "candidate_promotion_review.json", "strategy_profiles.json",
        "strategy_shadow_results.json", "strategy_risk_scorecard.json",
        "strategy_tax_scorecard.json",
    }
    for fn in sandbox_expected:
        assert C.NEW_ARTIFACTS[fn].namespace == OutputNamespace.SANDBOX, fn


def test_event_streams_are_append_only_policy():
    for fn in ("pattern_events.jsonl", "opportunity_events.jsonl",
               "outcome_events.jsonl", "user_action_log.jsonl"):
        c = C.NEW_ARTIFACTS[fn]
        assert c.namespace == OutputNamespace.POLICY
        assert c.append_only is True


def test_every_artifact_registered_in_registry_with_matching_path():
    reg = ar.load_registry()
    arts = reg.get("artifacts", {})
    for fn, c in C.NEW_ARTIFACTS.items():
        assert fn in arts, f"{fn} missing from artifact_registry.yaml"
        assert arts[fn]["path"] == c.path, fn
        # planned artifacts must not escalate the governance gate
        assert arts[fn]["severity_if_missing"] == "info", fn
        assert arts[fn]["required"] is False, fn


def test_registry_rows_are_schema_valid():
    reg = ar.load_registry()
    arts = reg.get("artifacts", {})
    for fn in C.NEW_ARTIFACTS:
        assert ar._row_schema_ok(arts[fn]), f"{fn} row failed schema validation"


# ---------------------------------------------------------------------------
# Record dataclasses always carry observe_only
# ---------------------------------------------------------------------------


def test_record_dataclasses_force_observe_only():
    idea = C.SystemImprovementIdea(
        id="si-1", title="t", category=C.SystemImprovementCategory.TESTING.value,
        source="s", created_at="x", updated_at="x", observe_only=False)
    assert idea.to_dict()["observe_only"] is True

    prof = C.StrategyProfile(strategy_id=C.StrategyId.BOOM_BUCKET.value,
                             name="Boom", objective="upside", observe_only=False)
    pd = prof.to_dict()
    assert pd["observe_only"] is True
    # boom caps default to the resolved §23.5 Higher tier
    assert pd["max_total_speculative"] == C.BOOM_BUCKET_TOTAL_CAP == 0.15
    assert pd["max_per_idea"] == C.BOOM_BUCKET_PER_IDEA_CAP == 0.05

    opp = C.OpportunityScore(candidate="X", candidate_type="public_ticker",
                             access_route="etf", observe_only=False)
    assert opp.to_dict()["observe_only"] is True

    shadow = C.ShadowRecord(candidate="X", theme="AI", candidate_type="etf",
                            discovered_date="x", observe_only=False)
    assert shadow.to_dict()["observe_only"] is True

    ev = C.LearningEvent(event_id="e1", timestamp="x", source="s",
                         run_mode="daily", namespace="policy", observe_only=False)
    assert ev.to_dict()["observe_only"] is True


def test_system_improvement_idea_has_all_spec_fields():
    idea = C.SystemImprovementIdea(id="i", title="t", category="testing",
                                   source="s", created_at="x", updated_at="x")
    d = idea.to_dict()
    expected = {
        "id", "title", "category", "source", "created_at", "updated_at", "status",
        "priority", "impact_score", "urgency_score", "effort_score", "risk_score",
        "confidence_score", "roadmap_alignment_score", "final_rank_score",
        "summary", "evidence", "affected_modules", "affected_artifacts",
        "proposed_change", "acceptance_criteria", "suggested_tests",
        "safety_constraints", "blocked_actions", "implementation_prompt",
        "owner_decision", "duplicate_of", "cooldown_until", "observe_only",
    }
    assert expected <= set(d)


# ---------------------------------------------------------------------------
# Blocked actions + status vocabularies
# ---------------------------------------------------------------------------


def test_blocked_strategy_actions_cover_execution_surface():
    for a in ("place_trade", "submit_order", "move_money",
              "broker_write_action", "auto_rebalance", "modify_real_holdings"):
        assert a in C.BLOCKED_STRATEGY_ACTIONS


def test_opportunity_status_vocab():
    vals = {s.value for s in C.OpportunityStatus}
    assert {"DISCOVERED", "QUALIFIED", "APPROVED_WATCHLIST_REVIEW", "HYPE_NOISE",
            "ACCESS_LIMITED", "PRIVATE_WATCH_ONLY"} <= vals


# ---------------------------------------------------------------------------
# Degraded payloads
# ---------------------------------------------------------------------------


def test_degraded_payload_is_observe_only_and_flagged():
    p = C.degraded_payload("opportunity_radar.json", "2026-06-09T00:00:00", "api down")
    assert p["observe_only"] is True and p["no_trade"] is True
    assert p["degraded_mode"] is True
    assert p["degraded_reason"] == "api down"
    assert "opportunities" in p  # required field seeded


def test_degraded_payload_rejects_append_only_streams():
    import pytest
    with pytest.raises(ValueError):
        C.degraded_payload("pattern_events.jsonl", "x", "r")


def test_degraded_payload_unknown_artifact_raises():
    import pytest
    with pytest.raises(KeyError):
        C.degraded_payload("not_a_real_artifact.json", "x", "r")
