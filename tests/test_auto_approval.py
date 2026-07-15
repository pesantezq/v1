"""
Tests for the bounded GPT auto-approval channel (simulation-only).

Authority spine first: the four hard-gates and the structural guarantee that the
auto-approval actor can never impersonate a human approver.
"""
from __future__ import annotations

import pytest

from portfolio_automation.sim_governance import auto_approval as AA
from portfolio_automation.sim_governance import schemas as S


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _clean_watchlist_candidate(**over) -> dict:
    """A simulation-lane watchlist candidate that satisfies every authority gate."""
    base = {
        "candidate_id": "cand_abc",
        "candidate_type": "watchlist",
        "workflow": S.WORKFLOW_WATCHLIST,
        "proposal_type": S.PROPOSAL_WATCHLIST_ADD,
        "symbol": "NVDA",
        "confidence": 0.92,
        "target_lane": "simulation",
        "production_mutation": False,
        "feeds_decision_engine": False,
        "is_human_approved": False,
    }
    base.update(over)
    return base


def _gate(results, name):
    for r in results:
        if r.gate_name == name:
            return r
    return None


# --------------------------------------------------------------------------
# Authority hard-gates
# --------------------------------------------------------------------------

def test_authority_gates_pass_for_clean_simulation_candidate():
    results = AA.run_authority_gates(_clean_watchlist_candidate())
    assert AA.all_passed(results)


def test_production_mutation_candidate_is_rejected():
    results = AA.run_authority_gates(_clean_watchlist_candidate(production_mutation=True))
    assert not AA.all_passed(results)
    g = _gate(results, "no_production_mutation")
    assert g is not None and g.passed is False


def test_feeds_decision_engine_candidate_is_rejected():
    results = AA.run_authority_gates(_clean_watchlist_candidate(feeds_decision_engine=True))
    assert not AA.all_passed(results)
    assert _gate(results, "does_not_feed_decision_engine").passed is False


def test_non_simulation_lane_is_rejected():
    results = AA.run_authority_gates(_clean_watchlist_candidate(target_lane="production"))
    assert not AA.all_passed(results)
    assert _gate(results, "target_lane_is_simulation").passed is False


def test_human_approved_marked_candidate_is_rejected():
    results = AA.run_authority_gates(_clean_watchlist_candidate(is_human_approved=True))
    assert not AA.all_passed(results)
    assert _gate(results, "not_human_approved").passed is False


def test_missing_authority_fields_fail_closed():
    # A candidate that simply omits the authority fields must NOT be trusted.
    results = AA.run_authority_gates({"candidate_id": "x", "candidate_type": "watchlist"})
    assert not AA.all_passed(results)


def test_gate_result_has_structured_trace():
    g = _gate(AA.run_authority_gates(_clean_watchlist_candidate(production_mutation=True)),
              "no_production_mutation")
    assert g.gate_name == "no_production_mutation"
    assert g.observed_value is True
    assert g.required_value is False
    assert isinstance(g.reason, str) and g.reason


# --------------------------------------------------------------------------
# The auto-approval actor can never impersonate a human (regression)
# --------------------------------------------------------------------------

def test_auto_approval_channel_marker_is_not_a_human_approver():
    # The whole safety model depends on this staying true; assert it explicitly.
    assert S.is_human_approver(AA.AUTO_APPROVAL_CHANNEL) is False
    assert S.is_human_approver("auto_approval") is False
    assert S.is_human_approver("gpt_auto_approver") is False


def test_auto_approval_channel_marker_constant():
    assert AA.AUTO_APPROVAL_CHANNEL == "auto_approval"


# --------------------------------------------------------------------------
# Enablement + kill-switch precedence (fail-closed)
# --------------------------------------------------------------------------

def _cfg(**over) -> dict:
    base = {
        "enabled": True,
        "watchlist_enabled": True,
        "strategy_enabled": False,
        "min_confidence": 0.85,
        "watchlist_daily_cap": 2,
        "strategy_daily_cap": 0,
        "max_active_awaiting_veto": 5,
    }
    base.update(over)
    return base


