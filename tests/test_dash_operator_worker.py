import json
from pathlib import Path
from gui_v2.data import operator_control as oc


def _seed_order(root, wid, status, created_at="2020-01-01T00:00:00+00:00"):
    rec = {"work_order_id": wid, "status": status, "created_at": created_at,
           "status_history": [{"status": status, "at": created_at}]}
    d = Path(root) / "outputs" / "operator_control"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "work_orders.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")


def test_view_composition(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"operator_worker": {}}))
    _seed_order(tmp_path, "wo_a", "queued")
    _seed_order(tmp_path, "wo_b", "failed")
    monkeypatch.setattr(oc, "quarantine_inventory", lambda root: [])
    v = oc.operator_worker_view(tmp_path)
    assert "readiness" in v and v["readiness"]["overall_ready"].endswith("/5")
    assert "cost" in v
    by_id = {o["work_order_id"]: o for o in v["orders"]}
    assert by_id["wo_a"]["cancellable"] is True
    assert by_id["wo_b"]["cancellable"] is False
    assert v["counts"]["open"] >= 1


def test_stale_flag(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"operator_worker": {}}))
    _seed_order(tmp_path, "wo_old", "queued", created_at="2020-01-01T00:00:00+00:00")
    monkeypatch.setattr(oc, "quarantine_inventory", lambda root: [])
    v = oc.operator_worker_view(tmp_path)
    assert next(o for o in v["orders"] if o["work_order_id"] == "wo_old")["stale"] is True
    assert v["counts"]["stale"] >= 1


def test_degrades_without_orders_dir(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"operator_worker": {}}))
    monkeypatch.setattr(oc, "quarantine_inventory", lambda root: [])
    v = oc.operator_worker_view(tmp_path)
    assert v["orders"] == [] and v["degraded"] is False


def test_age_hours_handles_nonstring_created_at(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"operator_worker": {}}))
    # Seed a work order with an int created_at (malformed JSONL record)
    rec = {"work_order_id": "wo_int", "status": "queued", "created_at": 12345,
           "status_history": [{"status": "queued", "at": "2020-01-01T00:00:00+00:00"}]}
    d = Path(tmp_path) / "outputs" / "operator_control"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "work_orders.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")
    monkeypatch.setattr(oc, "quarantine_inventory", lambda root: [])
    # Must not raise AttributeError
    v = oc.operator_worker_view(tmp_path)
    wo = next(o for o in v["orders"] if o["work_order_id"] == "wo_int")
    assert wo["age_hours"] is None
    assert wo["stale"] is False
