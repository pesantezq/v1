"""Tests for the LLM-backed daily sim-governance reviewer.

Covers the ``make_openai_reviewer`` factory, its JSON parsing + graceful
heuristic fallback, the ``build_configured_reviewer`` gate, and the
end-to-end acceptance criterion: ``daily_ai_review_result.review_method == 'llm'``
when an LLM reviewer is wired in.

All model calls are monkeypatched — no real OpenAI call is made and no spend
occurs during the test suite.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from portfolio_automation.sim_governance import daily_ai_review as REV
from portfolio_automation.sim_governance import schemas as S

NOW = "2026-07-02T00:00:00+00:00"


@pytest.fixture
def base_dir(tmp_path: Path) -> str:
    (tmp_path / "outputs" / "promotion_review").mkdir(parents=True, exist_ok=True)
    return str(tmp_path / "outputs")


def _packet() -> dict:
    """A minimal but schema-faithful review packet (advisory + watchlist)."""
    advisory = [{
        "candidate_id": "cand_adv_1", "workflow": S.WORKFLOW_ADVISORY,
        "symbol": "META", "proposal_type": "crowd_context_change",
        "what_changed": "confirmed_attention", "risk_impact": "low",
        "confidence": 0.86, "data_quality": "ok", "sim_ready_hint": True,
    }]
    watchlist = [{
        "candidate_id": "cand_wl_1", "workflow": S.WORKFLOW_WATCHLIST,
        "symbol": "AAPL", "proposal_type": "watchlist_add",
        "what_changed": "add AAPL", "risk_impact": "medium",
        "confidence": 0.55, "data_quality": "ok", "sim_ready_hint": False,
    }]
    return {
        "generated_at": NOW,
        "candidate_count": 2,
        "instruction": "Review BOTH workflows together.",
        "covers_workflows": [S.WORKFLOW_ADVISORY, S.WORKFLOW_WATCHLIST],
        "advisory_candidates": advisory,
        "watchlist_candidates": watchlist,
        "estimated_prompt_tokens": 200,
    }


# ---------------------------------------------------------------------------
# Acceptance mechanism: any injected reviewer flips review_method to 'llm'
# ---------------------------------------------------------------------------

def test_injected_reviewer_marks_review_method_llm(base_dir):
    res = REV.run_daily_ai_review(
        _packet(), NOW, base_dir=base_dir, daily_cost_cap_usd=0.50,
        reviewer=REV.heuristic_reviewer,
    )
    assert res["status"] == "reviewed"
    assert res["review_method"] == "llm"          # ← acceptance criterion
    assert res["actual_cost_usd"] > 0.0           # LLM path records real spend


# ---------------------------------------------------------------------------
# make_openai_reviewer: parse model JSON into verdicts
# ---------------------------------------------------------------------------

def test_openai_reviewer_parses_model_json(monkeypatch):
    model_json = json.dumps([
        {"candidate_id": "cand_adv_1", "decision": "ready_for_production_review",
         "reason": "clean evidence", "evidence_strength": "strong",
         "risk_level": "low", "missing_evidence": [], "rollback_readiness": "ready"},
        {"candidate_id": "cand_wl_1", "decision": "continue_testing",
         "reason": "needs more data", "evidence_strength": "moderate",
         "risk_level": "medium", "missing_evidence": ["more history"],
         "rollback_readiness": "partial"},
    ])
    monkeypatch.setattr(REV, "_call_llm", lambda **kw: model_json)

    reviewer = REV.make_openai_reviewer(provider="openai", model="gpt-4o-mini")
    verdicts = reviewer(_packet())

    assert len(verdicts) == 2
    by_id = {v["candidate_id"]: v for v in verdicts}
    assert by_id["cand_adv_1"]["decision"] == S.DECISION_READY
    assert by_id["cand_wl_1"]["decision"] == S.DECISION_CONTINUE_TESTING
    # AI can NEVER self-approve — human review is always required.
    assert all(v["required_human_review"] is True for v in verdicts)
    # decisions are constrained to the allowed set
    assert all(v["decision"] in S.REVIEW_DECISIONS for v in verdicts)


def test_openai_reviewer_handles_code_fenced_json(monkeypatch):
    fenced = "```json\n" + json.dumps(
        [{"candidate_id": "cand_adv_1", "decision": "reject", "reason": "low conf"}]
    ) + "\n```"
    monkeypatch.setattr(REV, "_call_llm", lambda **kw: fenced)
    reviewer = REV.make_openai_reviewer()
    verdicts = reviewer(_packet())
    by_id = {v["candidate_id"]: v for v in verdicts}
    assert by_id["cand_adv_1"]["decision"] == S.DECISION_REJECT
    # the watchlist candidate the model omitted is still covered (filled)
    assert "cand_wl_1" in by_id


def test_openai_reviewer_coerces_unknown_decision(monkeypatch):
    model_json = json.dumps([
        {"candidate_id": "cand_adv_1", "decision": "definitely buy it",
         "reason": "garbled"},
    ])
    monkeypatch.setattr(REV, "_call_llm", lambda **kw: model_json)
    reviewer = REV.make_openai_reviewer()
    verdicts = reviewer(_packet())
    by_id = {v["candidate_id"]: v for v in verdicts}
    # unrecognised decision degrades to the conservative continue_testing
    assert by_id["cand_adv_1"]["decision"] == S.DECISION_CONTINUE_TESTING


# ---------------------------------------------------------------------------
# Graceful fallback: API failure / unparseable → heuristic verdicts preserved
# ---------------------------------------------------------------------------

def test_openai_reviewer_falls_back_on_api_error(monkeypatch):
    def _boom(**kw):
        raise RuntimeError("OpenAI unreachable")
    monkeypatch.setattr(REV, "_call_llm", _boom)

    reviewer = REV.make_openai_reviewer()
    verdicts = reviewer(_packet())

    # verdicts are NOT lost — heuristic covers every candidate
    assert {v["candidate_id"] for v in verdicts} == {"cand_adv_1", "cand_wl_1"}
    assert all("[llm-fallback:heuristic]" in v["reason"] for v in verdicts)


def test_openai_reviewer_falls_back_on_unparseable(monkeypatch):
    monkeypatch.setattr(REV, "_call_llm", lambda **kw: "I cannot help with that.")
    reviewer = REV.make_openai_reviewer()
    verdicts = reviewer(_packet())
    assert {v["candidate_id"] for v in verdicts} == {"cand_adv_1", "cand_wl_1"}
    assert all("[llm-fallback:heuristic]" in v["reason"] for v in verdicts)


def test_openai_reviewer_fills_model_omitted_candidate(monkeypatch):
    model_json = json.dumps([
        {"candidate_id": "cand_adv_1", "decision": "ready_for_production_review",
         "reason": "clean"},
    ])
    monkeypatch.setattr(REV, "_call_llm", lambda **kw: model_json)
    reviewer = REV.make_openai_reviewer()
    verdicts = reviewer(_packet())
    by_id = {v["candidate_id"]: v for v in verdicts}
    assert set(by_id) == {"cand_adv_1", "cand_wl_1"}
    assert "[llm-omitted:heuristic]" in by_id["cand_wl_1"]["reason"]


def test_openai_reviewer_salvages_truncated_json(monkeypatch):
    # A valid array prefix truncated mid-object (model hit its token limit).
    truncated = (
        '```json\n[\n'
        '  {"candidate_id": "cand_adv_1", "decision": "ready_for_production_review",'
        ' "reason": "clean", "risk_level": "low"},\n'
        '  {"candidate_id": "cand_wl_1", "decision": "continue_te'  # cut off here
    )
    monkeypatch.setattr(REV, "_call_llm", lambda **kw: truncated)
    reviewer = REV.make_openai_reviewer()
    verdicts = reviewer(_packet())
    by_id = {v["candidate_id"]: v for v in verdicts}
    # the one complete object is recovered from the truncated array...
    assert by_id["cand_adv_1"]["decision"] == S.DECISION_READY
    assert "[llm-fallback:heuristic]" not in by_id["cand_adv_1"]["reason"]
    # ...and the cut-off candidate is still covered via the heuristic fill.
    assert "cand_wl_1" in by_id


def test_parse_verdict_json_salvage_unit():
    txt = '[{"candidate_id":"a","decision":"reject"}, {"candidate_id":"b","dec'
    got = REV._parse_verdict_json(txt)
    assert [o["candidate_id"] for o in got] == ["a"]


def test_openai_reviewer_empty_packet_returns_empty(monkeypatch):
    called = {"n": 0}
    def _spy(**kw):
        called["n"] += 1
        return "[]"
    monkeypatch.setattr(REV, "_call_llm", _spy)
    reviewer = REV.make_openai_reviewer()
    empty = {"advisory_candidates": [], "watchlist_candidates": []}
    assert reviewer(empty) == []
    assert called["n"] == 0   # no model call when there is nothing to review


# ---------------------------------------------------------------------------
# build_configured_reviewer: gating (config flag + kill-switch + key presence)
# ---------------------------------------------------------------------------

def test_build_configured_reviewer_disabled_by_default():
    assert REV.build_configured_reviewer({}) is None
    assert REV.build_configured_reviewer({"llm_enabled": False}) is None


def test_build_configured_reviewer_enabled_with_key(monkeypatch):
    monkeypatch.setattr(REV, "get_secret", lambda name, default=None: "sk-test")
    monkeypatch.delenv("STOCKBOT_SIM_GOV_LLM_DISABLED", raising=False)
    rv = REV.build_configured_reviewer({"llm_enabled": True, "model": "gpt-4o-mini"})
    assert callable(rv)


def test_build_configured_reviewer_no_key_stays_heuristic(monkeypatch):
    monkeypatch.setattr(REV, "get_secret", lambda name, default=None: "")
    monkeypatch.delenv("STOCKBOT_SIM_GOV_LLM_DISABLED", raising=False)
    # llm_enabled but no key → None (honest: don't claim 'llm' when it can't run)
    assert REV.build_configured_reviewer({"llm_enabled": True}) is None


def test_build_configured_reviewer_kill_switch(monkeypatch):
    monkeypatch.setattr(REV, "get_secret", lambda name, default=None: "sk-test")
    monkeypatch.setenv("STOCKBOT_SIM_GOV_LLM_DISABLED", "1")
    assert REV.build_configured_reviewer({"llm_enabled": True}) is None


# ---------------------------------------------------------------------------
# End-to-end acceptance: configured reviewer → run → review_method == 'llm'
# ---------------------------------------------------------------------------

def test_end_to_end_configured_reviewer_llm_method(base_dir, monkeypatch):
    monkeypatch.setattr(REV, "get_secret", lambda name, default=None: "sk-test")
    monkeypatch.delenv("STOCKBOT_SIM_GOV_LLM_DISABLED", raising=False)
    monkeypatch.setattr(REV, "_call_llm", lambda **kw: json.dumps([
        {"candidate_id": "cand_adv_1", "decision": "ready_for_production_review",
         "reason": "clean"},
        {"candidate_id": "cand_wl_1", "decision": "continue_testing", "reason": "wait"},
    ]))
    reviewer = REV.build_configured_reviewer({"llm_enabled": True, "model": "gpt-4o-mini"})
    res = REV.run_daily_ai_review(_packet(), NOW, base_dir=base_dir,
                                  daily_cost_cap_usd=0.50, reviewer=reviewer)
    assert res["status"] == "reviewed"
    assert res["review_method"] == "llm"
    assert res["counts"][S.DECISION_READY] == 1
