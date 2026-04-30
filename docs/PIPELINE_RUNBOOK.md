# Pipeline Runbook

## Primary Entry Points

- `bash scripts/preflight.sh`
- `bash scripts/run_daily_safe.sh`
- `python main.py --run-mode daily`
- `python main.py --run-mode weekly`
- `python main.py --run-mode monthly`
- `python run_daily_pipeline.py`
- `python -m watchlist_scanner`
- `python -m theme_engine --mode daily`

## Daily

### Main portfolio run

Command:
`python main.py --run-mode daily`

Production gate:

- run `bash scripts/preflight.sh` first
- production cron should call `bash scripts/run_daily_safe.sh`
- FMP compliance must remain `RESULT: COMPLIANT`
- FMP-focused tests must pass before the daily pipeline is allowed to run
- no endpoint changes may bypass `fmp_endpoint_registry.py`

Typical outcomes:

- updates `outputs/latest`
- writes scored finance recommendations and portfolio artifacts
- may run theme engine and watchlist scanner if enabled
- writes `outputs/latest/decision_plan.json` and `outputs/latest/decision_plan.md`
- auto-runs AI validation after the decision plan step
- writes:
  - `outputs/latest/ai_decision_validation.json`
  - `outputs/latest/ai_decision_validation.md`
- auto-runs the decision outcome tracker after validation
- writes:
  - `outputs/policy/decision_outcomes.jsonl`
  - `outputs/policy/decision_outcome_summary.json`
  - `outputs/policy/decision_outcome_summary.md`
- records snapshot and run history in SQLite
- copies successful outputs to `outputs/history/YYYY-MM-DD`

Decision-layer execution order:

```text
decision_plan
  -> decision_explanations
  -> ai_decision_validation
  -> decision_outcome_tracker
```

### Analysis-only daily orchestrator

Command:
`python run_daily_pipeline.py`

Stages:

1. theme discovery
2. watchlist scan
3. weight tuning
4. policy evaluation
5. allocation preview
6. allocation simulation
7. policy activation check
8. system summary
9. daily memo

### Daily safe wrapper

Command:
`bash scripts/run_daily_safe.sh`

Wrapper behavior:

- auto-detects repo root
- activates `.venv`
- loads `.env` when present
- runs preflight before pipeline execution
- writes logs to `logs/daily_safe_YYYY-MM-DD.log`
- can use `DRY_RUN_MODE=1` to call `python main.py --run-mode daily --dry-run`
- `DRY_RUN_MODE=1` follows the current application dry-run behavior, which may still emit cache-only watchlist artifacts
- for a strictly preflight-only validation, run `bash scripts/preflight.sh` without the wrapper

## Weekly

Command:
`python main.py --run-mode weekly`

Expected additions:

- digest-oriented reporting
- broader recommendation and memo context
- same artifact/update path as daily

## Monthly

Command:
`python main.py --run-mode monthly`

Expected additions:

- full FMP candidate scan
- contribution planning
- compounding dashboard
- richer memo artifacts
- theme boosts on scanner candidates when available

## Watchlist-Only Run

Command:
`python -m watchlist_scanner`

Expected outputs:

- `outputs/latest/watchlist_signals.json`
- `outputs/latest/watchlist_alerts.csv`
- `outputs/latest/watchlist_summary.md`
- `outputs/portfolio/portfolio_snapshot.json`
- `outputs/portfolio/portfolio_summary.md`
- `outputs/performance/performance_summary.json`

## Theme-Only Run

Command:
`python -m theme_engine --mode daily`

Expected outputs:

- `outputs/latest/theme_signals.json`
- `outputs/latest/watch_candidates.json`
- `theme_signals` rows in SQLite

## Expected Core Artifacts

After a healthy full run, expect at least:

- `outputs/latest/watchlist_signals.json`
- `outputs/latest/theme_signals.json`
- `outputs/latest/watch_candidates.json`
- `outputs/portfolio/portfolio_snapshot.json`
- `outputs/policy/policy_recommendation.json`
- `outputs/policy/recommendation_evaluation.json`
- `outputs/performance/performance_summary.json`
- `outputs/latest/system_decision_summary.json`

## Same-Day Rerun After Code Deployment

### When to use this

Use `bash scripts/rerun_today_safe.sh` when:

- You deployed a code fix mid-day and `outputs/latest` artifacts are stale.
- The daily pipeline already ran once today, recorded `status='completed'` in `run_history`, and will refuse to run again because the idempotency guard treats today's run as done.
- You need a fresh run of `main.py --run-mode daily` with the new code, same calendar day.

### When NOT to use this

Do NOT use this script when:

- The pipeline failed partway through — `status='failed'` already means it will retry on the next scheduled run. Use `bash scripts/run_daily_safe.sh` directly.
- You want to re-run only the watchlist scanner — use `python -m watchlist_scanner` instead.
- You want a dry run — use `DRY_RUN_MODE=1 bash scripts/run_daily_safe.sh`.
- You are not sure why the previous run produced stale output — investigate the logs first.

