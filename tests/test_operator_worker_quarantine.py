import json
from pathlib import Path

import pytest

from operator_control import worker_runner as wr
from operator_control import work_orders as wo
from operator_control.repair_policies import WorkOrderValidationError


def _seed(root: Path, status: str) -> str:
    """Create a work order record at the given status by appending a folded record."""
    root.mkdir(parents=True, exist_ok=True)
    wid = f"wo_{status}"
    rec = {"work_order_id": wid, "status": status, "probe_id": "p1",
           "skill_id": "s1", "mode": "safe_repair", "requested_action": "fix",
           "created_at": "2026-06-19T00:00:00+00:00",
           "status_history": [{"status": status, "at": "2026-06-19T00:00:00+00:00",
                               "actor": "test", "note": "seed"}]}
    p = root / "outputs" / "operator_control" / "work_orders.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    return wid


def _audit_events(root: Path) -> list[dict]:
    p = root / "outputs" / "operator_control" / "audit_log.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def test_cancel_queued_order(tmp_path):
    wid = _seed(tmp_path, "queued")
    out = wr.cancel(tmp_path, wid, actor="test", note="dead")
    assert out["status"] == "cancelled"
    assert wo.get_work_order(tmp_path, wid)["status"] == "cancelled"
    assert any(e["event_type"] == "work_order_cancelled" for e in _audit_events(tmp_path))


def test_cancel_claimed_order_ok(tmp_path):
    wid = _seed(tmp_path, "claimed")
    assert wr.cancel(tmp_path, wid, actor="test")["status"] == "cancelled"


def test_cancel_running_order_rejected(tmp_path):
    wid = _seed(tmp_path, "running")
    with pytest.raises(WorkOrderValidationError):
        wr.cancel(tmp_path, wid, actor="test")


def test_cancel_completed_order_rejected(tmp_path):
    wid = _seed(tmp_path, "completed")
    with pytest.raises(WorkOrderValidationError):
        wr.cancel(tmp_path, wid, actor="test")


def test_cancel_unknown_id_raises(tmp_path):
    _seed(tmp_path, "queued")
    with pytest.raises(wr.WorkerRunnerError):
        wr.cancel(tmp_path, "wo_does_not_exist", actor="test")


def _seed_failed_with_worktree(root: Path, wid: str, with_diff: bool):
    """Seed a failed order and a fake worktree dir; optionally with a changed file."""
    rec = {"work_order_id": wid, "status": "failed", "probe_id": "p", "skill_id": "s",
           "mode": "safe_repair", "requested_action": "x",
           "created_at": "2026-06-19T00:00:00+00:00",
           "status_history": [{"status": "failed", "at": "2026-06-19T00:00:00+00:00",
                               "actor": "t", "note": "quarantined"}]}
    p = root / "outputs" / "operator_control" / "work_orders.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    wt = root / ".worktrees" / wid
    wt.mkdir(parents=True, exist_ok=True)
    return wt


def test_quarantine_review_identifies_salvageable(tmp_path, monkeypatch):
    wt_diff = _seed_failed_with_worktree(tmp_path, "wo_diff", with_diff=True)
    _seed_failed_with_worktree(tmp_path, "wo_nodiff", with_diff=False)
    # No worktree at all for this failed order:
    p = tmp_path / "outputs" / "operator_control" / "work_orders.jsonl"
    with p.open("a") as fh:
        fh.write(json.dumps({"work_order_id": "wo_nowt", "status": "failed",
                             "created_at": "2026-06-19T00:00:00+00:00"}) + "\n")

    def fake_changed(wtpath, base="main"):
        return ["some/file.py"] if str(wtpath).endswith("wo_diff") else []
    monkeypatch.setattr(wr.worktree, "changed_files", fake_changed)

    review = wr.quarantine_review(tmp_path)
    ids = {it["work_order_id"]: it for it in review["items"]}
    assert ids["wo_diff"]["salvageable"] is True
    assert ids["wo_diff"]["changed_file_count"] == 1
    assert ids["wo_nodiff"]["salvageable"] is False
    assert "wo_nowt" not in ids  # no worktree -> not listed
    assert review["pending"] == 1  # only wo_diff


def test_quarantine_discard_removes_worktree(tmp_path, monkeypatch):
    _seed_failed_with_worktree(tmp_path, "wo_kill", with_diff=True)
    removed = {}
    monkeypatch.setattr(wr.worktree, "remove_worktree",
                        lambda root, path, force=False: removed.update(path=str(path), force=force))
    out = wr.quarantine_discard(tmp_path, "wo_kill", actor="test")
    assert out["removed"] is True
    assert removed["force"] is True
    assert wo.get_work_order(tmp_path, "wo_kill")["status"] == "failed"  # status unchanged
    assert any(e["event_type"] == "worker_quarantine_discarded" for e in _audit_events(tmp_path))


def test_quarantine_salvage_reports_only(tmp_path, monkeypatch):
    _seed_failed_with_worktree(tmp_path, "wo_keep", with_diff=True)
    called = {"removed": False}
    monkeypatch.setattr(wr.worktree, "remove_worktree",
                        lambda *a, **k: called.update(removed=True))
    out = wr.quarantine_salvage(tmp_path, "wo_keep", actor="test")
    assert out["branch"] == "operator/wo_keep"
    assert "wo_keep" in out["integration_command"]
    assert called["removed"] is False  # salvage NEVER removes
    assert any(e["event_type"] == "worker_quarantine_salvaged" for e in _audit_events(tmp_path))


def test_status_includes_quarantine_pending(tmp_path, monkeypatch):
    _seed_failed_with_worktree(tmp_path, "wo_p", with_diff=True)
    monkeypatch.setattr(wr.worktree, "changed_files", lambda w, base="main": ["f.py"])
    monkeypatch.setattr(wr.worktree, "list_worktrees", lambda root: [])
    st = wr.status(tmp_path)
    assert st["quarantine_pending"] == 1
