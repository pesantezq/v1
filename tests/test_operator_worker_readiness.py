import json
from portfolio_automation.operator_worker_readiness import (
    operator_worker_readiness, RECOGNIZED_STATUSES, DECLARED_GATES,
)


def _write_config(tmp_path, declared=None, cost_cap=None):
    # Real schema: worker config lives under the existing top-level
    # "operator_control" object (alongside autonomous_worker), NOT a new
    # "operator_worker" key.
    cfg = {"operator_control": {}}
    if declared is not None:
        cfg["operator_control"]["readiness_declared"] = declared
    if cost_cap is not None:
        cfg["operator_control"]["cost_cap_usd_per_day"] = cost_cap
    (tmp_path / "config.json").write_text(json.dumps(cfg))


def test_five_primary_gates_present(tmp_path):
    _write_config(tmp_path)
    r = operator_worker_readiness(tmp_path)
    assert set(r["gates"]) == {"auth", "bounded_cmd", "audit", "rollback", "quarantine"}
    assert r["overall_ready"].endswith("/5")
    assert "cost" in r and "cap_configured" in r["cost"]  # cost is separate, not a gate


def test_declared_gate_missing_defaults_amber(tmp_path):
    _write_config(tmp_path, declared={})
    r = operator_worker_readiness(tmp_path)
    assert r["gates"]["bounded_cmd"]["status"] == "amber"
    assert r["gates"]["bounded_cmd"]["source"] == "declared"


def test_declared_gate_evidence_free_defaults_amber(tmp_path):
    _write_config(tmp_path, declared={"rollback": {
        "status": "green", "declared_by": "op", "declared_at": "2026-06-18T00:00:00Z",
        "evidence": []}})  # empty evidence
    r = operator_worker_readiness(tmp_path)
    assert r["gates"]["rollback"]["status"] == "amber"


def test_declared_gate_dangling_evidence_defaults_amber(tmp_path):
    _write_config(tmp_path, declared={"bounded_cmd": {
        "status": "green", "declared_by": "op", "declared_at": "2026-06-18T00:00:00Z",
        "evidence": ["does/not/exist.py"]}})
    r = operator_worker_readiness(tmp_path)
    assert r["gates"]["bounded_cmd"]["status"] == "amber"


def test_declared_gate_unrecognized_status_defaults_amber(tmp_path):
    _write_config(tmp_path, declared={"bounded_cmd": {
        "status": "bogus", "declared_by": "op", "declared_at": "2026-06-18T00:00:00Z",
        "evidence": ["config.json"]}})
    r = operator_worker_readiness(tmp_path)
    assert r["gates"]["bounded_cmd"]["status"] == "amber"


def test_declared_gate_valid_evidence_honored(tmp_path):
    (tmp_path / "config.json")  # created below
    _write_config(tmp_path, declared={"bounded_cmd": {
        "status": "green", "declared_by": "op", "declared_at": "2026-06-18T00:00:00Z",
        "evidence": ["config.json"], "note": "ok"}})
    r = operator_worker_readiness(tmp_path)
    g = r["gates"]["bounded_cmd"]
    assert g["status"] == "green"
    assert g["declared_by"] == "op" and g["evidence"] == ["config.json"]


def test_cost_cap_unconfigured(tmp_path):
    _write_config(tmp_path)
    r = operator_worker_readiness(tmp_path)
    assert r["cost"]["cap_configured"] is False
    assert r["cost"]["cap_usd"] is None


def test_audit_gate_green_when_logs_present(tmp_path):
    _write_config(tmp_path)
    d = tmp_path / "outputs" / "operator_control"
    d.mkdir(parents=True)
    (d / "audit_log.jsonl").write_text("{}\n")
    (d / "worker_cost_log.jsonl").write_text(
        json.dumps({"cost_usd": 1.5}) + "\n")
    r = operator_worker_readiness(tmp_path)
    assert r["gates"]["audit"]["status"] == "green"
    assert r["cost"]["lifetime_usd"] == 1.5


def test_degraded_on_unreadable_root(tmp_path):
    # config.json is a directory -> json load raises -> degraded
    (tmp_path / "config.json").mkdir()
    r = operator_worker_readiness(tmp_path)
    assert r["observe_only"] is True
    assert r["overall_ready"] == "0/5"
    assert "error" in r
