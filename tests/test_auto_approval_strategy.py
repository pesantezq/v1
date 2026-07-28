"""
Auto-approval strategy anchor (simulation) + event-aware CAS rollback.

The strategy channel ships DISABLED (cap 0); these tests exercise the mechanism so a
future enablement is safe. It must NEVER travel the human `record_strategy_decision`
path and must never mark itself human-approved.

WS5 structural guard (see .superpowers/audit/ws-04-05-14-18-health.md): a
ranking change must never automatically re-anchor the active strategy. The
audit found the ONLY thing keeping this inert today is that the daily
candidate collector hardcodes watchlist-only candidates -- nothing in the
gate chain itself refused a ranking-derived strategy candidate. The
``not_ranking_triggered`` gate in ``run_strategy_gates`` closes that: it
independently recomputes the leaderboard's #1-ranked tactic (never trusting
anything the candidate claims about its own origin) and refuses whenever the
candidate's ``strategy_id`` resolves to it. See ``test_ranking_derived_*``
below and the paired ``test_human_approved_selection_still_succeeds``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from portfolio_automation.sim_governance import auto_approval as AA
from portfolio_automation.sim_governance import schemas as S
from portfolio_automation.strategy import strategy_selection as SS

VALID = {"aggressive_growth", "defensive"}
NOW = "2026-07-14T00:00:00Z"


def _write_leaderboard(base_dir: str, top_tactic_id: str) -> None:
    """Fabricate a minimal sandbox leaderboard fixture with the given tactic ranked #1."""
    path = Path(base_dir) / "sandbox" / "strategy_leaderboard.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "status": "ok",
        "leaderboard": [
            {"tactic_id": top_tactic_id, "name": top_tactic_id, "strategy_score": 1.75},
            {"tactic_id": "some_other_tactic", "name": "Other", "strategy_score": 0.4},
        ],
    }))


class _Approver:
    """GPT approver spy — must never be called once a deterministic gate refuses
    (cost posture: GPT only runs after every deterministic gate has passed)."""
    def __init__(self):
        self.calls = 0

    def __call__(self, prompt):
        self.calls += 1
        raise AssertionError("GPT approver must not be invoked when a deterministic gate refuses")


def _strategy_cfg(**over):
    base = {"enabled": True, "watchlist_enabled": False, "strategy_enabled": True,
            "strategy_daily_cap": 1, "max_active_awaiting_veto": 5,
            "min_confidence": 0.85}
    base.update(over)
    return base


def _strategy_candidate(strategy_id: str, **over) -> dict:
    base = {"candidate_id": "cand_s1", "candidate_type": "strategy",
            "strategy_id": strategy_id, "target_lane": "simulation",
            "production_mutation": False, "feeds_decision_engine": False,
            "is_human_approved": False, "confidence": 0.95,
            "source_verdict_id": "v_" + strategy_id}
    base.update(over)
    return base


def test_ranking_derived_candidate_is_refused(tmp_path):
    """A candidate whose strategy_id resolves to the leaderboard's CURRENT #1-ranked
    tactic must be refused with a recorded reason -- never silently accepted, and
    never even reaches the (paid) GPT approver."""
    base = str(tmp_path)
    _write_leaderboard(base, top_tactic_id="aggressive_growth")
    ap = _Approver()

    res = AA.run_auto_approval(
        candidates=[_strategy_candidate("aggressive_growth")],
        now=NOW, base_dir=base, config=_strategy_cfg(),
        source_artifact_path="outputs/promotion_review/daily_ai_review_result.json",
        source_artifact_hash="hashA", env={}, kill_file_exists=False,
        watchlist=None, valid_strategy_ids=VALID, approver=ap,
    )

    assert res["applied_count"] == 0
    assert res["rejected_count"] == 1
    assert res["results"][0]["status"] == "rejected_deterministic"
    assert ap.calls == 0, "ranking-derived candidate reached the GPT approver"

    # Nothing was mutated: no active-strategy selection exists.
    assert SS.load_active_selection(base) == {}

    # The refusal was recorded, not silently dropped.
    events = AA.load_events(base_dir=base)
    rejects = [e for e in events if e.get("kind") == AA.EVENT_DETERMINISTIC_REJECT]
    assert len(rejects) == 1
    gate_names = {g["gate_name"] for g in rejects[0]["gate_trace"]}
    assert "not_ranking_triggered" in gate_names
    failed = [g for g in rejects[0]["gate_trace"] if g["gate_name"] == "not_ranking_triggered"]
    assert failed[0]["passed"] is False
    assert "ranking" in failed[0]["reason"].lower()


def test_ranking_derived_candidate_via_profile_prefix_is_refused(tmp_path):
    """The guard must also catch the profile_<id> materialization form, not just an
    exact tactic_id match, since that's how strategy profiles are named on the
    leaderboard in production (see resolve_anchor_tactic_id)."""
    base = str(tmp_path)
    _write_leaderboard(base, top_tactic_id="profile_defensive")
    ap = _Approver()

    res = AA.run_auto_approval(
        candidates=[_strategy_candidate("defensive")],
        now=NOW, base_dir=base, config=_strategy_cfg(),
        source_artifact_path="outputs/promotion_review/daily_ai_review_result.json",
        source_artifact_hash="hashB", env={}, kill_file_exists=False,
        watchlist=None, valid_strategy_ids=VALID, approver=ap,
    )

    assert res["applied_count"] == 0
    assert res["rejected_count"] == 1
    assert ap.calls == 0


