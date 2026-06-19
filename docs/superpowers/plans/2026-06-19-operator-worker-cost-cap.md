# Operator Worker — Phase 2: Enforced Cost Cap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the operator worker an enforced operational spend ceiling (pre-dispatch daily gate + per-run hard rails + post-run overage flag), shipped configured-but-inert.

**Architecture:** Add a `cost_cap` config block; enforce it in three layers inside `operator_control/worker_runner.py` (day-gate before dispatch, `--max-turns`/timeout rails on both exec paths, post-run audit flag); correct the readiness `_cost` telemetry to a UTC-day window; extend the daily-check health line. No new modules — all changes land in existing files.

**Tech Stack:** Python 3.12, stdlib only (`subprocess`, `datetime`, `json`), pytest. The claude CLI's `--max-turns` flag and `subprocess.run(timeout=…)` are the hard rails.

## Global Constraints

- **Ships configured-but-inert:** `config.json` `operator_control.autonomous_worker.enabled` stays `false`. The cap only fires on the autonomous path; do NOT enable autonomous in this plan.
- **Additive / degrade-open:** a missing `cost_cap` block or any null/`<=0` knob means that limit is NOT enforced — behaviour identical to today.
- **Protected boundaries (do not touch):** `decision_engine.py`, any score semantics, `outputs/latest/decision_plan.json`, trade execution. The worker still never merges, never pushes; protected-path + production-impact guards unchanged.
- **Cost is telemetry, never a readiness gate** — the 5-gate contract in `operator_worker_readiness` is unchanged (still 5 gates; cost stays a separate line).
- **Test runner:** use `.venv/bin/python -m pytest` (the repo venv). Running the FULL suite mutates the protected `config/signal_registry.yaml` (known isolation bug) — for this plan, run targeted + the operator-control/readiness tests; only run the full suite at the end and restore `signal_registry.yaml` if it changed (preserve `default_weight: 0.4947`).
- **Commits:** stage explicit paths (never `git commit -am` — the working tree carries unrelated modified `outputs/*` + `config.json`). End commit messages with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

### Task 1: Config block + cap-reader helpers

Adds the `cost_cap` config block and the two pure reader helpers every later task consumes.

**Files:**
- Modify: `config.json` (add `operator_control.cost_cap`)
- Modify: `operator_control/worker_runner.py` (add `_cost_cap_cfg`, `_rec_date`, `_today_spend_usd` after `read_cost_log`, which ends at line 473)
- Test: `tests/test_operator_worker_cost_cap.py` (new)

**Interfaces:**
- Produces:
  - `_cost_cap_cfg(root) -> dict` with keys `usd_per_run`, `usd_per_day`, `max_turns_per_run`, `max_run_seconds`; each value is the positive number from config or `None` if absent/invalid. `max_turns_per_run` is coerced to `int`.
  - `_today_spend_usd(root) -> float` — sum of `cost_usd` over cost-log records whose `timestamp` parses to the current UTC date, rounded to 6 dp.
  - `_rec_date(rec) -> datetime.date | None` — parse a record's `timestamp` to a UTC date, `None` on missing/unparseable.

- [ ] **Step 1: Add the config block**

In `config.json`, inside the existing `"operator_control"` object (alongside `worker_container`, `autonomous_worker`, `readiness_declared`), add:

```jsonc
"cost_cap": {
  "usd_per_run": 3.0,
  "usd_per_day": 10.0,
  "max_turns_per_run": 40,
  "max_run_seconds": 1200,
  "note": "Phase 2 enforced cost cap. usd_* = money semantics (day-gate refuses dispatch / post-run AMBER flag); max_turns_per_run + max_run_seconds = hard rails that kill the child. Inert until autonomous_worker.enabled=true (Phase 4). Null/<=0 disables that knob."
}
```

Verify it parses:
Run: `.venv/bin/python -c "import json; print(json.load(open('config.json'))['operator_control']['cost_cap'])"`
Expected: prints the dict including `'usd_per_day': 10.0`.

- [ ] **Step 2: Write the failing test for the helpers**

Create `tests/test_operator_worker_cost_cap.py`:

