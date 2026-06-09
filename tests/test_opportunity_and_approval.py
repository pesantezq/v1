"""Phase 8 + Phase 4 — market-opportunity prompts, approval queues, prompt generators."""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

import portfolio_automation.market_opportunity_prompts as mop
import portfolio_automation.approval_queue as aq
import portfolio_automation.claude_code_prompts as ccp


def _now():
    return datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)


def _radar(tmp_path: Path):
    sb = tmp_path / "outputs" / "sandbox"; sb.mkdir(parents=True, exist_ok=True)
    (tmp_path / "outputs" / "latest").mkdir(parents=True, exist_ok=True)
    sb.joinpath("opportunity_radar.json").write_text(json.dumps({"opportunities": [
        {"candidate": "AMD", "candidate_type": "public_ticker", "theme": "AI Infrastructure",
         "final_status": "QUALIFIED", "opportunity_score": 0.62, "boom_score": 0.5,
         "risk_score": 0.3, "investability_score": 0.8},
        {"candidate": "SpaceX", "candidate_type": "private_ipo", "theme": "Space Economy",
         "final_status": "PRIVATE_WATCH_ONLY", "opportunity_score": 0.5},
        {"candidate": "JUNK", "candidate_type": "public_ticker", "final_status": "REJECTED"}]}))


# ── Phase 8: market opportunity prompts ──────────────────────────────────

def test_market_prompts_only_reviewable_and_sandbox(tmp_path):
    _radar(tmp_path)
    mop.write_market_opportunity_artifacts(tmp_path, _now())
    sb = tmp_path / "outputs" / "sandbox"
    cards = json.loads((sb / "market_opportunity_review_cards.json").read_text())["cards"]
    names = {c["candidate"] for c in cards}
    assert "AMD" in names and "JUNK" not in names  # REJECTED excluded
    # never writes official recommendation
    assert not (tmp_path / "outputs" / "latest" / "decision_plan.json").exists()


def test_private_prompt_is_access_route_only(tmp_path):
    _radar(tmp_path)
    mop.write_market_opportunity_artifacts(tmp_path, _now())
    prompts = json.loads((tmp_path / "outputs" / "sandbox" / "market_opportunity_prompts.json").read_text())
    spacex = next(p for p in prompts["prompts"] if p["candidate"] == "SpaceX")
    assert "not directly tradeable" in spacex["prompt_text"].lower()


def test_opportunity_queue_blocks_execution(tmp_path):
    _radar(tmp_path)
    mop.write_market_opportunity_artifacts(tmp_path, _now())
    q = json.loads((tmp_path / "outputs" / "sandbox" / "opportunity_approval_queue.json").read_text())
    for item in q["queue"]:
        assert "approve_to_watchlist_review" in item["allowed_actions"]
        assert "place_trade" in item["blocked_actions"]


def test_summarizer_hook_falls_back(tmp_path):
    _radar(tmp_path)
    def boom(_):
        raise RuntimeError("llm down")
    res = mop.write_market_opportunity_artifacts(tmp_path, _now(), summarizer=boom)
    assert res["degraded"] is False  # deterministic prompts still produced


# ── Phase 4: prompt generators ───────────────────────────────────────────

def test_system_improvement_prompt_has_safety_and_forbidden():
    idea = {"title": "Add stale probe", "category": "observability", "summary": "x",
            "evidence": ["e"], "proposed_change": "add probe", "affected_modules": ["m.py"],
            "acceptance_criteria": ["probe added"], "suggested_tests": ["t"]}
    p = ccp.generate_system_improvement_prompt(idea)
    assert "Forbidden" in p and "No auto-trading" in p
    assert "Final report" in p and "Acceptance criteria" in p


def test_market_research_prompt_forbids_execution_and_flags_private():
    p = ccp.generate_market_opportunity_research_prompt(
        {"candidate": "SpaceX", "theme": "Space", "final_status": "PRIVATE_WATCH_ONLY",
         "summary": "research"})
    assert "research evidence only" in p.lower() or "research output is evidence only" in p.lower()
    assert "not directly tradeable" in p.lower()
    assert "No auto-trading" in p


# ── Phase 4: approval queue (append-only, cooldown, executes nothing) ─────

def test_record_decision_append_only_and_executes_nothing(tmp_path):
    rec = aq.record_decision(tmp_path, "system_improvement", "si-1",
                             "approve_for_implementation", now=_now())
    assert rec["executes_nothing"] is True
    dec = (tmp_path / "outputs" / "policy" / "system_improvement_decisions.jsonl").read_text()
    assert "si-1" in dec
    # mirrored to user_action_log event stream
    assert (tmp_path / "outputs" / "policy" / "user_action_log.jsonl").exists()


def test_record_decision_rejects_bad_decision(tmp_path):
    with pytest.raises(ValueError):
        aq.record_decision(tmp_path, "system_improvement", "x", "place_trade", now=_now())
    with pytest.raises(ValueError):
        aq.record_decision(tmp_path, "not_a_queue", "x", "reject", now=_now())


def test_build_queues_suppresses_decided_items(tmp_path):
    L = tmp_path / "outputs" / "latest"; L.mkdir(parents=True)
    (tmp_path / "outputs" / "sandbox").mkdir(parents=True)
    L.joinpath("system_improvement_ideas.json").write_text(json.dumps({"ideas": [
        {"id": "si-a", "title": "A", "category": "testing", "priority": "high",
         "final_rank_score": 0.7, "status": "proposed"},
        {"id": "si-b", "title": "B", "category": "docs", "priority": "low",
         "final_rank_score": 0.4, "status": "proposed"}]}))
    # reject si-b
    aq.record_decision(tmp_path, "system_improvement", "si-b", "reject", now=_now())
    aq.build_action_queues(tmp_path, _now())
    q = json.loads((L / "system_improvement_action_queue.json").read_text())
    ids = {i["id"] for i in q["queue"]}
    assert "si-a" in ids and "si-b" not in ids  # rejected (in cooldown) suppressed


def test_cooldown_elapses(tmp_path):
    L = tmp_path / "outputs" / "latest"; L.mkdir(parents=True)
    (tmp_path / "outputs" / "sandbox").mkdir(parents=True)
    L.joinpath("system_improvement_ideas.json").write_text(json.dumps({"ideas": [
        {"id": "si-c", "title": "C", "category": "testing", "priority": "high",
         "final_rank_score": 0.7, "status": "proposed"}]}))
    # reject 20 days ago → cooldown (14d) elapsed → resurfaces
    aq.record_decision(tmp_path, "system_improvement", "si-c", "reject",
                       now=_now() - timedelta(days=20))
    aq.build_action_queues(tmp_path, _now())
    q = json.loads((L / "system_improvement_action_queue.json").read_text())
    assert "si-c" in {i["id"] for i in q["queue"]}
