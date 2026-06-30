"""Phase 12 — cadence integration: assert the SQG program stages are actually
wired into the daily/weekly safe-run scripts (catches a producer that exists in
code but never runs)."""
from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_DAILY = (_ROOT / "scripts" / "run_daily_safe.sh").read_text(encoding="utf-8")
_WEEKLY = (_ROOT / "scripts" / "run_weekly_safe.sh").read_text(encoding="utf-8")


@pytest.mark.parametrize("needle", [
    "run_manifest import begin_run",
    "run_manifest import complete_run",
    "daily_input_snapshot import run_daily_input_snapshot",
    "decision_context_capture import run_decision_context_capture",
    "quant_feedback import run_quant_feedback",
    "semantic_liveness import run_semantic_liveness",
    "scenario_risk import build_scenario_risk",
])
def test_daily_stage_wired(needle):
    assert needle in _DAILY, f"daily pipeline missing stage: {needle}"


@pytest.mark.parametrize("needle", [
    "strategy_mandate import build_strategy_mandates",
    "experiment_registry import read_registry",
])
def test_weekly_stage_wired(needle):
    assert needle in _WEEKLY, f"weekly pipeline missing stage: {needle}"


def test_manifest_begin_runs_before_complete():
    assert _DAILY.index("begin_run") < _DAILY.index("complete_run"), \
        "manifest begin must precede complete in the daily wrapper"


def test_snapshot_runs_before_decision_context_capture():
    # the context capture binds to the frozen snapshot, so the snapshot stage
    # must run first.
    assert _DAILY.index("run_daily_input_snapshot") < _DAILY.index("run_decision_context_capture")
