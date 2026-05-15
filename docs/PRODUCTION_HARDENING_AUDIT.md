# Production Hardening Audit

Date: 2026-05-14
Author: senior architect review (read-only)
Scope: entry points, run modes, state boundaries, namespaces, failure isolation,
idempotency, logs/status, testability, rollback, operator workflow.

This document is an audit. It proposes no code changes by itself. It is the
basis for a phased hardening track that runs alongside (not instead of) the
official roadmap step `gui_operator_cockpit_redesign`.

## Working Constraints

- No broad rewrites.
- No business-logic changes (scoring / allocation / recommendations / decisions).
- No safety-boundary changes (advisory-only, no broker integration).
- All work is additive and reversible.

---

## 1. What's Working Well

These are intentional design choices that already pay off. Hardening must preserve them.

| Area | Status | Evidence |
|------|--------|----------|
| Two-lane operating model | Clearly declared in policy | `portfolio_automation/run_mode_governance.py` defines `RunModePolicy` for all 6 modes |
| Sandbox lane status artifact | Implemented and consistent | `outputs/sandbox/discovery/sandbox_run_status.json` written by `tools/daily_sandbox_run.py` |
| Manual update approval gate | Enforced | `tools/manual_portfolio_update.py:71` calls `assert_can_update_portfolio_state(approved=True)` |
| Decision plan → validator → outcome tracker chain | Observable, idempotent | `outputs/policy/decision_outcomes.jsonl` deduplicates by `run_id` |
| Config schema validation at load time | Enforced | `config/schema.py:validate_structured_config()` raises `ConfigValidationError` |
| Preflight checks | Comprehensive | `scripts/preflight.sh` validates venv, FMP compliance, pytest, key presence |
| API key redaction in agent layer | Implemented | `agent/io_utils.py:_REDACT_RE` covers common token patterns |
| Email send dedup | Hash-based via SQLite | `email_history` table `INSERT OR REPLACE` on `digest_hash` |
| Append-only audit logs in policy namespace | Multiple | `decision_outcomes.jsonl`, `memo_delivery_log.jsonl`, `approval_decisions.jsonl` |
| FAIL-FAST on stage 1 of daily chain | Operator-correct | `scripts/run_daily.sh` propagates main.py exit code; later stages non-blocking |
| Cleanly separated namespace policy | Documented | `portfolio_automation/data_governance.py` + `docs/OUTPUT_ARTIFACT_CONTRACTS.md` |

---

## 2. Production Surface

### 2.1 Entry Points (verified against code)

| Entry Point | Run Mode | Status Artifact | Exit Convention |
|-------------|----------|-----------------|-----------------|
| `main.py --run-mode daily\|weekly\|monthly` | `DAILY` / `WEEKLY_REVIEW` | None (result dict in memory only) | 0=success, 1=failure |
| `run_daily_pipeline.py` | Not declared | `StepResult` list printed to stderr | 0 if all ok, 1 if any failed |
| `tools/daily_sandbox_run.py` | `DISCOVERY` (hardcoded) | `outputs/sandbox/discovery/sandbox_run_status.{json,md}` | Always 0 |
| `tools/manual_portfolio_update.py --approve` | `MANUAL_UPDATE` | `outputs/policy/manual_portfolio_updates.jsonl` + config backup | non-zero on validation/approval failure |
| `policy_evaluator/__main__.py` | Not declared | None | 0 / 1 |
| `scripts/run_daily_safe.sh` | wraps main.py | log file only | propagates main.py |
| `scripts/run_daily.sh` | orchestrates 4 stages | log file only | propagates stage 1 |
| `scripts/run_daily_sandbox_safe.sh` | wraps daily_sandbox_run | log file + status artifact | always 0 |
| systemd: `stockbot-daily.timer` | 06:30 daily | n/a | journal |
| systemd: `stockbot-sandbox-daily.timer` | weekdays 09:30 (example, not enabled) | n/a | journal |
| systemd: `stockbot-streamlit.service` | always-on (GUI paused) | n/a | journal |

### 2.2 Artifact Namespaces (verified)

| Namespace | Path | Writers | Status |
|-----------|------|---------|--------|
| LATEST | `outputs/latest/` | live pipeline (~30 artifacts) | Backed by docs/OUTPUT_ARTIFACT_CONTRACTS.md |
| POLICY | `outputs/policy/` | audit + governance (~10 artifacts) | Mix of overwrite and append-only |
| PORTFOLIO | `outputs/portfolio/` | snapshot writer | Two artifacts |
| PERFORMANCE | `outputs/performance/` | weight tuning, alloc | ~6 artifacts |
| SANDBOX | `outputs/sandbox/discovery/` | research lane only | Governance: `discover_only=true`, `sandbox_only=true` flags hardcoded |
| HISTORICAL | `outputs/backtest/` | replay only | Operator-triggered only |

