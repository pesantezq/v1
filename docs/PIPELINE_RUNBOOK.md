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
- records snapshot and run history in SQLite
- copies successful outputs to `outputs/history/YYYY-MM-DD`

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
