"""End-to-end workflow tests for the entire next-stage platform.

Exercises complete chains (not just per-module units): full orchestrator run →
all artifacts → operator decision workflows → dashboard render → Claude Code
prompt generation → event store → cross-cutting safety. Hermetic: builds a
realistic temp repo from synthetic-but-representative fixtures.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from portfolio_automation.next_stage.run_next_stage import run_all


# ---------------------------------------------------------------------------
# Realistic repo fixture
# ---------------------------------------------------------------------------


def _build_repo(tmp_path: Path, *, broker_fresh=True, broker_enabled=True) -> Path:
    (tmp_path / "config").mkdir(parents=True)
    (tmp_path / ".agent").mkdir()
    L = tmp_path / "outputs" / "latest"; L.mkdir(parents=True)

    (tmp_path / "config.json").write_text(json.dumps({
        "watchlist_scanner": {"watchlist": ["AAPL", "MSFT", "NVDA", "AMD"]},
        "portfolio": {
            "holdings": [
                {"symbol": "QQQ", "shares": 20, "target_weight": 0.45},
                {"symbol": "QLD", "shares": 4, "target_weight": 0.05, "is_leveraged": True},
                {"symbol": "GLD", "shares": 10, "target_weight": 0.30},
                {"symbol": "VXUS", "shares": 15, "target_weight": 0.20}],
            "cash_available": 1200.0,
            "broker_aware": {"enabled": broker_enabled}}}))
    (tmp_path / "config" / "universe_lists.yaml").write_text(
        "broad_market_etfs: [SPY, QQQ, VTI, VXUS]\n"
        "sector_etfs: [XLK, XLE, XLF]\n"
        "commodity_proxies: [GLD, URA, USO]\n"
        "theme_baskets:\n"
        "  AI Infrastructure: [NVDA, AMD, SMCI]\n"
        "  Space Economy: [RKLB]\n"
        "  Defense: [LMT, NOC]\n"
        "private_ipo_watch:\n"
        "  - name: SpaceX\n    theme: Space Economy\n    access_route: proxy\n    proxies: [RKLB]\n"
        "user_themes: []\n")
    (tmp_path / ".agent" / "project_state.yaml").write_text(
        "next_official_step:\n  primary: observe_and_iterate\n")

    # real-schema signal artifacts (enrich the radar + improvement detectors)
    L.joinpath("market_opportunities.json").write_text(json.dumps({"promoted": [
        {"symbol": "AMD", "score": 78, "label": "momentum"},
        {"symbol": "NVDA", "score": 71, "label": "compounder"}]}))
    L.joinpath("watchlist_signals.json").write_text(json.dumps({"results": [
        {"symbol": "AMD", "signal_score": 0.7, "confidence": 0.8},
        {"symbol": "MSFT", "signal_score": 0.5, "confidence": 0.7}]}))
    L.joinpath("theme_signals.json").write_text(json.dumps({"themes": [
        {"name": "AI Infrastructure", "confidence": 0.85},
        {"name": "Defense", "confidence": 0.6}]}))
    L.joinpath("data_quality_report.json").write_text(json.dumps({
        "available": True, "total_symbols": 40, "healthy_symbols": 30,
        "warning_symbols": ["A", "B", "C"], "critical_symbols": []}))
    L.joinpath("daily_run_status.json").write_text(json.dumps({
        "stage_summary": {"total": 24, "ok": 22, "warn": 2, "failed": 0},
        "content_warn_count": 1, "required_missing_count": 0}))
    L.joinpath("confidence_calibration.json").write_text(json.dumps({
        "available": True, "insufficient_data": False,
        "overall_calibration_gap": 0.22, "overall_hit_rate": 0.5}))
    L.joinpath("ai_budget_summary.json").write_text(json.dumps({
        "blocked": False, "warning": False,
        "monthly_cost_total_usd": 4.0, "monthly_cost_limit_usd": 20.0}))
    if broker_fresh:
        ts = datetime.now(timezone.utc).isoformat()
        L.joinpath("schwab_positions.json").write_text(json.dumps({"positions": [
            {"symbol": "QQQ", "quantity": 20, "market_value": 8000.0, "average_cost": 350,
             "unrealized_gain": 1000},
            {"symbol": "QLD", "quantity": 4, "market_value": 2000.0},
            {"symbol": "GLD", "quantity": 10, "market_value": 2000.0}]}))
        L.joinpath("schwab_portfolio_snapshot.json").write_text(json.dumps({
            "snapshot_timestamp": ts, "totals": {"market_value": 12000.0, "cash": 1200.0}}))
    return tmp_path


@pytest.fixture
def repo(tmp_path):
    return _build_repo(tmp_path)


def _now():
    return datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 1. Full orchestrator → all artifacts, observe-only, no decision plan
# ---------------------------------------------------------------------------


def test_full_orchestrator_all_steps_ok(repo):
    res = run_all(repo, _now())
    steps = [k for k, v in res.items() if isinstance(v, dict) and "ok" in v]
    assert steps, "no steps ran"
    for name in steps:
        assert res[name]["ok"] is True, f"step {name} failed: {res[name].get('error')}"


def test_full_run_produces_every_expected_artifact(repo):
    run_all(repo, _now())
    expected = {
        "sandbox": ["universe_scan_candidates.json", "opportunity_radar.json",
                    "theme_candidates.json", "private_ipo_watchlist.json",
                    "shadow_opportunity_tracking.json", "shadow_portfolios.json",
                    "candidate_promotion_review.json", "market_opportunity_prompts.json",
                    "market_opportunity_review_cards.json", "opportunity_approval_queue.json",
                    "strategy_profiles.json", "strategy_comparison.json",
                    "strategy_shadow_results.json", "strategy_risk_scorecard.json",
                    "strategy_tax_scorecard.json"],
        "latest": ["system_improvement_ideas.json", "system_improvement_brief.md",
                   "system_improvement_scorecard.json", "operator_action_queue.json",
                   "system_improvement_action_queue.json", "strategy_review_queue.json"],
        "portfolio": ["broker_aware_portfolio.json"],
        "policy": ["system_improvement_history.jsonl"],
    }
    for ns, files in expected.items():
        for fn in files:
            p = repo / "outputs" / ns / fn
            assert p.exists(), f"missing {ns}/{fn}"
            if fn.endswith(".json"):
                d = json.loads(p.read_text())
                assert d.get("observe_only") is True, f"{fn} not observe_only"


def test_full_run_never_writes_decision_plan(repo):
    run_all(repo, _now())
    assert not (repo / "outputs" / "latest" / "decision_plan.json").exists()


_FORBIDDEN_TOKENS = ("place_trade", "submit_order", "execute_trade", "move_money",
                     "auto_rebalance", "broker_write_action", "modify_real_holdings")


def _strings_outside_blocked(node, under_blocked=False):
    """Yield all string values NOT under a *blocked*-named key."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _strings_outside_blocked(v, "block" in str(k).lower())
    elif isinstance(node, list):
        for v in node:
            yield from _strings_outside_blocked(v, under_blocked)
    elif isinstance(node, str) and not under_blocked:
        yield node


