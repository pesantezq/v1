# Operator-Worker Control Surface — Design Spec

Date: 2026-06-18
Status: approved (brainstorm) → pending implementation plan
Route: `GET /dashboard/operator` (+ two action endpoints)
Lens: developer · Cadence: GUI (live) + daily-check health line

## Purpose

Give the operator a single dashboard home for the **operator worker** — the
component named (2026-06-18) as the next major dev milestone ("controlled
execution and remediation"). It surfaces worker mode, the 5-precondition
readiness gate, the work-order queue, the quarantine-review queue, and lifetime
cost; and exposes the two **safe** actions (cancel a non-started order, view a
quarantine diff). It does NOT enable autonomous execution — that switch stays
CLI-only until the hardening phases land (see
`docs/operator_worker_hardening_spec.md`).

## Governance frame

The operator worker is the one component that can change *code*, so it is held
to a stricter bar than the advisory lanes. This surface is **read + safe
actions** only:

- It never runs a worker, never enables autonomous mode, never merges/pushes.
- The only state mutation is **cancel** — a legal, audited terminal transition
  already supported by the domain model (`queued|awaiting_approval|approved →
  cancelled`, audit event `work_order_cancelled`).
- `decision_engine.py`, scoring, and `outputs/latest/decision_plan.json` are
  untouched. Quarantine diffs are view-only.

## Locked decisions (from brainstorm)

1. Dedicated `/dashboard/operator` route + nav entry (not a System-tab card).
2. Read + safe actions (cancel, view quarantine diff); no autonomous-enable.
3. Hybrid readiness assessor (auto-detect what's verifiable; declared block for
   qualitative gates).
4. Readiness is **computed live** by the loader — no persisted artifact, so no
   pipeline wiring or staleness debt.
5. Reuse the existing `_require_auth` dependency and the existing
   `gui_v2/data/operator_control.py` loader.

## Architecture

### Component 1 — `portfolio_automation/operator_worker_readiness.py` (NEW)

Pure, observe-only function:

```
operator_worker_readiness(root: str | Path) -> dict
```

Returns:
```jsonc
{
  "observe_only": true,
  "gates": {
    "auth":        {"status": "amber", "reason": "runs as root, no container", "source": "auto"},
    "bounded_cmd": {"status": "amber", "reason": "probe/skill allowlist + impact gate; no OS sandbox", "source": "declared"},
    "audit":       {"status": "green", "reason": "audit_log.jsonl + worker_cost_log.jsonl present", "source": "auto"},
    "rollback":    {"status": "amber", "reason": "containment only; no applied-change rollback", "source": "declared"},
    "quarantine":  {"status": "green", "reason": "protected-path diff guard present", "source": "auto"}
  },
  "overall_ready": "2/5",
  "autonomous_enabled": false,
  "cost": {"lifetime_usd": 5.43, "cap_usd": null, "cap_pct": null}
}
```

Detection rules:
- `auth` (auto): amber if `os.geteuid()==0`; further note "no container" unless
  `/.dockerenv` exists or `/proc/1/cgroup` shows a container. Green only when
  non-root AND containerized.
- `audit` (auto): green when both `outputs/operator_control/audit_log.jsonl` and
  `outputs/operator_control/worker_cost_log.jsonl` exist.
- `quarantine` (auto): green when the protected-path guard symbol is importable
  from `operator_control` (presence check, not execution).
- `cost` (auto): `lifetime_usd` summed from `worker_cost_log.jsonl`; `cap_usd`
  from `config.json:operator_worker.cost_cap_usd_per_day` (null today → uncapped,
  surfaced as a warning, not a gate failure).
- `bounded_cmd`, `rollback` (declared): read
  `config.json:operator_worker.readiness_declared.{bounded_cmd,rollback}` →
  each `{status, reason}`; default `amber` with the current-state reason when
  the block is absent. As hardening phases land, an operator flips these.

`overall_ready` = "`<count of green>`/5". Degraded dict on any exception
(`{"observe_only": true, "error": "...", "gates": {}, "overall_ready": "0/5"}`).

### Component 2 — `gui_v2/data/operator_control.py` (EXTEND)

Add:
```
operator_worker_view(root: str | Path) -> dict
```
Composes:
- `readiness` ← `operator_worker_readiness(root)`
- `orders` ← work orders folded by id (reuse existing fold + `_summarize`);
  per-order: id, status, age, probe_id/skill_id, report link, and a
  `cancellable` bool (status in {queued, awaiting_approval, approved}).
- `quarantine` ← scan `.worktrees/wo_*` whose branch matches `operator/*`; for
  each, `git diff main...<branch> --stat` summary + an `already_in_main` flag
  (compare each changed file's worktree content vs `git show HEAD:<path>`).
  View-only.
- `cost` ← from readiness.

Degrades gracefully (missing dirs/worktrees → empty lists, never raises).

### Component 3 — routes (`gui_v2/app.py`), all `Depends(_require_auth)`

- `GET /dashboard/operator` → render `operator.html` with `operator_worker_view`.
- `POST /dashboard/operator/cancel` (Form: `work_order_id`) →
  `work_orders.transition_work_order(root, id, new_status="cancelled",
  actor="dashboard", note="cancelled via operator dashboard")`. The policy graph
  rejects illegal source states; on `WorkOrderValidationError` return a flash/400
  and redirect back. Success → 303 redirect to `/dashboard/operator`.
- `GET /dashboard/operator/quarantine/{work_order_id}/diff` → read-only diff
  stat (text) for that order's worktree; 404 if no worktree.

### Component 4 — template + nav

- `gui_v2/templates/operator.html` (NEW) using `_ui` macros (`responsive_table`,
  status badges). Sections, in order: Readiness (5 badge rows + `N/5 ready` +
  per-gate source tag + cost line), Orders (table; `Cancel` button rendered only
  when `cancellable`), Quarantine review (table: worktree · branch · diff stat ·
  in-main? · view-diff link), Cost.
- `base.html`: add `("/dashboard/operator","Operator")` to the nav tuple.

## Data flow

Browser → `GET /dashboard/operator` → `_require_auth` → `operator_worker_view`
(reads config + `outputs/operator_control/*.jsonl` + git worktrees, all live) →
`operator.html`. Cancel: form POST → `_require_auth` → `transition_work_order`
(validated + audited) → 303 redirect. No producer/pipeline involvement.

## Error handling

- Readiness + loader wrap IO in try/except → degraded structures, never 500.
- Cancel on illegal/unknown id → 400 + redirect with a message; no ledger write.
- Auth: open mode when `GUI_V2_AUTH_USER/PASS` unset (matches existing tabs);
  401 when configured.

## Testing

- `tests/test_operator_worker_readiness.py`: healthy vs degraded; auto branches
  (root/non-root, container present/absent, logs present/absent, cap present/
  absent); declared-block read + default; `overall_ready` count.
- `tests/test_dash_operator_worker.py` (or extend existing): fold-by-status,
  quarantine detection (synthetic worktree fixture), `cancellable` flag, degraded.
- `tests/test_operator_routes.py` (or extend gui_v2 route tests): GET renders;
  POST cancel legal → 303 + transition recorded; POST cancel illegal/unknown →
  400, no write; 401 when auth configured; quarantine-diff 404 when absent.
- Reuse existing gui_v2 test harness/fixtures.

## Health pairing (repo requirement)

Extend `/daily-tool-analysis` line 6g (operator-control) to fold in
`readiness N/5` + cost-cap utilization (when a cap exists). Daily cadence,
developer lens. No new agent. Add a test asserting the line renders under both a
healthy and a degraded readiness fixture.

## Out of scope

- Autonomous-enable toggle (CLI-only until hardening Phases 1–3 land).
- Any worker *execution* trigger from the GUI (dispatch stays on existing
  endpoints / CLI).
- A persisted readiness artifact / new pipeline stage (live computation only).
- `decision_engine`, scoring, allocation, or decision_plan changes.

## Follow-on (noted, not in this spec)

The CLI `cancel` gap (domain supports `cancelled`; `worker_runner` lacks a
`cancel` subcommand — only `fail`) is tracked in
`docs/operator_worker_hardening_spec.md` Phase 3.
