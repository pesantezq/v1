"""Phase 2 + 12-13 — Strategy Lab dashboard view (tolerant loader + route render)."""
from __future__ import annotations

import json
from pathlib import Path

from gui_v2.data.dash_next_stage import collect_strategy_lab_view


def test_loader_tolerates_all_absent(tmp_path):
    v = collect_strategy_lab_view(tmp_path)
    assert v["observe_only"] is True
    assert isinstance(v["cards"], list) and v["cards"]  # cards always present
    # absent artifacts → "not yet produced" labels, no crash
    radar_card = next(c for c in v["cards"] if c["title"] == "Opportunity Radar")
    assert "not yet produced" in radar_card["label"]


def test_loader_surfaces_artifacts_when_present(tmp_path):
    sb = tmp_path / "outputs" / "sandbox"; sb.mkdir(parents=True)
    latest = tmp_path / "outputs" / "latest"; latest.mkdir(parents=True)
    sb.joinpath("opportunity_radar.json").write_text(json.dumps({"opportunities": [
        {"candidate": "AMD", "final_status": "QUALIFIED", "opportunity_score": 0.6,
         "boom_score": 0.5, "risk_score": 0.3, "theme": "AI"}]}))
    sb.joinpath("strategy_comparison.json").write_text(json.dumps({"comparison": [
        {"name": "Long-Term Compounding", "final_strategy_rank": 0.77,
         "expected_objective_fit": 0.7, "expected_risk_level": 0.45,
         "max_drawdown_estimate": 0.22, "tax_efficiency": 1.0,
         "opportunity_capture_score": 0.1, "after_tax_degraded": True}]}))
    latest.joinpath("system_improvement_ideas.json").write_text(json.dumps({"ideas": [
        {"id": "si-1", "title": "Add probe", "category": "observability",
         "priority": "high", "final_rank_score": 0.7, "summary": "x"}]}))
    v = collect_strategy_lab_view(tmp_path)
    assert v["strategies"][0]["name"] == "Long-Term Compounding"
    assert v["radar"][0]["candidate"] == "AMD"
    assert v["improvement_ideas"][0]["id"] == "si-1"


def test_route_renders_200_and_observe_only(tmp_path, monkeypatch):
    from gui_v2 import app as appmod
    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    monkeypatch.setattr(appmod, "REPO_ROOT", tmp_path)
    from fastapi.testclient import TestClient
    r = TestClient(appmod.app).get("/dashboard/strategy-lab")
    assert r.status_code == 200
    assert "Observe-only" in r.text
    assert "Strategy Lab" in r.text
    # never an execution control
    for bad in ("place order", "buy now", "sell now", "execute trade"):
        assert bad not in r.text.lower()


def test_route_responsive_table_markers(tmp_path, monkeypatch):
    from gui_v2 import app as appmod
    sb = tmp_path / "outputs" / "sandbox"; sb.mkdir(parents=True)
    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    sb.joinpath("strategy_comparison.json").write_text(json.dumps({"comparison": [
        {"name": "X", "final_strategy_rank": 0.5, "expected_objective_fit": 0.5,
         "expected_risk_level": 0.5, "max_drawdown_estimate": 0.2, "tax_efficiency": 0.5,
         "opportunity_capture_score": 0.0, "after_tax_degraded": False}]}))
    monkeypatch.setattr(appmod, "REPO_ROOT", tmp_path)
    from fastapi.testclient import TestClient
    html = TestClient(appmod.app).get("/dashboard/strategy-lab").text
    assert "hidden md:block" in html and "md:hidden" in html  # responsive pair


# ── Simulation Graphs section (added 2026-06-16) ────────────────────────────

_SIM_FORBIDDEN = (
    "execute trade", "buy now", "sell now", "place order", "auto-trade",
    "auto trade", "auto-approve", "rebalance now", "promotion approved",
    "official recommendation",
)


