# Operator Worker — Phase 2: Enforced Cost Cap (design)

Status: **approved-pending-spec-review** · Date: 2026-06-19
Author: brainstorming session (continuation of operator-worker hardening)
Predecessors: `docs/operator_worker_hardening_spec.md` (4-phase plan),
`2026-06-18-operator-worker-phase1-container-isolation-design.md` (Phase 1),
`2026-06-18-operator-worker-control-surface-design.md` (GUI surface).

## Why

The operator worker is the one component that can change *code*, so it is held to a
stricter bar than the advisory lanes and stays disabled until all five owner-set
preconditions are green (see `docs/operator_worker_hardening_spec.md`). Phase 1
(OS isolation / container) shipped inert. **Phase 2 closes the precondition-3
adjunct gap: the worker's operational spend is *recorded* (`worker_cost_log.jsonl`)
but never *bounded*.** A runaway headless session — or a `drain` loop over many
orders — can spend without ceiling.

This phase adds an **enforced** cost cap. It ships **configured-but-inert**:
`operator_control.autonomous_worker.enabled` stays `false`; the cap only takes
effect on the autonomous path, which Phase 4 enables. Adding the cap does not, by
itself, change any runtime behaviour today.

## Scope / non-goals

- **In scope:** config keys, enforcement in `operator_control/worker_runner.py`,
  readiness `_cost` correction, daily-check health line, tests.
- **Out of scope (unchanged):** `decision_engine.py`, any score semantics, the
  promotion governance lanes, the FMP/AI *decision* budget (this is a SEPARATE
  operational ledger), trade execution (the worker never executes trades).
- The worker remains create/repair-only and never merges, pushes, or touches
  `outputs/latest/decision_plan.json`.

## Design decisions (resolved during brainstorming)

1. **Per-run kill mechanism = timeout + max-turns proxy** (not a streaming
   cost-kill). Cost is monotonic in wall-clock time and in turns, so a
   `subprocess` timeout and `--max-turns` are robust hard ceilings that kill the
   child without a fragile `stream-json` + `Popen` incremental-cost parser.
2. **Explicit independent knobs**, not a derived USD→turns/seconds conversion. The
   USD caps keep pure money semantics; turns/seconds are separate hard rails the
   operator sets directly. No opaque, pricing-drift-prone conversion constant.
3. **Day-gate refusal defers, leaves the order eligible** (does not mark it
   failed). The worker did nothing wrong; the order naturally retries once the UTC
   day rolls over. `drain()` treats the deferral as a stop signal.
4. **Accounting window = current UTC calendar day** for both the day-gate and the
   displayed `cap_pct`. Matches the `usd_per_day` wording; resets at UTC midnight.

## Config

New nested block in `config.json` under `operator_control` (defaults intentionally
low):

```jsonc
"operator_control": {
  "cost_cap": {
    "usd_per_run": 3.0,        // money semantics: post-run AMBER flag if a run exceeds this
    "usd_per_day": 10.0,       // money semantics: refuse dispatch if today's UTC spend >= this
    "max_turns_per_run": 40,   // hard rail -> claude --max-turns (kills the child)
    "max_run_seconds": 1200    // hard rail -> subprocess timeout (kills the child)
  }
}
```

A missing `cost_cap` block, or a key that is null / <= 0, means **that specific
limit is not enforced** (degrade-open per-knob), and readiness reports
`cap_configured: false`. This keeps the change additive: a config without the block
behaves exactly as today.

## Enforcement — `operator_control/worker_runner.py`

A small helper reads + normalizes the cap once per run:

```python
def _cost_cap_cfg(root) -> dict:
    """Return {usd_per_run, usd_per_day, max_turns_per_run, max_run_seconds},
    each None if absent/invalid. Reads operator_control.cost_cap."""
```

Helper for today's spend (shared with readiness semantics, but runner reads its own
log):

```python
def _today_spend_usd(root) -> float:
    today = datetime.now(timezone.utc).date()
    return sum(float(c.get("cost_usd") or 0.0)
               for c in read_cost_log(root)
               if _rec_date(c) == today)   # _rec_date parses c["timestamp"]
```

Three enforcement layers inside `run()`:

### Layer A — pre-dispatch day-gate (before `_prepare`)
Placed **before** `_prepare` so a deferred order is never claimed and no worktree is
created. After the `autonomous_enabled(root)` check:

```python
cap = _cost_cap_cfg(root)
if cap["usd_per_day"] and _today_spend_usd(root) >= cap["usd_per_day"]:
    audit_log.record_event(root, event_type="worker_cost_cap_deferred",
        actor=actor, work_order_id=work_order_id,
        details={"today_usd": ..., "cap_usd": cap["usd_per_day"]},
        safety_result="deferred: daily cost cap")
    return {"work_order_id": work_order_id, "mode_of_runner": "autonomous",
            "result": "deferred_cost_cap", "today_usd": ..., "cap_usd": cap["usd_per_day"]}
```

The order is **not** transitioned — because the gate runs before `_prepare`, the
order is never claimed; it stays in its current eligible state (e.g. `created`) and
is picked up on a later run.

`drain()` gains a stop branch:

```python
res = run(root, elig[-1]["work_order_id"], actor=actor)
results.append(res)
if res.get("result") == "deferred_cost_cap":
    break   # day cap reached; re-attempting the same order would also defer
```

