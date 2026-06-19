# Operator Control — Phase 2 Worker Runner

Status: **shipped** (default-inert autonomous path) · CLI-only · never merges or
pushes. Builds on Phase 1 (`docs/operator_control.md`).

The worker runner consumes Phase 1 work orders. By default it only *scaffolds*
(prepares an isolated worktree + prompt for a human to run). An *autonomous*
headless path exists behind a hard, default-off gate.

## The two paths

### Scaffolding (default, always available)
`claim` an eligible order (`queued`, or `approved` if approval was required —
`awaiting_approval` is refused) → create a `git worktree` at `.worktrees/<id>`
on branch `operator/<id>` off `main` → drop `WORKER_PROMPT.md` + `RUN_WORKER.md`
→ **stop**. A human launches Claude Code in the worktree, does the work, writes a
report, then runs `complete`/`fail`.

### Autonomous headless (opt-in, gated like `auto_apply`)
Runs only when **all three** gates pass:
1. `config.json` → `operator_control.autonomous_worker.enabled = true`
2. env `STOCKBOT_OPERATOR_WORKER_AUTONOMOUS=1`
3. no kill-switch file `config/operator_worker.DISABLED`

When enabled: claim → worktree → `running` → headless `claude -p` (cwd =
worktree, restricted `operator_control/worker_settings.json` profile) → run the
skill's `required_tests` → deterministic guards → write report → `completed` or
`failed`. **Never merges, never pushes.** Default (no gates) → behaves as
scaffolding.

## Deterministic safety backstops (not LLM-trusted)

- **Protected-path diff guard** (`operator_control/protected_paths.py`): after the
  worker runs, the worktree diff vs `main` is checked against a deny-list
  (`decision_engine.py`, `scoring.py`, `portfolio_decision_engine.py`,
  `config/signal_registry.yaml`, `config.json`, `.claude/**`, `deploy/**` +
  `*.service`, `requirements.txt`, `.env*`, `portfolio_automation/brokers/**`).
  Any hit → **quarantine**: order → `failed`, report flags the violation, the
  worktree is retained, audit `worker_protected_path_violation`.
- **Restricted permission profile** (`worker_settings.json`): headless `claude`
  runs with push/merge/install/systemd/sudo denied and protected files
  edit-denied.
- **Test gate:** the skill's `required_tests` must pass for `completed`.
- **Single-flight lock** (reuses `run_lock`): one worker at a time.
- Autonomous run records may emit `observe_only:false` on their own record (like
  `auto_apply`) but every event is audited; no other repo invariant changes.

## CLI

```bash
python -m operator_control.worker_runner status
python -m operator_control.worker_runner scaffold --id <id>     # always available
python -m operator_control.worker_runner run --id <id>          # autonomous if gated; else scaffolds
python -m operator_control.worker_runner run-next               # next eligible order
python -m operator_control.worker_runner complete --id <id>     # after manual/auto work
python -m operator_control.worker_runner fail --id <id> --note "..."
```

## Human review / merge workflow

The runner deliberately leaves a branch for you. To review and integrate:

```bash
git -C .worktrees/<id> diff main           # inspect the worker's changes
git -C .worktrees/<id> log --oneline main..HEAD
# if good, integrate by hand from the repo root:
git checkout main && git merge --no-ff operator/<id>
# clean up:
git worktree remove .worktrees/<id>
```

Quarantined (failed) worktrees are left in place on purpose — inspect, then
remove with `git worktree remove --force .worktrees/<id>`.

## Activating the autonomous path

1. Add to `config.json`:
   `{"operator_control": {"autonomous_worker": {"enabled": true}}}`
2. Export `STOCKBOT_OPERATOR_WORKER_AUTONOMOUS=1` for the runner process.
3. Ensure `config/operator_worker.DISABLED` does not exist.
   To halt instantly at any time: `touch config/operator_worker.DISABLED`.

## Phase 3 — scheduled drain & report review

- **Drain (`worker_runner drain` / `scripts/operator_worker_drain.sh`):** runs
  eligible orders through the autonomous path in a bounded loop (`--max`,
  default 10). It is a **NO-OP unless the autonomous worker is enabled** (the
  same three-part gate) — so it ships doubly-inert. Never merges/pushes. The
  crontab line is documented in the script header but **not installed**;
  installing cron is an explicit operator action taken only after enabling the
  autonomous worker:
  ```bash
  bash scripts/operator_worker_drain.sh 10        # manual; inert unless gated on
  ```
