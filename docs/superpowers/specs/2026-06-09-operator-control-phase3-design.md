# Operator Control — Phase 3 (scheduled drain + report review) design + plan

Date: 2026-06-09 · Branch: `operator-control-worker-runner` · Completes the
operator-control arc (Phase 1 create → Phase 2 run → Phase 3 schedule+review).

Authorized by operator directive "finish all phases then push to main" —
implemented with conservative defaults (everything default-inert / read-only),
no new approval gate beyond Phase 2's.

## Scope

1. **Scheduled drain (default-inert).** A `drain` command + shell script that
   runs eligible work orders through the *already-gated* autonomous path. It is
   a **NO-OP unless the autonomous worker is enabled** (Phase 2's three-part
   gate) — unattended *scaffolding* is useless, so the drain only acts when
   autonomous is on. Never merges or pushes. The crontab line is documented but
   **not installed** (installing cron is an operator action).
2. **Report review surface (read-only).** A `/dashboard/operator/report/{id}`
   route that renders a completed/failed order's report + metadata, plus a
   "View report" link from the work-order queue. No controls.

## Decisions (conservative defaults; no new gate)

- Drain reuses Phase 2's `autonomous_enabled` gate — it adds **no** new way to
  execute. If autonomous is off, drain exits "inert".
- Drain is bounded (`--max`, default 10) and sequential (each order goes through
  `run`, which holds the single-flight lock).
- Report route is strictly read-only; the `work_order_id` is validated against
  `^wo_[0-9A-Za-z_]+$` to prevent path traversal into `reports/`.

## Plan (TDD)

### Task 1 — `worker_runner.drain()` + CLI
- `drain(root, max_orders=10, actor="cron")`: if not `autonomous_enabled` →
  `{"drained":0,"status":"inert",...}`. Else loop: pick eldest eligible order,
  `run()` it, repeat until none or `max_orders`. Return `{"drained":n,"status":"ran","results":[...]}`.
- CLI subcommand `drain --max N --actor X`.
- Tests: inert when off; processes N eligible (mock `_invoke_claude`/`_run_tests`/
  `changed_files`) and they become `completed`.

### Task 2 — `scripts/operator_worker_drain.sh`
- Inert wrapper calling `python -m operator_control.worker_runner drain`. Document
  a commented crontab line in the runbook. Not installed.

### Task 3 — Report review route + template
- `GET /dashboard/operator/report/{work_order_id}`: 404 on unknown/invalid id
  (regex guard); render `dashboard/operator_report.html` with the order metadata
  + the report markdown shown in an escaped `<pre>` (no injection, no controls).
- "View report" link in `operator_panel.html` queue for orders whose status is
  `completed`/`failed`.
- Tests: renders for a completed order with a report; 404 for unknown id; 404 for
  a traversal attempt (`../`); no `<form>`/execution controls in the report view.

### Task 4 — Docs
- Extend `docs/operator_control_worker_runner.md` (drain + cron line + review
  surface). Roadmap Phase 3 → shipped.

## Non-goals
- No cron installation, no systemd, no merge/push automation. The drain remains
  inert until the operator enables autonomous AND installs the documented cron.