def test_enabled_when_all_flags_true_and_no_kill():
    reason = AA.auto_approval_disabled_reason(
        _cfg(), component="watchlist", env={}, kill_file_exists=False)
    assert reason is None


def test_env_kill_switch_wins_over_everything():
    reason = AA.auto_approval_disabled_reason(
        _cfg(), component="watchlist",
        env={"STOCKBOT_AUTO_APPROVAL_DISABLED": "1"}, kill_file_exists=False)
    assert reason == "env_kill_switch"


def test_file_kill_switch_disables():
    reason = AA.auto_approval_disabled_reason(
        _cfg(), component="watchlist", env={}, kill_file_exists=True)
    assert reason == "file_kill_switch"


def test_global_config_disable():
    reason = AA.auto_approval_disabled_reason(
        _cfg(enabled=False), component="watchlist", env={}, kill_file_exists=False)
    assert reason == "global_disabled"


def test_component_flag_disable():
    reason = AA.auto_approval_disabled_reason(
        _cfg(strategy_enabled=False), component="strategy", env={}, kill_file_exists=False)
    assert reason == "component_disabled"


def test_component_env_kill_switch():
    reason = AA.auto_approval_disabled_reason(
        _cfg(), component="watchlist",
        env={"STOCKBOT_AUTO_APPROVAL_WATCHLIST_DISABLED": "true"}, kill_file_exists=False)
    assert reason == "component_kill_switch"


def test_invalid_config_fails_closed():
    reason = AA.auto_approval_disabled_reason(
        {"enabled": "yes-please"}, component="watchlist", env={}, kill_file_exists=False)
    assert reason == "invalid_config"


def test_precedence_env_kill_beats_file_kill():
    reason = AA.auto_approval_disabled_reason(
        _cfg(), component="watchlist",
        env={"STOCKBOT_AUTO_APPROVAL_DISABLED": "1"}, kill_file_exists=True)
    assert reason == "env_kill_switch"


# --------------------------------------------------------------------------
# Deterministic gates (watchlist)
# --------------------------------------------------------------------------

def test_watchlist_min_confidence_boundary():
    cfg = _cfg(min_confidence=0.85)
    ctx = {"active_count": 0, "max_symbols": 5, "applied_today": 0,
           "active_awaiting_veto": 0, "prohibited": set(), "static": set(),
           "conflicting_symbols": set()}
    below = AA.run_watchlist_gates(_clean_watchlist_candidate(confidence=0.84), cfg, ctx)
    at = AA.run_watchlist_gates(_clean_watchlist_candidate(confidence=0.85), cfg, ctx)
    assert _gate(below, "min_confidence").passed is False
    assert _gate(at, "min_confidence").passed is True


def test_watchlist_capacity_gate():
    cfg = _cfg()
    ctx = {"active_count": 5, "max_symbols": 5, "applied_today": 0,
           "active_awaiting_veto": 0, "prohibited": set(), "static": set(),
           "conflicting_symbols": set()}
    res = AA.run_watchlist_gates(_clean_watchlist_candidate(), cfg, ctx)
    assert _gate(res, "capacity_below_max").passed is False


def test_watchlist_daily_cap_gate():
    cfg = _cfg(watchlist_daily_cap=2)
    ctx = {"active_count": 0, "max_symbols": 5, "applied_today": 2,
           "active_awaiting_veto": 0, "prohibited": set(), "static": set(),
           "conflicting_symbols": set()}
    res = AA.run_watchlist_gates(_clean_watchlist_candidate(), cfg, ctx)
    assert _gate(res, "watchlist_daily_cap").passed is False


