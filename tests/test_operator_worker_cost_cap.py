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


import subprocess


def test_direct_path_includes_max_turns(tmp_path, monkeypatch):
    captured = {}
    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["timeout"] = kw.get("timeout")
        class P:
            returncode = 0
            stdout = '{"total_cost_usd": 0.1, "num_turns": 2, "result": "ok"}'
            stderr = ""
        return P()
    monkeypatch.setattr(wr.shutil, "which", lambda b: "/usr/bin/claude")
    monkeypatch.setattr(wr.subprocess, "run", fake_run)

    wr._run_direct_claude(tmp_path, "PROMPT", mode="safe_repair",
                          max_turns=40, max_run_seconds=1200)

    assert "--max-turns" in captured["argv"]
    assert captured["argv"][captured["argv"].index("--max-turns") + 1] == "40"
    assert captured["timeout"] == 1200


def test_direct_path_omits_rails_when_unset(tmp_path, monkeypatch):
    captured = {}
    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["timeout"] = kw.get("timeout", "MISSING")
        class P:
            returncode = 0
            stdout = '{"total_cost_usd": 0.1, "num_turns": 2, "result": "ok"}'
            stderr = ""
        return P()
    monkeypatch.setattr(wr.shutil, "which", lambda b: "/usr/bin/claude")
    monkeypatch.setattr(wr.subprocess, "run", fake_run)

    wr._run_direct_claude(tmp_path, "PROMPT", mode="diagnose")

    assert "--max-turns" not in captured["argv"]
    assert captured["timeout"] is None  # subprocess.run(timeout=None) == no timeout


def test_direct_path_timeout_returns_killed_dict(tmp_path, monkeypatch):
    def fake_run(argv, **kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kw.get("timeout"))
    monkeypatch.setattr(wr.shutil, "which", lambda b: "/usr/bin/claude")
    monkeypatch.setattr(wr.subprocess, "run", fake_run)

    out = wr._run_direct_claude(tmp_path, "PROMPT", mode="safe_repair",
                                max_turns=40, max_run_seconds=5)

    assert out["ok"] is False
    assert out["cost_usd"] == 0.0
    assert "cost-cap" in out["error"]


def test_container_timeout_tightens_not_loosens(tmp_path, monkeypatch):
    """max_run_seconds can only lower the container's existing timeout."""
    from operator_control import worker_workspace, worker_container
    cfg = {"workspace_root": str(tmp_path / "ws"), "run_as_user": "x",
           "credentials_dir": str(tmp_path / "creds"), "image_build_ts": 1.0,
           "resource_limits": {"timeout_seconds": 1800}}
    (tmp_path / "ws").mkdir()
    ws_dir = tmp_path / "ws" / "clone"
    ws_dir.mkdir()
    monkeypatch.setattr(worker_workspace, "create_isolated_workspace", lambda *a, **k: str(ws_dir))
    monkeypatch.setattr(worker_workspace, "destroy_workspace", lambda *a, **k: None)
    monkeypatch.setattr(worker_container, "build_container_launch_spec", lambda **k: ["true"])
    captured = {}
    def fake_run(argv, **kw):
        captured["timeout"] = kw.get("timeout")
        raise RuntimeError("stop after capture")  # we only need the timeout value
    monkeypatch.setattr(wr.subprocess, "run", fake_run)
    # worker_settings.json must exist for the copy2; it ships in the package dir.
    out = wr._run_via_container(ws_dir, "PROMPT", "safe_repair", cfg, str(tmp_path),
                                "wo_c", max_turns=40, max_run_seconds=600)
    assert captured["timeout"] == 600  # min(600, 1800)
    assert out["ok"] is False  # the RuntimeError is caught -> error-dict


def test_post_run_overage_flag_emitted(tmp_path, monkeypatch):
    _write_config(tmp_path, {"usd_per_run": 3.0, "usd_per_day": 100.0})
    from operator_control import work_orders as wo, worktree as wtree
    order = {"work_order_id": "wo_big", "status": "queued", "probe_id": "p",
             "skill_id": "s", "mode": "safe_repair", "requested_action": "x"}
    monkeypatch.setattr(wr, "autonomous_enabled", lambda root: True)
    monkeypatch.setattr(wo, "get_work_order", lambda root, wid: dict(order))
    monkeypatch.setattr(wo, "transition_work_order", lambda root, wid, **kw: {"status": kw.get("new_status")})
    # Stub _prepare to return a real temp worktree dir with the prompt file.
    wtdir = tmp_path / "wt"
    wtdir.mkdir()
    (wtdir / "WORKER_PROMPT.md").write_text("PROMPT", encoding="utf-8")
    monkeypatch.setattr(wr, "_prepare", lambda root, wid, actor: (dict(order), wtdir, "operator/wo_big"))
    monkeypatch.setattr(wr, "get_skill", lambda sid: None)
    monkeypatch.setattr(wr, "_production_snapshot", lambda root: {})
    monkeypatch.setattr(wr, "_production_impact", lambda root, snap: [])
    # The worker "ran" and reported a cost above the per-run cap.
    monkeypatch.setattr(wr, "_invoke_claude",
                        lambda *a, **k: {"ok": True, "cost_usd": 5.0, "num_turns": 3, "duration_ms": 1000})
    monkeypatch.setattr(wtree, "changed_files", lambda wt, base="main": [])
    monkeypatch.setattr(wr, "violating_paths", lambda diff: [])
    monkeypatch.setattr(wr, "_run_tests", lambda wt, tests: {"passed": True})
    monkeypatch.setattr(wr, "_write_report", lambda *a, **k: None)

    res = wr.run(tmp_path, "wo_big", actor="test")

    assert res["result"] == "completed"  # outcome unaffected by the flag
    events = [json.loads(l) for l in (tmp_path / "outputs/operator_control/audit_log.jsonl").read_text().splitlines() if l.strip()]
    flagged = [e for e in events if e["event_type"] == "worker_cost_cap_exceeded"]
    assert len(flagged) == 1
    assert flagged[0]["details"]["cost_usd"] == 5.0
    assert flagged[0]["details"]["cap_usd"] == 3.0
