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
- `python -m tools.manual_portfolio_update --input <csv> --cash <n> --as-of <YYYY-MM-DD> --approve` — operator-driven manual holdings/cash update; see [MANUAL_PORTFOLIO_UPDATE.md](MANUAL_PORTFOLIO_UPDATE.md)
- `python -m tools.daily_sandbox_run` (or `bash scripts/run_daily_sandbox_safe.sh`) — observe-only daily sandbox/research lane orchestrator; see [DAILY_SANDBOX_RUN.md](DAILY_SANDBOX_RUN.md)

## Daily

### Safe-wrapper 13-stage pipeline (production cron path)

Command:
`bash scripts/run_daily_safe.sh`

This is what cron runs at 09:00 UTC (`crontab -l`). Each stage is logged
to `logs/daily_safe_YYYY-MM-DD.log` with a `== Stage Name ==` banner. The
official decision-emitting stage (Stage 1) is fail-fast; everything after
it is non-blocking so a single observability advisor failing cannot
abort the run after the official plan has landed.

| # | Stage | Producer | Primary artifact(s) |
|---|---|---|---|
| 0 | News intelligence (pre-pipeline) | `portfolio_automation.news.run_news_intelligence` | `outputs/latest/news_intelligence.json` |
| 1 | Daily pipeline (FAIL-FAST) | `main.py --run-mode daily` | `outputs/latest/decision_plan.json` + `.md` |
| 2 | Weight tuning | `watchlist_scanner.weight_tuning` | `outputs/performance/weight_tuning_suggestions.json` |
| 3 | Policy evaluator | `policy_evaluator.evaluator` | `outputs/policy/*` |
| 4 | Allocation preview | `watchlist_scanner.allocation_preview` | `outputs/latest/allocation_preview.json` |
| 5 | Allocation policy simulation | `watchlist_scanner.allocation_policy_simulation` | `outputs/performance/allocation_policy_simulation.json` |
| 6 | Allocation policy activation | `watchlist_scanner.allocation_policy_activation` | `outputs/performance/approved_*.json` (when all rules pass) |
| 7 | System decision summary | `watchlist_scanner.system_summary` | `outputs/latest/system_decision_summary.json` + `.md` |
| 7b | Risk delta panel | `portfolio_automation.risk_delta_advisor` | `outputs/latest/risk_delta.json` + `.md` |
| 7c | Retune impact tracker | `portfolio_automation.retune_impact_tracker` | `outputs/latest/retune_impact.json` + `data/gauge_versions.jsonl` |
| 7d | FMP budget telemetry | `portfolio_automation.fmp_budget_telemetry` | `outputs/latest/fmp_budget_status.json` + `data/fmp_budget_history.jsonl` |
| 8 | News intelligence (post-pipeline refresh) | `portfolio_automation.news.run_news_intelligence` | rewrites `outputs/latest/news_intelligence.json` (cache hits, 0 budget) |
| 8b | Discovery news integration | `portfolio_automation.discovery.news_integration` | `outputs/sandbox/discovery/news_enriched_candidates.json` |
| 9 | Automatic promotion governance | `portfolio_automation.discovery.automatic_promotion_governance` | `outputs/sandbox/discovery/automatic_promotion_*.json` |
| 9c | Crowd Radar (public knowledge velocity) | `portfolio_automation.social_intelligence.public_knowledge_velocity` | `outputs/sandbox/discovery/crowd_knowledge_state.json` + 4 more (observe-only, sandbox-only, **default-disabled**) |
| 9c1 | Crowd Radar multi-source (no-extra-cost) | `portfolio_automation.social_sources.run_multi_source_crowd` | `crowd_source_dev_doc_audit.json` + `crowd_source_health.json` + `crowd_multi_source_velocity.json` + summary (ApeWisdom active; FMP/Finnhub probes; Stocktwits/Quiver blocked) |
| 9c2 | Crowd Radar activation check | `portfolio_automation.social_intelligence.activation_check` | `crowd_radar_activation_check.json` (multi-source readiness; pure; reads 9c1 health) |
| 10 | Daily memo + email | `watchlist_scanner.daily_memo` | `outputs/latest/daily_memo.{txt,md}` |
| 11 | Daily run status | `portfolio_automation.daily_run_status` | `outputs/latest/daily_run_status.json` + `.md` |