---

## 3. Gaps By Dimension

Each gap has a severity (low / medium / high), a location, and a proposed action category
(matches the user's preferred ordering in section 5). No fixes are proposed inline.

### 3.1 Entry Points

**G-E1 — `run_daily_pipeline.py` has no run-mode declaration.** [medium]
- Source: orchestration audit (see section 2.1).
- Risk: a future caller can invoke it from a sandbox context and silently produce
  artifacts in `outputs/latest/` because the writers downstream don't check policy.
- Category: *standardize orchestration*.

**G-E2 — `policy_evaluator/__main__.py` has no run-mode declaration and no documented schedule.** [low]
- Risk: an operator running this ad-hoc writes to `outputs/policy/` without an audit trail.
- Category: *standardize orchestration*.

**G-E3 — `main.py` produces no status artifact for the official lane.** [high]
- The function returns a structured dict (`main.py:585–599`) but it is never serialized.
- Operators cannot answer "did today's run succeed?" without grepping `logs/YYYY-MM-DD.log` or
  inspecting the SQLite `run_history` table.
- The sandbox lane has `sandbox_run_status.json` — the official lane has nothing equivalent.
- Category: *standardize orchestration results* + *health/status*.

### 3.2 Run Modes

**G-R1 — Run-mode governance is advisory, not enforced.** [high]
- `main.py:2670–2678` instantiates `RunModeContext` and logs the policy, but downstream writers
  never call `assert_can_write_namespace()` or `assert_can_emit_recommendation()`.
- Two-lane separation today depends on convention (which writer is called) rather than the write
  path itself.
- The sandbox lane is correctly hardcoded to `RunMode.DISCOVERY` in `tools/daily_sandbox_run.py:69`,
  so the practical risk is low — but the *invariant* is not load-bearing in code.
- Category: *standardize artifact registry* (writers gain a guard at the registry boundary).

**G-R2 — `main.py --run-mode` CLI accepts only `{daily, weekly, monthly}`.** [low]
- `DISCOVERY`, `BACKTEST`, `HISTORICAL_REPLAY` are not selectable from this entry point — correct
  by design — but the validation lives in argparse choices, not in the run-mode governance layer.
  If a new mode is added to `RunMode`, argparse and policy will drift.
- Category: *standardize orchestration*.

### 3.3 State Mutation Boundaries

**G-S1 — Config file is mutated in-place by `tools/manual_portfolio_update.py`.** [medium]
- Atomic rewrite is implemented and a backup is taken (`outputs/policy/portfolio_backups/`), so
  rollback exists. But the *config file itself* is shared with the daily pipeline, which means a
  manual-update run during a daily run would race. There is no file lock.
- Category: *failure isolation* + *operator workflow*.

**G-S2 — SQLite `snapshots` table uses autoincrement PK.** [low]
- A rerun on the same day appends rows. Downstream consumers must filter by `run_id`. Currently OK
  but easy to misuse.
- Category: *idempotency*.

### 3.4 Artifact Namespaces

**G-N1 — Several artifacts are written but not in `docs/OUTPUT_ARTIFACT_CONTRACTS.md`.** [low]
- Examples: `outputs/latest/watchlist_alerts.csv`, `outputs/latest/watchlist_summary.md`,
  `outputs/latest/market_opportunities.json` (partial).
- Risk: an external consumer cannot rely on these being stable.
- Category: *standardize artifact registry*.

**G-N2 — No machine-readable artifact registry exists.** [medium]
- Today, the registry is prose in `docs/OUTPUT_ARTIFACT_CONTRACTS.md`. There is no single source of
  truth that the code references. A small `artifacts_registry.py` (mapping `name → (path, namespace,
  writer, schema_version, observe_only_required)`) would make missing-artifact diagnostics
  programmable and would let GUI/memo loaders share a contract.
- Category: *standardize artifact registry*.

### 3.5 Failure Isolation

**G-F1 — `ai_budget.record_ai_usage_event()` swallows filesystem errors.** [medium]
- `portfolio_automation/ai_budget.py:422–440` logs `WARNING` and continues, but does not record the
  failure to any status artifact. The AI cost audit log can be silently incomplete.
- Category: *failure isolation*.

**G-F2 — `decision_outcome_tracker` price-fetch failure is unsurfaced.** [medium]
- A FMP outage during outcome resolution leaves rows unresolved with no operator-visible signal.
- Category: *failure isolation* + *health/status*.

**G-F3 — `daily_memo._safe_load()` returns `{}` on corrupt JSON.** [low]
- Currently the memo's `data_health` block surfaces missing artifacts, but a *corrupt* (non-empty
  but unparseable) artifact is indistinguishable from missing. Operators cannot tell that an upstream
  step partially wrote a file.
