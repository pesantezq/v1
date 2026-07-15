"""
The daily-pipeline stage helper: inert when disabled, structured status, sim-only writes.
"""
from __future__ import annotations

import pytest

from portfolio_automation.sim_governance import auto_approval as AA
from portfolio_automation.sim_governance import schemas as S

NOW = "2026-07-14T12:00:00Z"
APPROVE = '{"decision":"approve","within_bounds":true,"reason":"clean"}'


def _review():
    return {"generated_at": NOW, "verdicts": [
        {"candidate_id": "c1", "decision": S.DECISION_READY, "workflow": S.WORKFLOW_WATCHLIST}]}


def _cbi():
    return {"c1": {"candidate_id": "c1", "symbol": "NVDA", "confidence": 0.9,
                   "proposal_type": S.PROPOSAL_WATCHLIST_ADD}}


def _sim_cfg(**over):
    aa = {"enabled": False, "watchlist_enabled": False, "strategy_enabled": False,
          "min_confidence": 0.85, "watchlist_daily_cap": 2, "strategy_daily_cap": 0,
          "max_active_awaiting_veto": 5, "sim_watchlist_db_path": "data/sim_wl.db",
          "sim_max_symbols": 5}
    aa.update(over)
    return {"auto_approval": aa}


def test_stage_inert_when_disabled(tmp_path):
    res = AA.run_stage(root=str(tmp_path), now=NOW, sim_gov_config=_sim_cfg(enabled=False),
                       review_result=_review(), candidates_by_id=_cbi(),
                       base_dir=str(tmp_path / "outputs"), env={}, approver=lambda p: APPROVE)
    assert res["ok"] is True
    assert res["enabled"] is False
    assert res["disabled_reason"] == "global_disabled"
    assert res["applied_count"] == 0
    # No simulation DB created, no ledger written.
    assert not (tmp_path / "data" / "sim_wl.db").exists()
    assert AA.load_events(base_dir=str(tmp_path / "outputs")) == []


def test_stage_applies_when_enabled(tmp_path):
    res = AA.run_stage(
        root=str(tmp_path), now=NOW,
        sim_gov_config=_sim_cfg(enabled=True, watchlist_enabled=True),
        review_result=_review(), candidates_by_id=_cbi(),
        base_dir=str(tmp_path / "outputs"), env={}, approver=lambda p: APPROVE)
    assert res["ok"] is True
    assert res["applied_count"] == 1
    assert AA.EVENT_APPLIED in [e["kind"] for e in AA.load_events(base_dir=str(tmp_path / "outputs"))]


def test_stage_never_raises_on_bad_input(tmp_path):
    res = AA.run_stage(root=str(tmp_path), now=NOW, sim_gov_config={"auto_approval": None},
                       review_result=None, candidates_by_id=None,
                       base_dir=str(tmp_path / "outputs"), env={})
    assert res["ok"] in (True, False)  # must return a dict, not raise