def test_no_execution_tokens_outside_blocked_actions(repo):
    run_all(repo, _now())
    for p in (repo / "outputs").rglob("*.json"):
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        for s in _strings_outside_blocked(data):
            low = s.lower()
            for tok in _FORBIDDEN_TOKENS:
                assert tok not in low, f"{p.name}: execution token {tok!r} outside blocked_actions in {s!r}"


# ---------------------------------------------------------------------------
# 2. Opportunity pipeline chain: radar → prompts → review cards → queue
# ---------------------------------------------------------------------------


def test_opportunity_chain_consistent(repo):
    run_all(repo, _now())
    sb = repo / "outputs" / "sandbox"
    radar = {o["candidate"] for o in json.loads((sb / "opportunity_radar.json").read_text())["opportunities"]}
    cards = {c["candidate"] for c in json.loads((sb / "market_opportunity_review_cards.json").read_text())["cards"]}
    # every review card corresponds to a radar candidate
    assert cards <= radar
    assert cards, "no review cards produced"


def test_private_candidate_flows_as_watch_only(repo):
    run_all(repo, _now())
    sb = repo / "outputs" / "sandbox"
    priv = json.loads((sb / "private_ipo_watchlist.json").read_text())["items"]
    assert any(i["candidate"] == "SpaceX" for i in priv)
    radar = json.loads((sb / "opportunity_radar.json").read_text())["opportunities"]
    spacex = [o for o in radar if o["candidate"] == "SpaceX"]
    assert spacex and spacex[0]["final_status"] in ("PRIVATE_WATCH_ONLY", "ACCESS_LIMITED")


