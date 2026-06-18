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

    def fake_container(worktree_path, prompt_md, mode, cfg, root, work_order_id=""):
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


# ---------------------------------------------------------------------------
# Task 6: isolated-clone lifecycle tests
# ---------------------------------------------------------------------------


def _full_container_cfg(tmp_path, enabled=True):
    """Config with all required fields including workspace_root and credentials_dir."""
    ws_root = str(tmp_path / "ws")
    creds_dir = str(tmp_path / "creds")
    cfg_block = {
        "enabled": enabled,
        "podman_path": "/usr/bin/podman",
        "image_ref": "localhost/stockbot-worker",
        "image_digest": "sha256:abc",
        "image_build_ts": None,
        "container_uid": 1000,
        "container_gid": 1000,
        "run_as_user": "stockbot-worker",
        "credentials_dir": creds_dir,
        "workspace_root": ws_root,
        "attestation_path": "outputs/operator_control/worker_attestation.json",
        "attestation_max_age_days": 30,
        "resource_limits": {
            "pids": 512, "memory": "2g", "cpus": "2",
            "tmpfs_size": "512m", "timeout_seconds": 1800,
        },
        "env_allowlist": ["HOME"],
        "cap_drop_exceptions": [],
    }
    cfg = {"operator_control": {"worker_container": cfg_block}}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    return tmp_path, cfg_block


def test_container_uses_isolated_clone_not_worktree_path(tmp_path, monkeypatch):
    """_run_via_container must pass the isolated clone path to build_container_launch_spec,
    never the prod-linked worktree_path, and must call destroy_workspace afterward."""
    root, cfg = _full_container_cfg(tmp_path)
    sentinel_ws = str(tmp_path / "ws" / "wo_test123")
    Path(sentinel_ws).mkdir(parents=True, exist_ok=True)
    calls = {"create": [], "destroy": [], "spec_workspace": []}

    monkeypatch.setattr(wr.worker_workspace, "create_isolated_workspace",
                        lambda repo_root, ws_root, woid: calls["create"].append(woid) or sentinel_ws)
    monkeypatch.setattr(wr.worker_workspace, "destroy_workspace",
                        lambda path, ws_root: calls["destroy"].append(path))
    monkeypatch.setattr(wr.worker_workspace, "extract_validated_diff",
                        lambda ws: "1 file changed")
    monkeypatch.setattr(wr.shutil, "copy2", lambda src, dst: None)

    def fake_spec(*, cfg, workspace_dir, creds_dir, attest_dir, claude_argv):
        calls["spec_workspace"].append(workspace_dir)
        return ["/usr/bin/podman", "run", "--rm", "image", "claude"]

    monkeypatch.setattr(wr.worker_container, "build_container_launch_spec", fake_spec)

    class _Proc:
        returncode = 0
        stdout = '{"type":"result","is_error":false,"total_cost_usd":0.01}'
        stderr = ""

    monkeypatch.setattr(wr.subprocess, "run", lambda *a, **k: _Proc())

    monkeypatch.setattr(wr.worker_container, "verify_runtime_attestation",
                        lambda att, cfg, now, image_build_ts, config_mtime: (True, []))

    worktree_path = str(tmp_path / "worktrees" / "wo_test123")
    out = wr._run_via_container(worktree_path, "prompt", "diagnose", cfg, str(root), "wo_test123")

    # Clone created, not worktree_path
    assert calls["create"] == ["wo_test123"]
    # build_container_launch_spec received the sentinel clone path, NOT worktree_path
    assert calls["spec_workspace"] == [sentinel_ws]
    assert sentinel_ws != worktree_path
    # destroy called after successful run
    assert calls["destroy"] == [sentinel_ws]
    # isolated=True on success
    assert out["isolated"] is True
    assert out["execution_mode"] == "container"