```python
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
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_operator_worker_cost_cap.py -q`
Expected: FAIL — `AttributeError: module 'operator_control.worker_runner' has no attribute '_cost_cap_cfg'`.

- [ ] **Step 4: Implement the helpers**

In `operator_control/worker_runner.py`, immediately after `read_cost_log` (which `return`s at line 473, before `def run(`), insert:

```python
def _cost_cap_cfg(root) -> dict:
    """Read operator_control.cost_cap. Each value is the positive number from
    config, or None if absent/invalid (<=0). A missing block disables all caps
    (additive / degrade-open)."""
    try:
        cfg = json.loads((Path(root) / "config.json").read_text(encoding="utf-8"))
        block = (cfg.get("operator_control") or {}).get("cost_cap") or {}
    except Exception:
        block = {}

    def _pos(key):
        v = block.get(key)
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None

    mt = _pos("max_turns_per_run")
    return {
        "usd_per_run": _pos("usd_per_run"),
        "usd_per_day": _pos("usd_per_day"),
        "max_turns_per_run": int(mt) if mt else None,
        "max_run_seconds": _pos("max_run_seconds"),
    }


def _rec_date(rec):
    """Parse a cost-log record's UTC timestamp to a date, or None."""
    ts = rec.get("timestamp")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc).date()
    except (ValueError, AttributeError):
        return None


def _today_spend_usd(root) -> float:
    """Sum cost_usd over cost-log records timestamped on the current UTC day."""
    today = datetime.now(timezone.utc).date()
    return round(sum(float(c.get("cost_usd") or 0.0)
                     for c in read_cost_log(root)
                     if _rec_date(c) == today), 6)
```

(`json`, `datetime`, `timezone`, `Path`, and `read_cost_log` are all already in scope — see imports at lines 17-44 and `read_cost_log` at line 460.)

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_operator_worker_cost_cap.py -q`
Expected: PASS (5 passed).

- [ ] **Step 6: Compile + commit**

Run: `.venv/bin/python -m py_compile operator_control/worker_runner.py`
Expected: no output (exit 0).

```bash
git add config.json operator_control/worker_runner.py tests/test_operator_worker_cost_cap.py
git commit -m "feat(operator-worker): cost_cap config block + reader helpers

Adds operator_control.cost_cap (usd_per_run/_per_day + max_turns/max_run_seconds)
and the _cost_cap_cfg/_today_spend_usd helpers. Degrade-open: missing/<=0 knob
disables that limit. Inert until autonomous enabled.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Layer A — pre-dispatch day-gate + drain stop

Refuse dispatch (without claiming the order or creating a worktree) once today's UTC spend reaches the daily cap; defer the order rather than failing it; stop `drain` on a deferral.

**Files:**
- Modify: `operator_control/worker_runner.py` — `run()` (insert after the `autonomous_enabled` check at line 484-485, before `_prepare` at line 487) and `drain()` (line 640-645)
- Test: `tests/test_operator_worker_cost_cap.py` (append)

**Interfaces:**
- Consumes: `_cost_cap_cfg`, `_today_spend_usd` (Task 1); `audit_log.record_event` (signature: keyword-only `event_type, actor, work_order_id=None, …, details=None, safety_result=…`).
- Produces: `run()` returns `{"result": "deferred_cost_cap", "today_usd": float, "cap_usd": float, …}` when the day-gate trips; introduces the local `cap = _cost_cap_cfg(root)` variable in `run()` that Task 3 and Task 4 reuse. `drain()` breaks its loop on a `deferred_cost_cap` result.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_operator_worker_cost_cap.py`:

```python
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
    sentinel = {"_prepared": False}
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_operator_worker_cost_cap.py -q -k "day_gate or drain_stops"`
Expected: FAIL — `test_day_gate_defers_and_does_not_claim` raises the `AssertionError("must not prepare")` (gate not implemented yet), and `test_drain_stops_on_deferral` runs 5 times.

- [ ] **Step 3: Implement the day-gate in `run()`**

In `operator_control/worker_runner.py`, in `run()`, change:

```python
        if not autonomous_enabled(root):
            return scaffold(root, work_order_id, actor=actor)

        order, wt, branch = _prepare(root, work_order_id, actor)
