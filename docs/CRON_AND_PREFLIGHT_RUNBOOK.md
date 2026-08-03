# Cron And Preflight Runbook

Last updated 2026-05-20.

## Purpose

Use these wrappers for production-safe daily operation. They do not change scoring or execution behavior; they only gate the advisory pipeline behind environment, compliance, and regression checks.

## Manual Preflight

Run from the repo root:

```bash
bash scripts/preflight.sh
```

What it validates:

- repo root detection
- `.venv` presence and active python path
- required FMP compliance files
- `FMP_API_KEY` from environment or `.env`
- `python -m fmp_endpoint_compliance` returns `RESULT: COMPLIANT`
- `python -m pytest tests/ -k fmp -v` passes
- targeted `py_compile` succeeds (now covers the six observability v2
  modules: `risk_delta_advisor`, `retune_impact_tracker`,
  `fmp_budget_telemetry`, `daily_run_status`, `resolution_due_probe`,
  `news/run_news_intelligence`)
- "Wrapper Syntax Check" — `bash -n scripts/run_daily_safe.sh`
- "Advisor Smoke Imports" — imports each observability v2 module to catch
  missing-symbol regressions before the safe-wrapper run starts

## Manual Daily Safe Run

Normal production-style run:

```bash
bash scripts/run_daily_safe.sh
```

Validation run using application dry-run semantics:

```bash
DRY_RUN_MODE=1 bash scripts/run_daily_safe.sh
```

Behavior:

- creates `logs/daily_safe_YYYY-MM-DD.log` if needed
- runs preflight first
- stops immediately if preflight fails
- only runs `python main.py --run-mode daily` after preflight passes (Stage 1 —
  the only fail-fast stage)
- runs the 16 non-blocking post-pipeline stages after Stage 1 succeeds —
  news intelligence (0/8/8b), weight tuning, policy evaluator, allocation
  preview/simulation/activation, system summary, risk delta, retune
  impact, FMP budget, resolution-due probe, automatic promotion governance,
  sandbox lane status, daily memo + email, and daily run status. See
  `docs/PIPELINE_RUNBOOK.md` for the full stage table.
- preserves the real process exit code for Stage 1; subsequent stages do not
  alter the exit code (failures are recorded in `daily_run_status.json`)
- `DRY_RUN_MODE=1` passes `--dry-run` to `main.py`, but current watchlist components may still emit cache-only artifacts
- for a strictly preflight-only, no-pipeline validation, run `bash scripts/preflight.sh` by itself

## Example Cron Entry

```cron
0 7 * * 1-5 cd /opt/stockbot && bash scripts/run_daily_safe.sh >> logs/cron_daily_safe.log 2>&1
```

### Monthly universe-membership refresh (added 2026-08-03)

```cron
30 6 1 * * /opt/stockbot/scripts/run_monthly_universe_refresh.sh
```

Forces a full S&P 500 constituent re-resolution + `full_scan` so watchlist
**membership** stays current. Not required for recovery — the weekly path already
self-heals whenever the cached watchlist falls below the trust floor — but
`weekly_refresh()` only re-filters what is already cached, so without this the
universe never gains new members while the cache looks healthy.

Deliberately **not** `--run-mode monthly`: that mode also applies theme boosts to
scanner candidates and routes email through `send_monthly_memo`, which is more
than a membership refresh. The wrapper uses the narrowest reusable mechanism,
`main.py --run-mode weekly --force-universe-refresh`.

- **Call volume:** ≈3,700 FMP calls per full scan (~503 profile + ~503
  key-metrics/financial-growth + ~503 quote), measured live 2026-08-03. Profiles
  cache 7d, key-metrics 30d. Safe monthly on a flat subscription with the
  uncapped `daily` run-mode budget; **do not shorten the cadence**.
- **Timing:** 1st of the month, 06:30 UTC — pre-market and clear of the Monday
  08:00 weekly / 08:30 ETF / 09:00 daily cluster.
- **Overlap safety:** takes the same `stockbot-discovery-pulse.lock` flock as the
  weekly run and discovery pulses, so it skips cleanly rather than overlapping.
- Writes `outputs/policy/scanner_recovery_canary.{json,md}` for acceptance.

Notes:

- run from repo root so relative paths remain stable
- keep `.venv` and `.env` in `/opt/stockbot`
- use `DRY_RUN_MODE=1` only for validation or incident isolation, not for normal production runs
- when you need a no-pipeline check, run only `bash scripts/preflight.sh`

## Log Inspection

Primary wrapper log:

```bash
tail -n 200 logs/daily_safe_$(date +%F).log
```

Cron append-only log:

```bash
tail -n 200 logs/cron_daily_safe.log
```

Search for failures:

```bash
grep -n "FAIL\\|DAILY RUN FAILED\\|RESULT:" logs/daily_safe_$(date +%F).log
```

## Common Failures And Fixes

### `.venv` Missing

Symptoms:

- preflight fails before compliance or tests begin

Fix:

- recreate the virtual environment in `/opt/stockbot/.venv`
- reinstall dependencies with `python -m pip install -r requirements.txt`

### Wrong Python

Symptoms:

- preflight reports that the active python is not from `.venv`

Fix:

- run through `bash scripts/preflight.sh` or `bash scripts/run_daily_safe.sh`
- avoid calling a system `python` directly for production runs

### `FMP_API_KEY` Missing

Symptoms:

- preflight fails in the environment section

Fix:

- export `FMP_API_KEY` in the shell, or
- add `FMP_API_KEY=...` to `/opt/stockbot/.env`

### Compliance Failed

Symptoms:

- `python -m fmp_endpoint_compliance` exits nonzero
- output does not include `RESULT: COMPLIANT`

Fix:

- inspect `fmp_endpoint_registry.py`, `fmp_client.py`, and any new FMP call sites
- restore registry coverage for every endpoint
- do not bypass the registry with direct URLs

### Tests Failed

Symptoms:

- `python -m pytest tests/ -k fmp -v` reports failures

Fix:

- inspect the failing FMP tests first
- resolve schema, registry, fallback, or stable-endpoint regressions before rerunning preflight

### Daily Pipeline Failed

Symptoms:

- preflight passes but `DAILY RUN FAILED` appears after the main run starts

Fix:

- inspect `logs/daily_safe_YYYY-MM-DD.log`
- rerun `DRY_RUN_MODE=1 bash scripts/run_daily_safe.sh` to separate wrapper/preflight behavior from full side effects
- if needed, run `python main.py --run-mode daily --dry-run` manually inside `.venv` for focused debugging
- if you need a strictly no-pipeline validation, stop at `bash scripts/preflight.sh`