def test_container_destroys_clone_even_on_startup_failure(tmp_path, monkeypatch):
    """destroy_workspace must be called even when the container startup fails."""
    root, cfg = _full_container_cfg(tmp_path)
    sentinel_ws = str(tmp_path / "ws" / "wo_fail")
    calls = {"destroy": []}

    monkeypatch.setattr(wr.worker_workspace, "create_isolated_workspace",
                        lambda *a: sentinel_ws)
    monkeypatch.setattr(wr.worker_workspace, "destroy_workspace",
                        lambda path, ws_root: calls["destroy"].append(path))
    monkeypatch.setattr(wr.shutil, "copy2", lambda src, dst: None)

    def fake_spec(*, cfg, workspace_dir, creds_dir, attest_dir, claude_argv):
        return ["/usr/bin/podman", "run", "--rm", "image"]

    monkeypatch.setattr(wr.worker_container, "build_container_launch_spec", fake_spec)

    def boom(*a, **k):
        raise OSError("podman not found")

    monkeypatch.setattr(wr.subprocess, "run", boom)

    out = wr._run_via_container(str(tmp_path / "wt"), "prompt", "diagnose", cfg, str(root), "wo_fail")

    assert out["ok"] is False
    assert "container startup failed" in (out.get("error") or "")
    assert out["stdout"] == "" and out["stderr"] == ""
    assert calls["destroy"] == [sentinel_ws]


def test_container_no_destroy_if_clone_creation_fails(tmp_path, monkeypatch):
    """If create_isolated_workspace raises, destroy_workspace must NOT be called."""
    root, cfg = _full_container_cfg(tmp_path)
    calls = {"destroy": []}

    monkeypatch.setattr(wr.worker_workspace, "create_isolated_workspace",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("clone failed")))
    monkeypatch.setattr(wr.worker_workspace, "destroy_workspace",
                        lambda path, ws_root: calls["destroy"].append(path))

    with pytest.raises(RuntimeError, match="clone failed"):
        wr._run_via_container(str(tmp_path / "wt"), "prompt", "diagnose", cfg, str(root), "wo_noclone")

    assert calls["destroy"] == []


def test_container_attestation_failure_has_stdout_stderr_keys(tmp_path, monkeypatch):
    """All container error-return dicts must include stdout and stderr keys."""
    root, cfg = _full_container_cfg(tmp_path)
    monkeypatch.setattr(wr.worker_workspace, "create_isolated_workspace",
                        lambda *a: str(tmp_path / "ws" / "wo_att"))
    monkeypatch.setattr(wr.worker_workspace, "destroy_workspace", lambda *a: None)
    monkeypatch.setattr(wr.shutil, "copy2", lambda src, dst: None)

    def fake_spec(*, cfg, workspace_dir, creds_dir, attest_dir, claude_argv):
        return ["/usr/bin/podman"]

    monkeypatch.setattr(wr.worker_container, "build_container_launch_spec", fake_spec)

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(wr.subprocess, "run", lambda *a, **k: _Proc())
    monkeypatch.setattr(wr.worker_container, "verify_runtime_attestation",
                        lambda att, cfg, now, image_build_ts, config_mtime: (False, ["uid mismatch"]))

    out = wr._run_via_container(str(tmp_path / "wt"), "prompt", "diagnose", cfg, str(root), "wo_att")

    assert out["ok"] is False
    assert "stdout" in out and "stderr" in out
    assert out["isolated"] is False


def test_isolated_true_only_on_successful_container_run(tmp_path, monkeypatch):
    """isolated=True only on the success path; all failure paths return isolated=False."""
    root, cfg = _full_container_cfg(tmp_path)
    monkeypatch.setattr(wr.worker_workspace, "create_isolated_workspace",
                        lambda *a: str(tmp_path / "ws" / "wo_iso"))
    monkeypatch.setattr(wr.worker_workspace, "destroy_workspace", lambda *a: None)
    monkeypatch.setattr(wr.worker_workspace, "extract_validated_diff", lambda ws: "")
    monkeypatch.setattr(wr.shutil, "copy2", lambda src, dst: None)

    def fake_spec(*, cfg, workspace_dir, creds_dir, attest_dir, claude_argv):
        return ["/usr/bin/podman"]

    monkeypatch.setattr(wr.worker_container, "build_container_launch_spec", fake_spec)

    class _Proc:
        returncode = 0
        stdout = '{"type":"result","is_error":false,"total_cost_usd":0.0}'
        stderr = ""

    monkeypatch.setattr(wr.subprocess, "run", lambda *a, **k: _Proc())
    monkeypatch.setattr(wr.worker_container, "verify_runtime_attestation",
                        lambda att, cfg, now, image_build_ts, config_mtime: (True, []))

    out = wr._run_via_container(str(tmp_path / "wt"), "prompt", "diagnose", cfg, str(root), "wo_iso")
    assert out["isolated"] is True