Stages 0 + 8 are deliberately paired: the pre-pipeline run gets first
claim on the FMP daily budget (one batched call), and the post-pipeline
refresh hits cache (zero budget). When Stage 0 fails (e.g. FMP outage),
the scanner can still proceed but discovery enrichment sees zero news
packets — that's surfaced in the memo's "FMP budget" line.

The daily memo (Stage 10) reads `system_decision_summary.json` for its
generated-at timestamp; Stage 7 must therefore run before Stage 10. If
this ordering ever breaks, the memo header will display the previous
summary's date and operators will see a stale-data banner.

### Forcing a re-run on the same calendar day

The pipeline is idempotent — `main.py` checks
`PortfolioStateStore.is_completed(run_id)` and exits 0 with
`skip_reason=idempotent_already_completed` when today's run already
finished. To force a re-run (e.g. after a config tweak), mark today
failed first:

```bash
.venv/bin/python -c "from state_store import PortfolioStateStore; \
  from datetime import date; \
  PortfolioStateStore().fail_run(f'{date.today().isoformat()}_daily')"
bash scripts/run_daily_safe.sh
```

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

### Daily sandbox/research lane

Command:
`python -m tools.daily_sandbox_run`  (or `bash scripts/run_daily_sandbox_safe.sh`)

Purpose: refresh the sandbox/research lane artifacts (news-enriched
candidates, automatic promotion governance) on a daily cadence —
independent of, and non-blocking to, the official daily pipeline.

Steps (each is non-blocking; failure of one does not abort the others):

1. `discovery_news_integration` — calls `run_discovery_news_integration(run_mode="discovery")`
2. `automatic_promotion_governance` — calls `run_automatic_promotion_governance(run_mode="discovery", write_files=True)`
3. `discovery_replay` — runs only if `outputs/sandbox/discovery/replay_price_outcomes.json` is present (skipped otherwise)

Outputs (sandbox namespace only):

- `outputs/sandbox/discovery/sandbox_run_status.json`
- `outputs/sandbox/discovery/sandbox_run_status.md`
- (plus any artifacts written by the underlying module entry points)

Safety:

- observe-only; never calls brokers, never executes trades
- never mutates `config.json`, the watchlist, allocation policy, scoring,
  or any decision artifact
- only writes to `outputs/sandbox/discovery/`
- exits 0 even if individual steps fail (status artifact records each step)

Optional environment:

- `DRY_RUN_MODE=1 bash scripts/run_daily_sandbox_safe.sh` — runs the
  module steps but skips writing the sandbox_run_status artifacts

See [DAILY_SANDBOX_RUN.md](DAILY_SANDBOX_RUN.md) for the full spec.

### Crowd Radar / Public Knowledge Velocity Layer (Stage 9c)

`python -m portfolio_automation.social_intelligence.public_knowledge_velocity --root . --run-mode discovery`

Purpose: classify the state of public knowledge around tickers from
API-compliant public discussion (Reddit-first). **Observe-only, sandbox-only,
default-disabled.** Writes 5 artifacts under `outputs/sandbox/discovery/`
(`crowd_knowledge_state`, `public_knowledge_velocity`, `social_signal_backtest`,
`social_source_compliance`, `crowd_radar_summary.md`).

Enable / disable:

- Master switch: `config.json` → `crowd_radar.enabled` (default `false`).
- Credentials: `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT`
  (absent → `source_status=no_credentials`, no network call).
- Kill-switch: `config/crowd_radar.DISABLED` file **or**
  `STOCKBOT_CROWD_RADAR_DISABLED=1`.

Failure modes (all write a degraded artifact; the daily run is never aborted):

| Condition | `source_status` |
|---|---|
| `enabled=false` / kill-switch | `disabled` |
| no REDDIT_* creds | `no_credentials` |
| API 429 | `rate_limited` |
| ToS review lapsed | `source_terms_blocked` |
| fetched but nothing classifiable | `insufficient_data` |
| unexpected error | `error` |