### Why SQLite run_history is the idempotency source

`main.py` checks `run_history` before executing. If a row for today's `run_id` (`YYYY-MM-DD_daily`) exists with `status='completed'`, the pipeline exits early to prevent double-runs, double-archiving, and duplicate state writes.

`rerun_today_safe.sh` resets exactly that one row to `status='failed'` — the minimum change that unlocks a re-run. It does not touch any other rows, does not delete the database, and does not remove `outputs/` or `outputs/history/`. After the re-run completes, `main.py` writes the row back to `status='completed'` with a fresh `completed_at`.

### What the script does step by step

1. Detects repo root and activates `.venv`.
2. Looks up today's `run_history` row and prints it — if the row does not exist, exits without changes.
3. Requires you to type `rerun` exactly to confirm (any other input aborts with no changes).
4. Runs `UPDATE run_history SET status='failed', completed_at=NULL WHERE run_id='YYYY-MM-DD_daily'`.
5. Runs `bash scripts/preflight.sh` — FMP compliance check, FMP test suite, API key validation.
6. Runs `python main.py --run-mode daily`.
7. Verifies `outputs/latest/decision_plan.json` exists and that the first decision row contains `decision_reason` and `decision_reason_structured`.
8. Prints the final `run_history` row so you can confirm `status='completed'`.

### Command

```bash
bash scripts/rerun_today_safe.sh
```

Logs are written to `logs/rerun_YYYY-MM-DD_HHMMSS.log`.

### Minimal same-day rerun workaround

If you need the exact manual override instead of the wrapper, reset today's `run_history` row and rerun:

```bash
sqlite3 data/portfolio.db "UPDATE run_history SET status='failed', completed_at=NULL WHERE run_id='<YYYY-MM-DD>_daily';"
python main.py --run-mode daily
```

Use this only when today's run already completed and you intentionally need a fresh same-day rewrite of `outputs/latest`.

---

## Common Failures And Fixes

### Missing `FMP_API_KEY`

Symptoms:

- broader-market scanner unavailable
- FMP fallback disabled
- preflight fails before the daily run starts

Fix:

- add `FMP_API_KEY` to `.env`
- rerun `bash scripts/preflight.sh`

### Missing `ALPHA_VANTAGE_API_KEY`

Symptoms:

- watchlist scan cannot fetch live AV data

Fix:

- add `ALPHA_VANTAGE_API_KEY` to `.env`
- use `--dry-run` only when cache is intentionally being reused

### FMP Compliance Failure

Symptoms:

- `python -m fmp_endpoint_compliance` is non-compliant
- wrapper stops before the daily run

Fix:

- inspect registry coverage before touching the daily pipeline
- restore compliant endpoint usage
- do not bypass the registry with direct URLs

### FMP Circuit Breaker Open

Symptoms:

- scanner skipped with subsystem disabled message

Fix:

- inspect `subsystem_health` in SQLite
- correct the credential or provider issue
- clear or reset the subsystem row only after root cause is fixed

### Budget Exhaustion

Symptoms:

- `degraded_mode`
- `cache_only`
- `fallback_watchlist`
- stale-cache warnings

Fix:

- accept degraded output when appropriate
- warm caches with fuller runs
- reduce requested universe breadth if needed

### Missing Recommendation History

Symptoms:

- `recommendation_evaluation.json` has zeros or empty dicts

Fix:

- run `main.py` enough times to produce scored recommendation history
- do not treat this as a pipeline error

### AI Validation Unavailable

Symptoms:

- `ai_decision_validation.json` missing
- GUI `AI Validation` section unavailable

Fix:

- confirm `decision_plan.json` exists first
- inspect validator logs after the decision-plan write step
- remember validator failures are non-fatal by design

### Outcome Tracker Sparse Or Empty

Symptoms:

- `decision_outcome_summary.json` exists but shows low counts or `null` hit rate
- GUI `Decision Performance` section has little data

Fix:

- allow more daily runs to accumulate history
- confirm `outputs/policy/decision_outcomes.jsonl` is being appended
- do not treat unresolved or low-history states as pipeline failure

### GUI Missing Data

Symptoms:

- dashboard pages show missing optional artifacts

Fix:

- confirm required JSON files exist
- run `main.py` or `run_daily_pipeline.py`
- remember that many GUI panels intentionally degrade gracefully

## Operator Notes

- `outputs/latest` is the current working set.
- `outputs/history/YYYY-MM-DD` is the archived daily snapshot after successful `main.py` runs.
- SQLite is persistent system memory. Deleting it resets cooldowns, evaluation state, and subsystem health.
- For VPS automation, prefer `bash scripts/run_daily_safe.sh` over direct cron calls to `python main.py`.
