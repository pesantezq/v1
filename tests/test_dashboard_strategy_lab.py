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