```

to:

```python
        if not autonomous_enabled(root):
            return scaffold(root, work_order_id, actor=actor)

        # Pre-dispatch daily cost gate. Runs BEFORE _prepare so a deferred order
        # is never claimed and no worktree is created — it stays eligible and is
        # picked up once the UTC day rolls over.
        cap = _cost_cap_cfg(root)
        if cap["usd_per_day"] is not None:
            today_usd = _today_spend_usd(root)
            if today_usd >= cap["usd_per_day"]:
                audit_log.record_event(
                    root, event_type="worker_cost_cap_deferred", actor=actor,
                    work_order_id=work_order_id,
                    details={"today_usd": today_usd, "cap_usd": cap["usd_per_day"]},
                    safety_result="deferred: daily cost cap",
                )
                return {"work_order_id": work_order_id, "mode_of_runner": "autonomous",
                        "result": "deferred_cost_cap", "today_usd": today_usd,
                        "cap_usd": cap["usd_per_day"]}

        order, wt, branch = _prepare(root, work_order_id, actor)
```

- [ ] **Step 4: Implement the drain stop branch**

In `drain()`, change:

```python
        # Oldest-created first (list_work_orders is newest-first).
        results.append(run(root, elig[-1]["work_order_id"], actor=actor))
    return {"drained": len(results), "status": "ran", "results": results}
```

to:

```python
        # Oldest-created first (list_work_orders is newest-first).
        res = run(root, elig[-1]["work_order_id"], actor=actor)
        results.append(res)
        if isinstance(res, dict) and res.get("result") == "deferred_cost_cap":
            break  # daily cost cap reached — re-attempting would defer again
    return {"drained": len(results), "status": "ran", "results": results}
```

- [ ] **Step 5: Run to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_operator_worker_cost_cap.py -q -k "day_gate or drain_stops"`
Expected: PASS (3 passed).

- [ ] **Step 6: Compile + commit**

Run: `.venv/bin/python -m py_compile operator_control/worker_runner.py`
Expected: exit 0.

```bash
git add operator_control/worker_runner.py tests/test_operator_worker_cost_cap.py
git commit -m "feat(operator-worker): pre-dispatch daily cost-cap gate + drain stop

Refuse dispatch (before claim/worktree) once today's UTC spend reaches
usd_per_day; defer (leave eligible), audit worker_cost_cap_deferred, and
stop drain on the deferral. Introduces the run()-local cap variable.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Layer B — per-run hard rails (`--max-turns` + timeout)

Thread the turn/time ceilings through `_invoke_claude` to both execution paths so the child is hard-killed at the rail; preserve today's exact behaviour when the knobs are unset.

**Files:**
- Modify: `operator_control/worker_runner.py` — `_run_direct_claude` (174-213), `_run_via_container` (225-305), `_invoke_claude` (308-345 signature + both call sites), and the `_invoke_claude` call in `run()` (494-499)
- Test: `tests/test_operator_worker_cost_cap.py` (append)

**Interfaces:**
- Consumes: `cap["max_turns_per_run"]`, `cap["max_run_seconds"]` (the `cap` var from Task 2).
- Produces: `_run_direct_claude(worktree_path, prompt_md, mode="diagnose", max_turns=None, max_run_seconds=None)`, `_run_via_container(worktree_path, prompt_md, mode, cfg, root, work_order_id, max_turns=None, max_run_seconds=None)`, `_invoke_claude(worktree_path, prompt_md, mode="diagnose", root=".", work_order_id="", max_turns=None, max_run_seconds=None)`. When a knob is `None` the corresponding flag/timeout is omitted (identical to current behaviour).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_operator_worker_cost_cap.py`:

```python
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_operator_worker_cost_cap.py -q -k "direct_path or container_timeout"`
Expected: FAIL — `_run_direct_claude()` / `_run_via_container()` got an unexpected keyword argument `max_turns`.

- [ ] **Step 3: Modify `_run_direct_claude`**

