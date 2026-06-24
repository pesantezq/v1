"""Tests for the strategy-lab approval / active-strategy selection core.

Covers the human-gated, sandbox-only selection writer:
  - approve persists active_strategy_selection.json + logs the decision
  - approve supersedes a prior active selection
  - reject/defer log only; reject of the active strategy clears it
  - AI / non-human approver is rejected (cannot self-approve)
  - invalid strategy_id (not in the review queue) is rejected
The module never writes decision_plan.json / config.json / signal_registry.yaml.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from portfolio_automation.strategy.strategy_selection import (
    record_strategy_decision,
    load_active_selection,
    resolve_anchor_tactic_id,
    mark_operator_selected,
)

VALID = ["long_term_compounding", "risk_parity_lite", "momentum_rotation"]


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_approve_persists_selection_and_logs(tmp_path):
    out = tmp_path / "outputs"
    res = record_strategy_decision(
        "risk_parity_lite", "approve", "operator",
        valid_strategy_ids=VALID, strategy_name="Risk Parity (lite)", base_dir=out,
    )
    assert res["ok"] is True
    assert res["active_strategy_id"] == "risk_parity_lite"

    sel = load_active_selection(out)
    assert sel["active_strategy_id"] == "risk_parity_lite"
    assert sel["name"] == "Risk Parity (lite)"
    assert sel["status"] == "approved"
    assert sel["approved_by"] == "operator"
    assert sel["observe_only"] is True
    assert sel["no_trade"] is True

    log = _read_jsonl(out / "policy" / "strategy_decisions.jsonl")
    assert len(log) == 1
    assert log[0]["strategy_id"] == "risk_parity_lite"
    assert log[0]["decision"] == "approve"
    assert log[0]["approver"] == "operator"


def test_approve_supersedes_prior(tmp_path):
    out = tmp_path / "outputs"
    record_strategy_decision("long_term_compounding", "approve", "operator",
                             valid_strategy_ids=VALID, base_dir=out)
    res = record_strategy_decision("risk_parity_lite", "approve", "operator",
                                   valid_strategy_ids=VALID, base_dir=out)
    assert res["ok"] is True
    assert res["supersedes"] == "long_term_compounding"
    sel = load_active_selection(out)
    assert sel["active_strategy_id"] == "risk_parity_lite"
    assert sel["supersedes"] == "long_term_compounding"


def test_reject_non_active_logs_only_keeps_active(tmp_path):
    out = tmp_path / "outputs"
    record_strategy_decision("risk_parity_lite", "approve", "operator",
                             valid_strategy_ids=VALID, base_dir=out)
    res = record_strategy_decision("momentum_rotation", "reject", "operator",
                                   valid_strategy_ids=VALID, base_dir=out)
    assert res["ok"] is True
    # active is still the previously approved one
    assert load_active_selection(out)["active_strategy_id"] == "risk_parity_lite"
    log = _read_jsonl(out / "policy" / "strategy_decisions.jsonl")
    assert log[-1]["decision"] == "reject"
    assert log[-1]["strategy_id"] == "momentum_rotation"


def test_reject_of_active_clears_selection(tmp_path):
    out = tmp_path / "outputs"
    record_strategy_decision("risk_parity_lite", "approve", "operator",
                             valid_strategy_ids=VALID, base_dir=out)
    res = record_strategy_decision("risk_parity_lite", "reject", "operator",
                                   valid_strategy_ids=VALID, base_dir=out)
    assert res["ok"] is True
    assert res["active_strategy_id"] is None
    assert load_active_selection(out).get("active_strategy_id") is None


def test_defer_logs_only_no_selection_change(tmp_path):
    out = tmp_path / "outputs"
    record_strategy_decision("risk_parity_lite", "approve", "operator",
                             valid_strategy_ids=VALID, base_dir=out)
    record_strategy_decision("momentum_rotation", "defer", "operator",
                             valid_strategy_ids=VALID, base_dir=out)
    assert load_active_selection(out)["active_strategy_id"] == "risk_parity_lite"
    assert _read_jsonl(out / "policy" / "strategy_decisions.jsonl")[-1]["decision"] == "defer"


def test_ai_approver_rejected(tmp_path):
    out = tmp_path / "outputs"
    res = record_strategy_decision("risk_parity_lite", "approve", "ai_reviewer",
                                   valid_strategy_ids=VALID, base_dir=out)
    assert res["ok"] is False
    assert "human" in res["reason"].lower() or "approver" in res["reason"].lower()
    # nothing written
    assert load_active_selection(out).get("active_strategy_id") is None
    assert not (out / "policy" / "strategy_decisions.jsonl").exists()


def test_invalid_strategy_id_rejected(tmp_path):
    out = tmp_path / "outputs"
    res = record_strategy_decision("not_a_real_strategy", "approve", "operator",
                                   valid_strategy_ids=VALID, base_dir=out)
    assert res["ok"] is False
    assert not (out / "policy" / "strategy_decisions.jsonl").exists()


def test_invalid_decision_rejected(tmp_path):
    out = tmp_path / "outputs"
    res = record_strategy_decision("risk_parity_lite", "promote", "operator",
                                   valid_strategy_ids=VALID, base_dir=out)
    assert res["ok"] is False


def test_load_active_selection_absent_returns_empty(tmp_path):
    out = tmp_path / "outputs"
    assert load_active_selection(out) == {}


# --- anchor resolver (strategy_id -> tactic_id) ---

TACTIC_IDS = {"shadow_actual_baseline", "profile_long_term_compounding",
              "profile_risk_parity_lite", "benchmark_spy"}


def test_resolve_anchor_maps_profile_prefix():
    assert resolve_anchor_tactic_id("long_term_compounding", TACTIC_IDS) == \
        "profile_long_term_compounding"


def test_resolve_anchor_exact_match_wins():
    ids = {"profile_long_term_compounding", "long_term_compounding"}
    assert resolve_anchor_tactic_id("long_term_compounding", ids) == "long_term_compounding"


def test_resolve_anchor_none_when_absent():
    assert resolve_anchor_tactic_id("not_a_strategy", TACTIC_IDS) is None
    assert resolve_anchor_tactic_id(None, TACTIC_IDS) is None


# --- operator_selected marking ---

def test_mark_operator_selected_flags_matching_row():
    rows = [{"strategy_id": "a"}, {"strategy_id": "b"}, {"strategy_id": "c"}]
    out = mark_operator_selected(rows, "b")
    assert [r["operator_selected"] for r in out] == [False, True, False]


def test_mark_operator_selected_none_flags_all_false():
    rows = [{"strategy_id": "a"}, {"strategy_id": "b"}]
    out = mark_operator_selected(rows, None)
    assert all(r["operator_selected"] is False for r in out)