Safety: runs in `discovery` run-mode so it MAY write the sandbox namespace, but
the run-mode governance layer forbids it from writing `outputs/latest/` /
`decision_plan.json`. Crowd signals adjust a capped `crowd_research_priority_score`
only — never BUY/SELL/HOLD/REBALANCE/TRIM/SCALE/PROMOTE. See
[PUBLIC_KNOWLEDGE_VELOCITY_LAYER.md](PUBLIC_KNOWLEDGE_VELOCITY_LAYER.md).

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

## Daily Memo Generation and Email Delivery

### Generating the daily memo

Command:
```
python -m watchlist_scanner.daily_memo
```

Writes `outputs/latest/daily_memo.txt` and `outputs/latest/daily_memo.md`.  Optionally appends a sandbox Discovery Research section when sandbox discovery artifacts exist.

### Memo email delivery

Controlled by `portfolio_automation/memo_email_sender.py`.  **Disabled by default** — no SMTP connections unless `MEMO_EMAIL_ENABLED=1`.

#### Required environment variables

| Variable | Default | Description |
|---|---|---|
| `MEMO_EMAIL_ENABLED` | `0` | Set to `1` to enable delivery |
| `MEMO_EMAIL_DRY_RUN` | `1` | Set to `0` to send (CLI overrides this) |
| `MEMO_EMAIL_SMTP_HOST` | — | SMTP server hostname |
| `MEMO_EMAIL_SMTP_PORT` | `587` | SMTP port |
| `MEMO_EMAIL_USERNAME` | — | SMTP auth username |
| `MEMO_EMAIL_PASSWORD` | — | SMTP auth password (never logged) |
| `MEMO_EMAIL_FROM` | — | From address |
| `MEMO_EMAIL_TO` | — | Comma-separated To recipients |
| `MEMO_EMAIL_CC` | — | Comma-separated CC (optional) |
| `MEMO_EMAIL_BCC` | — | Comma-separated BCC (optional) |
| `MEMO_EMAIL_USE_TLS` | `1` | STARTTLS (set `0` for plain) |
| `MEMO_EMAIL_SUBJECT_PREFIX` | — | Optional subject prefix (e.g. `[PROD]`) |
| `MEMO_EMAIL_STRICT_FAILURE` | `0` | Set to `1` to raise on SMTP error |
| `MEMO_EMAIL_FORCE_RESEND` | `0` | Set to `1` to bypass duplicate-send protection |

#### Gmail / app-password note

For Gmail, generate an **App Password** (not your account password) under Google Account → Security → App Passwords.  Use it as `MEMO_EMAIL_PASSWORD`.

#### CLI usage

```bash
# Verify config and message build — no SMTP connection
python -m portfolio_automation.memo_email_sender --dry-run

# Send (requires MEMO_EMAIL_SMTP_HOST / USERNAME / PASSWORD / FROM / TO in env)
python -m portfolio_automation.memo_email_sender --send

# Re-send even if already sent today
python -m portfolio_automation.memo_email_sender --force-resend
```

#### Idempotency

The module reads `outputs/policy/memo_delivery_log.jsonl` before sending.  If a `sent=true` entry exists for the same `run_id` or `memo_date`, the send is skipped.  Dry-run runs do **not** create idempotency records.

#### Troubleshooting

- **`reason: disabled`** — set `MEMO_EMAIL_ENABLED=1`
- **`reason: missing_smtp_config`** — check `MEMO_EMAIL_SMTP_HOST`, `MEMO_EMAIL_USERNAME`, `MEMO_EMAIL_PASSWORD`, `MEMO_EMAIL_FROM`
- **`reason: invalid_or_missing_recipients`** — check `MEMO_EMAIL_TO` is a valid `user@domain.com` address
- **`reason: memo_file_missing`** — run `python -m watchlist_scanner.daily_memo` first to generate memo files
- **`reason: already_sent`** — set `MEMO_EMAIL_FORCE_RESEND=1` to override
- **SMTP error** — check `error_class` and `error_message_sanitized` in `outputs/latest/memo_delivery_status.json`

