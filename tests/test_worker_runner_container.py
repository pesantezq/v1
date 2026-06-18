"""Tests for fail-closed container routing in worker_runner._invoke_claude.

Task 6: Phase 1 container isolation — wires the worker's claude invocation
to run inside the rootless container when enabled, with strict fail-closed
behavior. Direct path ONLY runs when container mode is disabled.
"""
import json
import pytest
from pathlib import Path
import operator_control.worker_runner as wr


def _cfg(tmp_path, enabled):
    cfg = {"operator_control": {"worker_container": {
        "enabled": enabled, "podman_path": "/usr/bin/podman", "image_ref": "localhost/stockbot-worker",
        "image_digest": "sha256:abc", "container_uid": 1000, "container_gid": 1000,
        "run_as_user": "stockbot-worker", "credentials_dir": str(tmp_path/"creds"),
        "workspace_root": str(tmp_path/"ws"),
        "resource_limits": {"pids": 512, "memory": "2g", "cpus": "2", "tmpfs_size": "512m", "timeout_seconds": 1800},
        "env_allowlist": ["HOME"], "cap_drop_exceptions": [], "attestation_max_age_days": 30}}}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    return tmp_path


def test_disabled_uses_direct_recorded_unisolated(tmp_path, monkeypatch):
    root = _cfg(tmp_path, enabled=False)
    monkeypatch.setattr(wr, "_run_direct_claude", lambda *a, **k: {"ok": True})
    out = wr._invoke_claude(str(tmp_path), "prompt", mode="diagnose", root=str(root))
    assert out["execution_mode"] == "direct" and out["isolated"] is False


def test_enabled_podman_missing_fails_closed_no_direct(tmp_path, monkeypatch):
    root = _cfg(tmp_path, enabled=True)
    called = {"direct": False}
    monkeypatch.setattr(wr, "_run_direct_claude",
                        lambda *a, **k: called.__setitem__("direct", True) or {"ok": True})
    monkeypatch.setattr(wr.worker_container, "probe_container_capabilities",
                        lambda cfg: {"podman_present": False, "image_present": False,
                                     "digest_pinned": True, "rootless_ok": True})
    out = wr._invoke_claude(str(tmp_path), "prompt", mode="diagnose", root=str(root))
    assert out["ok"] is False and called["direct"] is False
    assert "podman" in (out.get("error") or "").lower()


def test_enabled_validation_failure_fails_closed(tmp_path, monkeypatch):
    root = _cfg(tmp_path, enabled=True)
    monkeypatch.setattr(wr.worker_container, "validate_container_configuration",
                        lambda cfg: (False, ["bad"]))
    monkeypatch.setattr(wr, "_run_direct_claude",
                        lambda *a, **k: pytest.fail("direct must not run"))
    out = wr._invoke_claude(str(tmp_path), "prompt", mode="diagnose", root=str(root))
    assert out["ok"] is False


def test_enabled_all_caps_pass_runs_via_container(tmp_path, monkeypatch):
    """When all preconditions pass, _run_via_container is called."""
    root = _cfg(tmp_path, enabled=True)
    # Patch validate to succeed
    monkeypatch.setattr(wr.worker_container, "validate_container_configuration",
                        lambda cfg: (True, []))
    # Patch probe to succeed
    monkeypatch.setattr(wr.worker_container, "probe_container_capabilities",
                        lambda cfg: {"podman_present": True, "image_present": True,
                                     "digest_pinned": True, "rootless_ok": True})
    # Patch _run_via_container to return container-mode success
    container_called = {"called": False}

    def fake_container(worktree_path, prompt_md, mode, cfg, root):
        container_called["called"] = True
        return {"ok": True, "execution_mode": "container", "isolated": True,
                "cost_usd": 0.0, "num_turns": None, "duration_ms": None, "result_text": None}

    monkeypatch.setattr(wr, "_run_via_container", fake_container)
    monkeypatch.setattr(wr, "_run_direct_claude",
                        lambda *a, **k: pytest.fail("direct must not run"))

    out = wr._invoke_claude(str(tmp_path), "prompt", mode="diagnose", root=str(root))
    assert out["ok"] is True
    assert out["execution_mode"] == "container"
    assert out["isolated"] is True
    assert container_called["called"] is True


def test_no_config_falls_through_to_direct(tmp_path, monkeypatch):
    """When config.json is missing or has no worker_container block, use direct."""
    # No config.json in tmp_path root
    monkeypatch.setattr(wr, "_run_direct_claude", lambda *a, **k: {"ok": True})
    out = wr._invoke_claude(str(tmp_path), "prompt", mode="diagnose", root=str(tmp_path))
    assert out["execution_mode"] == "direct"
    assert out["isolated"] is False


def test_worker_container_cfg_returns_none_on_missing(tmp_path):
    """_worker_container_cfg returns None when config.json is absent."""
    result = wr._worker_container_cfg(str(tmp_path))
    assert result is None


def test_worker_container_cfg_returns_none_when_no_section(tmp_path):
    """_worker_container_cfg returns None when operator_control.worker_container is absent."""
    (tmp_path / "config.json").write_text(json.dumps({"other": "stuff"}))
    result = wr._worker_container_cfg(str(tmp_path))
    assert result is None


def test_worker_container_cfg_returns_block_when_present(tmp_path):
    """_worker_container_cfg returns the worker_container block when present."""
    root = _cfg(tmp_path, enabled=True)
    result = wr._worker_container_cfg(str(root))
    assert result is not None
    assert result["enabled"] is True
    assert result["image_ref"] == "localhost/stockbot-worker"


def test_rootless_cap_fail_closes(tmp_path, monkeypatch):
    """If rootless is not ok, fail closed with no fallback to direct."""
    root = _cfg(tmp_path, enabled=True)
    monkeypatch.setattr(wr.worker_container, "validate_container_configuration",
                        lambda cfg: (True, []))
    monkeypatch.setattr(wr.worker_container, "probe_container_capabilities",
                        lambda cfg: {"podman_present": True, "image_present": True,
                                     "digest_pinned": True, "rootless_ok": False})
    monkeypatch.setattr(wr, "_run_direct_claude",
                        lambda *a, **k: pytest.fail("direct must not run"))
    out = wr._invoke_claude(str(tmp_path), "prompt", mode="diagnose", root=str(root))
    assert out["ok"] is False
    assert "rootless=False" in (out.get("error") or "")


def test_image_missing_fails_closed(tmp_path, monkeypatch):
    """If image is not present, fail closed."""
    root = _cfg(tmp_path, enabled=True)
    monkeypatch.setattr(wr.worker_container, "validate_container_configuration",
                        lambda cfg: (True, []))
    monkeypatch.setattr(wr.worker_container, "probe_container_capabilities",
                        lambda cfg: {"podman_present": True, "image_present": False,
                                     "digest_pinned": True, "rootless_ok": True})
    monkeypatch.setattr(wr, "_run_direct_claude",
                        lambda *a, **k: pytest.fail("direct must not run"))
    out = wr._invoke_claude(str(tmp_path), "prompt", mode="diagnose", root=str(root))
    assert out["ok"] is False
    assert "image=False" in (out.get("error") or "")
