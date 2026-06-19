import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from operator_control import worker_runner as wr


def _write_config(root: Path, cost_cap: dict | None):
    oc = {"autonomous_worker": {"enabled": True}}
    if cost_cap is not None:
        oc["cost_cap"] = cost_cap
    (root / "config.json").write_text(json.dumps({"operator_control": oc}), encoding="utf-8")


def _cost_log_path(root: Path) -> Path:
    p = root / "outputs" / "operator_control" / "worker_cost_log.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _append_cost(root: Path, cost_usd: float, when: datetime):
    rec = {"timestamp": when.isoformat(), "work_order_id": "wo_x", "cost_usd": cost_usd}
    with _cost_log_path(root).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def test_cost_cap_cfg_reads_block(tmp_path):
    _write_config(tmp_path, {"usd_per_run": 3.0, "usd_per_day": 10.0,
                             "max_turns_per_run": 40, "max_run_seconds": 1200})
    cap = wr._cost_cap_cfg(tmp_path)
    assert cap == {"usd_per_run": 3.0, "usd_per_day": 10.0,
                   "max_turns_per_run": 40, "max_run_seconds": 1200}


def test_cost_cap_cfg_missing_block_all_none(tmp_path):
    _write_config(tmp_path, None)
    cap = wr._cost_cap_cfg(tmp_path)
    assert cap == {"usd_per_run": None, "usd_per_day": None,
                   "max_turns_per_run": None, "max_run_seconds": None}


def test_cost_cap_cfg_zero_or_negative_is_none(tmp_path):
    _write_config(tmp_path, {"usd_per_run": 0, "usd_per_day": -1,
                             "max_turns_per_run": 0, "max_run_seconds": None})
    cap = wr._cost_cap_cfg(tmp_path)
    assert all(v is None for v in cap.values())


def test_today_spend_sums_only_today(tmp_path):
    _write_config(tmp_path, {"usd_per_day": 10.0})
    now = datetime.now(timezone.utc)
    _append_cost(tmp_path, 2.5, now)
    _append_cost(tmp_path, 1.0, now)
    _append_cost(tmp_path, 99.0, now - timedelta(days=1))  # yesterday — excluded
    assert wr._today_spend_usd(tmp_path) == pytest.approx(3.5)


def test_today_spend_empty_log_is_zero(tmp_path):
    _write_config(tmp_path, {"usd_per_day": 10.0})
    assert wr._today_spend_usd(tmp_path) == 0.0


def _seed_order(root: Path, monkeypatch, status="queued"):
    """Make worker_runner think one eligible order exists, autonomous is on,
    and capture any transitions. Returns the transitions list."""
    from operator_control import work_orders as wo
    order = {"work_order_id": "wo_test", "status": status,
             "probe_id": "p1", "skill_id": "s1", "mode": "safe_repair",
             "requested_action": "fix"}
    transitions = []
    monkeypatch.setattr(wr, "autonomous_enabled", lambda root: True)
    monkeypatch.setattr(wo, "get_work_order", lambda root, wid: dict(order))
    monkeypatch.setattr(wo, "list_work_orders", lambda root: [dict(order)])
    monkeypatch.setattr(wo, "transition_work_order",
                        lambda root, wid, **kw: transitions.append(kw) or {"status": kw.get("new_status")})
    return transitions


def test_day_gate_defers_and_does_not_claim(tmp_path, monkeypatch):
    _write_config(tmp_path, {"usd_per_day": 10.0})
    _append_cost(tmp_path, 10.0, datetime.now(timezone.utc))  # already at cap
    transitions = _seed_order(tmp_path, monkeypatch)
    # If _prepare were reached it would create a worktree; make that explode so
    # the test fails loudly if the gate doesn't short-circuit.
    monkeypatch.setattr(wr, "_prepare", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not prepare")))

    res = wr.run(tmp_path, "wo_test", actor="test")

    assert res["result"] == "deferred_cost_cap"
    assert res["cap_usd"] == 10.0
    assert transitions == []  # order never claimed/transitioned
    events = [json.loads(l) for l in (tmp_path / "outputs/operator_control/audit_log.jsonl").read_text().splitlines() if l.strip()]
    assert any(e["event_type"] == "worker_cost_cap_deferred" for e in events)


def test_day_gate_ignores_yesterday_spend(tmp_path, monkeypatch):
    _write_config(tmp_path, {"usd_per_day": 10.0})
    _append_cost(tmp_path, 50.0, datetime.now(timezone.utc) - timedelta(days=1))
    _seed_order(tmp_path, monkeypatch)
    monkeypatch.setattr(wr, "_prepare",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("reached prepare")))
    # Day-gate should NOT trip (yesterday excluded) -> it proceeds to _prepare,
    # which we stubbed to raise: a RuntimeError (not the deferral) proves the gate passed.
    with pytest.raises(RuntimeError, match="reached prepare"):
        wr.run(tmp_path, "wo_test", actor="test")


def test_drain_stops_on_deferral(tmp_path, monkeypatch):
    _write_config(tmp_path, {"usd_per_day": 10.0})
    monkeypatch.setattr(wr, "autonomous_enabled", lambda root: True)
    from operator_control import work_orders as wo
    monkeypatch.setattr(wo, "list_work_orders",
                        lambda root: [{"work_order_id": "wo_a", "status": "queued"}])
    calls = {"n": 0}
    def fake_run(root, wid, actor="cron"):
        calls["n"] += 1
        return {"result": "deferred_cost_cap"}
    monkeypatch.setattr(wr, "run", fake_run)
    monkeypatch.setattr(wr, "_eligible", lambda o: True)

    out = wr.drain(tmp_path, max_orders=5, actor="cron")

    assert calls["n"] == 1  # stopped after the first deferral, did not loop to 5
    assert out["drained"] == 1