#### Safety constraints

- Never executes trades or modifies portfolio state
- Never calls AI/LLM or market-data APIs
- Never logs or writes SMTP password or secrets to any artifact
- `observe_only: true`, `no_trade: true` hard-coded in every output
- Failure is non-blocking by default (`MEMO_EMAIL_STRICT_FAILURE=0`)

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


## GUI v2 dashboard (opt-in)

The FastAPI-based dashboard runs alongside the existing Streamlit GUI.
Streamlit on port 8501 is unchanged; this is port 8502.

**Enable** (one-time):

```bash
sudo cp /opt/stockbot/deploy/systemd/stockbot-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now stockbot-dashboard.service
journalctl -u stockbot-dashboard.service -n 30 --no-pager
```

**URL:** `http://<vps-ip>:8502`. Restrict via cloud firewall or use an SSH
tunnel — same security posture as 8501.

**Disable / retire:**

```bash
sudo systemctl disable --now stockbot-dashboard.service
```

The daily timer and the Streamlit unit are unaffected by any of this.

## Operator control / work orders (Phase 1)

The dashboard can turn health/quality probes into **allowlisted work orders**
(observe-only; the web app only *creates* records, it never executes a worker).
State lives in `outputs/operator_control/` (append-only `work_orders.jsonl` +
`audit_log.jsonl`; generated `prompts/`). CLI:

```bash
python -m operator_control.work_orders list
python -m operator_control.work_orders create --probe-id data_quality.warnings \
    --skill-id diagnose_data_quality_warnings --mode diagnose --created-by enrique_cli
python -m operator_control.work_orders show --id <id>
python -m operator_control.work_orders generate-prompt --id <id>
```

Full architecture, safety model, lifecycle, and the recommended Phase 2 worker
runner: see `docs/operator_control.md`.

### Portfolio Simulation Suite (weekly, sandbox)

`python -m portfolio_automation.portfolio_sim.run_portfolio_backtest --root . --run-mode discovery`
`python -m portfolio_automation.portfolio_sim.run_portfolio_projection --root . --run-mode discovery`

Two weekly stages in `run_weekly_safe.sh` (non-blocking). **Observe-only,
sandbox-only, default-disabled.** Objective: maximize excess return vs the S&P 500.

Enable: `config.json portfolio_sim.enabled=true` (the suite reads the offline 5y
price archive — no FMP/network needed when archives are present; missing tickers
are recorded under `missing_price_history` and backfilled via free FMP if a client
is wired). Writes 5 artifacts under `outputs/sandbox/` + the auto-generated
`docs/STRATEGY_CATALOG.md`. Disabled / insufficient-data → degraded artifact, no
crash. Surfaced in the GUI Strategy Lab tab (Backtest + Projection sections).
See [superpowers spec](superpowers/specs/2026-06-12-portfolio-tactic-backtest-design.md).

---

## FMP Budget-Aware Data Orchestrator (governor)

A single guarded FMP access layer in `portfolio_automation/data_budget/` that
wraps the existing `fmp_client.FMPClient` (keeping its file `_DiskCache` +
`_CallCounter` + endpoint registry). **All** FMP call sites obtain their client
via the factory instead of constructing `FMPClient` directly:

```python
from portfolio_automation.data_budget.factory import governed_client
client = governed_client("daily")   # or gui_refresh / weekly_review / monthly / discovery / historical_replay
```

An AST guard test (`tests/test_data_budget_no_direct_construction.py`) forbids
direct `FMPClient()` construction outside the sanctioned low-level set
(`fmp_client.py`, the governor/factory, backtests, replay_runner, scripts, tests).

**Limits enforced** (the governor is the budget authority; the inner FMPClient is
constructed uncapped, `daily_budget=0`, so it does not double-cap):
- **Token bucket** — `rate_per_min` 240 sustained, `burst` 300 hard cap (in-process,
  per run). High-priority run modes wait briefly when the bucket is empty;
  low-priority (discovery) skip → serve cache/stale.
