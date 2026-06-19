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
