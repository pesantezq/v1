# Changelog Decisions

Use this file to record high-impact changes that affect meaning, not just implementation.

## How To Use This File

Add an entry whenever a change affects:

- scoring semantics
- ranking weights
- conviction or sizing behavior
- allocation caps
- output contracts
- SQLite schema
- architecture boundaries

## Required Entry Format

### Date

`YYYY-MM-DD`

### Area

One of:

- scoring
- allocation
- alerts
- state
- output_contract
- architecture
- evaluation

### Files / Functions

Name the exact files and functions changed.

### Decision

Describe what changed in plain language.

### Why

State the problem being solved.

### Invariants Preserved

Explicitly state what did not change.

### Downstream Impact

List affected artifacts, tests, and GUI surfaces.

### Artifact Health Severity

If artifact health behavior changes, record whether the change affects:

- `critical_missing`
- `defaulting`
- `optional_missing`

Explicitly note:

- which artifacts changed severity
- whether `missing_artifact_count` changed
- whether GUI/memo/system-summary wording changed
- which producer step owns the artifact

---

## FMP Stable Baseline (v1.0)

### Date

2026-04-28

### Area

architecture

### Files / Functions

- `fmp_client.py` — all `_EP_*` constants, `get_batch_quotes`, `get_batch_profiles`, `get_historical_prices`, `get_ratios`, `get_stock_news`, `get_key_metrics`, `get_income_statement`
- `fmp_endpoint_registry.py` — new; machine-readable endpoint source of truth
- `fmp_endpoint_compliance.py` — new; runnable compliance checker (`python -m fmp_endpoint_compliance`)
- `watchlist_scanner/scanner.py` — FMP primary for all technical + fundamentals + news data
- `watchlist_scanner/fundamentals_engine.py` — `parse_fmp_profile`, `parse_fmp_fundamentals_bundle`
- `tests/test_fmp_endpoint_registry_compliance.py` — new; 23 compliance contract tests
- `docs/REGRESSION_CHECKLIST.md` — FMP compliance block added to section 3
- `docs/CLAUDE_AGENT_RULES.md` — FMP Data Rules hard constraint added

### Decision

Migrated all daily scanner and fundamentals paths to FMP stable endpoints (`https://financialmodelingprep.com/stable/`). Implemented an endpoint registry as the single source of truth and a runnable compliance checker that gates any future endpoint changes. Achieved 257/257 passing tests on VPS with zero violations.

- All core endpoints (`quote`, `profile`, `historical-price-eod/full`, `ratios`, `news/stock`, `key-metrics`, `income-statement`) use `FMP_STABLE_BASE_URL`.
- Legacy v3/v4 methods (`get_sp500_constituents`, `get_batch_profiles_v3`, `get_bulk_profiles`, etc.) retained for universe pipeline only — explicitly classified as `legacy_optional` or `premium_optional` and excluded from the daily scanner.
- Alpha Vantage demoted to true fallback; AV OHLCV skipped when FMP historical data is present.
- `technical_data_completeness` field added to scan output: `full` | `partial` | `price_only` | `missing`.

### Why

FMP v3 endpoints were returning HTTP 403 for historical prices and news on the Starter plan. The system was silently degrading with 0/22 profiles loaded. Stable endpoints resolve the auth issue and are guaranteed available at 300 calls/min on the Starter plan.

### Invariants Preserved

- Advisory-only operation — no execution logic changed
- `signal_score`, `confidence_score`, `conviction_score` semantics unchanged
- Output file paths and top-level contract shapes unchanged
- SQLite schema unchanged
- Alpha Vantage fallback path preserved for symbols where FMP data is unavailable

### Downstream Impact

- Daily scanner now reliably loads profiles, historical prices, and news for all watchlist symbols
- `fmp_endpoint_compliance` must pass (`RESULT: COMPLIANT`) before any FMP wiring change is merged
- `pytest tests/ -k fmp` (257 tests) is the regression gate for all FMP-related changes
- Enables safe high-frequency scanning at FMP Starter plan limits (300 calls/min)

---

## Baseline

### Date

2026-04-28

### Area

architecture

### Files / Functions

