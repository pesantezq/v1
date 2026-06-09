# Operator Control / Claude Code Worker — Phase 1

Status: **shipped Phase 1 (create-only)** · observe-only · advisory-only · no
trade execution.

The operator-control plane lets the dashboard turn the health/quality *probes*
it already surfaces into **allowlisted work orders** that a future Claude Code
*worker* can pick up — generate a focused prompt, run in a sandbox/worktree, run
tests, and report back for human review. Phase 1 implements everything up to and
including prompt generation; it does **not** execute a worker.

```
Dashboard probe → recommended skill → allowlisted work order → worker prompt
   → [Phase 2: Claude Code worker → sandbox/worktree → tests → repair report]
   → dashboard review → optional human approval
```

## Why this is safe

* **Observe-only.** Nothing here executes trades, places broker orders, runs
  shell commands, restarts services, installs dependencies, or edits production
  code. The web app can only *create* a work-order record.
* **Structural command-injection guard.** `requested_action` is composed
  **only** from the probe + skill registries. There is no field on a work order
  (or on the create request) through which a caller can store an executable
  command string. The create form submits only registry ids (`probe_id`,
  `skill_id`, `mode`) as hidden inputs — no free-text, no `<textarea>`.
* **Allowlist, not free-form.** A `(probe, skill, mode)` tuple that is not
  explicitly allowlisted in the registries cannot become a work order.
* **Append-only audit trail.** Work orders and audit events are append-only
  JSONL — never rewritten or truncated.
* **Decision source of truth is untouched.** `outputs/latest/decision_plan.json`
  remains the sole source of advisory actions. Quant operator actions are
  labeled **proposal-only**; nothing here is dressed as official advice.

## Reuse-first architecture

The plane is a thin **control layer over existing artifacts**, not a new
pipeline:

| Reused | How |
|---|---|
| `gui_v2/data/shared.py` `card()` contract | The GUI adapter emits the same card/view-model shape. |
| `components/_ui.html` macros | `components/operator_panel.html` renders actions + queue with the existing design system. |
| Existing status artifacts (`daily_run_status.json`, `data_quality_report.json`, `fmp_budget_status.json`, `ai_budget_summary.json`, `broker_sync_status.json`, `confidence_calibration.json`, `gate_retune_suggestions.json`, `risk_delta.json`, `decision_plan.json`, …) | Probes **reference** these paths via `source_artifact`; they never copy contents. |
| GUI auth (`_require_auth`) | The create endpoint reuses the same auth dependency. |
| Control-state precedent (`data/*_check_state.json`) | Operator-control state lives outside `OutputNamespace` (see below). |

### Namespace note

Operator-control artifacts live under `outputs/operator_control/` and are
written directly by the package — **not** via `OutputNamespace`.
`OutputNamespace` governs *pipeline* artifacts (user-scoped, validated, consumed
by the daily run). The operator-control plane is human-triggered governance
state that sits *over* the pipeline (the same role as `data/*_check_state.json`),
so it owns its own directory. This is an intentional, documented exception.

## Components

```
operator_control/
  __init__.py          # output paths (work_orders.jsonl, audit_log.jsonl, prompts/, reports/)
  probe_registry.py    # 14 known dashboard probes (Probe dataclass + lookups)
  skill_registry.py    # 10 allowlisted skills + GLOBAL_FORBIDDEN_ACTIONS
  repair_policies.py   # validation, approval rule, status graph (pure, no I/O)
  audit_log.py         # append-only audit events
  work_orders.py       # append-only event-sourced storage + CLI
  worker_prompts.py    # render a Claude Code worker prompt from a work order
gui_v2/data/operator_control.py     # GUI adapter (probes+orders → view-model)
gui_v2/templates/components/operator_panel.html   # action buttons + queue
```

### Probe

`probe_id, display_name, source_view, source_artifact, severity, description,
recommended_skill_id, allowed_actions, risk_level, approval_required,
observe_only_notice`. Probes ship for: daily-run failed/warned stages, data
quality, pipeline status, AI budget, FMP budget, memo delivery, Schwab broker
health, artifact registry, quant confidence calibration, pattern efficacy,
retune suggestions, portfolio risk-near-cap, advisory decision queue, memo
generation/readability.

### Skill

