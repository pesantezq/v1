"""Tests for the Research Strategy Lab leaderboard GUI section."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from gui_v2.data.dash_next_stage import collect_strategy_lab_view

_FORBIDDEN = ("buy now", "sell now", "place order", "rebalance now")


def test_loader_empty_when_absent(tmp_path):
    v = collect_strategy_lab_view(tmp_path)
    assert v["strategy_lab"]["available"] is False


def test_loader_reads_leaderboard(tmp_path):
    sb = tmp_path / "outputs" / "sandbox"
    sb.mkdir(parents=True)
    (sb / "strategy_leaderboard.json").write_text(json.dumps({
        "status": "ok", "objective": "maximize_excess_vs_sp500", "tactic_count": 2,
        "created_at": "2026-06-12T00:00:00Z",
        "leaderboard": [
            {"name": "Mean-Variance", "tactic_id": "research_mean_variance", "strategy_score": 2.4,
             "mean_excess_vs_spy": 0.12, "prob_beat_spy": 1.0, "worst_max_drawdown": -0.2,
             "academic_basis": "Markowitz 1952", "still_works_oos": True, "approximate": False},
            {"name": "Momentum", "tactic_id": "research_momentum_rotation", "strategy_score": 0.3,
             "mean_excess_vs_spy": 0.04, "prob_beat_spy": 0.6, "worst_max_drawdown": -0.3,
             "academic_basis": "Jegadeesh & Titman 1993", "still_works_oos": False, "approximate": False},
        ]}))
    v = collect_strategy_lab_view(tmp_path)
    assert v["strategy_lab"]["available"] is True
    assert v["strategy_lab"]["rows"][0]["name"] == "Mean-Variance"
    assert v["strategy_lab"]["rows"][1]["still_works_oos"] is False


def test_route_renders_no_trade_verbs():
    from gui_v2.app import app

    client = TestClient(app)
    r = client.get("/dashboard/strategy-lab")
    assert r.status_code == 200
    text = r.text.lower()
    for verb in _FORBIDDEN:
        assert verb not in text