# ---------------------------------------------------------------------------
# 3. Strategy chain: profiles → comparison ranked → review queue gated
# ---------------------------------------------------------------------------


def test_strategy_chain_ranked_and_gated(repo):
    run_all(repo, _now())
    comp = json.loads((repo / "outputs" / "sandbox" / "strategy_comparison.json").read_text())
    ranks = [m["final_strategy_rank"] for m in comp["comparison"]]
    assert ranks == sorted(ranks, reverse=True) and len(ranks) == 8
    q = json.loads((repo / "outputs" / "latest" / "strategy_review_queue.json").read_text())
    for item in q["queue"]:
        assert "modify_real_holdings" in item["blocked_actions"]


# ---------------------------------------------------------------------------
# 4. Approval workflow round-trip: decide → suppress → cooldown elapse
# ---------------------------------------------------------------------------


def test_approval_workflow_roundtrip(repo):
    from portfolio_automation.approval_queue import record_decision, build_action_queues
    run_all(repo, _now())
    q0 = json.loads((repo / "outputs" / "latest" / "system_improvement_action_queue.json").read_text())
    assert q0["queue"], "expected open improvement items"
    target = q0["queue"][0]["id"]

    # reject it → rebuild → suppressed
    record_decision(repo, "system_improvement", target, "reject", now=_now())
    build_action_queues(repo, _now())
    q1 = json.loads((repo / "outputs" / "latest" / "system_improvement_action_queue.json").read_text())
    assert target not in {i["id"] for i in q1["queue"]}

    # decision logged append-only + mirrored to user_action_log
    dec = (repo / "outputs" / "policy" / "system_improvement_decisions.jsonl").read_text()
    assert target in dec
    assert (repo / "outputs" / "policy" / "user_action_log.jsonl").exists()


def test_cooldown_elapses_resurfaces(tmp_path):
    """A reject whose cooldown has fully elapsed lets the item resurface."""
    from portfolio_automation.approval_queue import record_decision, build_action_queues
    repo = _build_repo(tmp_path)
    run_all(repo, _now())
    q0 = json.loads((repo / "outputs" / "latest" / "system_improvement_action_queue.json").read_text())
    assert q0["queue"]
    target = q0["queue"][0]["id"]
    # only an OLD reject exists (20d ago, cooldown 14d → elapsed) → resurfaces
    record_decision(repo, "system_improvement", target, "reject", now=_now() - timedelta(days=20))
    build_action_queues(repo, _now())
    q1 = json.loads((repo / "outputs" / "latest" / "system_improvement_action_queue.json").read_text())
    assert target in {i["id"] for i in q1["queue"]}


def test_opportunity_approval_executes_nothing(repo):
    from portfolio_automation.approval_queue import record_decision
    run_all(repo, _now())
    q = json.loads((repo / "outputs" / "sandbox" / "opportunity_approval_queue.json").read_text())
    if q["queue"]:
        item = q["queue"][0]["id"]
        rec = record_decision(repo, "opportunity", item, "approve_to_watchlist_review", now=_now())
        assert rec["executes_nothing"] is True
        # approving did NOT create a decision plan or any official recommendation
        assert not (repo / "outputs" / "latest" / "decision_plan.json").exists()