def test_watchlist_static_and_prohibited_symbol_rejected():
    cfg = _cfg()
    ctx = {"active_count": 0, "max_symbols": 5, "applied_today": 0,
           "active_awaiting_veto": 0, "prohibited": {"BADX"}, "static": {"NVDA"},
           "conflicting_symbols": set()}
    res = AA.run_watchlist_gates(_clean_watchlist_candidate(symbol="NVDA"), cfg, ctx)
    assert _gate(res, "not_prohibited_or_static").passed is False


def test_watchlist_symbol_format_gate():
    cfg = _cfg()
    ctx = {"active_count": 0, "max_symbols": 5, "applied_today": 0,
           "active_awaiting_veto": 0, "prohibited": set(), "static": set(),
           "conflicting_symbols": set()}
    res = AA.run_watchlist_gates(_clean_watchlist_candidate(symbol="not a symbol!"), cfg, ctx)
    assert _gate(res, "symbol_format").passed is False


def test_watchlist_max_active_awaiting_veto_gate():
    cfg = _cfg(max_active_awaiting_veto=5)
    ctx = {"active_count": 0, "max_symbols": 5, "applied_today": 0,
           "active_awaiting_veto": 5, "prohibited": set(), "static": set(),
           "conflicting_symbols": set()}
    res = AA.run_watchlist_gates(_clean_watchlist_candidate(), cfg, ctx)
    assert _gate(res, "max_active_awaiting_veto").passed is False


def test_watchlist_all_gates_pass_for_clean_candidate():
    cfg = _cfg()
    ctx = {"active_count": 0, "max_symbols": 5, "applied_today": 0,
           "active_awaiting_veto": 0, "prohibited": set(), "static": set(),
           "conflicting_symbols": set()}
    res = AA.run_watchlist_gates(_clean_watchlist_candidate(), cfg, ctx)
    assert AA.all_passed(res)


# --------------------------------------------------------------------------
# Idempotency key
# --------------------------------------------------------------------------

def _key(**over):
    base = dict(source_verdict_id="v1", candidate_type="watchlist", target_id="NVDA",
                source_artifact_hash="hashA", policy_version="p1")
    base.update(over)
    return AA.idempotency_key(**base)


def test_idempotency_key_stable_for_same_inputs():
    assert _key() == _key()


def test_idempotency_key_changes_with_artifact_hash():
    assert _key() != _key(source_artifact_hash="hashB")


def test_idempotency_key_changes_with_target():
    assert _key() != _key(target_id="AMD")


# --------------------------------------------------------------------------
# GPT approver — fail-closed, never widens bounds
# --------------------------------------------------------------------------

def _cand_for_gpt():
    return _clean_watchlist_candidate()


def test_gpt_approves_in_bounds():
    v = AA.gpt_approve_candidate(
        _cand_for_gpt(),
        approver=lambda p: '{"decision":"approve","within_bounds":true,"reason":"clean"}')
    assert v["verdict"] == "approve_in_bounds"


def test_gpt_vetoes():
    v = AA.gpt_approve_candidate(
        _cand_for_gpt(),
        approver=lambda p: '{"decision":"veto","within_bounds":true,"reason":"thin"}')
    assert v["verdict"] == "veto"


def test_gpt_malformed_output_is_invalid():
    v = AA.gpt_approve_candidate(_cand_for_gpt(), approver=lambda p: "not json at all")
    assert v["verdict"] == "invalid_or_unavailable"


def test_gpt_empty_reply_is_invalid():
    v = AA.gpt_approve_candidate(_cand_for_gpt(), approver=lambda p: "")
    assert v["verdict"] == "invalid_or_unavailable"


def test_gpt_exception_is_invalid_fail_closed():
    def boom(_):
        raise RuntimeError("timeout")
    v = AA.gpt_approve_candidate(_cand_for_gpt(), approver=boom)
    assert v["verdict"] == "invalid_or_unavailable"