Change the signature and the argv/subprocess block. The signature line:

```python
def _run_direct_claude(worktree_path, prompt_md: str, mode: str = "diagnose") -> dict:
```

becomes:

```python
def _run_direct_claude(worktree_path, prompt_md: str, mode: str = "diagnose",
                       max_turns: int | None = None,
                       max_run_seconds: float | None = None) -> dict:
```

Then change:

```python
    argv = [claude_bin, "-p", prompt_md, "--output-format", "json",
            "--settings", str(settings)]
    if mode == "safe_repair":
        argv += ["--permission-mode", "acceptEdits"]
    proc = subprocess.run(
        argv, cwd=str(worktree_path), capture_output=True, text=True, env=child_env,
    )
    return _parse_claude_json(proc)
```

to:

```python
    argv = [claude_bin, "-p", prompt_md, "--output-format", "json",
            "--settings", str(settings)]
    if max_turns:
        argv += ["--max-turns", str(max_turns)]
    if mode == "safe_repair":
        argv += ["--permission-mode", "acceptEdits"]
    try:
        proc = subprocess.run(
            argv, cwd=str(worktree_path), capture_output=True, text=True,
            env=child_env, timeout=max_run_seconds,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": "", "cost_usd": 0.0,
                "num_turns": None, "duration_ms": None, "result_text": None,
                "error": f"killed: cost-cap wall-clock ceiling ({max_run_seconds}s)"}
    return _parse_claude_json(proc)
```

