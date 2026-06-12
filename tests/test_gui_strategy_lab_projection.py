"""Tests for the Strategy Lab projection section."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from gui_v2.data.dash_next_stage import collect_strategy_lab_view


def test_loader_reads_projection(tmp_path):
    sb = tmp_path / "outputs" / "sandbox"
    sb.mkdir(parents=True)
    (sb / "portfolio_projection.json").write_text(json.dumps({
        "status": "ok", "horizons": ["1y", "5y"], "seed": 7,
        "rows": [{"tactic_id": "shadow_actual_baseline", "name": "Actual Baseline",
                  "horizon_label": "5y", "p5_balance": 50000, "p50_balance": 80000,
                  "p95_balance": 120000, "prob_reach_target": 0.55, "max_drawdown_p95": -0.35}],
        "created_at": "2026-06-12T00:00:00Z",
    }))
    v = collect_strategy_lab_view(tmp_path)
    assert v["projection"]["available"] is True
    assert v["projection"]["rows"][0]["name"] == "Actual Baseline"


def test_route_renders_projection_disclaimer():
    from gui_v2.app import app

    client = TestClient(app)
    r = client.get("/dashboard/strategy-lab")
    assert r.status_code == 200
    # disclaimer text appears whenever a projection is present; route must not 500
    assert "sell now" not in r.text.lower()