def test_gpt_approve_but_out_of_bounds_is_veto():
    # The approver tries to approve while flagging it is NOT within bounds -> veto.
    v = AA.gpt_approve_candidate(
        _cand_for_gpt(),
        approver=lambda p: '{"decision":"approve","within_bounds":false,"reason":"wider"}')
    assert v["verdict"] == "veto"


def test_gpt_verdict_is_not_an_approval_when_not_approve_in_bounds():
    assert AA.is_gpt_approval({"verdict": "veto"}) is False
    assert AA.is_gpt_approval({"verdict": "invalid_or_unavailable"}) is False
    assert AA.is_gpt_approval({"verdict": "approve_in_bounds"}) is True


# --------------------------------------------------------------------------
# Circuit breaker
# --------------------------------------------------------------------------

def test_circuit_breaker_starts_disengaged():
    assert AA.circuit_breaker_reason({}) is None


def test_circuit_breaker_engaged_blocks():
    state = AA.engage_circuit_breaker({}, "rollback_failed", now="2026-07-14T00:00:00Z")
    assert AA.circuit_breaker_reason(state) == "rollback_failed"


# --------------------------------------------------------------------------
# Audit event builder — applied events carry the channel invariants
# --------------------------------------------------------------------------

def test_applied_event_carries_authority_invariants():
    ev = AA.make_applied_event(
        now="2026-07-14T00:00:00Z", idempotency_key="idk_x",
        candidate=_clean_watchlist_candidate(),
        gpt_verdict={"verdict": "approve_in_bounds", "reason": "clean"},
        gate_trace=[], before_state=None, after_state={"symbol": "NVDA"},
        source_verdict_id="v1", source_artifact_path="outputs/x.json",
        source_artifact_hash="hashA", source_verdict_timestamp="2026-07-14T00:00:00Z",
        model_id="gpt-4o-mini", prompt_version="v1", policy_version="p1",
        config_version="c1")
    assert ev["approval_channel"] == "auto_approval"
    assert ev["is_human_approved"] is False
    assert ev["target_lane"] == "simulation"
    assert ev["production_mutation"] is False
    assert ev["feeds_decision_engine"] is False
    assert ev["kind"] == AA.EVENT_APPLIED
    assert ev["event_id"] and ev["application_status"] == "applied"
    # A human approver must never validate this record's channel.
    assert S.is_human_approver(ev["approval_channel"]) is False


# --------------------------------------------------------------------------
# Deterministic gates (strategy) — ships disabled, cap 0
# --------------------------------------------------------------------------

def test_strategy_gates_one_active_invariant():
    cfg = _cfg(strategy_enabled=True, strategy_daily_cap=1)
    cand = {"candidate_id": "c", "candidate_type": "strategy", "strategy_id": "aggressive_growth",
            "target_lane": "simulation", "production_mutation": False,
            "feeds_decision_engine": False, "is_human_approved": False, "confidence": 0.9}
    ctx = {"applied_today": 0, "active_awaiting_veto": 0, "active_strategy_count": 2,
           "valid_strategy_ids": {"aggressive_growth"}, "prior_active_capturable": True}
    res = AA.run_strategy_gates(cand, cfg, ctx)
    assert _gate(res, "one_active_strategy_invariant").passed is False


def test_strategy_gates_daily_cap_zero_blocks_by_default():
    cfg = _cfg(strategy_enabled=True, strategy_daily_cap=0)
    cand = {"candidate_id": "c", "candidate_type": "strategy", "strategy_id": "aggressive_growth",
            "target_lane": "simulation", "production_mutation": False,
            "feeds_decision_engine": False, "is_human_approved": False, "confidence": 0.9}
    ctx = {"applied_today": 0, "active_awaiting_veto": 0, "active_strategy_count": 1,
           "valid_strategy_ids": {"aggressive_growth"}, "prior_active_capturable": True}
    res = AA.run_strategy_gates(cand, cfg, ctx)
    assert _gate(res, "strategy_daily_cap").passed is False


# --------------------------------------------------------------------------
# Append-only ledger + derived summary + audit-before-mutate
# --------------------------------------------------------------------------