(`subprocess.run(timeout=None)` is the stdlib default — no timeout — so unset preserves today's behaviour.)

- [ ] **Step 4: Modify `_run_via_container`**

Change the signature:

```python
def _run_via_container(worktree_path, prompt_md: str, mode: str, cfg: dict, root: str,
                       work_order_id: str) -> dict:
```

to:

```python
def _run_via_container(worktree_path, prompt_md: str, mode: str, cfg: dict, root: str,
                       work_order_id: str, max_turns: int | None = None,
                       max_run_seconds: float | None = None) -> dict:
```

Add `--max-turns` to the container claude argv — change:

```python
        claude_argv = ["claude", "-p", prompt_md, "--output-format", "json",
                       "--settings", "/work/.worker_settings.json"]
        if mode == "safe_repair":
            claude_argv += ["--permission-mode", "acceptEdits"]
```

to:

```python
        claude_argv = ["claude", "-p", prompt_md, "--output-format", "json",
                       "--settings", "/work/.worker_settings.json"]
        if max_turns:
            claude_argv += ["--max-turns", str(max_turns)]
        if mode == "safe_repair":
            claude_argv += ["--permission-mode", "acceptEdits"]
```

Tighten (never loosen) the container timeout — change:

```python
        argv = ["runuser", "-u", cfg["run_as_user"], "--", *spec]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=cfg["resource_limits"]["timeout_seconds"])
        except Exception as exc:
            return {"ok": False, "execution_mode": "container", "isolated": False,
                    "stdout": "", "stderr": "",
                    "error": f"container startup failed: {exc}",
                    "cost_usd": 0.0, "num_turns": None, "duration_ms": None, "result_text": None}
```

to:

```python
        argv = ["runuser", "-u", cfg["run_as_user"], "--", *spec]
        container_timeout = cfg["resource_limits"]["timeout_seconds"]
        if max_run_seconds:
            container_timeout = min(max_run_seconds, container_timeout)
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=container_timeout)
        except Exception as exc:
            return {"ok": False, "execution_mode": "container", "isolated": False,
                    "stdout": "", "stderr": "",
                    "error": f"container startup failed (or cost-cap timeout): {exc}",
                    "cost_usd": 0.0, "num_turns": None, "duration_ms": None, "result_text": None}
```

- [ ] **Step 5: Modify `_invoke_claude` (signature + both forwards)**

Change the signature:

```python
def _invoke_claude(worktree_path, prompt_md: str, mode: str = "diagnose",
                   root: str = ".", work_order_id: str = "") -> dict:
```

to:

```python
def _invoke_claude(worktree_path, prompt_md: str, mode: str = "diagnose",
                   root: str = ".", work_order_id: str = "",
                   max_turns: int | None = None,
                   max_run_seconds: float | None = None) -> dict:
```

Change the direct forward:

```python
        out = _run_direct_claude(worktree_path, prompt_md, mode)
        out["execution_mode"] = "direct"
        out["isolated"] = False
        return out
```

to:

```python
        out = _run_direct_claude(worktree_path, prompt_md, mode,
                                 max_turns=max_turns, max_run_seconds=max_run_seconds)
        out["execution_mode"] = "direct"
        out["isolated"] = False
        return out
```

Change the container forward (final return of the function):

```python
    return _run_via_container(worktree_path, prompt_md, mode, cfg, root, work_order_id)
```

to:

```python
    return _run_via_container(worktree_path, prompt_md, mode, cfg, root, work_order_id,
                              max_turns=max_turns, max_run_seconds=max_run_seconds)
```

- [ ] **Step 6: Pass the rails from `run()`**

In `run()`, change the `_invoke_claude` call:

```python
        worker = _invoke_claude(
            wt, (wt / "WORKER_PROMPT.md").read_text(encoding="utf-8"),
            mode=order.get("mode", "diagnose"),
            root=str(root),
            work_order_id=work_order_id,
        )
```

to:

```python
        worker = _invoke_claude(
            wt, (wt / "WORKER_PROMPT.md").read_text(encoding="utf-8"),
            mode=order.get("mode", "diagnose"),
            root=str(root),
            work_order_id=work_order_id,
            max_turns=cap["max_turns_per_run"],
            max_run_seconds=cap["max_run_seconds"],
        )
```

(`cap` is in scope from Task 2's day-gate block.)

- [ ] **Step 7: Run the new tests + the existing runner suite**

Run: `.venv/bin/python -m pytest tests/test_operator_worker_cost_cap.py tests/test_operator_worker_runner.py -q`
Expected: PASS (cost-cap tests + the pre-existing runner tests all green — confirms the signature changes didn't break existing callers).

- [ ] **Step 8: Compile + commit**

Run: `.venv/bin/python -m py_compile operator_control/worker_runner.py`
Expected: exit 0.

```bash
git add operator_control/worker_runner.py tests/test_operator_worker_cost_cap.py
git commit -m "feat(operator-worker): per-run cost rails (--max-turns + timeout)

Thread max_turns/max_run_seconds through _invoke_claude to both exec paths.
Direct path gains --max-turns + subprocess timeout (TimeoutExpired -> killed
error-dict); container path adds --max-turns and tightens (never loosens) its
timeout to min(max_run_seconds, resource_limits.timeout_seconds). Unset knobs
preserve current behaviour.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Layer C — post-run per-run overage flag

After a run's cost is recorded, audit any run whose cost exceeded `usd_per_run`. The order's pass/fail outcome is unaffected (money is already spent; the rails in Task 3 prevent large overages — this surfaces any that slip through).

**Files:**
- Modify: `operator_control/worker_runner.py` — `run()`, immediately after the `_record_cost(...)` call (502-503)
- Test: `tests/test_operator_worker_cost_cap.py` (append)

**Interfaces:**
- Consumes: the `cap` var, `_record_cost`'s return value `rec` (a dict with `cost_usd`), `audit_log.record_event`.
- Produces: a `worker_cost_cap_exceeded` audit event when `rec["cost_usd"] > cap["usd_per_run"]`. No change to the order transition.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_operator_worker_cost_cap.py`. This drives `run()` end-to-end with the heavy collaborators stubbed so only the overage-flag path is exercised:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_operator_worker_cost_cap.py -q -k overage`
Expected: FAIL — no `worker_cost_cap_exceeded` event (`len(flagged) == 1` assertion fails, `0 != 1`).

- [ ] **Step 3: Implement the overage flag**

In `run()`, change:

```python
        # Cost is incurred regardless of outcome — log it once, immediately,
        # in the operational ledger (NOT the FMP/AI decision budget).
        _record_cost(root, order, worker,
                     status=("ok" if worker.get("ok") else "worker_error"))
```

to:

```python
        # Cost is incurred regardless of outcome — log it once, immediately,
        # in the operational ledger (NOT the FMP/AI decision budget).
        rec = _record_cost(root, order, worker,
                           status=("ok" if worker.get("ok") else "worker_error"))
        # Per-run overage flag: the rails (max_turns/timeout) prevent large
        # overages; this surfaces any run that still exceeded the per-run cap.
        # Does NOT change the order's pass/fail outcome — money is already spent.
        if cap["usd_per_run"] is not None and rec["cost_usd"] > cap["usd_per_run"]:
            audit_log.record_event(
                root, event_type="worker_cost_cap_exceeded", actor=actor,
                work_order_id=work_order_id,
                details={"cost_usd": rec["cost_usd"], "cap_usd": cap["usd_per_run"]},
                safety_result="flagged: per-run cost cap exceeded",
            )
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_operator_worker_cost_cap.py -q -k overage`
Expected: PASS (1 passed).

- [ ] **Step 5: Compile + commit**

Run: `.venv/bin/python -m py_compile operator_control/worker_runner.py`
Expected: exit 0.

```bash
git add operator_control/worker_runner.py tests/test_operator_worker_cost_cap.py
git commit -m "feat(operator-worker): post-run per-run cost-cap overage flag

Audit worker_cost_cap_exceeded when a recorded run cost exceeds usd_per_run.
Does not change the order outcome — surfaces overages the rails didn't catch.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Readiness `_cost` — nested cap + UTC-day window

Correct the telemetry: read the cap from the nested `cost_cap` block and compute `cap_pct` from today's UTC spend (not lifetime), adding a `today_usd` field. This is the deterministic signal the daily-check reads.

**Files:**
- Modify: `portfolio_automation/operator_worker_readiness.py` — imports (line 11-13) + `_cost` (139-159)
- Test: `tests/test_operator_worker_cost_cap.py` (append)

**Interfaces:**
- Produces: `_cost(root, oc_cfg)` returns `{"lifetime_usd": float, "today_usd": float, "cap_usd": float|None, "cap_pct": float|None, "cap_configured": bool}`. `cap_pct = today_usd / cap_usd * 100`. Reads `oc_cfg["cost_cap"]["usd_per_day"]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_operator_worker_cost_cap.py`:

```python
from portfolio_automation import operator_worker_readiness as owr


def test_readiness_cost_today_window(tmp_path):
    oc_cfg = {"cost_cap": {"usd_per_day": 10.0}}
    p = tmp_path / "outputs" / "operator_control" / "worker_cost_log.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    with p.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"timestamp": now.isoformat(), "cost_usd": 2.0}) + "\n")
        fh.write(json.dumps({"timestamp": now.isoformat(), "cost_usd": 3.0}) + "\n")
        fh.write(json.dumps({"timestamp": (now - timedelta(days=1)).isoformat(), "cost_usd": 40.0}) + "\n")

    cost = owr._cost(tmp_path, oc_cfg)

    assert cost["cap_configured"] is True
    assert cost["cap_usd"] == 10.0
    assert cost["today_usd"] == pytest.approx(5.0)
    assert cost["lifetime_usd"] == pytest.approx(45.0)
    assert cost["cap_pct"] == pytest.approx(50.0)  # 5.0 / 10.0, NOT lifetime


def test_readiness_cost_no_cap_block(tmp_path):
    cost = owr._cost(tmp_path, {})
    assert cost["cap_configured"] is False
    assert cost["cap_usd"] is None
    assert cost["cap_pct"] is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_operator_worker_cost_cap.py -q -k readiness_cost`
Expected: FAIL — current `_cost` has no `today_usd` key (`KeyError`) and reads the flat (now-absent) `cost_cap_usd_per_day`, so `cap_configured` is False even with the nested block.

- [ ] **Step 3: Add the datetime import**

In `portfolio_automation/operator_worker_readiness.py`, the import block (lines 11-13) currently reads:

```python
import json
import os
import time
```

Change to:

```python
import json
import os
import time
from datetime import datetime, timezone
```

- [ ] **Step 4: Rewrite `_cost`**

Replace the whole `_cost` function (lines 139-159):

```python
def _cost(root: Path, oc_cfg: dict[str, Any]) -> dict[str, Any]:
    lifetime = 0.0
    today_total = 0.0
    today = datetime.now(timezone.utc).date()
    p = root / "outputs" / "operator_control" / "worker_cost_log.jsonl"
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                c = float(rec.get("cost_usd") or 0.0)
            except (ValueError, json.JSONDecodeError):
                continue
            lifetime += c
            ts = rec.get("timestamp")
            if ts:
                try:
                    d = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc).date()
                    if d == today:
                        today_total += c
                except (ValueError, AttributeError):
                    pass
    except OSError:
        pass
    cap = (oc_cfg.get("cost_cap") or {}).get("usd_per_day")
    cap_configured = isinstance(cap, (int, float)) and not isinstance(cap, bool) and cap > 0
    cap_pct = round(today_total / cap * 100, 1) if cap_configured else None
    return {"lifetime_usd": round(lifetime, 4),
            "today_usd": round(today_total, 4),
            "cap_usd": cap if cap_configured else None,
            "cap_pct": cap_pct, "cap_configured": bool(cap_configured)}
```

- [ ] **Step 5: Run the new test + the existing readiness suite**

Run: `.venv/bin/python -m pytest tests/test_operator_worker_cost_cap.py -q -k readiness_cost && .venv/bin/python -m pytest -q -k "operator_worker_readiness or operator_worker_view"`
Expected: PASS. If a pre-existing readiness test asserted the old `_cost` shape (no `today_usd`, lifetime-based `cap_pct`), update that assertion to the new shape and note it in the commit.

- [ ] **Step 6: Compile + commit**

Run: `.venv/bin/python -m py_compile portfolio_automation/operator_worker_readiness.py`
Expected: exit 0.

```bash
git add portfolio_automation/operator_worker_readiness.py tests/test_operator_worker_cost_cap.py
git commit -m "fix(operator-worker): readiness _cost reads nested cap + UTC-day window

cap_pct now = today's UTC spend / usd_per_day (was lifetime/cap, which read
>100% forever). Reads operator_control.cost_cap.usd_per_day; adds today_usd.
Flows through operator_worker_view (passthrough) to the daily check.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Daily-check health line (Analysis+Health pairing)

Satisfy the repo's Analysis+Health requirement: the daily check must surface cap utilization, AMBER at ≥80%, and the new audit events. This is a markdown skill edit (no code); the deterministic signal it consumes is tested in Task 5.

**Files:**
- Modify: `.claude/commands/daily-tool-analysis.md` — artifact-read entry (line 66), AMBER triggers (line 198), and the operator-control body line 6g (line 328)

**Interfaces:**
- Consumes: `operator_worker_view(root)["cost"]` = `{lifetime_usd, today_usd, cap_usd, cap_pct, cap_configured}` (Task 5); `worker_cost_cap_deferred` / `worker_cost_cap_exceeded` audit events.

- [ ] **Step 1: Extend the artifacts-read entry (line 66)**

Append to the end of the line-66 audit-log bullet (after "never blocks the decision core)"):

```markdown
 Also fold the cost-cap audit events: `worker_cost_cap_deferred` (a run was deferred because today's spend hit `usd_per_day` — informational, the order stays eligible) and `worker_cost_cap_exceeded` (a run breached the per-run cap — AMBER, verify the rails fired).
```

- [ ] **Step 2: Update the operator-control body line (6g, line 328)**

Change the cost segment of the 6g template from:

```
cost ${lifetime_usd}{/{cap_usd} ({cap_pct}%) if cap_configured else ' (uncapped)'}
```

to (show today's spend as the numerator against the daily cap, keep lifetime as context, and add the events):

```
cost today ${today_usd}{/{cap_usd} ({cap_pct}% of daily) if cap_configured else ' (uncapped)'} · lifetime ${lifetime_usd}{ · deferred {n} · over-cap {m} if any cost-cap events today}
```

And change the 6g AMBER clause from:

```
AMBER on `quarantined ≥ 1` or stale-run; readiness `<5/5` is reported, not alerted
```

to:

```
AMBER on `quarantined ≥ 1`, stale-run, `cap_pct ≥ 80` (daily cost cap nearly exhausted), or any `worker_cost_cap_exceeded` event today; readiness `<5/5` is reported, not alerted
```

- [ ] **Step 3: Add the AMBER trigger to the dispatch section (line 198)**

In the AMBER-triggers list, append to the operator-control AMBER bullet (the one ending "inspect the worktree"):

```markdown
 OR `operator_cost_cap_pct ≥ 80` (daily operator-worker cost cap ≥80% exhausted — runs will defer once it reaches 100%) OR a `worker_cost_cap_exceeded` event today (a run breached the per-run rail; confirm the `--max-turns`/timeout rails are configured). Cost-cap AMBER is observe-only and never RED.
```

- [ ] **Step 4: Sanity-check the markdown renders (no code test)**

Run: `grep -n "cap_pct ≥ 80\|today_usd\|worker_cost_cap_exceeded\|worker_cost_cap_deferred" .claude/commands/daily-tool-analysis.md`
Expected: matches in the line-66 entry, the 6g line, and the AMBER-triggers section (≥4 lines).

- [ ] **Step 5: Commit**

```bash
git add .claude/commands/daily-tool-analysis.md
git commit -m "feat(daily-check): surface operator-worker cost-cap utilization + events

Daily operator-control line now shows today_usd/cap_usd (cap_pct% of daily) and
folds worker_cost_cap_deferred/_exceeded events. AMBER at cap_pct>=80 or any
per-run over-cap event today. Observe-only, never RED. Pairs Phase 2 cost cap
with its health check (Analysis+Health requirement).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Full-suite verification + docs sync

**Files:**
- Modify: `docs/operator_worker_hardening_spec.md` (mark Phase 2 done), `docs/operator_control_worker_runner.md` (document the cap) — via the portfolio-docs skill or directly.

- [ ] **Step 1: Run the operator-control + readiness suites**

Run: `.venv/bin/python -m pytest tests/test_operator_worker_cost_cap.py tests/test_operator_worker_runner.py -q -k "" && .venv/bin/python -m pytest -q -k "operator_worker or operator_control"`
Expected: all PASS.

- [ ] **Step 2: Run the full suite (note the isolation caveat)**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS except the 3 known pre-existing failures (tuning_proposals 0.45-vs-0.4947, run_loop disabled-vs-oos_immature) — confirm no NEW failures.

- [ ] **Step 3: Restore the protected registry if the full suite mutated it**

Run: `git diff --stat config/signal_registry.yaml`
If changed: `git checkout config/signal_registry.yaml` and confirm `default_weight: 0.4947` is intact. Also `git checkout` any fresh `config/`/`history` snapshots the suite wrote.

- [ ] **Step 4: Update docs**

Mark Phase 2 complete in `docs/operator_worker_hardening_spec.md` (the precondition-3 table row + the "two real blockers" line — cost cap is now enforced) and add a short "Cost cap" section to `docs/operator_control_worker_runner.md` describing the three layers and the config knobs. Use the `/portfolio-docs` skill or edit directly.

- [ ] **Step 5: Commit docs**

```bash
git add docs/operator_worker_hardening_spec.md docs/operator_control_worker_runner.md
git commit -m "docs(operator-worker): mark Phase 2 cost cap enforced

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Notes for the implementer

- **Do not push or merge.** Per repo policy, stop at the production boundary and hand the push to the operator. The VPS dashboard service serves stale code until restarted — that's the operator's call too.
- **The `cap` variable lifetime:** Task 2 introduces `cap = _cost_cap_cfg(root)` near the top of `run()`'s try-block; Tasks 3 and 4 read it later in the same function. Keep it where Task 2 puts it.
- **Why the tests stub so heavily:** `run()` orchestrates worktrees, git, the claude subprocess, and work-order state. The cost-cap logic is a thin slice through that; stubbing the collaborators keeps each test on exactly one new behaviour. This matches the existing `tests/test_operator_worker_runner.py` style.
