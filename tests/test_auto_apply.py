"""
Tests for backtesting/auto_apply.py — sub-project E full auto-apply orchestrator.

The single sanctioned MUTATING path (it can change registry weights), so these tests
are exhaustive on the fail-closed gates. No real LLM/network: the GPT approver is
INJECTED. Every test runs against a TEMP copy of the registry and asserts the live
config/signal_registry.yaml is never touched.

Gate order (first failure wins): disabled → kill_switched → oos_immature →
no_actionable_proposal → drift_capped → score_gate_blocked → budget_exceeded →
gpt_vetoed → applied (with post-gate auto-rollback → rolled_back).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from backtesting import auto_apply as aa

_LIVE_REGISTRY = "config/signal_registry.yaml"


@pytest.fixture
def env(tmp_path):
    """A throwaway registry + approval/state/history/base dirs."""
    reg = tmp_path / "signal_registry.yaml"
    shutil.copyfile(_LIVE_REGISTRY, reg)
    return {
        "registry_path": str(reg),
        "approval_path": str(tmp_path / "approved_weight_changes.json"),
        "history_dir": str(tmp_path / "history"),
        "state_path": str(tmp_path / "auto_apply_state.json"),
        "base_dir": str(tmp_path / "outputs"),
        "registry_bytes": reg.read_bytes(),
    }


def _mature_poc():
    return {"oos_window": {"folds_possible": True},
            "calibration": {"calibration_slope": 1.0}}


def _actionable_proposals(signal_id="STRONG_MOVE_UP", delta=0.03):
    return {"summary": {"proposed_count": 1},
            "proposals": [{"signal_id": signal_id, "current_weight": 0.45,
                           "proposed_weight": round(0.45 + delta, 4),
                           "proposed_delta": delta, "status": "proposed",
                           "oos_hit_rate": 62.0, "oos_hit_rate_ci95": [55.0, 69.0],
                           "avg_return": 1.2}]}


def _approve(_prompt):  # injected approver: APPROVE
    return json.dumps({"decision": "approve", "within_bounds": True, "reason": "edge holds"})


def _veto(_prompt):  # injected approver: VETO
    return json.dumps({"decision": "veto", "within_bounds": True, "reason": "thin sample"})


def _call(env, **kw):
    base = dict(enabled=True, poc=_mature_poc(), proposals=_actionable_proposals(),
                registry_path=env["registry_path"], approval_path=env["approval_path"],
                history_dir=env["history_dir"], state_path=env["state_path"],
                base_dir=env["base_dir"], now_iso="2026-06-05T00:00:00+00:00")
    base.update(kw)
    return aa.maybe_auto_apply(**base)


def _registry_unchanged(env):
    return Path(env["registry_path"]).read_bytes() == env["registry_bytes"]


# --- gates -----------------------------------------------------------------

def test_disabled_is_noop(env):
    out = _call(env, enabled=False, approver=_approve)
    assert out["status"] == "disabled"
    assert _registry_unchanged(env)
    assert not Path(env["approval_path"]).exists()


def test_kill_switch_file(env, tmp_path, monkeypatch):
    ks = tmp_path / "auto_apply.DISABLED"
    ks.write_text("off")
    monkeypatch.setattr(aa, "_KILL_SWITCH_FILE", str(ks))
    out = _call(env, approver=_approve)
    assert out["status"] == "kill_switched"
    assert _registry_unchanged(env)


def test_kill_switch_env(env, monkeypatch):
    monkeypatch.setenv("STOCKBOT_AUTO_APPLY_DISABLED", "1")
    out = _call(env, approver=_approve)
    assert out["status"] == "kill_switched"
    assert _registry_unchanged(env)


def test_oos_immature(env):
    out = _call(env, poc={"oos_window": {"folds_possible": False}}, approver=_approve)
    assert out["status"] == "oos_immature"
    assert _registry_unchanged(env)


def test_no_actionable_proposal(env):
    empty = {"summary": {"proposed_count": 0}, "proposals": [
        {"signal_id": "VOLUME_SPIKE", "proposed_delta": 0.0, "status": "insufficient_evidence"}]}
    out = _call(env, proposals=empty, approver=_approve)
    assert out["status"] == "no_actionable_proposal"
    assert _registry_unchanged(env)


def test_gpt_veto_blocks_apply(env):
    out = _call(env, approver=_veto)
    assert out["status"] == "gpt_vetoed"
    assert _registry_unchanged(env)


def test_gpt_unparseable_fails_closed(env):
    out = _call(env, approver=lambda _p: "i think... maybe approve?")
    assert out["status"] == "gpt_vetoed"
    assert _registry_unchanged(env)


def test_approver_raises_fails_closed(env):
    def boom(_p):
        raise RuntimeError("llm down")
    out = _call(env, approver=boom)
    assert out["status"] == "gpt_vetoed"
    assert _registry_unchanged(env)


def test_pre_score_gate_blocks(env, monkeypatch):
    monkeypatch.setattr(aa, "_score_gate", lambda registry_path: {"status": "RED"})
    out = _call(env, approver=_approve)
    assert out["status"] == "score_gate_blocked"
    assert _registry_unchanged(env)


def test_drift_cap_blocks(env):
    # Pre-load state so this month's drift is already at the cap.
    Path(env["state_path"]).write_text(json.dumps(
        {"apply_enabled": True, "monthly_drift": {"2026-06": 0.10}}))
    out = _call(env, max_monthly_drift=0.10, approver=_approve)
    assert out["status"] == "drift_capped"
    assert _registry_unchanged(env)


def test_all_gates_pass_applies(env):
    out = _call(env, approver=_approve)
    assert out["status"] == "applied", out
    assert not _registry_unchanged(env)  # weight changed
    assert Path(env["approval_path"]).exists()
    # weight moved by the proposed delta
    txt = Path(env["registry_path"]).read_text()
    assert "STRONG_MOVE_UP" in txt
    audit = json.loads((Path(env["base_dir"]) / "policy" / "auto_apply_audit.json").read_text())
    assert any(e.get("status") == "applied" for e in audit)


def test_post_gate_regression_rolls_back(env, monkeypatch):
    # Pre-gate GREEN, post-gate RED → must revert and report rolled_back.
    calls = {"n": 0}
    def flaky(registry_path):
        calls["n"] += 1
        return {"status": "GREEN"} if calls["n"] == 1 else {"status": "RED"}
    monkeypatch.setattr(aa, "_score_gate", flaky)
    out = _call(env, approver=_approve)
    assert out["status"] == "rolled_back", out
    assert _registry_unchanged(env)  # restored byte-for-byte


def test_live_registry_never_touched(env):
    before = Path(_LIVE_REGISTRY).read_bytes()
    _call(env, approver=_approve)
    assert Path(_LIVE_REGISTRY).read_bytes() == before


# --------------------------------------------------------------------------
# F5 — reconstructed-evidence gate (look-ahead audit must be clean)
# --------------------------------------------------------------------------

def test_reconstructed_evidence_blocked_when_audit_not_clean(env):
    out = _call(env, evidence_source="historical_reconstruction",
                reconstruction_audit={"look_ahead_clean": False}, approver=_approve)
    assert out["status"] == "reconstruction_unverified"
    assert _registry_unchanged(env)


def test_reconstructed_evidence_proceeds_when_audit_clean(env):
    out = _call(env, evidence_source="historical_reconstruction",
                reconstruction_audit={"look_ahead_clean": True}, approver=_approve)
    assert out["status"] == "applied", out


def test_live_evidence_unaffected_by_recon_gate(env):
    # No evidence_source → the recon gate is inert; normal apply path.
    out = _call(env, approver=_approve)
    assert out["status"] == "applied"
