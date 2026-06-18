# tests/test_operator_quarantine.py
import subprocess
import pytest
from pathlib import Path
from gui_v2.data import operator_quarantine as q


def _run(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def _repo_with_worktree(tmp_path, *, diverge=True, merged=False):
    """Build a real git repo + an operator/* worktree branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "git", "init", "-q", "-b", "main")
    _run(repo, "git", "config", "user.email", "t@t")
    _run(repo, "git", "config", "user.name", "t")
    (repo / "f.txt").write_text("base\n")
    _run(repo, "git", "add", "."); _run(repo, "git", "commit", "-qm", "base")
    wt = repo / ".worktrees" / "wo_test_abc"
    _run(repo, "git", "worktree", "add", "-q", "-b", "operator/wo_test_abc", str(wt))
    if diverge:
        (wt / "g.txt").write_text("change\n")
        _run(wt, "git", "add", "."); _run(wt, "git", "commit", "-qm", "wt change")
    if merged:
        _run(repo, "git", "merge", "-q", "operator/wo_test_abc")
    return repo


def test_inventory_diverged_branch(tmp_path, monkeypatch):
    repo = _repo_with_worktree(tmp_path, diverge=True, merged=False)
    # only validated IDs: stub list_work_orders to return our order
    monkeypatch.setattr(q, "list_work_orders",
                        lambda root: [{"work_order_id": "wo_test_abc"}])
    inv = q.quarantine_inventory(repo)
    assert len(inv) == 1
    e = inv[0]
    assert e["branch"] == "operator/wo_test_abc"
    assert e["unique_commits"] == 1
    assert e["is_ancestor_of_main"] is False
    assert e["already_in_main"] is False
    assert "g.txt" in e["changed_paths"]


def test_inventory_merged_branch_is_ancestor(tmp_path, monkeypatch):
    repo = _repo_with_worktree(tmp_path, diverge=True, merged=True)
    monkeypatch.setattr(q, "list_work_orders",
                        lambda root: [{"work_order_id": "wo_test_abc"}])
    inv = q.quarantine_inventory(repo)
    assert inv[0]["is_ancestor_of_main"] is True
    assert inv[0]["already_in_main"] is True


def test_diff_unknown_id_not_found(tmp_path, monkeypatch):
    repo = _repo_with_worktree(tmp_path)
    monkeypatch.setattr(q, "list_work_orders",
                        lambda root: [{"work_order_id": "wo_test_abc"}])
    # an ID that is NOT in the validated records must be rejected
    res = q.quarantine_diff(repo, "wo_evil; rm -rf /")
    assert res["found"] is False


def test_diff_path_traversal_id_rejected(tmp_path, monkeypatch):
    repo = _repo_with_worktree(tmp_path)
    monkeypatch.setattr(q, "list_work_orders",
                        lambda root: [{"work_order_id": "wo_test_abc"}])
    res = q.quarantine_diff(repo, "../../etc/passwd")
    assert res["found"] is False


def test_missing_git_degrades(tmp_path, monkeypatch):
    repo = _repo_with_worktree(tmp_path)
    monkeypatch.setattr(q, "list_work_orders",
                        lambda root: [{"work_order_id": "wo_test_abc"}])
    monkeypatch.setattr(q, "_git", lambda *a, **k: q._failed_cp("git missing"))
    inv = q.quarantine_inventory(repo)
    assert inv == []  # when _git always fails, worktree list is empty → no entries


def test_inventory_fails_closed_when_no_valid_ids(tmp_path, monkeypatch):
    """When list_work_orders returns [] (or raises), unregistered worktrees must NOT be surfaced."""
    repo = _repo_with_worktree(tmp_path, diverge=True)
    # Return empty list — simulates failure or empty registry
    monkeypatch.setattr(q, "list_work_orders", lambda root: [])
    inv = q.quarantine_inventory(repo)
    assert inv == []  # fail-closed: wo_test_abc is not in valid IDs so must be excluded

    # Also test when list_work_orders raises
    def _raise(root):
        raise RuntimeError("db unavailable")
    monkeypatch.setattr(q, "list_work_orders", _raise)
    inv2 = q.quarantine_inventory(repo)
    assert inv2 == []  # _valid_ids catches the exception → valid=set() → wo_id not in valid → excluded


def test_output_is_bounded(tmp_path, monkeypatch):
    repo = _repo_with_worktree(tmp_path)
    monkeypatch.setattr(q, "list_work_orders",
                        lambda root: [{"work_order_id": "wo_test_abc"}])
    inv = q.quarantine_inventory(repo)
    assert len(inv[0]["stat_summary"]) <= q.MAX_OUTPUT_BYTES
    assert len(inv[0]["changed_paths"]) <= q.MAX_PATHS
