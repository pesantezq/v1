# Operator Control — Phase 2 Worker Runner (design spec)

Date: 2026-06-09 · Branch: `operator-control-worker-runner` · Status: design, pending implementation.

Builds on Phase 1 (`docs/operator_control.md`): the create-only, observe-only
work-order plane. Phase 2 adds the **runner** that consumes queued/approved
work orders — the first component in the system that can *execute* a worker
(behind a default-off, hard-gated flag), versus merely observing.

## Decisions locked in brainstorming

1. **Execution model: hybrid.** Default behavior is *scaffolding + manual
   launch* (no process auto-invokes `claude`). An *autonomous headless* path is
   available only behind a second hard flag, modeled on `auto_apply`
   (default-inert, kill-switch).
2. **Autonomous scope: all modes including `safe_repair`.** When the autonomous
   flag is on, the unattended worker may edit non-protected files in the
   worktree and run tests. Containment is the **worktree + never-merge + human
   review** boundary plus deterministic post-run guards — NOT mode restriction.

## Goals / non-goals

**Goals:** turn a work order into prepared, isolated worker context; optionally
run a headless worker; capture a diff + test results into a report; leave
everything for human review. Make the dangerous path default-off and fully
audited.

**Non-goals (Phase 2 explicitly does NOT):** merge to `main`, push to any
remote, restart services, install dependencies, run on a cron/schedule, or edit
protected/decision/scoring/broker logic. The web app stays create-only and is
not modified beyond a read-only status card.

## Architecture & hard invariants

A new module `operator_control/worker_runner.py` + CLI, running **outside the
web process**. The runner is the consumer of work orders; Phase 1 is unchanged.

Invariants for **both** paths:
- **Never merges to `main`, never pushes, never mutates the live working tree.**
  All work happens in an isolated `git worktree` at `.worktrees/wo_<id>` on a
  throwaway branch `operator/wo_<id>` cut from `main`.
- **Quarantine on failure:** a failed/violating run's worktree is left in place
  for forensics, never auto-deleted.
- **Single-flight lock** (reuse the `run_lock.py` pattern) — one worker at a time.
- **Manual CLI trigger only.** No cron in Phase 2; a scheduled drain is a
  separate future step needing its own sign-off.
- Every transition + guard result is appended to `outputs/operator_control/audit_log.jsonl`.

## The two paths

### Scaffolding (default, always available)
1. `claim`: select an eligible order — `queued`, or `approved` if approval was
   required. **Refuses `awaiting_approval`** (must be approved first) and any
   terminal status.
2. Transition order → `claimed` (audit).
3. Create worktree `.worktrees/wo_<id>` on branch `operator/wo_<id>` off `main`.
4. Generate the worker prompt (reuse `worker_prompts.render_prompt`) into the
   worktree, plus a `RUN_WORKER.md` helper documenting exactly how to launch
   Claude Code there and where to write the report.
5. **Stop.** A human launches `claude` in the worktree, does the work, writes the
   report to `outputs/operator_control/reports/{id}.md`, then runs
   `worker_runner complete --id <id>` (or `fail`).

### Autonomous headless (opt-in, gated)
Gate (ALL required): `config.json operator_control.autonomous_worker.enabled=true`
**AND** env `STOCKBOT_OPERATOR_WORKER_AUTONOMOUS=1` **AND** no kill-switch file
`config/operator_worker.DISABLED`. Default = all off → behaves as scaffolding.

Steps 1–4 as above, then:
5. Transition → `running`.
6. Invoke `claude` headless: `claude -p <prompt> --output-format json`, cwd =
   worktree, with a **restricted worker permission profile**
   (`operator_control/worker_settings.json`) that denies push / network-install
   / systemd and confines writes to the worktree. Capture stdout/stderr/exit.
7. Run the skill's `required_tests` inside the worktree; capture results.
8. Deterministic guards (below).
9. Write the report (diff summary + test results + worker output) to
   `reports/{id}.md`.
10. Transition → `completed` (tests passed, no violation) or `failed`. Never merges.
11. Human reviews the branch/worktree and decides to merge or discard (outside
    the runner).

## Deterministic safety backstops (do not trust the LLM)