- Category: *failure isolation*.

**G-F4 — `scripts/run_daily_safe.sh:94` tees raw stderr to the log file with no redaction.** [medium]
- If any exception message includes a URL with query parameters (`?apikey=...`), it lands in
  `logs/daily_safe_*.log` unredacted. The Python layer redacts; the shell wrapper does not.
- Category: *config / secrets*.

### 3.6 Idempotency

**G-I1 — `outputs/policy/ai_usage_events.jsonl` has no event-level dedup.** [medium]
- A pipeline rerun appends every LLM call again, inflating `ai_budget_summary.json` daily cost.
- Category: *idempotency*.

**G-I2 — `outputs/policy/memo_delivery_log.jsonl` appends every attempt, not just successes.** [low]
- This is by design (audit trail), but combined with the `email_history` SQLite dedup, the JSONL
  can show many failed attempts before a single successful send. Documented behavior would help
  consumers.
- Category: *standardize artifact registry* (document the contract).

**G-I3 — `outputs/history/YYYY-MM-DD/` archival is not explicitly verified after each run.** [low]
- The archival logic exists but no status artifact records "archival OK / size N bytes / N files".
- Category: *health/status*.

### 3.7 Logs / Status

**G-L1 — No "last run" command or artifact for the official lane.** [high]
- An operator cannot run a single command and get: last run timestamp, run mode, stage results,
  warnings, missing artifacts, AI cost so far today.
- The data exists scattered across `system_decision_summary.json`, SQLite `run_history`,
  `ai_budget_summary.json`, `memo_delivery_status.json`, and the daily log file.
- Category: *health/status*.

**G-L2 — Log format is free-text only.** [low]
- All Python logging is `TIMESTAMP | LEVEL | LOGGER | MESSAGE`. Parsing for tooling requires regex.
  Structured JSON event logs exist for AI usage and outcomes, but not for pipeline-stage events.
- Category: *health/status*. (Defer until needed — not urgent.)

### 3.8 Testability

**G-T1 — No top-level "smoke test" entry point.** [medium]
- `preflight.sh` runs FMP compliance + pytest, but there is no command to quickly verify that the
  daily pipeline can write each documented artifact in `outputs/latest/` *without* doing a full
  market-data pull. The closest is `--dry-run` on `main.py`, which still pulls data.
- Category: *deployment verification*.

**G-T2 — Many artifacts have no schema-enforcing test.** [medium]
- `docs/OUTPUT_ARTIFACT_CONTRACTS.md` documents shape, but only some artifacts have tests that load
  the written file and verify fields. A registry-driven contract test would close this.
- Category: *standardize artifact registry*.

### 3.9 Rollback

**G-RB1 — Config rollback path exists; pipeline-output rollback does not.** [low]
- `outputs/policy/portfolio_backups/` keeps config snapshots. But if a daily run produces a bad
  `outputs/latest/`, there is no documented procedure to restore yesterday's outputs.
- Mitigation: `outputs/history/YYYY-MM-DD/` keeps daily archives.
- Category: *operator workflow* (runbook).

**G-RB2 — No documented "stop the daily timer cleanly" procedure for the operator.** [low]
- `systemctl stop stockbot-daily.timer` is implicit. No runbook.
- Category: *operator workflow*.

### 3.10 Operator Workflow

**G-O1 — Inconsistent return-code convention.** [medium]
- Official lane: non-zero on failure. Sandbox lane: always 0 (failures captured in status artifact).
  Both are defensible individually; the inconsistency is the issue.
- Category: *standardize orchestration*.

**G-O2 — No single "status now" command.** [high]
- Operators need to read multiple JSON files + the daily log + SQLite to assess production health.
  See G-L1.
- Category: *health/status*.

---

## 4. Severity Roll-up

| Severity | Count | Driver |
|----------|-------|--------|
| High | 4 | G-E3, G-R1, G-L1, G-O2 (all related: orchestration result + health/status surface) |
| Medium | 8 | G-E1, G-N2, G-F1, G-F2, G-F4, G-I1, G-T1, G-T2, G-O1, G-S1 |
| Low | 7 | small contract / documentation / convenience gaps |

The four "high" findings cluster around the same underlying gap: **the official lane has no
machine-readable status output**. Closing that one gap retires three of them.

---

## 5. Proposed Hardening Sequence

