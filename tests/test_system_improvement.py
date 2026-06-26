"""Phase 3 — daily system-improvement producer.

Covers: deterministic detection from telemetry, no market verbs, dedup/cooldown,
graceful degradation, artifact writes, optional summarizer hook.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

import portfolio_automation.system_improvement as si


def _latest(tmp_path: Path) -> Path:
    d = tmp_path / "outputs" / "latest"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now():
    return datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_detects_invalid_json_from_registry_status(tmp_path):
    _latest(tmp_path).joinpath("artifact_registry_status.json").write_text(json.dumps({
        "invalid_json": ["foo.json"], "missing": [], "counts": {"missing_required": 0},
    }))
    payload = si.build_system_improvement(tmp_path, _now())
    titles = [i["title"] for i in payload["ideas"]]
    assert any("invalid-JSON" in t for t in titles)
    art = next(i for i in payload["ideas"] if "invalid-JSON" in i["title"])
    assert art["category"] == "artifact_contract"
    assert art["observe_only"] is True


def test_detects_failed_stage_and_data_quality(tmp_path):
    L = _latest(tmp_path)
    L.joinpath("daily_run_status.json").write_text(json.dumps({
        "stage_summary": {"ok": 10, "failed": 2, "warn": 0}, "content_warn_count": 0}))
    L.joinpath("data_quality_report.json").write_text(json.dumps({"critical_count": 3}))
    payload = si.build_system_improvement(tmp_path, _now())
    cats = {i["category"] for i in payload["ideas"]}
    assert "reliability" in cats and "data_quality" in cats


def test_roadmap_alignment_idea(tmp_path):
    _latest(tmp_path)
    agent = tmp_path / ".agent"
    agent.mkdir()
    agent.joinpath("project_state.yaml").write_text("next_official_step: observe_and_iterate\n")
    payload = si.build_system_improvement(tmp_path, _now())
    road = [i for i in payload["ideas"] if i["category"] == "roadmap_alignment"]
    assert road and "observe_and_iterate" in road[0]["summary"]


# ---------------------------------------------------------------------------
# Safety: never a market recommendation
# ---------------------------------------------------------------------------


def test_no_market_verbs_anywhere(tmp_path):
    L = _latest(tmp_path)
    L.joinpath("daily_run_status.json").write_text(json.dumps({"stage_summary": {"failed": 1}}))
    L.joinpath("data_quality_report.json").write_text(json.dumps({"warning_count": 5}))
    payload = si.build_system_improvement(tmp_path, _now())
    blob = json.dumps(payload)
    # The producer must not emit buy/sell/hold/trade as standalone market verbs.
    for idea in payload["ideas"]:
        text = " ".join([idea["title"], idea["summary"], idea["proposed_change"]])
        assert not re.search(r"\b(buy|sell|hold|trade)\b", text, re.IGNORECASE), text
    assert "ideas" in payload


def test_every_idea_carries_safety_constraints_and_blocked_actions(tmp_path):
    L = _latest(tmp_path)
    L.joinpath("daily_run_status.json").write_text(json.dumps({"stage_summary": {"failed": 1}}))
    payload = si.build_system_improvement(tmp_path, _now())
    for idea in payload["ideas"]:
        assert idea["safety_constraints"]
        assert "place_trade" in idea["blocked_actions"]
        assert "modify_real_holdings" in idea["blocked_actions"]


# ---------------------------------------------------------------------------
# Dedup / cooldown (§15)
# ---------------------------------------------------------------------------


def test_rejected_idea_suppressed_during_cooldown(tmp_path):
    L = _latest(tmp_path)
    L.joinpath("data_quality_report.json").write_text(json.dumps({"critical_count": 3}))
    # find the key it would produce
    payload0 = si.build_system_improvement(tmp_path, _now())
    key = si.idea_key("data_quality", next(
        i["title"] for i in payload0["ideas"] if i["category"] == "data_quality"))
    # write history rejecting it with a future cooldown
    pol = tmp_path / "outputs" / "policy"
    pol.mkdir(parents=True)
    future = (_now().date() + timedelta(days=10)).isoformat()
    pol.joinpath("system_improvement_history.jsonl").write_text(json.dumps({
        "idea_key": key, "owner_decision": "rejected", "cooldown_until": future}) + "\n")
    payload1 = si.build_system_improvement(tmp_path, _now())
    assert all(si.idea_key(i["category"], i["title"]) != key for i in payload1["ideas"])


def test_completed_idea_not_resurfaced(tmp_path):
    L = _latest(tmp_path)
    L.joinpath("data_quality_report.json").write_text(json.dumps({"critical_count": 3}))
    p0 = si.build_system_improvement(tmp_path, _now())
    key = si.idea_key("data_quality", next(
        i["title"] for i in p0["ideas"] if i["category"] == "data_quality"))
    pol = tmp_path / "outputs" / "policy"; pol.mkdir(parents=True)
    pol.joinpath("system_improvement_history.jsonl").write_text(json.dumps({
        "idea_key": key, "status": "completed"}) + "\n")
    p1 = si.build_system_improvement(tmp_path, _now())
    assert all(si.idea_key(i["category"], i["title"]) != key for i in p1["ideas"])


# ---------------------------------------------------------------------------
# Unified suppression: a recorded operator decision (decisions.jsonl) closes the
# idea at the producer/brief layer too — keyed on item_id, no history line needed.
# ---------------------------------------------------------------------------


def test_decision_mark_completed_suppresses_without_history_line(tmp_path):
    L = _latest(tmp_path)
    L.joinpath("data_quality_report.json").write_text(json.dumps({"critical_count": 3}))
    p0 = si.build_system_improvement(tmp_path, _now())
    item_id = next(i["id"] for i in p0["ideas"] if i["category"] == "data_quality")
    pol = tmp_path / "outputs" / "policy"; pol.mkdir(parents=True)
    # decisions.jsonl uses the record_decision() schema (item_id); NO history owner_decision line
    pol.joinpath("system_improvement_decisions.jsonl").write_text(json.dumps({
        "item_id": item_id, "queue": "system_improvement", "decision": "mark_completed",
        "cooldown_until": None}) + "\n")
    p1 = si.build_system_improvement(tmp_path, _now())
    assert all(i["id"] != item_id for i in p1["ideas"])


def test_decision_defer_cooldown_then_resurface(tmp_path):
    L = _latest(tmp_path)
    L.joinpath("data_quality_report.json").write_text(json.dumps({"critical_count": 3}))
    p0 = si.build_system_improvement(tmp_path, _now())
    item_id = next(i["id"] for i in p0["ideas"] if i["category"] == "data_quality")
    pol = tmp_path / "outputs" / "policy"; pol.mkdir(parents=True)
    dpath = pol.joinpath("system_improvement_decisions.jsonl")
    # during cooldown → suppressed
    future = (_now().date() + timedelta(days=10)).isoformat()
    dpath.write_text(json.dumps({
        "item_id": item_id, "queue": "system_improvement", "decision": "defer",
        "cooldown_until": future}) + "\n")
    assert all(i["id"] != item_id
               for i in si.build_system_improvement(tmp_path, _now())["ideas"])
    # after cooldown elapses → resurfaces
    past = (_now().date() - timedelta(days=1)).isoformat()
    dpath.write_text(json.dumps({
        "item_id": item_id, "queue": "system_improvement", "decision": "defer",
        "cooldown_until": past}) + "\n")
    assert any(i["id"] == item_id
               for i in si.build_system_improvement(tmp_path, _now())["ideas"])


def test_legacy_id_field_decision_tolerated_no_crash(tmp_path):
    """Legacy hand-written records keyed on 'id' (not 'item_id') must not crash the
    build; they simply do not suppress (the known limitation this feature documents)."""
    L = _latest(tmp_path)
    L.joinpath("data_quality_report.json").write_text(json.dumps({"critical_count": 3}))
    p0 = si.build_system_improvement(tmp_path, _now())
    item_id = next(i["id"] for i in p0["ideas"] if i["category"] == "data_quality")
    pol = tmp_path / "outputs" / "policy"; pol.mkdir(parents=True)
    pol.joinpath("system_improvement_decisions.jsonl").write_text(json.dumps({
        "id": item_id, "decision": "mark_completed"}) + "\n")  # legacy 'id' field
    p1 = si.build_system_improvement(tmp_path, _now())  # must not raise
    assert any(i["id"] == item_id for i in p1["ideas"])  # not suppressed (legacy field)


# ---------------------------------------------------------------------------
# Degradation + ranking
# ---------------------------------------------------------------------------


def test_empty_root_degrades_to_empty_not_crash(tmp_path):
    payload = si.build_system_improvement(tmp_path, _now())
    assert payload["observe_only"] is True
    assert payload["ideas"] == []
    assert payload["idea_count"] == 0


def test_ideas_sorted_by_rank_desc(tmp_path):
    L = _latest(tmp_path)
    L.joinpath("daily_run_status.json").write_text(json.dumps({"stage_summary": {"failed": 1}}))
    L.joinpath("ai_budget_summary.json").write_text(json.dumps(
        {"monthly_cost_total_usd": 19.0, "monthly_cost_limit_usd": 20.0, "blocked": False}))
    payload = si.build_system_improvement(tmp_path, _now())
    assert len(payload["ideas"]) >= 2  # reliability + cost_budget
    ranks = [i["final_rank_score"] for i in payload["ideas"]]
    assert ranks == sorted(ranks, reverse=True)


# ---------------------------------------------------------------------------
# Writes + summarizer hook
# ---------------------------------------------------------------------------


def test_writes_four_artifacts(tmp_path):
    L = _latest(tmp_path)
    L.joinpath("data_quality_report.json").write_text(json.dumps({"warning_count": 2}))
    res = si.write_system_improvement_artifacts(tmp_path, _now())
    base = tmp_path / "outputs"
    assert (base / "latest" / "system_improvement_ideas.json").exists()
    assert (base / "latest" / "system_improvement_brief.md").exists()
    assert (base / "latest" / "system_improvement_scorecard.json").exists()
    assert (base / "policy" / "system_improvement_history.jsonl").exists()
    assert res["degraded"] is False


def test_summarizer_hook_used_and_falls_back(tmp_path):
    _latest(tmp_path)
    # success path
    res = si.write_system_improvement_artifacts(
        tmp_path, _now(), summarizer=lambda b: "POLISHED BRIEF")
    brief = (tmp_path / "outputs" / "latest" / "system_improvement_brief.md").read_text()
    assert "POLISHED BRIEF" in brief
    # failure path → deterministic brief stands (no crash)
    def boom(_):
        raise RuntimeError("llm down")
    si.write_system_improvement_artifacts(tmp_path, _now(), summarizer=boom)
    brief2 = (tmp_path / "outputs" / "latest" / "system_improvement_brief.md").read_text()
    assert "System Improvement Brief" in brief2
