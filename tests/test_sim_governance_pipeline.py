"""
Integration tests for the sim-governance orchestrator + GUI loader.

Verifies the daily orchestrator runs all stages non-blocking, the config loader
honors flags, and the GUI governance view reports lane/budget/queue state.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from portfolio_automation.sim_governance import daily_governance_run as RUN

NOW = "2026-06-16T00:00:00+00:00"


def _write_config(root: Path, enabled: bool = True) -> None:
    (root / "config.json").write_text(json.dumps({
        "portfolio": {"watchlist": ["AAPL", "MSFT"]},
        "sim_governance": {
            "enabled": enabled,
            "simulation_lane": {"enabled": True},
            "ai_review": {"enabled": True, "daily_cost_cap_usd": 0.50,
                          "provider": "openai", "model": "gpt-4o-mini"},
            "production_application": {"apply_watchlist_overlay": False,
                                       "apply_advisory_overlay": False},
        },
    }), encoding="utf-8")


def test_config_loader_defaults(tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    cfg = RUN.load_sim_governance_config(tmp_path)
    assert cfg["enabled"] is True
    assert cfg["ai_review"]["daily_cost_cap_usd"] == 0.50
    assert cfg["production_application"]["apply_watchlist_overlay"] is False


def test_orchestrator_runs_all_stages(tmp_path):
    _write_config(tmp_path)
    status = RUN.run_daily_governance(tmp_path, NOW)
    assert status["enabled"] is True
    assert status["simulation_lane_active"] is True
    for stage in ("simulation_lane", "bundle", "packet", "ai_review",
                  "proposals", "production_application"):
        assert status["stages"][stage]["ok"] is True, stage
    # status artifact written to the PROMOTION_REVIEW namespace
    assert (tmp_path / "outputs" / "promotion_review" / "daily_governance_status.json").exists()
    # AI review stayed within the cap
    assert status["stages"]["ai_review"]["estimated_cost_usd"] <= 0.50


def test_orchestrator_respects_disabled_flag(tmp_path):
    _write_config(tmp_path, enabled=False)
    status = RUN.run_daily_governance(tmp_path, NOW)
    assert status["enabled"] is False
    assert "simulation_lane" not in status["stages"]


def test_orchestrator_is_non_blocking_on_bad_root(tmp_path):
    # No config.json, empty dir → must not raise, degrades gracefully.
    status = RUN.run_daily_governance(tmp_path, NOW)
    assert isinstance(status, dict)
    assert "stages" in status


def test_gui_governance_loader(tmp_path):
    _write_config(tmp_path)
    RUN.run_daily_governance(tmp_path, NOW)
    from gui_v2.data.dash_governance import collect_governance_view
    view = collect_governance_view(tmp_path)
    assert view["persona"] == "governance"
    assert view["simulation_lane_active"] is True
    assert view["ai_daily_cap_usd"] == 0.50
    assert "ai_budget_remaining_usd" in view
    assert len(view["cards"]) == 4
    # labels surfaced for the GUI lifecycle states
    assert view["labels"]["sim_active"] == "Simulation Active"
