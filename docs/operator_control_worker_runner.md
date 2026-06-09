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

## What Phase 2 does NOT do

- Never merges to `main`, never pushes to any remote.
- No cron/schedule — manual CLI trigger only.
- No service restart, dependency install, or deploy change.
- No trade/broker execution; the protected-path guard + restricted profile keep
  the worker out of scoring/decision/broker/config/secret territory.
- The web app is unchanged beyond a read-only System-tab runner card.