- `main.py:run_portfolio_update`
- `watchlist_scanner/__main__.py:run`
- `watchlist_scanner/scanner.py`
- `watchlist_scanner/confidence.py:compute_confidence`
- `watchlist_scanner/conviction.py:apply_conviction_layer`
- `watchlist_scanner/portfolio_construction.py:apply_portfolio_construction_layer`
- `allocation_engine.py:suggest_allocation`
- `policy_evaluator/*`
- `state_store.py`

### Decision

Documented the current baseline architecture, output contracts, scoring meanings, and state schema without changing application behavior.

### Why

AI agents and maintainers need a stable source of truth before making behavior changes.

### Invariants Preserved

- advisory-only operation
- separate `signal_score` and `confidence_score`
- output file paths and top-level contract shapes
- existing SQLite schema

### Downstream Impact

- documentation consumers
- AI coding agents
- future regression review

---

## Artifact Health Severity Model

### Date

2026-04-29

### Area

output_contract

### Files / Functions

- `watchlist_scanner/system_summary.py` — `compute_data_health`, artifact-health classification, dry-run logging
- `watchlist_scanner/daily_memo.py` — `_health_items`
- `gui_operator_data.py` / `gui/app.py` — inherited health wording via shared summary data
- `tests/test_system_summary.py`
- `tests/test_daily_memo.py`
- `tests/test_gui_decision_center.py`
- `docs/OUTPUT_ARTIFACT_CONTRACTS.md`
- `docs/REGRESSION_CHECKLIST.md`
- `docs/CLAUDE_AGENT_RULES.md`

### Decision

Refined artifact health reporting into three severities:

- `critical_missing`
  True required pipeline artifact is absent.
- `defaulting`
  Policy/config artifact is absent but safe default behavior is active.
- `optional_missing`
  Non-critical artifact is absent but a fallback source exists.

Current non-critical examples:

- `outputs/performance/approved_ranking_config.json` → `defaulting`
- `outputs/performance/approved_allocation_policy.json` → `defaulting`
- `outputs/latest/theme_opportunities.json` when `theme_signals.json` exists → `optional_missing`

### Why

The prior wording made expected absent policy artifacts look like broken required outputs. That inflated `missing_artifact_count` and created noisy, misleading health warnings in system summary, memo, and GUI.

### Invariants Preserved

- No scoring changes
- No decision or allocation behavior changes
- No observe-only behavior changes
- No artifact path changes
- `decision_plan.json` remains the Decision Center source of truth

### Downstream Impact

- `missing_artifact_count` now reflects only truly required artifacts
- system-summary dry-run logging is severity-aware
- memo and GUI health wording can distinguish required missing vs defaulting vs optional absence
- regression tests now guard against non-critical artifacts inflating critical missing counts

---

## AI Validation Layer

### Date

2026-04-29

### Area

architecture

### Files / Functions

- `portfolio_automation/ai_decision_validator.py`
- `main.py` â€” post-decision-plan validation hook
- `gui_operator_data.py` â€” validation artifact loader
- `gui/app.py` â€” `AI Validation` section
- `tests/test_ai_decision_validator.py`

### Decision

Added a deterministic-first AI validation layer that runs after `decision_plan.json` is written and produces:

- `outputs/latest/ai_decision_validation.json`
- `outputs/latest/ai_decision_validation.md`

Validation statuses:

- `aligned`
- `caution`
- `contradiction`
- `insufficient_context`

### Why

The system needed a downstream quality-check layer that could validate whether decision narratives and capital-action language match the already-emitted decision without changing the decision itself.

### Invariants Preserved

- observe-only operation
- no decision mutation
- no scoring changes
- no allocation changes
- validator is non-blocking

### Downstream Impact

- GUI now has an `AI Validation` section
- validation artifacts are available for later analytics
- future agents can inspect contradiction rates without touching decision logic

---

## Contradiction Detection And Negation Fix

### Date

2026-04-29

### Area

evaluation

### Files / Functions

- `portfolio_automation/ai_decision_validator.py` â€” contradiction detection, negation handling
- `tests/test_ai_decision_validator.py`

### Decision