def test_strategy_gate_fails_closed_when_leaderboard_unreadable(tmp_path):
    """No leaderboard on disk at all -> cannot prove the candidate is NOT
    ranking-derived -> the gate must refuse rather than silently accept."""
    base = str(tmp_path)  # no strategy_leaderboard.json written
    ap = _Approver()

    res = AA.run_auto_approval(
        candidates=[_strategy_candidate("aggressive_growth")],
        now=NOW, base_dir=base, config=_strategy_cfg(),
        source_artifact_path="outputs/promotion_review/daily_ai_review_result.json",
        source_artifact_hash="hashC", env={}, kill_file_exists=False,
        watchlist=None, valid_strategy_ids=VALID, approver=ap,
    )

    assert res["applied_count"] == 0
    assert res["rejected_count"] == 1
    assert ap.calls == 0


def test_non_ranking_candidate_still_clears_the_gate(tmp_path):
    """Sanity check: a candidate that does NOT match the leaderboard's #1 tactic
    clears `not_ranking_triggered` (the guard is targeted, not a blanket freeze)."""
    base = str(tmp_path)
    _write_leaderboard(base, top_tactic_id="some_other_tactic")
    ctx = {"applied_today": 0, "active_awaiting_veto": 0, "active_strategy_count": 0,
           "valid_strategy_ids": VALID, "prior_active_capturable": True,
           "leaderboard_top_tactic_id": "some_other_tactic"}
    res = AA.run_strategy_gates(_strategy_candidate("aggressive_growth"), _strategy_cfg(), ctx)
    gate = next(g for g in res if g.gate_name == "not_ranking_triggered")
    assert gate.passed is True


def test_human_approved_selection_still_succeeds(tmp_path):
    """The human-gated path (`record_strategy_decision`) is untouched by the new
    guard -- it doesn't consult the leaderboard at all, so it keeps working
    exactly as before, even for the SAME strategy_id the guard above refused
    on the auto-approval channel."""
    base = str(tmp_path)
    # Deliberately do NOT write a leaderboard fixture -- the human path must not
    # need or care about it.
    res = SS.record_strategy_decision(
        "aggressive_growth", "approve", "pesantez",
        valid_strategy_ids=VALID, base_dir=base)

    assert res["ok"] is True
    assert res["active_strategy_id"] == "aggressive_growth"
    sel = SS.load_active_selection(base)
    assert sel["active_strategy_id"] == "aggressive_growth"
    assert sel["approved_by"] == "pesantez"
    assert sel["status"] == "approved"
    assert "approval_channel" not in sel  # never marked as the auto-approval channel
    assert S.is_human_approver("pesantez") is True


def test_auto_anchor_is_not_human_approved(tmp_path):
    base = str(tmp_path)
    res = SS.record_auto_strategy_anchor(
        "aggressive_growth", valid_strategy_ids=VALID, now=NOW, base_dir=base)
    assert res["ok"] is True
    sel = SS.load_active_selection(base)
    assert sel["active_strategy_id"] == "aggressive_growth"
    assert sel["is_human_approved"] is False
    assert sel["approval_channel"] == AA.AUTO_APPROVAL_CHANNEL
    # The channel marker can never pass the human-approver gate.
    assert S.is_human_approver(sel["approval_channel"]) is False


def test_auto_anchor_rejects_unknown_strategy(tmp_path):
    res = SS.record_auto_strategy_anchor(
        "does_not_exist", valid_strategy_ids=VALID, now=NOW, base_dir=str(tmp_path))
    assert res["ok"] is False


def test_apply_strategy_then_rollback_restores_prior(tmp_path):
    base = str(tmp_path)
    # Prior active strategy set by a human.
    SS.record_strategy_decision("defensive", "approve", "pesantez",
                                valid_strategy_ids=VALID, base_dir=base)
    cand = {"candidate_id": "c", "candidate_type": "strategy",
            "strategy_id": "aggressive_growth", "target_lane": "simulation",
            "production_mutation": False, "feeds_decision_engine": False,
            "is_human_approved": False, "confidence": 0.9}
    res = AA.apply_strategy_candidate(cand, now=NOW, base_dir=base, valid_strategy_ids=VALID)
    assert res["status"] == "applied"
    assert SS.load_active_selection(base)["active_strategy_id"] == "aggressive_growth"
    event = {"strategy_id": "aggressive_growth", "target_id": "aggressive_growth",
             "before_state": res["before_state"], "after_state": res["after_state"]}
    rb = AA.rollback_strategy_event(event, base_dir=base)
    assert rb["status"] == "rolled_back"
    # Prior human-approved strategy restored.
    assert SS.load_active_selection(base)["active_strategy_id"] == "defensive"


def test_rollback_strategy_conflict_when_changed_since(tmp_path):
    base = str(tmp_path)
    cand = {"candidate_id": "c", "candidate_type": "strategy",
            "strategy_id": "aggressive_growth", "target_lane": "simulation",
            "production_mutation": False, "feeds_decision_engine": False,
            "is_human_approved": False, "confidence": 0.9}
    res = AA.apply_strategy_candidate(cand, now=NOW, base_dir=base, valid_strategy_ids=VALID)
    # A human re-anchors to a different strategy AFTER the auto-apply.
    SS.record_strategy_decision("defensive", "approve", "pesantez",
                                valid_strategy_ids=VALID, base_dir=base)
    event = {"strategy_id": "aggressive_growth", "target_id": "aggressive_growth",
             "before_state": res["before_state"], "after_state": res["after_state"]}
    rb = AA.rollback_strategy_event(event, base_dir=base)
    assert rb["status"] == "rollback_conflict"
    # The human's later choice is preserved, not overwritten.
    assert SS.load_active_selection(base)["active_strategy_id"] == "defensive"
