"""The portfolio-sim artifacts must be registered with a valid schema."""
from __future__ import annotations

import yaml

from portfolio_automation.artifact_registry import run_artifact_registry
import pytest

EXPECTED = [
    "portfolio_backtest.json", "portfolio_backtest_summary.md",
    "strategy_catalog.json", "portfolio_projection.json", "crowd_tactic_backtest.json",
]


def test_rows_present_and_weekly():
    reg = yaml.safe_load(open("portfolio_automation/artifact_registry.yaml"))
    arts = reg["artifacts"]
    for key in EXPECTED:
        assert key in arts, f"{key} not registered"
        assert arts[key]["cadence"] == "weekly"
        assert arts[key]["producer"] == "portfolio_sim"


# serial: writes the checkout's outputs/latest/artifact_registry_status.json, which
# test_scanner_dataset_sufficiency / test_system_improvement read (attributed 2026-09-27).
@pytest.mark.serial
def test_registry_schema_valid():
    r = run_artifact_registry(root=".")
    assert (r.get("counts") or {}).get("schema_invalid", 0) == 0
