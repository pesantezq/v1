"""Tests for dashboard auto-update with manual intervention (Phases A/B/C)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _git(root, *a):
    return subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True, check=True)


@pytest.fixture()
def gitrepo(tmp_path):
    """A git repo with commit A on HEAD; origin/main ref set via update-ref."""
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "f.txt").write_text("A\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "A")
    (tmp_path / "outputs" / "operator_control").mkdir(parents=True)
    return tmp_path


# ---------------------------------------------------------------------------
# Detection (Phase A)
# ---------------------------------------------------------------------------


def test_up_to_date(gitrepo):
    from gui_v2.data import deploy_status as ds

    ds.write_running_sha(gitrepo)
    head = _git(gitrepo, "rev-parse", "HEAD").stdout.strip()
    _git(gitrepo, "update-ref", "refs/remotes/origin/main", head)
    st = ds.collect_deploy_status(gitrepo, fetch=False)
    assert st["state"] == "up_to_date"
    assert st["commits_behind"] == 0


def test_update_available_fast_forward(gitrepo):
    from gui_v2.data import deploy_status as ds

    ds.write_running_sha(gitrepo)  # stamp A (running)
    # advance to B and point origin/main at B
    (gitrepo / "f.txt").write_text("B\n")
    _git(gitrepo, "commit", "-aqm", "B")
    b = _git(gitrepo, "rev-parse", "HEAD").stdout.strip()
    _git(gitrepo, "update-ref", "refs/remotes/origin/main", b)
    st = ds.collect_deploy_status(gitrepo, fetch=False)
    assert st["state"] == "update_available"
    assert st["fast_forward"] is True
    assert st["commits_behind"] == 1


def test_divergent(gitrepo):
    from gui_v2.data import deploy_status as ds

    ds.write_running_sha(gitrepo)  # running = A
    # create an unrelated commit on a side branch → origin/main not a ff of A
    _git(gitrepo, "checkout", "-q", "--orphan", "side")
    (gitrepo / "g.txt").write_text("X\n")
    _git(gitrepo, "add", "-A")
    _git(gitrepo, "commit", "-q", "-m", "X")
    x = _git(gitrepo, "rev-parse", "HEAD").stdout.strip()
    _git(gitrepo, "update-ref", "refs/remotes/origin/main", x)
    _git(gitrepo, "checkout", "-q", "main")
    st = ds.collect_deploy_status(gitrepo, fetch=False)
    assert st["state"] == "divergent"
    assert st["fast_forward"] is False


def test_git_unavailable_is_safe(tmp_path):
    from gui_v2.data import deploy_status as ds

    # not a git repo → unknown, never raises
    st = ds.collect_deploy_status(tmp_path, fetch=False)
    assert st["state"] in ("unknown",)


def test_deploy_card_states(gitrepo):
    from gui_v2.data import deploy_status as ds

    assert ds.deploy_card({"state": "up_to_date", "running_short": "abc"})["status"] == "ok"
    assert ds.deploy_card({"state": "update_available", "commits_behind": 2,
                           "running_short": "a", "latest_short": "b"})["status"] == "warning"
    assert ds.deploy_card({"state": "unknown"})["status"] == "info"


# ---------------------------------------------------------------------------
# GUI (Phases A/B/C)
# ---------------------------------------------------------------------------


@pytest.fixture()
def client_root(tmp_path, monkeypatch):
    from gui_v2 import app as app_module
    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    monkeypatch.setattr(app_module, "REPO_ROOT", tmp_path)
    return TestClient(app_module.app), tmp_path, app_module


def test_system_shows_deployment_card(client_root):
    client, _, _ = client_root
    assert "Deployment" in client.get("/dashboard/system").text


def test_request_update_records_audit_no_exec(client_root):
    client, root, _ = client_root
    resp = client.post("/dashboard/operator/request-update", follow_redirects=False)
    assert resp.status_code == 303
    from operator_control import audit_log
    assert any(e["event_type"] == "deploy_update_requested" for e in audit_log.read_events(root))


def test_apply_update_403_when_gate_off(client_root, monkeypatch):
    client, _, _ = client_root
    monkeypatch.delenv("GUI_V2_DEPLOY_APPLY", raising=False)
    assert client.post("/dashboard/operator/apply-update").status_code == 403


def test_apply_update_409_when_not_fast_forward(client_root, monkeypatch):
    client, _, app_module = client_root
    monkeypatch.setenv("GUI_V2_DEPLOY_APPLY", "1")
    from gui_v2.data import deploy_status as ds
    monkeypatch.setattr(ds, "collect_deploy_status",
                        lambda *a, **k: {"state": "divergent", "fast_forward": False})
    assert client.post("/dashboard/operator/apply-update").status_code == 409


def test_apply_update_spawns_detached_when_gated_and_ff(client_root, monkeypatch):
    client, _, app_module = client_root
    monkeypatch.setenv("GUI_V2_DEPLOY_APPLY", "1")
    from gui_v2.data import deploy_status as ds
    monkeypatch.setattr(ds, "collect_deploy_status",
                        lambda *a, **k: {"state": "update_available", "fast_forward": True,
                                         "running_sha": "aaa", "latest_sha": "bbb"})
    calls = {}
    monkeypatch.setattr(app_module.subprocess, "Popen",
                        lambda argv, **kw: calls.setdefault("argv", argv))
    resp = client.post("/dashboard/operator/apply-update", follow_redirects=False)
    assert resp.status_code == 303
    assert "dashboard_update.sh" in " ".join(calls["argv"])
