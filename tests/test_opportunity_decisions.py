"""Market-opportunity approval decisions (record-only sink + validation).

Validation enforces: human approver (AI can't self-approve), known
opportunity_id, and action restricted to that item's allowed_actions (so the
blocked trade-verbs are structurally un-invokable). Decisions append to
user_decisions.jsonl (POLICY).
"""
from __future__ import annotations

import json
from pathlib import Path

from portfolio_automation.next_stage.opportunity_decisions import (
    validate_opportunity_action,
    append_opportunity_decision,
)

QUEUE = [
    {"id": "mo-panw", "candidate": "PANW",
     "allowed_actions": ["approve_to_watchlist_review", "reject", "keep_watching"]},
    {"id": "mo-xom", "candidate": "XOM",
     "allowed_actions": ["reject", "keep_watching"]},
]


def test_validate_ok():
    v = validate_opportunity_action("mo-panw", "approve_to_watchlist_review", "operator", QUEUE)
    assert v["ok"] is True
    assert v["candidate"] == "PANW"


def test_validate_rejects_ai_approver():
    v = validate_opportunity_action("mo-panw", "reject", "ai_reviewer", QUEUE)
    assert v["ok"] is False


def test_validate_rejects_unknown_id():
    v = validate_opportunity_action("mo-ghost", "reject", "operator", QUEUE)
    assert v["ok"] is False


def test_validate_rejects_action_not_allowed_for_item():
    # mo-xom does not allow approve_to_watchlist_review
    v = validate_opportunity_action("mo-xom", "approve_to_watchlist_review", "operator", QUEUE)
    assert v["ok"] is False


def test_validate_rejects_trade_verb():
    v = validate_opportunity_action("mo-panw", "place_trade", "operator", QUEUE)
    assert v["ok"] is False


def test_append_writes_decision_line(tmp_path):
    out = tmp_path / "outputs"
    append_opportunity_decision(
        "mo-panw", "PANW", "approve_to_watchlist_review", "operator",
        base_dir=out, promote_result={"status": "promoted", "reason": "operator_approved"},
    )
    p = out / "policy" / "user_decisions.jsonl"
    rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    assert rows[-1]["opportunity_id"] == "mo-panw"
    assert rows[-1]["candidate"] == "PANW"
    assert rows[-1]["action"] == "approve_to_watchlist_review"
    assert rows[-1]["approver"] == "operator"
    assert rows[-1]["promote_result"]["status"] == "promoted"