def _ev(kind, key="idk_1", **over):
    base = {"kind": kind, "event_id": f"evt_{kind}_{key}", "idempotency_key": key,
            "ts": "2026-07-14T00:00:00Z", "target_id": "NVDA", "candidate_type": "watchlist"}
    base.update(over)
    return base


def test_ledger_append_is_additive(tmp_path):
    base = str(tmp_path)
    AA.append_event(_ev(AA.EVENT_ATTEMPT), base_dir=base)
    AA.append_event(_ev(AA.EVENT_APPLIED), base_dir=base)
    events = AA.load_events(base_dir=base)
    assert [e["kind"] for e in events] == [AA.EVENT_ATTEMPT, AA.EVENT_APPLIED]


def test_ledger_does_not_truncate_existing(tmp_path):
    base = str(tmp_path)
    AA.append_event(_ev(AA.EVENT_ATTEMPT), base_dir=base)
    AA.append_event(_ev(AA.EVENT_APPLIED), base_dir=base)
    AA.append_event(_ev(AA.EVENT_HUMAN_VETO), base_dir=base)
    assert len(AA.load_events(base_dir=base)) == 3


def test_applied_key_exists_only_after_applied_event(tmp_path):
    base = str(tmp_path)
    AA.append_event(_ev(AA.EVENT_ATTEMPT, key="idk_x"), base_dir=base)
    assert AA.applied_key_exists("idk_x", base_dir=base) is False
    AA.append_event(_ev(AA.EVENT_APPLIED, key="idk_x"), base_dir=base)
    assert AA.applied_key_exists("idk_x", base_dir=base) is True


def test_summary_counts_and_active_items(tmp_path):
    base = str(tmp_path)
    AA.append_event(_ev(AA.EVENT_APPLIED, key="idk_a", target_id="NVDA"), base_dir=base)
    AA.append_event(_ev(AA.EVENT_APPLIED, key="idk_b", target_id="AMD"), base_dir=base)
    AA.append_event(_ev(AA.EVENT_HUMAN_VETO, key="idk_a", target_id="NVDA"), base_dir=base)
    summary = AA.build_summary(base_dir=base, now="2026-07-15T00:00:00Z")
    assert summary["counters"]["applied"] == 2
    assert summary["counters"]["human_veto"] == 1
    # NVDA was vetoed -> only AMD remains active/awaiting.
    active_targets = {i["target_id"] for i in summary["active_items"]}
    assert active_targets == {"AMD"}


def test_summary_reports_circuit_breaker(tmp_path):
    base = str(tmp_path)
    AA.append_event(_ev(AA.EVENT_FAILURE), base_dir=base)
    summary = AA.build_summary(base_dir=base, now="2026-07-15T00:00:00Z",
                               state={"circuit_breaker": {"engaged": True, "reason": "ledger_corrupt"}})
    assert summary["circuit_breaker"]["engaged"] is True
    assert summary["circuit_breaker"]["reason"] == "ledger_corrupt"


def test_record_and_apply_writes_audit_before_mutation(tmp_path):
    base = str(tmp_path)
    applied = {"did": False}

    def mutate():
        applied["did"] = True
        return {"status": "ok"}

    res = AA.record_and_apply(_ev(AA.EVENT_APPLIED, key="idk_c"), mutate, base_dir=base)
    assert res["ok"] is True and applied["did"] is True
    assert AA.applied_key_exists("idk_c", base_dir=base) is True


def test_record_and_apply_does_not_mutate_if_audit_fails(tmp_path, monkeypatch):
    base = str(tmp_path)
    applied = {"did": False}

    def mutate():
        applied["did"] = True

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(AA, "append_event", boom)
    res = AA.record_and_apply(_ev(AA.EVENT_APPLIED, key="idk_d"), mutate, base_dir=base)
    assert res["ok"] is False
    assert applied["did"] is False  # mutation must NOT run without a durable audit record


