# tests/test_approval_packet_pipeline.py
from pathlib import Path

from portfolio_automation.sim_governance import daily_governance_run as dgr


def test_step8_disabled_by_default(tmp_path, monkeypatch):
    # Minimal config: sim_governance enabled but approval_packet OFF.
    cfg = {"enabled": True, "simulation_lane": {"enabled": True},
           "ai_review": {"enabled": False}, "approval_packet": {"enabled": False}}
    status = dgr.run_daily_governance(tmp_path, "2026-07-15T00:00:00+00:00",
                                      config=cfg, write_files=False)
    assert status["stages"]["approval_packet"]["status"] == "disabled"


def test_step8_builds_when_enabled(tmp_path, monkeypatch):
    called = {}

    def _fake_build(base_dir, now, *, deep_link_base="", veto_window_hours=48):
        called["deep_link_base"] = deep_link_base
        called["veto_window_hours"] = veto_window_hours
        return {"counts": {"tier_sim_within_veto": 2, "tier_production_pending": 3}}

    monkeypatch.setattr(dgr.approval_packet, "build_operator_packet", _fake_build)
    monkeypatch.setattr(dgr.approval_packet, "write_operator_packet",
                        lambda packet, *, base_dir: packet)
    cfg = {"enabled": True, "simulation_lane": {"enabled": True},
           "ai_review": {"enabled": False},
           "auto_approval": {"veto_window_hours": 24},
           "approval_packet": {"enabled": True, "deep_link_base": "https://x"}}
    status = dgr.run_daily_governance(tmp_path, "2026-07-15T00:00:00+00:00",
                                      config=cfg, write_files=False)
    assert status["stages"]["approval_packet"]["ok"] is True
    assert status["stages"]["approval_packet"]["counts"]["tier_production_pending"] == 3
    assert called == {"deep_link_base": "https://x", "veto_window_hours": 24}


def test_step8_never_sinks_run_on_error(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(dgr.approval_packet, "build_operator_packet", _boom)
    cfg = {"enabled": True, "simulation_lane": {"enabled": True},
           "ai_review": {"enabled": False},
           "approval_packet": {"enabled": True}}
    status = dgr.run_daily_governance(tmp_path, "2026-07-15T00:00:00+00:00",
                                      config=cfg, write_files=False)
    assert status["stages"]["approval_packet"]["ok"] is False
    assert "kaboom" in status["stages"]["approval_packet"]["error"]