- **Per-run-mode call budgets** (`config.json data_budget.run_modes`) — `call_budget: 0`
  means uncapped. Rationale: `gui_refresh` 30 (cache-first, light); `daily` 0
  (uncapped — the main pipeline, matches `api_limits.fmp_daily_calls_budget=0`);
  `weekly_review` 800 / `monthly` 1500 (bounded review windows); `discovery` 650
  (low priority, first skipped — sized to cover a cold crowd-intelligence run over the
  capped 60-symbol universe: ~9 per-symbol endpoints × 60 + ~11 shared ≈ 551, plus
  headroom; all within the existing paid Starter allowance — no extra cost);
  `historical_replay` `cache_only` (0 live — replay from the 5y archive, fetch only on
  cache miss).
- **Monthly bandwidth guard** — `monthly_bandwidth_gb` 20 (real response bytes summed
  in the `api_usage_ledger`). At the cap, low-priority run modes are disabled;
  portfolio/decision data is never blocked.

**Storage**: `data/fmp_budget.db` (SQLite, separate from `portfolio.db`) —
`api_usage_ledger` (per-call: ts, run_mode, endpoint, symbols, cache_hit, bytes,
skipped_reason) + `symbol_data_policy` (per-symbol ttl/priority).

**Artifacts** (Stage 7d2, observe-only): `fmp_usage_status.json`,
`fmp_cache_status.json`, `data_budget_status.json` (see OUTPUT_ARTIFACT_CONTRACTS).
Health folded into `/daily-tool-analysis` (line 6m) + GUI System panel.

**Kill-switch** (instant revert to direct `fmp_client` behavior): any of
`config.json data_budget.enabled=false`, env `STOCKBOT_FMP_GOVERNOR_DISABLED=1`,
or a `config/fmp_governor.DISABLED` file. Ships **enabled**.

See [design spec](superpowers/specs/2026-06-15-fmp-budget-governor-design.md) and
[plan](superpowers/plans/2026-06-15-fmp-budget-governor.md).

---

## Crowd Intelligence (observe-only)

Probe-gated FMP crowd-context layer (`portfolio_automation/crowd_intelligence/`).
**Context only — never feeds `decision_plan.json`, scoring, allocation, or trades.**

- **Phase 1 — capability probe** (manual diagnostic):
  ```bash
  cd /opt/stockbot && set -a; . ./.env; set +a
  .venv/bin/python scripts/probe_fmp_crowd_endpoints.py
  ```
  → `outputs/latest/fmp_endpoint_capabilities.json` + summary + DB table. Re-run
  after any FMP plan change.
- **Phase 2A — normalized context** (manual entrypoint; NOT cron-wired yet):
  ```bash
  .venv/bin/python -m portfolio_automation.crowd_intelligence.artifact_writer
  ```
  → `crowd_intelligence.json` / `.md` / `_status.json` for the holdings universe.
  Reads the Phase-1 capability map (skips PLAN_LOCKED), fetches AVAILABLE endpoints
  via the governed `FMPClient.get_json`, persists `crowd_raw_events` +
  `crowd_signal_daily`. Direct social sentiment is PLAN_LOCKED on Starter and stays
  disabled (weight 0).
- **Phase 2B — GUI context + daily wiring** (observe-only, artifact-only): the
  Portfolio tab shows context-only crowd cards per advisory pick (label, score,
  confidence, sources, reasons, enrichment lines) via `context_loader` +
  `advisory_context_enricher` + `gui_v2/data/dash_crowd_context.py`. **Daily stage
  7d3** (`run_aux_stage "Crowd intelligence"`) regenerates the artifacts post-pipeline,
  non-blocking. No FMP/HTTP/governor in 2B; never changes decisions/allocations.

See [Phase 1 spec](superpowers/specs/2026-06-15-crowd-intelligence-phase1-design.md),
[Phase 2A spec](superpowers/specs/2026-06-15-crowd-intelligence-phase2a-design.md),
[Phase 2B spec](superpowers/specs/2026-06-15-crowd-intelligence-phase2b-design.md),
and `docs/CROWD_INTELLIGENCE.md`.