`skill_id, name, description, allowed_probe_ids, allowed_modes,
forbidden_actions (+ GLOBAL_FORBIDDEN_ACTIONS), required_tests, risk_level,
approval_required_for_modes, output_report_requirements`. Modes are
`diagnose` (read+explain), `propose_fix` (write a proposal, never apply),
`safe_repair` (narrow, reversible, test-gated, approval-required).

### Work order

`work_order_id, created_at, created_by, source_view, probe_id, skill_id, mode,
risk_level, approval_required, status, status_history[], source_artifacts[],
requested_action, safety_constraints[], generated_prompt_path,
result_report_path, observe_only`.

## Work-order lifecycle

Statuses: `queued · claimed · running · completed · failed · awaiting_approval ·
approved · rejected · cancelled`. Phase 1 creates orders (`queued` or
`awaiting_approval`) and supports operator `approve/reject/cancel`. The
worker-driven transitions (`claimed/running/completed/failed`) are defined and
validated by the policy graph so the Phase 2 worker runner inherits a safe state
machine.

Approval is required when any of: the skill marks the mode approval-required, the
probe is `approval_required`, the mode is `safe_repair`, or the effective risk
level is `high`.

## Storage

* `outputs/operator_control/work_orders.jsonl` — append-only, event-sourced.
  Each create/transition appends a full snapshot line; readers fold by
  `work_order_id` (last line wins). The file only grows.
* `outputs/operator_control/audit_log.jsonl` — append-only audit events
  (`work_order_created`, `work_order_status_changed`, `prompt_generated`,
  `approval_granted/rejected`, `work_order_cancelled`, `validation_rejected`).
* `outputs/operator_control/prompts/{work_order_id}.md` — generated worker prompt.
* `outputs/operator_control/reports/{work_order_id}.md` — expected worker report
  path (written by the Phase 2 worker, not Phase 1).

## CLI

```bash
python -m operator_control.work_orders list [--status STATUS] [--json]
python -m operator_control.work_orders create \
    --probe-id data_quality.warnings \
    --skill-id diagnose_data_quality_warnings \
    --mode diagnose --created-by enrique_cli
python -m operator_control.work_orders show --id <work_order_id>
python -m operator_control.work_orders generate-prompt --id <work_order_id>
python -m operator_control.work_orders probes      # list known probes
python -m operator_control.work_orders skills      # list allowlisted skills
```

## Dashboard usage

Each persona tab renders an **Operator Actions** section (from
`components/operator_panel.html`):

* **System** — Diagnose buttons for run/quality/budget/registry/Schwab probes.
* **Quant** — proposal-only Diagnose / Propose-Fix actions, behind a prominent
  proposal-only banner.
* **Portfolio** — review-only Diagnose actions (Explain Risk / Advisory Queue).
  **No Buy/Sell/Trade/Execute/broker buttons** — enforced by test.
* **Memo** — Diagnose formatting / regenerate-memo-from-artifacts (never emails).
* **Today** — a one-line open-work-order count linking to System.

Each button is its own `<form>` POSTing to `POST /dashboard/operator/create`
(the path deliberately avoids the token "order", reserved for trade orders).
The endpoint validates the registry tuple and appends a work order — it never
executes a worker. When GUI auth is configured, the authenticated username is
the `created_by`.

## What Phase 1 does NOT do

* No worker execution — prompt generation only.
* No code edits, service restarts, dependency installs, or deploy changes.
* No trade execution, broker orders, or auto-trading (and never will).
* No changes to scoring, decision, allocation, signal, or recommendation logic.
* No email sending.

## Analysis + health coverage

Operator-control state is surfaced on the **System** tab (daily-cadence health
surface) and summarized on **Today**, satisfying the consumer requirement for a
daily-cadence feature. A complementary line in
`.claude/commands/daily-tool-analysis.md` (read `work_orders.jsonl` /
`audit_log.jsonl`; flag a growing `failed` / long-`awaiting_approval` queue) is
recommended but, because it edits a `.claude/commands/*` oversight file around a
worker-adjacent feature, is left for explicit operator sign-off.

## Recommended Phase 2 — Claude Code worker runner

A separate, opt-in runner that: claims a `queued`/`approved` work order →
renders the prompt → launches a Claude Code worker in an isolated
sandbox/worktree → runs the skill's `required_tests` → writes the result report
to `reports/{work_order_id}.md` → transitions the order to `completed`/`failed`
→ surfaces the report on the dashboard for human review. It must keep every
boundary in this document, run outside the web process, and remain default-off.
