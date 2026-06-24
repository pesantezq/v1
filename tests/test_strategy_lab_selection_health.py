"""strategy_lab_health surfaces the active-strategy selection + staleness.

Content-liveness: an approved active_strategy_id that no longer appears in the
current review queue is a stale selection → AMBER reason. A selection that is
still in the queue (or no selection at all) is clean.
"""
from __future__ import annotations

import json
from pathlib import Path

from portfolio_automation.portfolio_sim.strategy_lab_health import (
    check_active_strategy_selection,
)


def _seed(root: Path, active=None, queue_ids=()):
    pol = root / "outputs" / "policy"
    lat = root / "outputs" / "latest"
    pol.mkdir(parents=True, exist_ok=True)
    lat.mkdir(parents=True, exist_ok=True)
    if active is not None:
        (pol / "active_strategy_selection.json").write_text(
            json.dumps({"active_strategy_id": active}), encoding="utf-8")
    (lat / "strategy_review_queue.json").write_text(
        json.dumps({"queue": [{"strategy_id": i} for i in queue_ids]}), encoding="utf-8")


def test_no_selection_is_clean(tmp_path):
    _seed(tmp_path, active=None, queue_ids=["a", "b"])
    reasons, signals = check_active_strategy_selection(tmp_path)
    assert reasons == []
    assert signals["active_strategy_id"] is None


def test_active_in_queue_is_clean(tmp_path):
    _seed(tmp_path, active="a", queue_ids=["a", "b"])
    reasons, signals = check_active_strategy_selection(tmp_path)
    assert reasons == []
    assert signals["active_strategy_id"] == "a"


def test_active_not_in_queue_flags_stale(tmp_path):
    _seed(tmp_path, active="zzz", queue_ids=["a", "b"])
    reasons, signals = check_active_strategy_selection(tmp_path)
    assert any("stale_active_strategy_selection" in r for r in reasons)
    assert signals["active_strategy_id"] == "zzz"
