"""
End-to-end orchestration of the bounded auto-approval channel + the human veto API.

Real ExtendedWatchlist on a temp DB; GPT approver injected (never a live LLM).
"""
from __future__ import annotations

import pytest

from portfolio_automation.sim_governance import auto_approval as AA
from portfolio_automation.sim_governance import schemas as S
from watchlist_scanner.extended_watchlist import ExtendedWatchlist

NOW = "2026-07-14T12:00:00Z"


class Approver:
    """Injectable approver spy — records call count so we can assert cost posture."""
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def __call__(self, prompt):
        self.calls += 1
        return self.reply


APPROVE = '{"decision":"approve","within_bounds":true,"reason":"clean"}'
VETO = '{"decision":"veto","within_bounds":true,"reason":"thin evidence"}'


@pytest.fixture
def wl(tmp_path):
    return ExtendedWatchlist(db_path=str(tmp_path / "sim_wl.db"), max_symbols=5)


def _cfg(**over):
    base = {"enabled": True, "watchlist_enabled": True, "strategy_enabled": False,
            "min_confidence": 0.85, "watchlist_daily_cap": 2, "strategy_daily_cap": 0,
            "max_active_awaiting_veto": 5}
    base.update(over)
    return base


def _cand(symbol="NVDA", **over):
    base = {"candidate_id": "cand_1", "candidate_type": "watchlist",
            "workflow": S.WORKFLOW_WATCHLIST, "proposal_type": S.PROPOSAL_WATCHLIST_ADD,
            "symbol": symbol, "confidence": 0.92, "source_verdict_id": "v_" + symbol,
            "target_lane": "simulation", "production_mutation": False,
            "feeds_decision_engine": False, "is_human_approved": False}
    base.update(over)
    return base


def _run(candidates, *, base, cfg, approver, wl, **over):
    kw = dict(candidates=candidates, now=NOW, base_dir=base, config=cfg,
              source_artifact_path="outputs/promotion_review/daily_ai_review_result.json",
              source_artifact_hash="hashA", env={}, kill_file_exists=False,
              watchlist=wl, valid_strategy_ids=set(), approver=approver)
    kw.update(over)
    return AA.run_auto_approval(**kw)


def test_disabled_is_inert(tmp_path, wl):
    base = str(tmp_path)
    ap = Approver(APPROVE)
    res = _run([_cand()], base=base, cfg=_cfg(enabled=False), approver=ap, wl=wl)
    assert res["disabled_reason"] == "global_disabled"
    assert res["applied_count"] == 0
    assert ap.calls == 0
    assert wl.get_symbol("NVDA") is None
    assert AA.load_events(base_dir=base) == []


def test_clean_candidate_is_applied(tmp_path, wl):
    base = str(tmp_path)
    ap = Approver(APPROVE)
    res = _run([_cand()], base=base, cfg=_cfg(), approver=ap, wl=wl)
    assert res["applied_count"] == 1
    assert wl.get_symbol("NVDA")["is_active"] == 1
    kinds = [e["kind"] for e in AA.load_events(base_dir=base)]
    assert AA.EVENT_APPLIED in kinds


def test_gpt_veto_does_not_apply_and_routes_pending(tmp_path, wl):
    base = str(tmp_path)
    ap = Approver(VETO)
    res = _run([_cand()], base=base, cfg=_cfg(), approver=ap, wl=wl)
    assert res["applied_count"] == 0
    assert res["gpt_vetoed_count"] == 1
    assert res["pending_fallback_count"] >= 1
    assert wl.get_symbol("NVDA") is None
    assert AA.EVENT_GPT_VETO in [e["kind"] for e in AA.load_events(base_dir=base)]


def test_no_gpt_call_when_deterministic_gate_fails(tmp_path, wl):
    base = str(tmp_path)
    ap = Approver(APPROVE)
    # Confidence below threshold -> a deterministic gate fails BEFORE any GPT call.
    res = _run([_cand(confidence=0.10)], base=base, cfg=_cfg(), approver=ap, wl=wl)
    assert res["applied_count"] == 0
    assert res["rejected_count"] == 1
    assert ap.calls == 0  # cost posture: no model call when nothing passes the gates
    assert AA.EVENT_DETERMINISTIC_REJECT in [e["kind"] for e in AA.load_events(base_dir=base)]


