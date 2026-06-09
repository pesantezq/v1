"""Tests for the Phase 2 operator-control worker runner.

The `claude` subprocess and the test runner are monkeypatched, so these tests
never invoke a real LLM and never run nested pytest.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from operator_control import work_orders as wo_mod


def _git(root, *a):
    return subprocess.run(
        ["git", "-C", str(root), *a], capture_output=True, text=True, check=True
    )


@pytest.fixture()
def repo(tmp_path):
    """A real git repo with a committed file on `main`."""
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "keep.txt").write_text("hi\n")
    (tmp_path / "outputs" / "operator_control").mkdir(parents=True)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _order(repo, probe="data_quality.warnings",
           skill="diagnose_data_quality_warnings", mode="diagnose"):
    return wo_mod.create_work_order(
        repo, probe_id=probe, skill_id=skill, mode=mode, created_by="t"
    )


# ---------------------------------------------------------------------------
# worktree wrapper
# ---------------------------------------------------------------------------


def test_create_worktree_and_changed_files(repo):
    from operator_control import worktree

    wt, branch = worktree.create_worktree(repo, "wo_test", base="main")
    assert wt.exists() and branch == "operator/wo_test"
    (wt / "newfile.py").write_text("x = 1\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "edit")
    assert "newfile.py" in worktree.changed_files(wt, base="main")


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def test_autonomous_disabled_by_default(repo, monkeypatch):
    from operator_control import worker_runner

    monkeypatch.delenv("STOCKBOT_OPERATOR_WORKER_AUTONOMOUS", raising=False)
    assert worker_runner.autonomous_enabled(repo) is False


def test_autonomous_requires_all_three_gates(repo, monkeypatch):
    from operator_control import worker_runner

    (repo / "config").mkdir(exist_ok=True)
    (repo / "config.json").write_text(
        json.dumps({"operator_control": {"autonomous_worker": {"enabled": True}}})
    )
    monkeypatch.setenv("STOCKBOT_OPERATOR_WORKER_AUTONOMOUS", "1")
    assert worker_runner.autonomous_enabled(repo) is True
    # kill switch wins
    (repo / "config" / "operator_worker.DISABLED").write_text("x")
    assert worker_runner.autonomous_enabled(repo) is False


# ---------------------------------------------------------------------------
# Scaffolding path
# ---------------------------------------------------------------------------


def test_scaffold_creates_worktree_and_prompt(repo):
    from operator_control import worker_runner

    order = _order(repo)
    res = worker_runner.scaffold(repo, order["work_order_id"], actor="t")
    wt = Path(res["worktree"])
    assert wt.exists()
    assert (wt / "WORKER_PROMPT.md").exists()
    assert (wt / "RUN_WORKER.md").exists()
    assert wo_mod.get_work_order(repo, order["work_order_id"])["status"] == "claimed"


def test_scaffold_refuses_awaiting_approval(repo):
    from operator_control import worker_runner

    order = wo_mod.create_work_order(
        repo, probe_id="memo.generation_readability",
        skill_id="regenerate_memo_from_artifacts", mode="safe_repair", created_by="t",
    )
    assert order["status"] == "awaiting_approval"
    with pytest.raises(worker_runner.WorkerRunnerError):
        worker_runner.scaffold(repo, order["work_order_id"], actor="t")


# ---------------------------------------------------------------------------
# Autonomous path + guards
# ---------------------------------------------------------------------------


def _enable_autonomous(repo, monkeypatch):
    (repo / "config.json").write_text(
        json.dumps({"operator_control": {"autonomous_worker": {"enabled": True}}})
    )
    monkeypatch.setenv("STOCKBOT_OPERATOR_WORKER_AUTONOMOUS", "1")


def test_autonomous_completes_on_clean_diff_and_passing_tests(repo, monkeypatch):
    from operator_control import worker_runner

    _enable_autonomous(repo, monkeypatch)
    order = _order(repo)
    monkeypatch.setattr(worker_runner, "_invoke_claude",
                        lambda wt, p: {"ok": True, "stdout": "done"})
    monkeypatch.setattr(worker_runner.worktree, "changed_files",
                        lambda wt, base="main": ["operator_control/_scratch_note.py"])
    monkeypatch.setattr(worker_runner, "_run_tests",
                        lambda wt, tests: {"passed": True, "output": "ok"})
    res = worker_runner.run(repo, order["work_order_id"], actor="auto")
    assert res["mode_of_runner"] == "autonomous"
    assert wo_mod.get_work_order(repo, order["work_order_id"])["status"] == "completed"


def test_autonomous_quarantines_protected_path(repo, monkeypatch):
    from operator_control import worker_runner, audit_log

    _enable_autonomous(repo, monkeypatch)
    order = _order(repo)
    monkeypatch.setattr(worker_runner, "_invoke_claude",
                        lambda wt, p: {"ok": True, "stdout": ""})
    monkeypatch.setattr(worker_runner.worktree, "changed_files",
                        lambda wt, base="main": ["scoring.py"])
    monkeypatch.setattr(worker_runner, "_run_tests",
                        lambda wt, tests: {"passed": True, "output": ""})
    res = worker_runner.run(repo, order["work_order_id"], actor="auto")
    assert wo_mod.get_work_order(repo, order["work_order_id"])["status"] == "failed"
    assert any(e["event_type"] == "worker_protected_path_violation"
               for e in audit_log.read_events(repo))
    # worktree retained for forensics
    assert Path(res["worktree"]).exists()


def test_autonomous_fails_on_failing_tests(repo, monkeypatch):
    from operator_control import worker_runner

    _enable_autonomous(repo, monkeypatch)
    order = _order(repo)
    monkeypatch.setattr(worker_runner, "_invoke_claude",
                        lambda wt, p: {"ok": True, "stdout": ""})
    monkeypatch.setattr(worker_runner.worktree, "changed_files",
                        lambda wt, base="main": [])
    monkeypatch.setattr(worker_runner, "_run_tests",
                        lambda wt, tests: {"passed": False, "output": "1 failed"})
    worker_runner.run(repo, order["work_order_id"], actor="auto")
    assert wo_mod.get_work_order(repo, order["work_order_id"])["status"] == "failed"


def test_run_falls_back_to_scaffold_when_disabled(repo, monkeypatch):
    from operator_control import worker_runner

    monkeypatch.delenv("STOCKBOT_OPERATOR_WORKER_AUTONOMOUS", raising=False)
    order = _order(repo)
    res = worker_runner.run(repo, order["work_order_id"], actor="auto")
    assert res["mode_of_runner"] == "scaffold"  # no claude invoked


def test_autonomous_never_advances_main(repo, monkeypatch):
    """The runner must never move main or push. main HEAD is unchanged after a run."""
    from operator_control import worker_runner

    before = _git(repo, "rev-parse", "main").stdout.strip()
    _enable_autonomous(repo, monkeypatch)
    order = _order(repo)
    monkeypatch.setattr(worker_runner, "_invoke_claude",
                        lambda wt, p: {"ok": True, "stdout": ""})
    monkeypatch.setattr(worker_runner.worktree, "changed_files",
                        lambda wt, base="main": [])
    monkeypatch.setattr(worker_runner, "_run_tests",
                        lambda wt, tests: {"passed": True, "output": "ok"})
    worker_runner.run(repo, order["work_order_id"], actor="auto")
    after = _git(repo, "rev-parse", "main").stdout.strip()
    assert before == after


# ---------------------------------------------------------------------------
# Manual complete / fail
# ---------------------------------------------------------------------------


def test_complete_from_claimed(repo):
    from operator_control import worker_runner

    order = _order(repo)
    worker_runner.scaffold(repo, order["work_order_id"], actor="t")
    worker_runner.complete(repo, order["work_order_id"], actor="t")
    assert wo_mod.get_work_order(repo, order["work_order_id"])["status"] == "completed"


def test_fail_from_claimed(repo):
    from operator_control import worker_runner

    order = _order(repo)
    worker_runner.scaffold(repo, order["work_order_id"], actor="t")
    worker_runner.fail(repo, order["work_order_id"], actor="t", note="gave up")
    assert wo_mod.get_work_order(repo, order["work_order_id"])["status"] == "failed"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_status_and_scaffold(repo, capsys):
    from operator_control import worker_runner

    order = _order(repo)
    rc = worker_runner.main(["--root", str(repo), "scaffold", "--id", order["work_order_id"]])
    assert rc == 0
    assert "worktree" in capsys.readouterr().out.lower()
    rc = worker_runner.main(["--root", str(repo), "status"])
    assert rc == 0


# ---------------------------------------------------------------------------
# Phase 3 — scheduled drain
# ---------------------------------------------------------------------------


def test_drain_is_inert_when_autonomous_disabled(repo, monkeypatch):
    from operator_control import worker_runner

    monkeypatch.delenv("STOCKBOT_OPERATOR_WORKER_AUTONOMOUS", raising=False)
    _order(repo)  # an eligible order exists
    res = worker_runner.drain(repo, max_orders=5, actor="cron")
    assert res["status"] == "inert"
    assert res["drained"] == 0
    # the order is untouched (still queued — never claimed)
    orders = wo_mod.list_work_orders(repo)
    assert all(o["status"] == "queued" for o in orders)


def test_drain_runs_eligible_orders_when_enabled(repo, monkeypatch):
    from operator_control import worker_runner

    _enable_autonomous(repo, monkeypatch)
    o1 = _order(repo)
    o2 = _order(repo, probe="pipeline.run_status", skill="diagnose_pipeline_status")
    monkeypatch.setattr(worker_runner, "_invoke_claude", lambda wt, p: {"ok": True, "stdout": ""})
    monkeypatch.setattr(worker_runner.worktree, "changed_files", lambda wt, base="main": [])
    monkeypatch.setattr(worker_runner, "_run_tests", lambda wt, tests: {"passed": True, "output": "ok"})
    res = worker_runner.drain(repo, max_orders=10, actor="cron")
    assert res["status"] == "ran"
    assert res["drained"] == 2
    statuses = {o["work_order_id"]: o["status"] for o in wo_mod.list_work_orders(repo)}
    assert statuses[o1["work_order_id"]] == "completed"
    assert statuses[o2["work_order_id"]] == "completed"