### Layer B — per-run hard rails (thread caps to both exec paths)
`_invoke_claude(...)` gains `max_turns` + `max_run_seconds` params, forwarded to:

- **`_run_direct_claude`**: append `--max-turns <n>` to argv when set; wrap
  `subprocess.run(..., timeout=max_run_seconds)` in a `try/except
  subprocess.TimeoutExpired` returning the standard error-dict
  `{"ok": False, ..., "cost_usd": 0.0, "error": "killed: cost-cap wall-clock ceiling (<n>s)"}`.
- **`_run_via_container`**: append `--max-turns <n>` to `claude_argv` when set;
  set the subprocess timeout to `min(max_run_seconds, resource_limits.timeout_seconds)`
  when `max_run_seconds` is set (the cap can only **tighten**, never loosen, the
  existing container bound). Its existing `except Exception` already maps a
  `TimeoutExpired` to an `ok:False` error-dict; the error text is clarified.

When a cap knob is None, the corresponding flag/timeout is omitted — behaviour is
identical to today.

### Layer C — post-run overage flag (after `_record_cost`)
```python
rec = _record_cost(root, order, worker, status=...)
if cap["usd_per_run"] and rec["cost_usd"] > cap["usd_per_run"]:
    audit_log.record_event(root, event_type="worker_cost_cap_exceeded",
        actor=actor, work_order_id=work_order_id,
        details={"cost_usd": rec["cost_usd"], "cap_usd": cap["usd_per_run"]},
        safety_result="flagged: per-run cost cap exceeded")
```
This does **not** change the order's pass/fail outcome — the work is done and the
money is spent; the cap rails (Layer B) are what *prevent* large overages, and this
flag makes any that slip through visible.

## Readiness — `portfolio_automation/operator_worker_readiness.py`

`_cost(root, oc_cfg)` is corrected:

- Read the cap from the nested block: `cap = (oc_cfg.get("cost_cap") or {}).get("usd_per_day")`.
- Compute **today's** spend (UTC calendar day) for `cap_pct`, not lifetime:
  `cap_pct = round(day_spend / cap * 100, 1)`.
- Keep `lifetime_usd` as a separate informational field; add `today_usd`.

Result dict becomes:
`{lifetime_usd, today_usd, cap_usd, cap_pct, cap_configured}`.
Cost remains a telemetry line, **never a gate** (preserves the existing contract).

## Daily-check health pairing — `.claude/commands/daily-tool-analysis.md`

Extend the existing operator-control line (it already reports open/failed/quarantined
counts + worker mode):

- Read `operator_worker_readiness().cost` → report `today_usd / cap_usd (cap_pct%)`.
- **AMBER at `cap_pct >= 80`**; GREEN below.
- Surface today's `worker_cost_cap_deferred` / `worker_cost_cap_exceeded` audit
  events (count). A `worker_cost_cap_exceeded` event is **AMBER** (a run breached the
  per-run rail — verify the rails).
- This satisfies the repo Analysis+Health pairing requirement (daily cadence,
  developer + process-analyst lens).

## Tests — `tests/test_operator_worker_cost_cap.py`

1. **Day-gate defers, leaves eligible**: seed today's cost log >= `usd_per_day`;
   `run()` returns `result=deferred_cost_cap`; order status unchanged; no worktree
   created; `worker_cost_cap_deferred` audit event present.
2. **Day-gate ignores prior-day spend**: cost log entries dated yesterday do not
   trip today's gate.
3. **drain stops on deferral**: with day cap tripped, `drain()` returns after one
   deferred result and does not loop.
4. **argv carries `--max-turns`**: monkeypatch `subprocess.run`; assert
   `--max-turns 40` present in the direct-path argv; assert it is **absent** when the
   knob is None.
5. **timeout → killed error-dict**: monkeypatch `subprocess.run` to raise
   `TimeoutExpired`; assert `_run_direct_claude` returns `ok:False` with the
   cost-cap killed error and `cost_usd == 0.0`.
6. **container timeout tightening**: assert the container subprocess timeout is
   `min(max_run_seconds, resource_limits.timeout_seconds)`.
7. **post-run overage flag**: stub a worker result with `cost_usd > usd_per_run`;
   assert a `worker_cost_cap_exceeded` audit event and that the order's pass/fail
   outcome is unaffected.
8. **readiness nested cap + today window**: config with `cost_cap.usd_per_day`;
   cost log with today + yesterday entries; assert `cap_configured:true`,
   `cap_pct` computed from today's spend only, `today_usd` and `lifetime_usd` both
   present.

Run targeted first, then the relevant suites:
`python -m pytest tests/test_operator_worker_cost_cap.py tests/test_operator_worker_runner.py -q`
then the operator-control + readiness tests, then the full suite.

## Invariants preserved

- `observe_only`/gated: `autonomous_worker.enabled` stays `false`; the cap is inert
  until Phase 4. No change to today's behaviour for a config without the block.
- Never merges, never pushes; protected-path + production-impact guards untouched.
- No `decision_engine.py` / score-semantics changes. Additive + reversible
  (delete the `cost_cap` block to disable).
- Cost stays a telemetry line, never a readiness gate.

## Out of scope (deferred to Phase 3)
- Explicit applied-change rollback + quarantine-review/salvage path
  (`cancel`/`archive` terminal transition). Phase 3 per the hardening spec.