def test_authority_violation_never_applies(tmp_path, wl):
    base = str(tmp_path)
    ap = Approver(APPROVE)
    res = _run([_cand(feeds_decision_engine=True)], base=base, cfg=_cfg(), approver=ap, wl=wl)
    assert res["applied_count"] == 0
    assert ap.calls == 0
    assert wl.get_symbol("NVDA") is None


def test_idempotent_across_repeated_runs(tmp_path, wl):
    base = str(tmp_path)
    _run([_cand()], base=base, cfg=_cfg(), approver=Approver(APPROVE), wl=wl)
    res2 = _run([_cand()], base=base, cfg=_cfg(), approver=Approver(APPROVE), wl=wl)
    assert res2["applied_count"] == 0
    assert res2["already_applied_count"] == 1
    applied = [e for e in AA.load_events(base_dir=base) if e["kind"] == AA.EVENT_APPLIED]
    assert len(applied) == 1  # applied exactly once despite two runs


def test_circuit_breaker_blocks_applies(tmp_path, wl):
    base = str(tmp_path)
    ap = Approver(APPROVE)
    state = {"circuit_breaker": {"engaged": True, "reason": "ledger_corrupt"}}
    res = _run([_cand()], base=base, cfg=_cfg(), approver=ap, wl=wl, state=state)
    assert res["applied_count"] == 0
    assert res["disabled_reason"] == "circuit_breaker"
    assert ap.calls == 0


def test_daily_cap_stops_second_apply(tmp_path, wl):
    base = str(tmp_path)
    res = _run([_cand("NVDA"), _cand("AMD", candidate_id="c2"), _cand("TSLA", candidate_id="c3")],
               base=base, cfg=_cfg(watchlist_daily_cap=2), approver=Approver(APPROVE), wl=wl)
    assert res["applied_count"] == 2  # third blocked by daily cap


# --------------------------------------------------------------------------
# Human veto API
# --------------------------------------------------------------------------

def test_record_veto_rolls_back_applied_event(tmp_path, wl):
    base = str(tmp_path)
    _run([_cand()], base=base, cfg=_cfg(), approver=Approver(APPROVE), wl=wl)
    ev = [e for e in AA.load_events(base_dir=base) if e["kind"] == AA.EVENT_APPLIED][0]
    out = AA.record_veto(ev["event_id"], operator_identity="pesantez",
                         reason="not convinced", base_dir=base, watchlist=wl, now=NOW)
    assert out["status"] == "rolled_back"
    assert wl.get_symbol("NVDA") is None
    kinds = [e["kind"] for e in AA.load_events(base_dir=base)]
    assert AA.EVENT_HUMAN_VETO in kinds and AA.EVENT_ROLLBACK in kinds


def test_record_veto_is_idempotent(tmp_path, wl):
    base = str(tmp_path)
    _run([_cand()], base=base, cfg=_cfg(), approver=Approver(APPROVE), wl=wl)
    ev = [e for e in AA.load_events(base_dir=base) if e["kind"] == AA.EVENT_APPLIED][0]
    AA.record_veto(ev["event_id"], operator_identity="pesantez", base_dir=base, watchlist=wl, now=NOW)
    out2 = AA.record_veto(ev["event_id"], operator_identity="pesantez", base_dir=base, watchlist=wl, now=NOW)
    assert out2["status"] == "already_vetoed"


def test_record_veto_unknown_event(tmp_path, wl):
    base = str(tmp_path)
    out = AA.record_veto("evt_nope", operator_identity="pesantez", base_dir=base, watchlist=wl, now=NOW)
    assert out["status"] == "unknown_event"


def test_veto_conflict_when_state_changed(tmp_path, wl):
    base = str(tmp_path)
    _run([_cand()], base=base, cfg=_cfg(), approver=Approver(APPROVE), wl=wl)
    ev = [e for e in AA.load_events(base_dir=base) if e["kind"] == AA.EVENT_APPLIED][0]
    wl.demote_vetoed("NVDA")  # state changes out from under the auto-apply
    out = AA.record_veto(ev["event_id"], operator_identity="pesantez", base_dir=base, watchlist=wl, now=NOW)
    assert out["status"] == "rollback_conflict"
    assert AA.EVENT_ROLLBACK_CONFLICT in [e["kind"] for e in AA.load_events(base_dir=base)]