This matches the user's preferred ordering. Each phase is independently shippable and reversible.
No phase changes business logic.

### Phase A — Audit (this document)

**Status:** done. No code changes. No artifacts written by the audit itself.

### Phase B — Standardize Orchestration Results

Goal: every official-lane entry point emits a status artifact with a shared shape.

- Mirror `sandbox_run_status.json` to the official lane:
  `outputs/latest/pipeline_run_status.json` (and `.md`) written by `main.py` and
  `run_daily_pipeline.py`.
- Shared dataclass / writer in a new `portfolio_automation/run_status.py` (additive module).
- Fields: `generated_at`, `run_id`, `run_mode`, `observe_only`, `no_trade`, per-stage results
  (name, status, duration_sec, notes), errors list, warnings list, artifacts_written list,
  exit code mapping.
- Sandbox lane keeps its existing artifact unchanged. The new module powers both.
- Closes: G-E3, G-O1, partially G-L1, G-O2.

### Phase C — Standardize Artifact Registry

Goal: a single in-code registry that names, paths, and types every produced artifact.

- New module `portfolio_automation/artifacts_registry.py` (additive). Pure data; no I/O.
- Each entry: `name`, `namespace`, `relative_path`, `writer_module`, `consumer_modules`,
  `schema_version`, `optional`, `observe_only_required`.
- One contract test per artifact: load → validate top-level shape → confirm `observe_only=true`
  where required.
- Does *not* change any writer behaviour. Provides the foundation for later guards.
- Closes: G-N1, G-N2, G-I2 (documents append vs overwrite per artifact), G-T2.

### Phase D — Health / Status Inspection

Goal: an operator can answer "is production healthy?" in one command.

- New CLI: `python -m portfolio_automation.status` (or `tools/status.py`).
- Reads `pipeline_run_status.json` + `sandbox_run_status.json` + `ai_budget_summary.json` +
  `memo_delivery_status.json` + SQLite `run_history` last row, prints a single Markdown summary.
- Read-only. No artifact writes. Safe to call from cron or a future healthcheck.
- Closes: G-L1, G-O2, G-I3 (surfaces archival status), G-F2 (surfaces outcome-tracker degradation).

### Phase E — Config Validation Hardening

Goal: every required env var / config field has a single load-time check with a clear message.

- Centralize env-var reads via a `portfolio_automation/env.py` helper (additive). Existing call
  sites can opt in over time; no large refactor.
- Extend `preflight.sh` to call a `python -m portfolio_automation.env --check` that lists
  required, optional-with-default, and feature-flag vars and their current state.
- Closes: G-F4 (centralized helper redacts on log), partial G-T1.

### Phase F — Deployment Verification

Goal: post-deploy smoke test that proves the system can produce each documented artifact without
touching market-data APIs.

- New CLI: `python -m tools.smoke_test` or `scripts/smoke_test.sh`.
- Uses canned fixtures already present in `tests/` to run the daily pipeline against a temp
  `outputs/` directory, then asserts every artifact in the registry was written and validates.
- Closes: G-T1.

### Phase G — Package Restructure (deferred)

**Not in scope yet.** Re-evaluate only after B–F land and we can measure whether further structural
moves would reduce coupling. The current module layout is well-documented; moving files is high-risk
and offers no observable benefit until the boundaries above are first hardened.

---

## 6. Out of Scope (Explicit)

- No changes to `signal_score`, `confidence_score`, `effective_score`, `conviction_score`,
  `final_rank_score`, `recommendation_score` semantics.
- No changes to `decision_engine.py`, scoring logic, or recommendation logic.
- No changes to FMP registry/compliance rules.
- No removal or weakening of `observe_only` / `recommend_only` flags.
- No broker integration, execution, or auto-trading.
- No move of files across packages until phase G is approved.

---

## 7. Reversibility

Every proposed phase produces only additive modules and additive output artifacts.
Each phase can be rolled back by reverting the commit and deleting the new artifact paths.
No existing reader is modified to *require* the new artifacts — they remain optional reads.

---

## 8. Open Questions For Operator

1. Should the new `pipeline_run_status.json` live in `outputs/latest/` (overwritten daily) or
   `outputs/policy/` (audit-trail style append)? Recommend `outputs/latest/` for symmetry with the
   sandbox lane, plus archival via the existing `outputs/history/YYYY-MM-DD/` rollover.
2. Should the `status` CLI also report sandbox-lane status by default, or only on `--include-sandbox`?
3. Phase E centralizes env reads — is opt-in adoption acceptable, or should we sweep call sites
   in one pass? Recommend opt-in to keep blast radius small.