- **Report review (read-only GUI):** `GET /dashboard/operator/report/<id>`
  renders a completed/failed order's report + metadata (the report markdown is
  shown escaped in a `<pre>` — no execution, no controls). The work-order queue
  links the id of any `completed`/`failed` order to its report. The id is
  regex-validated (`^wo_[0-9A-Za-z_]+$`) so it can never traverse out of
  `outputs/operator_control/reports/`.

## Phase 4 — GUI "Repair" button (unattended auto-repair)

A **Repair** button on the System (data-quality) and Memo probes launches an
*unattended* worker that **auto-diagnoses then fixes**:

```
[Repair · auto] (System tab)
  → POST /dashboard/operator/dispatch
  → create work order, APPROVE it (the click IS the approval — the only gate)
  → spawn a DETACHED runner (web returns instantly; never blocks)
  → headless `claude` (login auth) edits in an isolated worktree (acceptEdits)
  → protected-path guard + test gate + production-impact gate
  → report (+ cost) → completed | failed ; queue links the report
```

Key facts:
- **Auth on the box, not an API key.** The worker subprocess runs with
  `ANTHROPIC_API_KEY` stripped so it authenticates via the Claude Code **login**
  in `~/.claude` (a stray external API key otherwise 401s). No external API key
  is used or required.
- **The only gate is the click.** `config.json` ships
  `operator_control.autonomous_worker.enabled=true`; the dispatch endpoint sets
  `STOCKBOT_OPERATOR_WORKER_AUTONOMOUS=1` for that run, so the operator need not
  set anything globally. The kill-switch (`config/operator_worker.DISABLED`)
  still forces a safe fallback to scaffolding.
- **Failed gate → no production impact (deterministic).** Before/after each run
  the runner snapshots `main` HEAD + `config.json` + `config/signal_registry.yaml`
  + `outputs/latest/decision_plan.json`. If any changed, the run is **failed**
  (`worker_production_impact` audit) — a worker can never bleed into production.
  This sits on top of worktree-isolation, never-merge/never-push, and the
  protected-path diff guard.
- **Operational cost tracking (separate ledger).** Every run appends to
  `outputs/operator_control/worker_cost_log.jsonl`
  (`{cost_usd, num_turns, probe_id, skill_id, why, status, budget_scope}`),
  surfaced on the System-tab Worker Runner card and via
  `python -m operator_control.worker_runner cost`. This is the worker's own
  operational spend — **deliberately NOT part of** the FMP/AI decision budget
  (`ai_budget_summary.json`).
- **Enforced cost cap (2026-06-19, configured-but-inert).** `operator_control.cost_cap`
  bounds the autonomous path in three layers: a **pre-dispatch daily gate**
  (refuse dispatch once today's UTC spend ≥ `usd_per_day` — the order is *deferred*,
  left eligible, not failed; `drain` stops; audits `worker_cost_cap_deferred`),
  **per-run hard rails** (`--max-turns` = `max_turns_per_run` + a `subprocess`
  timeout = `max_run_seconds`, which kill the headless child; the container timeout
  can only tighten), and a **post-run overage flag** (`worker_cost_cap_exceeded`
  audit if a run's cost > `usd_per_run`, outcome unchanged). Defaults: $3/run, $10/day,
  40 turns, 1200s. A missing block or null/≤0 knob disables that limit. Inert until
  the autonomous path is enabled (Phase 4). Utilization shows in the daily check
  (today_usd / cap_usd, AMBER ≥80%). See `docs/operator_worker_hardening_spec.md`.

### Residual risk (read before relying on repair)
The worker runs as the dashboard service's user (root) with only
**git-isolation** (the worktree), not process-isolation. The production-impact
gate + protected-path guard catch escapes deterministically and nothing merges
without review, but for heavy reliance on `safe_repair` run the worker as an
unprivileged user or in a container. Diagnose is read-only and lowest-risk.

## What Phase 2/3/4 does NOT do

- Never merges to `main`, never pushes to any remote.
- No cron/schedule — manual CLI trigger only.
- No service restart, dependency install, or deploy change.
- No trade/broker execution; the protected-path guard + restricted profile keep
  the worker out of scoring/decision/broker/config/secret territory.
- The web app is unchanged beyond a read-only System-tab runner card.