def _seed_sim_sandbox(root):
    sb = root / "outputs" / "sandbox"; sb.mkdir(parents=True, exist_ok=True)
    # Complete rows — the EXISTING Strategy Comparison table also reads this file
    # and is not defensive about absent fields (expected_objective_fit, etc.).
    def _row(sid, name, ret, vol, mdd, rank):
        return {"strategy_id": sid, "name": name, "after_tax_return_estimate": ret,
                "expected_volatility": vol, "max_drawdown_estimate": mdd, "final_strategy_rank": rank,
                "expected_objective_fit": 0.7, "expected_risk_level": vol, "tax_efficiency": 0.9,
                "opportunity_capture_score": 0.1, "after_tax_degraded": False}
    sb.joinpath("strategy_comparison.json").write_text(json.dumps({"comparison": [
        _row("a", "Alpha", 0.12, 0.30, 0.18, 0.8),
        _row("b", "Bravo", 0.06, 0.12, 0.09, 0.7)]}))
    sb.joinpath("portfolio_projection.json").write_text(json.dumps({
        "status": "ok", "horizons": ["1y"], "anchor_fan": {"1y": [
            {"month": 0, "p5": 1.0, "p50": 1.0, "p95": 1.0},
            {"month": 12, "p5": 0.9, "p50": 1.14, "p95": 1.5}]}}))


def _render(tmp_path, monkeypatch):
    from gui_v2 import app as appmod
    (tmp_path / "outputs" / "latest").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(appmod, "REPO_ROOT", tmp_path)
    from fastapi.testclient import TestClient
    return TestClient(appmod.app).get("/dashboard/strategy-lab")


def test_simulation_graphs_section_renders_with_data(tmp_path, monkeypatch):
    _seed_sim_sandbox(tmp_path)
    r = _render(tmp_path, monkeypatch)
    assert r.status_code == 200
    t = r.text
    assert "Simulation Graphs" in t
    # all six human-readable chart titles present
    for title in ("Growth Over Time", "How Deep the Losses Got", "Risk vs Return",
                  "Was Performance Consistent?", "How Contributions Change the Outcome",
                  "How the Portfolio Shifted Over Time"):
        assert title in t, title
    assert "<svg" in t  # at least one chart drew
    assert "Source &amp; safety" in t


def test_simulation_graphs_says_sandbox_only(tmp_path, monkeypatch):
    _seed_sim_sandbox(tmp_path)
    t = _render(tmp_path, monkeypatch).text
    assert "Simulation-only" in t
    assert "sandbox only" in t.lower()
    assert "decision_plan.json" in t  # official advisory source named


def test_simulation_graphs_no_trade_language(tmp_path, monkeypatch):
    _seed_sim_sandbox(tmp_path)
    low = _render(tmp_path, monkeypatch).text.lower()
    for bad in _SIM_FORBIDDEN:
        assert bad not in low, f"forbidden phrase '{bad}' in strategy-lab page"


def test_simulation_graphs_missing_artifact_does_not_crash(tmp_path, monkeypatch):
    # no sandbox + no latest artifact → empty state, page still 200
    r = _render(tmp_path, monkeypatch)
    assert r.status_code == 200
    assert "Simulation Graphs" in r.text
    assert "not available yet" in r.text.lower()


def test_simulation_graphs_malformed_artifact_does_not_crash(tmp_path, monkeypatch):
    latest = tmp_path / "outputs" / "latest"; latest.mkdir(parents=True, exist_ok=True)
    (latest / "simulation_charts.json").write_text("{bad json", encoding="utf-8")
    r = _render(tmp_path, monkeypatch)
    assert r.status_code == 200
    assert "Simulation Graphs" in r.text  # degrades, no 500


def test_simulation_graphs_chart_grid_is_responsive(tmp_path, monkeypatch):
    _seed_sim_sandbox(tmp_path)
    t = _render(tmp_path, monkeypatch).text
    # chart grid stacks on mobile (grid-cols-1) and goes two-up on desktop (lg:grid-cols-2)
    assert "grid-cols-1 lg:grid-cols-2" in t


def test_strategy_lab_view_includes_simulation_charts_key(tmp_path):
    v = collect_strategy_lab_view(tmp_path)
    assert "simulation_charts" in v
    assert v["simulation_charts"]["safety"]["can_execute_trades"] is False