Added explicit contradiction detection between decision type and capital-action language, then refined it so negated deployment phrases are not treated as contradictions.

Fixed example:

- `WAIT` + `Stand by â€” do not deploy capital until conditions improve.`
  no longer counts as contradiction

Still contradictory:

- `WAIT` + `deploy capital now`
- `WAIT` + `buy shares now`

### Why

The first contradiction pass generated false positives for valid hold-off language. The negation fix preserves validator usefulness without punishing correct observe-only phrasing.

### Invariants Preserved

- decision generation unchanged
- capital action semantics unchanged
- validator remains downstream only

### Downstream Impact

- lower false-positive contradiction counts
- cleaner GUI validation summaries
- more trustworthy validation artifacts for later calibration

---

## Decision Outcome Tracker

### Date

2026-04-29

### Area

evaluation

### Files / Functions

- `portfolio_automation/decision_outcome_tracker.py`
- `main.py` â€” post-validation outcome-tracker hook
- `gui_operator_data.py` â€” outcome summary loader
- `gui/app.py` â€” `Decision Performance` section
- `tests/test_decision_outcome_tracker.py`

### Decision

Added a downstream outcome tracker that snapshots decisions into JSONL history, resolves outcomes over time, and writes aggregated performance summaries:

- `outputs/policy/decision_outcomes.jsonl`
- `outputs/policy/decision_outcome_summary.json`
- `outputs/policy/decision_outcome_summary.md`

### Why

The system needed a durable feedback layer so future calibration and optimization work can use observed decision outcomes instead of anecdotal review.

### Invariants Preserved

- no execution behavior
- no same-run feedback into decisions
- no scoring changes
- no allocation changes
- tracker failures remain non-fatal

### Downstream Impact

- GUI now has a `Decision Performance` section
- hit-rate and return metrics are available by decision type and validation status
- future agents can reason about calibration using persisted history

---

## Self-Validating And Learning Architecture

### Date

2026-04-29

### Area

architecture

### Files / Functions

- `portfolio_automation/decision_engine.py`
- `portfolio_automation/decision_explainer.py`
- `portfolio_automation/ai_decision_validator.py`
- `portfolio_automation/decision_outcome_tracker.py`
- `main.py`
- `gui/app.py`
- `docs/ARCHITECTURE.md`

### Decision

Upgraded the production system from pure decision support to a layered observe-only architecture:

```text
Decide -> Explain -> Validate -> Track Outcomes
```

AI remains limited to explanation and validation. The feedback loop remains analytically downstream and does not control decisions.

### Why

Future agents need a stable model of where reasoning ends, where validation begins, and where learning artifacts accumulate over time.

### Invariants Preserved

- observe-only operation
- rules-first decision generation
- no trade execution
- `decision_plan.json` remains the downstream source of truth

### Downstream Impact

- richer operator visibility
- stronger explainability and QA surfaces
- better long-term maintainability for future AI agents

---

## Historical Replay / Backtest Calibration Design

### Date

2026-04-30

### Area

architecture

### Files / Functions

- `docs/HISTORICAL_REPLAY_BACKTEST.md`
- `docs/ARCHITECTURE.md`
- `docs/FEEDBACK_LOOP.md`
- `docs/FMP_COMPLIANCE.md`
- `docs/CHANGELOG_DECISIONS.md`
- proposed module: `portfolio_automation/historical_decision_replay.py`

### Decision

Added the design-only milestone for an offline historical replay / backtest calibration path that will use approved FMP historical EOD data to generate source-tagged replay outcome records for calibration and attribution.

Planned source flags:

- live rows: `source = "live"`
- replay rows: `source = "historical_replay"`

### Why

Live resolved decision history is still sparse. Historical replay is planned to accelerate confidence calibration and performance attribution without changing live decision logic.

### Invariants Preserved

- observe-only operation
- no trading or broker actions
- no scoring changes
- no threshold changes
- no automatic promotion to live policy
- status is design only, not implemented

### Downstream Impact

- future replay outputs should remain separate or source-aware
- future calibration, attribution, and triage metrics must distinguish live vs replay results
- architecture now documents replay as an offline path, not part of the live daily pipeline