# --------------------------------------------------------------------------
# Candidate collection from the daily AI review (pipeline glue)
# --------------------------------------------------------------------------

def test_collect_maps_ready_watchlist_verdicts_only():
    review = {"verdicts": [
        {"candidate_id": "c1", "decision": S.DECISION_READY, "workflow": S.WORKFLOW_WATCHLIST},
        {"candidate_id": "c2", "decision": S.DECISION_CONTINUE_TESTING, "workflow": S.WORKFLOW_WATCHLIST},
        {"candidate_id": "c3", "decision": S.DECISION_READY, "workflow": S.WORKFLOW_ADVISORY},
    ]}
    cbi = {
        "c1": {"candidate_id": "c1", "symbol": "NVDA", "confidence": 0.9,
               "proposal_type": S.PROPOSAL_WATCHLIST_ADD},
        "c2": {"candidate_id": "c2", "symbol": "AMD", "confidence": 0.9,
               "proposal_type": S.PROPOSAL_WATCHLIST_ADD},
        "c3": {"candidate_id": "c3", "symbol": None, "confidence": 0.9,
               "proposal_type": S.PROPOSAL_ADVISORY_CONTEXT},
    }
    cands = AA.collect_auto_approval_candidates(review, cbi)
    ids = [c["candidate_id"] for c in cands]
    assert ids == ["c1"]  # only the READY watchlist-eligible one
    c = cands[0]
    assert c["candidate_type"] == "watchlist"
    assert c["target_lane"] == "simulation"
    assert c["production_mutation"] is False
    assert c["feeds_decision_engine"] is False
    assert c["is_human_approved"] is False
    assert c["source_verdict_id"] == "c1"


# --------------------------------------------------------------------------
# Deterministic health assessment (healthy + degraded fixtures)
# --------------------------------------------------------------------------

def test_health_green_when_inert(tmp_path):
    h = AA.assess_health(base_dir=str(tmp_path), now="2026-07-14T00:00:00Z")
    assert h["status"] == "GREEN"


def test_health_amber_when_items_awaiting_veto(tmp_path):
    base = str(tmp_path)
    AA.append_event(AA.make_applied_event(
        now="2026-07-14T00:00:00Z", idempotency_key="idk_a",
        candidate=_clean_watchlist_candidate(), gpt_verdict={"verdict": "approve_in_bounds"},
        gate_trace=[], before_state=None, after_state={"symbol": "NVDA"},
        source_verdict_id="v1", source_artifact_path="x", source_artifact_hash="h",
        source_verdict_timestamp="2026-07-14T00:00:00Z", model_id="m"), base_dir=base)
    h = AA.assess_health(base_dir=base, now="2026-07-14T01:00:00Z")
    assert h["status"] == "AMBER"


def test_health_red_on_rollback_failed(tmp_path):
    base = str(tmp_path)
    AA.append_event({"kind": AA.EVENT_FAILURE, "idempotency_key": "idk_a",
                     "rollback_status": AA.ROLLBACK_FAILED, "ts": "2026-07-14T00:00:00Z"},
                    base_dir=base)
    h = AA.assess_health(base_dir=base, now="2026-07-14T01:00:00Z")
    assert h["status"] == "RED"
    assert any("rollback_failed" in r for r in h["reds"])


def test_health_red_on_authority_breach_in_applied_event(tmp_path):
    base = str(tmp_path)
    # An applied event that (impossibly) lacks the authority-channel invariants.
    AA.append_event({"kind": AA.EVENT_APPLIED, "event_id": "evt_bad", "idempotency_key": "idk_b",
                     "target_id": "NVDA", "is_human_approved": True, "target_lane": "production",
                     "ts": "2026-07-14T00:00:00Z"}, base_dir=base)
    h = AA.assess_health(base_dir=base, now="2026-07-14T01:00:00Z")
    assert h["status"] == "RED"