# ---------------------------------------------------------------------------
# 5. System-improvement idea → Claude Code prompt (safety block)
# ---------------------------------------------------------------------------


def test_idea_to_prompt_workflow(repo):
    from portfolio_automation.claude_code_prompts import generate_system_improvement_prompt
    run_all(repo, _now())
    ideas = json.loads((repo / "outputs" / "latest" / "system_improvement_ideas.json").read_text())["ideas"]
    assert ideas
    prompt = generate_system_improvement_prompt(ideas[0])
    assert "No auto-trading" in prompt and "Forbidden" in prompt
    assert "Final report" in prompt
    # the prompt itself must not direct any market action
    assert not re.search(r"\b(buy|sell) \d", prompt.lower())


# ---------------------------------------------------------------------------
# 6. Broker-aware workflow: fresh broker → broker source; stale → config
# ---------------------------------------------------------------------------


def test_broker_aware_uses_fresh_broker(repo):
    run_all(repo, _now())
    b = json.loads((repo / "outputs" / "portfolio" / "broker_aware_portfolio.json").read_text())
    assert b["holdings_source"] == "broker"
    assert b["feeds_decision_plan"] is False
    assert b["leverage"]["leveraged_exposure"] > 0  # QLD


def test_broker_aware_degrades_when_stale(tmp_path):
    repo = _build_repo(tmp_path, broker_fresh=False)
    run_all(repo, _now())
    b = json.loads((repo / "outputs" / "portfolio" / "broker_aware_portfolio.json").read_text())
    assert b["holdings_source"] == "config"
    assert b["degraded_mode"] is True


# ---------------------------------------------------------------------------
# 7. Dashboard render workflow: full run → Strategy Lab shows every section
# ---------------------------------------------------------------------------


def test_dashboard_renders_full_run(repo, monkeypatch):
    from gui_v2 import app as appmod
    from fastapi.testclient import TestClient
    run_all(repo, _now())
    monkeypatch.setattr(appmod, "REPO_ROOT", repo)
    r = TestClient(appmod.app).get("/dashboard/strategy-lab")
    assert r.status_code == 200
    html = r.text
    assert "Observe-only" in html
    for heading in ("Strategy Comparison", "Opportunity Radar",
                    "System Improvement Backlog", "Approval Queues"):
        assert heading in html, f"missing dashboard section: {heading}"
    for bad in ("place order", "buy now", "sell now", "execute trade"):
        assert bad not in html.lower()


# ---------------------------------------------------------------------------
# 8. Event store workflow: events accumulate + read back
# ---------------------------------------------------------------------------


def test_event_store_accumulates_user_actions(repo):
    from portfolio_automation.approval_queue import record_decision
    from portfolio_automation import event_store
    from portfolio_automation.next_stage.contracts import EventStream
    run_all(repo, _now())
    q = json.loads((repo / "outputs" / "latest" / "system_improvement_action_queue.json").read_text())
    ids = [i["id"] for i in q["queue"]][:2]
    for i in ids:
        record_decision(repo, "system_improvement", i, "defer", now=_now())
    evs = event_store.read_events(repo, EventStream.USER_ACTION)
    assert len(evs) >= len(ids)
    assert all(e["observe_only"] is True for e in evs)


# ---------------------------------------------------------------------------
# 9. Idempotency: re-running the full lane is safe
# ---------------------------------------------------------------------------


def test_full_run_is_rerunnable(repo):
    run_all(repo, _now())
    res2 = run_all(repo, _now())  # second run must not crash and must stay observe-only
    assert all(v["ok"] for k, v in res2.items() if isinstance(v, dict) and "ok" in v)
    assert not (repo / "outputs" / "latest" / "decision_plan.json").exists()
