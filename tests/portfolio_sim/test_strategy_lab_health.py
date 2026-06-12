"""Tests for the strategy-lab health assessor."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from portfolio_automation.portfolio_sim.strategy_lab_health import assess_strategy_lab_health

NOW = datetime(2026, 6, 12, tzinfo=timezone.utc)


def _write(root, name, payload):
    d = root / "outputs" / "sandbox"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(payload))


def test_absent_is_amber(tmp_path):
    r = assess_strategy_lab_health(tmp_path, now=NOW)
    assert r["status"] == "AMBER"
    assert "leaderboard_absent" in r["reasons"][0]


def test_disabled_is_amber(tmp_path):
    _write(tmp_path, "strategy_leaderboard.json", {"status": "disabled", "leaderboard": []})
    r = assess_strategy_lab_health(tmp_path, now=NOW)
    assert r["status"] == "AMBER"
    assert any("disabled" in x for x in r["reasons"])


def test_fresh_but_empty_is_red(tmp_path):
    _write(tmp_path, "strategy_leaderboard.json",
           {"status": "ok", "leaderboard": [], "created_at": "2026-06-12T00:00:00Z"})
    r = assess_strategy_lab_health(tmp_path, now=NOW)
    assert r["status"] == "RED"


def test_healthy_green(tmp_path):
    _write(tmp_path, "strategy_leaderboard.json", {
        "status": "ok", "created_at": "2026-06-12T00:00:00Z",
        "leaderboard": [{"tactic_id": "research_momentum_rotation", "name": "Momentum",
                         "strategy_score": 1.2, "mean_excess_vs_spy": 0.05, "still_works_oos": True}]})
    _write(tmp_path, "research_strategy_catalog.json", {"coverage_complete": True})
    _write(tmp_path, "walk_forward_results.json", {"results": {}})
    _write(tmp_path, "factor_exposure_report.json", {"factor_data_available": True})
    r = assess_strategy_lab_health(tmp_path, now=NOW)
    assert r["status"] == "GREEN"
    assert r["signals"]["top_tactic"] == "Momentum"


def test_failing_oos_is_amber(tmp_path):
    _write(tmp_path, "strategy_leaderboard.json", {
        "status": "ok", "created_at": "2026-06-12T00:00:00Z",
        "leaderboard": [{"tactic_id": "research_momentum_rotation", "name": "M",
                         "strategy_score": 0.1, "still_works_oos": False}]})
    _write(tmp_path, "research_strategy_catalog.json", {"coverage_complete": True})
    r = assess_strategy_lab_health(tmp_path, now=NOW)
    assert r["status"] == "AMBER"
    assert any("still_works_oos=false" in x for x in r["reasons"])
