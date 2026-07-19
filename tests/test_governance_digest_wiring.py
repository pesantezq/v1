"""The evening approval-digest email is the one-tap-from-email surface for the
human-gated production approval flow. It was shipped + tested (governance_digest)
but left UNWIRED — no runner called it. This asserts the daily governance lane
(Step 9) now invokes it, gated by config, and that the step is non-blocking.

Safety note: the digest only EMAILS a link to /dashboard/governance; approving
there runs through the human-gated promotion_approvals path. The digest itself
approves nothing — these tests assert wiring + gating, not any approval mutation.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import portfolio_automation.sim_governance.daily_governance_run as dgr


_BASE_CFG = {
    "enabled": True,
    "simulation_lane": {"enabled": False},
    "ai_review": {"enabled": False},
    "approval_packet": {"enabled": False},
    "production_application": {"apply_watchlist_overlay": False, "apply_advisory_overlay": False},
}


def test_digest_stage_disabled_when_config_off(tmp_path):
    cfg = {**_BASE_CFG, "auto_approval": {"evening_digest": {"enabled": False}}}
    status = dgr.run_daily_governance(tmp_path, "2026-07-19T09:00:00+00:00",
                                      config=cfg, write_files=False)
    assert status["stages"]["governance_digest"] == {"ok": True, "status": "disabled"}


def test_digest_invoked_when_config_on(tmp_path, monkeypatch):
    calls = {}

    def _spy(root, *, now, write_files):
        calls["root"] = str(root)
        calls["now"] = now
        return {"status": "sent"}

    monkeypatch.setattr(dgr.governance_digest, "run_evening_digest", _spy)
    cfg = {**_BASE_CFG, "auto_approval": {"evening_digest": {"enabled": True}}}
    status = dgr.run_daily_governance(tmp_path, "2026-07-19T09:00:00+00:00",
                                      config=cfg, write_files=False)
    assert calls, "run_evening_digest was not invoked by the lane"
    assert status["stages"]["governance_digest"]["status"] == "sent"
    assert status["stages"]["governance_digest"]["ok"] is True


def test_digest_stage_is_non_blocking_on_error(tmp_path, monkeypatch):
    def _boom(root, *, now, write_files):
        raise RuntimeError("smtp exploded")

    monkeypatch.setattr(dgr.governance_digest, "run_evening_digest", _boom)
    cfg = {**_BASE_CFG, "auto_approval": {"evening_digest": {"enabled": True}}}
    status = dgr.run_daily_governance(tmp_path, "2026-07-19T09:00:00+00:00",
                                      config=cfg, write_files=False)
    # lane must not raise; it records the failure and continues to roll-up counts
    assert status["stages"]["governance_digest"]["ok"] is False
    assert "smtp exploded" in status["stages"]["governance_digest"]["error"]
    assert "production_overlay_live" in status
