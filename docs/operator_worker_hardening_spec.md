# Operator Worker — Hardening Spec (next dev milestone)

Status: **draft** · Author: daily-tool-analysis follow-up · Date: 2026-06-18
Owner directive (2026-06-18): the next major capability milestone is **controlled
execution and remediation** — the operator worker — not additional analytical lanes.
The analytical breadth is already sufficient (85 research candidates, 59 simulation
candidates, 125 crowd-bus tickers, multiple strategy profiles, promotion governance,
learning loops). The limiting factor is now *safe action*.

## Governance frame

The system is **production-gated, simulation-active, human-approved for production
promotion** — not globally observe-only. The operator worker is the one component
that can change *code*, so it is held to a stricter bar than the advisory lanes: it
stays **disabled** (scaffold-only; `autonomous_enabled=false`) until every precondition
below is met. Enabling it is an explicit, reversible human step.

## Acceptance gate — 5 preconditions (owner-set)

Autonomous execution must not be enabled until ALL five hold:

| # | Precondition | Current state (2026-06-18) | Gap to close |
|---|---|---|---|
| 1 | **Authentication** | Uses `~/.claude` login (must NOT set `ANTHROPIC_API_KEY` or headless 401s). | Worker runs **as root** with git-isolation only — over-privileged. |
| 2 | **Bounded command policies** | Probe/skill allowlist (rejects unknown `probe_id`); `mode=safe_repair`; production-impact diff gate (main/config/registry/decision_plan unchanged). | No **OS-level** command sandbox — it runs a full headless session. |
| 3 | **Audit evidence** | `audit_log.jsonl` (event_type/actor/safety_result/details) + `worker_cost_log.jsonl` (cost/turns/duration/budget_scope). | ✅ Adequate. Cost-cap adjunct **CLOSED 2026-06-19** — spend is now enforced (see Phase 2 below); consider signing/append-only enforcement next. |
| 4 | **Rollback behavior** | Never merges, never pushes; quarantines on protected-path diff; aborts on `main` HEAD move. | Containment only — no explicit *applied-change* rollback path; salvage of good quarantined work is manual. |
| 5 | **Quarantine handling** | Protected-path diff guard quarantines bad runs into the worktree. | ✅ Works (verified: `wo_…4043c5` correctly quarantined a HEAD-moved run). |

**Original two real blockers: precondition 1 + 2 → OS isolation (run unprivileged,
in a container), and an enforced cost cap (precondition 3 adjunct).** Status
2026-06-19: the **cost cap is now enforced** (Phase 2, shipped configured-but-inert).
The remaining work before the Phase 4 enable gate is Phase 3 (explicit rollback +
quarantine-review). OS isolation (Phase 1) shipped inert; enabling it + the cap is
the Phase 4 step.

## Proposed work (phased, each shippable + reversible)

### Phase 1 — OS isolation (blocks #1, #2)
- Run the worker as a **non-root** user in a **container** (rootless Podman/Docker),
  repo bind-mounted read-only except the worktree; no host network beyond the
  Anthropic + FMP/OpenAI endpoints it needs.
- Mount `~/.claude` credentials read-only; assert `ANTHROPIC_API_KEY` is unset inside.
- Keep the existing git-isolation (per-WO worktree) inside the container.

### Phase 2 — enforced cost cap (blocks #3 adjunct) — ✅ SHIPPED 2026-06-19 (configured-but-inert)
Implemented as `operator_control.cost_cap` (nested block, NOT the originally-sketched
flat `operator_worker.cost_cap_usd_per_run`): `usd_per_run` ($3) + `usd_per_day` ($10)
money knobs, plus explicit `max_turns_per_run` (40) + `max_run_seconds` (1200) hard
rails. Three enforcement layers in `worker_runner.run()`:
- **Pre-dispatch daily gate** — refuse dispatch (before claim/worktree) once today's
  UTC spend ≥ `usd_per_day`; the order is *deferred* (left eligible), not failed;
  `drain` stops on the deferral. Audits `worker_cost_cap_deferred`.
- **Per-run hard rails** — `--max-turns` + a `subprocess` timeout on both exec paths
  (the container timeout can only *tighten*, never loosen). Cost is monotonic in
  turns+time, so these kill the child without a fragile streaming-cost parser (this
  is the deliberate substitute for the original "abort mid-run on cumulative cost").
- **Post-run overage flag** — audits `worker_cost_cap_exceeded` if a recorded run
  cost > `usd_per_run` (does not change the order outcome).
Surfaced in the daily check (operator-control line): today_usd / cap_usd (cap_pct%),
AMBER at ≥80% or any over-cap event. Inert until Phase 4 (autonomous disabled).
Design + plan: `docs/superpowers/specs/2026-06-19-operator-worker-cost-cap-design.md`,
`docs/superpowers/plans/2026-06-19-operator-worker-cost-cap.md`.

### Phase 3 — explicit rollback + quarantine review (rounds out #4)
- Add a `cancel`/`archive` terminal transition to `worker_runner` (today only
  `fail` exists — clearing dead orders inflates the `failed` count).
- Add a **quarantine-review** path: surface quarantined worktrees that contain a
  candidate diff, diff them against current main, and offer salvage-or-discard
  (the `wo_…4043c5` case — a real fix that main later reproduced — is the worked
  example of why this is needed).

### Phase 4 — enable gate
- Only after Phases 1–3 land + tests: flip `autonomous_enabled=true` behind the
  existing 3-part gate (config flag + env + no `operator_worker.DISABLED` file).
- Pair with a daily-check health line (already present) and a kill-switch drill.

## Out of scope
- No change to `decision_engine.py`, scoring, or any score semantics.
- The worker remains create/repair-only; it never executes trades or touches
  `outputs/latest/decision_plan.json`.

## Analysis + health pairing (repo requirement)
The daily-tool-analysis operator-control line already covers open/failed/quarantined
counts + worker mode. Extend it in Phase 2 with the cost-cap utilization signal and
in Phase 3 with a quarantine-review-pending count.