- **Protected-path diff guard** (`operator_control/protected_paths.py`): after the
  worker runs, `git -C <worktree> diff --name-only main` is checked against a
  deny-list: `decision_engine.py`, `scoring.py`, `portfolio_decision_engine.py`,
  `config/signal_registry.yaml`, `config.json`, `.claude/**`, `deploy/**` &
  systemd units, `requirements.txt`, `.env*`, and the broker modules
  (`portfolio_automation/brokers/**`). Any hit → **quarantine**: order →
  `failed`, report flags the violation, worktree retained, audit
  `worker_protected_path_violation`. Pure-Python; independent of the worker's
  promises.
- **Restricted permission profile:** headless `claude` runs with
  `worker_settings.json` denying push/network-install/systemd and confining
  writes to the worktree cwd.
- **Test gate:** required tests must pass for `completed`; else `failed` with
  captured output.
- **observe_only:** autonomous run records emit `observe_only:false` on their OWN
  record (like `auto_apply`) but are fully audited; every other repo invariant
  stays observe-only.

## Components / files

| File | Purpose |
|---|---|
| `operator_control/worker_runner.py` | claim / scaffold / run / run-next / complete / fail / status + CLI (`python -m operator_control.worker_runner`). |
| `operator_control/worktree.py` | Thin git-worktree wrapper: create/list/remove on `operator/wo_<id>` off `main`. |
| `operator_control/protected_paths.py` | Deny-list + `diff`-name checker (reused by the guard + testable in isolation). |
| `operator_control/worker_settings.json` | Restricted Claude Code permission profile for headless runs. |
| `config.json` → `operator_control.autonomous_worker` | `{enabled:false, …}` gate block (default-inert). |
| `config/operator_worker.DISABLED` | Kill-switch file (documented; not committed). |
| GUI: System-tab runner card | Read-only: last run, active worktrees, completed/failed counts. Reuses `card()`. No controls. |
| `docs/operator_control_worker_runner.md` | Runbook: gates, paths, guards, activation, review/merge workflow. |
| Tests: `tests/test_operator_worker_runner.py` | See below. |

CLI surface:
```
python -m operator_control.worker_runner status
python -m operator_control.worker_runner scaffold --id <id>      # always available
python -m operator_control.worker_runner run --id <id>           # autonomous (gated; else scaffolds)
python -m operator_control.worker_runner run-next                # next eligible order
python -m operator_control.worker_runner complete --id <id>      # after manual/auto work + report
python -m operator_control.worker_runner fail --id <id> --note ...
```

## Status / transition reuse

Reuses the Phase 1 policy graph (`repair_policies`): `queued/approved → claimed
→ running → completed|failed`. Adds no new statuses. `complete`/`fail` validate
via the existing `transition_work_order`.

## Testing

`tests/test_operator_worker_runner.py` (the `claude` subprocess is **mocked** —
no real LLM needed):
- Gates default-inert: with nothing set, `run` behaves as `scaffold` (no
  subprocess invoked).
- Scaffolding builds the worktree + writes prompt + `RUN_WORKER.md`; order → `claimed`.
- Eligibility: `awaiting_approval` is refused; `approved`/`queued` accepted.
- Protected-path guard: a mocked worker diff touching `scoring.py` →
  quarantine + `failed` + audit `worker_protected_path_violation`; worktree retained.
- Test gate: mocked failing tests → `failed`; passing + clean diff → `completed`.
- Never-merge / never-push: assert `main` and `origin` are untouched after a run.
- Kill-switch: `config/operator_worker.DISABLED` present → autonomous path halts
  (falls back to scaffold / refuses).
- Single-flight lock: a second concurrent `run` is refused.
- GUI: System tab renders the runner card cleanly with no runner state.

## Analysis + health pairing

- System-tab runner card (daily-cadence health surface) + audit events.
- Recommended `.claude/commands/daily-tool-analysis.md` line (flag a growing
  `failed`/quarantined count, long-`running` orders): left for explicit operator
  sign-off (oversight file), per the Phase 1 follow-up.

## Rollout

Ships **inert** (autonomous flag false, no kill-switch needed because default is
off). Scaffolding path is immediately usable. Activation runbook for the
autonomous path documented in `docs/operator_control_worker_runner.md`; turning
it on is an explicit operator action.
