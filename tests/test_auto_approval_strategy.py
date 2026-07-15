"""
Auto-approval strategy anchor (simulation) + event-aware CAS rollback.

The strategy channel ships DISABLED (cap 0); these tests exercise the mechanism so a
future enablement is safe. It must NEVER travel the human `record_strategy_decision`
path and must never mark itself human-approved.
"""
from __future__ import annotations

import pytest

from portfolio_automation.sim_governance import auto_approval as AA
from portfolio_automation.sim_governance import schemas as S
from portfolio_automation.strategy import strategy_selection as SS

VALID = {"aggressive_growth", "defensive"}
NOW = "2026-07-14T00:00:00Z"


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
