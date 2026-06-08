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

## Quant-Watch Probe Ledger — Sub-RED Quant Concern Tracker

### Date

`2026-06-08`

### Area

evaluation, architecture

### Files / Functions

- `portfolio_automation/quant_watch_probes.py` (new) — `run_quant_watch` (orchestrator), `detect`/`evaluate`/`update_ledger`/`render_status`/`overall_status`/`load_ledger`, 3 detectors (`detect_prior_gauge_underperformance`, `detect_negative_mean_return_persistence`, `detect_sector_drag`) + paired evaluators (`_eval_prior_gauge`, `_eval_neg_return`, `_eval_sector_drag`), `write_ledger`, helpers (`_select_prior_gauge`, `_active`/`_resolved`/`_escalated`/`_age_days`).
- `.claude/commands/quant-watch-analysis.md` (new) — skill: Steps 1–5 (run loop, manual judgment path, triage, heartbeat, notes).
- `.claude/commands/daily-tool-analysis.md` (modify) — artifact read entry (item 17), quant-watch sub-check delegation block, Step 4 body line 6e.
- `docs/quant_watch_probes.md` (new) — module documentation.
- `tests/test_quant_watch_probes.py` (new) — 46 unit + integration tests.

Spec: `docs/superpowers/specs/2026-06-08-quant-watch-probes-design.md`.
Plan: `docs/superpowers/plans/2026-06-08-quant-watch-probes.md`.

### Decision

Shipped the quant-watch probe ledger: a self-managing ledger of sub-RED quant
concerns. Three deterministic detectors fire below the `daily-tool-analysis` RED
trip-wires and register a probe; each run paired evaluators re-check open probes
(escalate-before-resolve); resolved/escalated probes are archived with
`resolved_at`, `resolution`, and `lifetime_days`. AMBER/RED-hybrid escalation:
an escalated probe has by construction crossed a daily RED gate, so the RED
*response* is deferred to `daily-tool-analysis` + `portfolio-attribution-analyst`
dispatch — quant-watch adds continuity and same-run visibility, not a second RED
authority.

Detector table (v1):
- D1 `prior_gauge_underperformance` (flagship) — fires when current-fp is ≥10pp
  below the prior gauge era and `|Δ vs pre_tracker|` < 10pp (sub-RED band).
  Escalates when the daily RED gate (`|Δ vs pre_tracker|` ≥10pp at n≥30) is
  later crossed.
- D2 `negative_mean_return_persistence` — fires when current-fp `mean_return_1d`
  < 0 at n≥30.
- D3 `sector_drag` — fires when a `sector:*` tag in `pattern_efficacy_monthly.json`
  is `loser` at n≥30.
- Manual judgment path (`detector: "manual"`) — never auto-resolved; operator
  retires by editing the ledger.

New skill `/quant-watch-analysis` drives the loop daily (on-demand, delegated from
`daily-tool-analysis` via the new sub-check block).

### Why

Quant concerns that fire below the daily RED threshold currently go untracked
between runs. Without a ledger they may persist silently for weeks (as the
prior-gauge trap did on the d95e gauge fingerprint). The watch-probe model
provides continuity + retrospective trail without touching pipeline state.

### Invariants Preserved

No change to `decision_engine.py`, `signal_score`, `confidence_score`,
`effective_score`, `conviction_score`, `final_rank_score`, or
`recommendation_score` semantics. No change to allocation or portfolio state.
Module mutates only its own ledger (`data/quant_watch_ledger.json`) and its
status artifact (`outputs/latest/quant_watch_status.json`); both are
runtime-generated and not committed. All new code is observe-only (`observe_only:
true` in every output). `next_official_step` unchanged.

### Downstream Impact

New artifacts: `data/quant_watch_ledger.json` (runtime, git-ignored),
`outputs/latest/quant_watch_status.json` (observe-only heartbeat). New skill
`.claude/commands/quant-watch-analysis.md`. Additive edits to
`.claude/commands/daily-tool-analysis.md` (artifact read + sub-check + body line).
Tests: `tests/test_quant_watch_probes.py` (46). No GUI/memo wording change; no
scoring/decision artifact change.

### Artifact Health Severity

`quant_watch_status.json` is `optional_missing` (absent until the runner fires the
first time → no flag, graceful green). New `overall_status` field: green/amber/red.
`missing_artifact_count` unchanged. Producer: `portfolio_automation.quant_watch_probes`
(via `/quant-watch-analysis` skill, delegated from `/daily-tool-analysis`). No
GUI/memo/system-summary wording change.

---

## Pattern-Loop sub-project F — Historical Signal Reconstruction (look-ahead-safe)

### Date

`2026-06-05`

### Area

evaluation

### Files / Functions

- `backtesting/historical_signal_recon.py` (new) — `reconstruct_signals` (point-in-time per ticker), `reconstruct_universe` (→ snapshot-compatible recon dir), `assert_no_lookahead` (truncation-equality audit), `write_reconstruction_audit`.
- `backtesting/auto_apply.py` — gate G2b `reconstruction_unverified` (fail-closed on dirty/absent look-ahead audit when evidence is reconstructed).
- `backtesting/backtest_health.py` — RED flag `reconstruction_lookahead_dirty`; `details.reconstruction`.
- `scripts/pattern_loop_reconstruct.sh` (new) — backfill → reconstruct → audit → run_loop over recon history.
- `.claude/commands/pattern-loop-analysis.md` — reads the reconstruction audit; `docs/PATTERN_LOOP_RECONSTRUCTION.md` (new).

### Decision

Reconstruct a multi-year historical signal set from the 5y FMP price archive, point-in-time, so the OOS window matures now instead of ~2027. Hybrid fidelity: pattern families (STRONG_MOVE_UP/DOWN, VOLUME_SPIKE) from `event_thresholds`; `signal_score`/`confidence` deferred (null). A truncation-equality **look-ahead audit** is the load-bearing safety; auto-apply is fail-closed against reconstructed evidence unless the audit is clean, after which (operator decision 2026-06-05) it runs full-auto.

### Why

The loop produces no proposals until the OOS window matures; reconstructing historical signals from prices we already can fetch yields real out-of-sample evidence now — but only if it is rigorously look-ahead-safe (else it tunes weights to hindsight → worse picks).

### Invariants Preserved

No scoring/`decision_engine.py` change. Reconstruction is observe-only. The only mutation is the existing E auto-apply (operator-approved), still gated + reversible + audited, now additionally fail-closed on the look-ahead audit. `next_official_step` unchanged.

### Downstream Impact

New artifacts under `outputs/backtest/`: `recon/<date>/watchlist_signals.json`, `reconstruction_audit.json`. Tests: `test_historical_signal_recon.py` (9), `test_lookahead_audit.py` (2), `test_recon_matures_window.py` (1), + `test_auto_apply.py` / `test_backtest_health.py` extensions. No GUI/memo change.

### Artifact Health Severity

`reconstruction_audit.json` is `optional_missing` (absent until the runner runs → no flag). New RED condition `reconstruction_lookahead_dirty`. Producer: `backtesting.historical_signal_recon` (via the reconstruct runner). No GUI/memo wording change.

---

## Pattern-Loop sub-project E — Full Auto-Apply via GPT Approver (INERT)

### Date

`2026-06-05`

### Area

scoring, architecture

### Files / Functions

- `backtesting/auto_apply.py` (new) — `maybe_auto_apply()` fail-closed orchestrator (8 gates + GPT approver + pre/post score-invariance gate + auto-rollback + kill-switch + audit).
- `backtesting/run_loop.py` — inert non-blocking integration (`_auto_apply_enabled()`, default False); `auto_apply` summary key.
- `backtesting/backtest_health.py` — flags `auto_apply_rolled_back` (RED), `auto_apply_active` (AMBER).
- `config.json` — `backtesting.auto_apply.{enabled:false, max_monthly_drift, max_abs_delta}`.
- `CLAUDE.md` — sanctioned-exception clause (Protected Semantics + Observe-Only Default).
- `.claude/commands/{daily,monthly}-tool-analysis.md` — dispatch review on every auto-apply event.
- `docs/PATTERN_LOOP_AUTO_APPLY.md` (new) — gates, kill-switch, rollback, activation runbook.

### Decision

Operator-approved (2026-06-05) **full auto-apply**: when enabled AND all gates clear, the system authors `config/approved_weight_changes.json` and invokes the reversible protected registry apply WITHOUT a per-change human, with a GPT approver (veto/approve-bounded only) layered on the deterministic gates. **This relaxes the previously hard owner-gated / observe-only invariant — narrowly, for registry `default_weight` data only.** Ships INERT (`enabled=false`); cannot fire until OOS maturity (≈2027).

### Why

Closes the Pattern-Improvement Loop: once real out-of-sample evidence exists, bounded weight improvements can be applied without manual toil — but only behind every existing safety gate plus an LLM approver, kill-switch, audit, and auto-rollback.

### Invariants Preserved

No change to scoring math / `decision_engine.py` / score semantics. Apply remains reversible (byte-for-byte snapshot + `revert_last`). Observe-only preserved for every other module. Oversight preserved: every applied/rolled_back event is audited, health-flagged, and dispatched for review. `next_official_step` unchanged.

### Downstream Impact

New artifact `outputs/policy/auto_apply_audit.json`. New config block. Tests: `tests/test_auto_apply.py` (13) + `test_backtest_health.py` (3) + `test_run_loop.py` (inert key). No live behavior change (inert).

### Artifact Health Severity

New audit artifact is `optional_missing` (absent/`disabled`/`oos_immature` = expected steady state). `auto_apply_rolled_back` is a NEW RED condition; `auto_apply_active` a NEW AMBER. `missing_artifact_count` unchanged. Producer: `backtesting.auto_apply` (via run_loop, inert).

---

## Pattern-Loop sub-project D — Feedback Proposers (calibration + tagging)

### Date

`2026-06-05`

### Area

evaluation

### Files / Functions

- `backtesting/calibration_proposer.py` (new) — `propose_calibration_correction()` + `write_calibration_proposal()`.
- `backtesting/tagging_proposer.py` (new) — `propose_tagging_fixes()` + `write_tagging_proposal()`.
- `backtesting/run_loop.py` — non-blocking integration after Step 4; `calibration_proposal`/`tagging_proposal` summary keys.
- `backtesting/backtest_health.py` — AMBER flags `calibration_correction_available`, `high_untagged_rate`.
- `.claude/commands/monthly-tool-analysis.md` — reads the two new POLICY artifacts; body line + dispatch note.

### Decision

Two observe-only/proposes-only proposers turn the two live-run defects (inverted confidence calibration; ~70% of signals untagged + `SIGNAL_SCORE` absent from the registry) into bounded, owner-gated review artifacts under `outputs/policy/`. Calibration apply is OOS-gated (`apply_gate` = `oos_unconfirmed` until the window matures, to avoid fitting a correction on the in-sample window). Tagging proposes a registry entry for unmapped families + a backfill-inference rule spec.

### Why

Both defects degrade attribution now, independent of the 2027 OOS clock — but a confident *fix* for either still needs validation, so they are proposed, not applied.

### Invariants Preserved

No mutation of `confidence_score`/scoring/`signal_registry.yaml`/the signal producer. Observe-only + owner-gated preserved; nothing is applied. `next_official_step` unchanged.

### Downstream Impact

New POLICY artifacts `calibration_correction_proposal.{json,md}`, `signal_tagging_proposal.{json,md}`. Tests added: `tests/test_calibration_proposer.py` (5), `tests/test_tagging_proposer.py` (4), + extensions to `test_run_loop.py`, `test_backtest_health.py`. No GUI/memo change.

### Artifact Health Severity

New artifacts are `optional_missing` (absence tolerated → no flag). `missing_artifact_count` unchanged. Producer: `backtesting.run_loop` (non-blocking). No GUI/memo/system-summary wording change.

---

## Pattern-Loop production Foundation (A+B+C)

### Date

`2026-06-05`

### Area

evaluation

### Files / Functions

- `backtesting/walk_forward.py` — new `oos_window_status()` (calendar-day maturity countdown).
- `backtesting/poc_simulation_harness.py` — `run_poc()` gains optional `oos_window` param.
- `backtesting/run_loop.py` — computes `oos_window_status`, passes it to `run_poc`, adds it to the returned summary.
- `backtesting/backtest_health.py` — surfaces `oos_window` in `details`.
- `scripts/pattern_loop_recheck.sh` (new), `scripts/monthly_check.sh` (calls it, non-blocking).
- `.claude/commands/monthly-tool-analysis.md` — reads the two Pattern-Loop artifacts, prints the OOS maturity countdown, dispatches `portfolio-backtest-health` on RED.

### Decision

Operationalize the observe-only Pattern-Improvement Loop: a monthly recompute (`run_loop --history --live`, FMP-only, no AI spend) wired before the monthly analysis, plus a deterministic OOS-window maturity countdown emitted as `poc_simulation_results.json.oos_window` and surfaced by the health layer. `proposed_count == 0` while `oos_window.folds_possible == false` is now explicitly treated as healthy/accruing, not a failure.

### Why

The first `real_signals_live` run (2026-06-05) confirmed the walk-forward OOS layer cannot produce evidence until signal history reaches ~315 calendar days (first folds ~2027-01, full window ~2027-03). Production value now is scheduled accrual + observability of that maturity, with every change still human/owner-gated.

### Invariants Preserved

No change to protected `signal_score`/`confidence_score`/`effective_score`/scoring/decision/allocation logic. Loop remains observe-only and proposes-only; Step 5 (governed apply) stays inert/owner-gated. `next_official_step` unchanged (`observe_and_iterate`).

### Downstream Impact

New optional field `oos_window` on `poc_simulation_results.json` (older artifacts tolerated → null). New artifacts now consumed at monthly cadence. Tests: `tests/test_walk_forward.py`, `tests/test_poc_simulation_harness.py`, `tests/test_run_loop.py`, `tests/test_backtest_health.py` extended. No GUI/memo surface change.

### Artifact Health Severity

No severity change: `oos_window` is `optional_missing` (absence tolerated). `missing_artifact_count` unchanged. No GUI/memo/system-summary wording change. Producer: `backtesting.run_loop` (→ `poc_simulation_results.json`, HISTORICAL namespace).

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

---

## Allocation Gauge Tactical Retune

### Date

2026-05-18

### Area

allocation

### Files / Functions

- `allocation_engine.py:DEFAULT_CONFIG`
- `watchlist_scanner/portfolio_construction.py:DEFAULT_PORTFOLIO_CONSTRUCTION_CONFIG`
- `decision_engine.py:_ABSOLUTE_MAX_ALLOCATION_PCT`
- `portfolio_automation/cash_deployment_plan.py:_MAX_POSITION_PCT`
- `watchlist_scanner/allocation_preview.py:_DEFAULT_MAX_TICKER_PCT` / `_DEFAULT_MAX_SECTOR_PCT`
- `tests/test_allocation_engine_tactical_retune.py` — new pin tests

### Decision

Operator-approved tactical retune of the sizing gauge (no scoring/decision-logic change):

- `compounder_base_pct` `0.05` → `0.10`
- `momentum_base_pct` `0.03` → `0.06`
- `max_position_cap` `0.08` → `0.15`
- `sector_cap` `0.20` → `0.35`
- `low_confidence_multiplier` `0.50` → `0.65`

Portfolio-construction defaults widened in lock-step:

- `baseline_position_pct` `0.02` → `0.04`
- `max_total_allocation` `0.10` → `0.30`
- `max_ticker_allocation` `0.02` → `0.05`
- `max_sector_allocation` `0.04` → `0.10`

### Why

Pre-retune sizing was producing recommendations that were too small to meaningfully
move the portfolio for a long-horizon, max-profit operator. Conviction and
confidence machinery, band selection, Kelly multipliers, and regime feedback are
unchanged — only the dollar dial moved.

### Invariants Preserved

- `signal_score`, `confidence_score`, `conviction_score`, `final_rank_score`,
  `recommendation_score` semantics unchanged
- decision logic in `portfolio_automation/decision_engine.py` unchanged
- output artifact schemas unchanged
- band assignments and degradation behavior unchanged

### Downstream Impact

- `decision_plan.json` allocation values now scale up to the new ceilings
- watchlist `portfolio_snapshot.json` normalized rows scale similarly
- `retune_impact.json` records the diff vs the pre-retune baseline (`commit
  4223654c`)

---

## Structural Caps Widened (Profit-Maximization)

### Date

2026-05-18

### Area

allocation

### Files / Functions

- `config.json:growth_mode.concentration_cap`
- `config.json:growth_mode.leverage_cap`
- `adjustment.py` and `guardrails.py` (consumers of these caps; logic unchanged)

### Decision

Operator-approved widening of the structural guard rules that emit SELL
recommendations on cap breaches:

- `concentration_cap` `0.40` → `0.60` (single-position max)
- `leverage_cap` `0.15` → `0.25` (total leveraged exposure max)

### Why

The previous caps were calibrated for a more conservative risk posture; the
widened caps align with the operator's explicit max-profit thesis and allow
high-conviction positions to grow without immediately triggering a structural
SELL.

### Invariants Preserved

- adjustment and guardrail logic is unchanged — only the threshold constants
  moved
- no scoring, conviction, or recommendation behavior changed
- the daily memo's Risk Delta block (`risk_delta_advisor`) reports current
  exposure against the new caps

### Downstream Impact

- `risk_delta.json` cap fields now read `0.60` / `0.25`
- existing positions that would have breached the old caps no longer surface as
  structural SELLs

---

## ml_advisor Enabled

### Date

2026-05-18

### Area

architecture

### Files / Functions

- `config/base.json:ml_advisor.enabled` (`false` → `true`)
- `config.json:ml_advisor` (already `true` — config now matches)

### Decision

The pattern-recognition ML advisor is enabled in the official lane. Combined
with the resolver fixes shipped 2026-05-19, the resolved-decisions history
already exceeds the `MIN_RECORDS_FOR_HIGH_CONFIDENCE = 30` threshold the advisor
uses to leave `status="insufficient_data"`.

### Why

The advisor was previously gated to keep ml outputs latent while resolution
plumbing was being hardened. With the FMP-fallback resolver in `outcome_evaluator`,
the natural-resolution path in `ml_history`, and the FMP price-snapshot
augmentation in `decision_outcome_tracker`, the historical record set is now
populated enough to produce informative pattern outputs.

### Invariants Preserved

- ml_advisor remains observe-only; it does not mutate decisions, scores, or
  allocations
- failure is non-blocking (independent try/except)
- output schema (`outputs/latest/ml_pattern_advisor.{json,md}`) unchanged

### Downstream Impact

- the daily memo's Advisor Stack now shows an ml pattern line instead of
  "ml_advisor disabled"
- the GUI v2 Today page surfaces the ml pattern signal on the advisor card

---

## FMP Budget Bump (230 → 250)

### Date

2026-05-18

### Area

architecture

### Files / Functions

- `config.json:api_limits.fmp_daily_calls_budget` (`230` → `250`)

### Decision

Raised the FMP daily call budget by 20 calls to give the two news-intelligence
runner stages (0 pre-pipeline + 8 post-pipeline cache refresh) and the expanded
sandbox lane enough headroom without flipping `fmp_budget_status` to `near_cap`
on a normal run.

### Why

The 17-stage wrapper has more producers reading FMP than the legacy 1-stage
path. The old `230` ceiling was hitting `near_cap` more often than was healthy.

### Invariants Preserved

- FMP endpoint registry and compliance rules unchanged
- no new endpoint usage introduced; only the budget ceiling moved
- `fmp_budget_telemetry` continues to report status against the live config
  value

### Downstream Impact

- `outputs/latest/fmp_budget_status.json` `budget` field now reads `250`
- memo's "FMP budget" line reflects the new ceiling

---

## Outcome Resolver Fixes (FMP Fallback + Auto-Resolve + Price Snapshot)

### Date

2026-05-19

### Area

evaluation

### Files / Functions

- `watchlist_scanner/outcome_evaluator.py` — new `_load_next_available_close_fmp`
  and `load_next_available_close` composite
- `ml_history.auto_resolve_pending_records` — natural-resolution path; fixes a
  latent `update_record_resolution` TypeError
- `portfolio_automation/decision_outcome_tracker._augment_price_map_with_fmp` —
  fills in non-watchlist decision symbols via FMP `batch_quotes`

### Decision

Three coordinated resolver fixes so the observe-only outcome history actually
fills in, instead of remaining sparse because of a missing AV cache or a
non-watchlist decision symbol:

1. **`outcome_evaluator` FMP fallback** — when the Alpha Vantage daily cache is
   empty for a symbol, fall back to `FMPClient.get_historical_prices` to resolve
   1d/3d/7d outcomes. The composite `load_next_available_close` keeps AV as
   primary and FMP as secondary.
2. **`ml_history` auto-resolve** — natural-resolution path that marks records
   resolved when their `rec_key` no longer surfaces in today's adjustments. Also
   fixes a latent argument-order bug in `update_record_resolution`.
3. **`decision_outcome_tracker` FMP price augmentation** — fills the
   `price_at_decision` field for non-watchlist decision symbols (which the
   watchlist-scoped price map otherwise misses) by calling
   `FMPClient.get_batch_quotes`.

### Why

The outcome resolver was leaving large fractions of the signal/decision history
unresolved because (a) AV-cache-only resolution failed on weekends and for
symbols outside the scanner's daily run; (b) historical adjustments that no
longer appeared got stuck pending; and (c) decisions on tickers the scanner
never scored had a null `price_at_decision`. With these fixes, the
`auto_resolve_pending_records` path closes most natural exits, and 1d/3d/7d
resolutions complete for the full decision universe.

### Invariants Preserved

- no scoring, decision, or allocation behavior changed
- `decision_plan.json` and `signal_outcomes.csv` schemas unchanged
- failure paths remain non-fatal (`auto_resolve` swallows exceptions per row)
- FMP usage stays inside the registry/compliance contract

### Downstream Impact

- `outputs/policy/decision_outcomes.jsonl` row counts climb significantly
- `outputs/performance/signal_outcomes.csv` `outcome_return_*` columns are
  populated much more often
- ml_advisor exceeds its `MIN_RECORDS_FOR_HIGH_CONFIDENCE = 30` threshold and
  produces real pattern outputs
- `outputs/latest/decisions_due_for_resolution.json` is now a meaningful probe;
  if any window stays stuck, the resolver has a real bug to investigate

---

## Discovery Persistence (Daily-Mode) + Cross-Day Reinforcement Gate + Pulse SLA

### Date

2026-05-30

### Area

evaluation

### Files / Functions

- `theme_engine/__main__.py` — new `_apply_persistence(store, enriched_themes, watch_candidates, run_date)`; Step 5 now calls it in all run modes (was an inline weekly/monthly-only block)
- `watchlist_scanner/extended_watchlist.py` — `ExtendedWatchlist.__init__` gains `reinforce_persistence_days` (default 3, const `_DEFAULT_REINFORCE_PERSISTENCE_DAYS`); `evaluate_candidates` reinforcement gate extended
- `portfolio_automation/discovery_pulse.py`, `main.py` — both `evaluate_candidates` callers pass `reinforce_persistence_days` from `extended_watchlist` config
- `portfolio_automation/daily_run_status.py` — `_check_pulse_last_run_age` warn threshold 360min → 840min
- `tests/test_theme_engine.py::TestApplyPersistence` (3), `tests/test_extended_watchlist_promotion.py` (7), `tests/test_daily_run_status.py` pulse-age tests (1 new + 1 updated)

### Decision

Three coordinated discovery-layer fixes shipped together:

1. **persistence_7d computed in daily mode.** `persistence_7d` was hardcoded to `0` for all themes in daily mode (only computed weekly/monthly), but the live pipeline runs the theme engine in **daily** mode — so persistence was always 0. `_apply_persistence` now computes trailing-7d theme persistence (distinct prior run-dates) in every mode, and additionally attaches per-candidate `persistence_7d` (prior distinct days + today; first-ever detection = 1).
2. **Cross-day reinforcement gate.** The extended-watchlist promotion gate credited a candidate as reinforced only on `len(themes) >= 2 OR "direct" in sources`. Single-theme candidates recurring day after day under one theme (NOC/LMT/RTX under "Defense") never qualified. The gate now also credits `persistence_7d >= reinforce_persistence_days` (default 3). The multi-theme and direct-mention paths are unchanged; `reinforce_persistence_days=0` disables the new path.
3. **Pulse SLA 6h → 14h.** `discovery_pulse.last_run_age` warned above 6h, but the longest by-design gap is the overnight window (weekend ~13.25h as seen at the 09:15 daily check), so it false-warned every morning. Threshold raised to 14h (840min).

### Why

The extended-watchlist promotion path has been dormant (0 rows lifetime). The discovery-health agent attributed this to `persistence_7d` being stuck at 0, but verification showed two *distinct* faults: persistence was genuinely always 0 (degrading `theme_alignment` scoring and the memo persistence label), AND the promotion gate never read persistence at all. Fixing both is what actually unblocks single-theme recurring candidates. The pulse SLA was a separate operator follow-up — a recurring benign morning warn.

### Invariants Preserved

- No `signal_score` / `confidence_score` / `conviction_score` / decision / allocation semantics changed
- Theme-engine output contract (`theme_signals.json`, `watch_candidates.json`) shape unchanged — only `persistence_7d` values now populate in daily mode
- Multi-theme and direct-mention reinforcement paths unchanged; new persistence path is additive and config-gated (default-on at 3)
- SQLite schema unchanged

### Downstream Impact

- `outputs/latest/theme_signals.json` `themes[].persistence_7d` now non-zero once ≥1 prior day of data exists
- `extended_watchlist` table may begin gaining rows for persistent single-theme candidates
- `daily_run_status.json` `discovery_pulse.last_run_age` stops warning on the benign overnight gap; `content_warn_count` drops by 1 on affected mornings
- Memo Top Insight persistence label is fed real values (see paired memo-label entry below)

---

## Applied-Fix Verification Loop (daily-tool-analysis consumer)

### Date

2026-05-30

### Area

evaluation

### Files / Functions

- `portfolio_automation/applied_fix_verifier.py` — new (observe-only, pure): `verify_applied_fixes`, `summarize`, `drop_resolved`; check kinds `liveness_row_not_warn`, `artifact_max_field_gt`; `applied_at` staleness guard
- `.claude/commands/daily-tool-analysis.md` — Step 1 computes `applied_fix_verdicts`; Step 2 blocks GREEN / raises AMBER on regression; Step 3 dispatches `portfolio-discovery-health` on a discovery-layer regression; Step 4 emits a "Fixes:" body line; Step 5 prunes confirmed fixes
- `data/daily_check_state.json` — new `applied_fixes` ledger field (state file; gitignored) with per-fix `verify` spec + batch `applied_at`
- `tests/test_applied_fix_verifier.py` (17)

### Decision

The daily-tool-analysis skill records fixes it ships into `daily_check_state.json:applied_fixes`. This was a passive audit record with no consumer. The new verifier re-checks each fix's machine-checkable `verify` spec against the next run's artifacts and classifies it `confirmed` / `regressed` / `pending` / `manual`. A `confirmed` fix is pruned from state (stop re-checking); a `regressed` fix blocks GREEN and dispatches discovery-health. An `applied_at` staleness guard returns `pending` (not a false `regressed`) until the pipeline has regenerated artifacts after the fix went live. By design, `artifact_max_field_gt` never emits `regressed` (a zero reading cannot distinguish "fix broke" from "first day of data").

### Why

Closes the producer-without-consumer debt on `daily_check_state.json:applied_fixes` (CLAUDE.md coverage requirement). Without it, a shipped fix that silently regresses would never be caught, and resolved findings would be re-flagged every run.

### Invariants Preserved

- Observe-only: the module writes no output artifact; it returns verdicts the skill consumes
- No scoring / decision / allocation behavior changed
- Backward compatible: a batch without `applied_at` skips the staleness guard; an unknown `verify.kind` yields `manual`

### Downstream Impact

- Daily heartbeat gains a "Fixes: N confirmed · N pending · N manual" line when `applied_fixes` is non-empty
- New AMBER trigger (`applied_fix_regressions` non-empty) and discovery-health dispatch trigger
- `data/daily_check_state.json` schema gains `applied_fixes`

---

## Memo Top-Insight Persistence Label Floor

### Date

2026-05-30

### Area

output_contract

### Files / Functions

- `watchlist_scanner/daily_memo.py` — `_build_top_insight` persistence label
- `tests/test_daily_memo.py::TestTopInsightPersistenceLabel` (5)

### Decision

The Top Insight persistence label was binary (`persistence >= 0.5` → "strong", else "moderate"), so a first-seen theme with `top_theme.persistence == 0.0` rendered as "moderate persistence" — misleading. Replaced with a three-tier clause: strong (`>=0.5`) / moderate (`0 < p < 0.5`) / "newly emerging (no prior-day persistence yet)" (`p <= 0`).

### Why

Surfaced by the memo-reviewer agent during a daily-tool-analysis run. Complements the same-day persistence_7d daily-mode fix: that fix populates the value, this fix labels a genuine 0.0 honestly.

### Invariants Preserved

- No scoring / decision / allocation behavior changed
- Memo compact contract unchanged (max 5 decisions / 3 risk / 3 changes); only the Top Insight wording for zero-persistence themes changed
- Strong and moderate wording for non-zero persistence unchanged

### Downstream Impact

- `outputs/latest/daily_memo.md` Top Insight line reads "newly emerging …" instead of "moderate persistence" when the dominant theme is first-seen

---

## Inaugural Monthly Tool Analysis (2026-05) — AMBER

### Date

2026-06-01

### Area

evaluation

### Files / Functions

- `docs/monthly_reports/2026-05.md` — full retrospective (owned by monthly-tool-analysis skill)
- `data/monthly_check_state.json` — monthly cadence state
- Defect sources referenced (no code change in this entry, follow-up only):
  - `watchlist_scanner/theme_engine.py` — OpenAI call site that never records an `ai_usage` event
  - `scanner/candidate_scanner.py` — FMP scanner path returning `fmp_succeeded:false` / `tier_b.evidence_count=0`
  - `portfolio_automation/pattern_learning.py:_match_outcome` — forward-only snapshot→outcome join

### Decision

First-ever monthly-tool-analysis run completed for 2026-05 (rolling 30d, 2026-05-02 → 2026-06-01) with verdict **AMBER**. Underlying system health is strong; AMBER is driven by three observability/maturation conditions, not a regression. The run surfaced two real defects for follow-up plus one self-healing maturation gap:

- **(a) Discovery spend telemetry false-zero.** `theme_engine.py` never calls `record_ai_usage` around its OpenAI call, so `discovery_pulse_status.openai_cost_usd_month` and the FMP discovery telemetry read 0.0. True spend is metered elsewhere in `ai_budget_summary.json` (~$0.0007/mo, negligible). Smoking gun: zero `theme_engine` events in `outputs/policy/ai_usage_events.jsonl`.
- **(b) FMP scanner still in fallback.** `tier_b.evidence_count=0` and `fmp_calls_month=0` despite a fresh `top100_watchlist.json`; the scanner path reports `fmp_succeeded:false`, collapsing the universe toward the static fallback set.
- **(c) Learning-loop outcome-maturation gap (self-healing).** `pattern_learning.py:_match_outcome` joins forward-only (`signal_time >= snapshot_date`); the earliest snapshot is 2026-05-29, so the 594 mature outcomes dated before that are structurally unreachable and every `pattern_efficacy_monthly` tag reads `resolved_1d=0` with null `vs_baseline_pp`. Not a code bug — first non-null 1d efficacy expected ~2026-06-05, 7d ~2026-06-09. A resolve-then-attribute back-join is noted as a design enhancement.

### Why

Record the inaugural monthly verdict and the validated state of the learning/discovery loops so the next monthly run has a baseline, and so the two genuine defects are tracked in `docs/roadmap.md` Known Issues rather than being lost in the report body.

### Invariants Preserved

- Observe-only retrospective; no portfolio, allocation, scoring, decision, or recommendation state modified
- No runtime behavior, test, or output-schema change in this entry — docs only
- `next_official_step` unchanged (`observe_and_iterate`)

### Downstream Impact

- `.agent/project_state.yaml` gains a `last_monthly_analysis` observation block (2026-06-01, AMBER)
- `docs/roadmap.md` gains a Known Issues section tracking defects (a) and (b)
- Validated facts recorded for next-run comparison: prior gauge era `f60e0b9d` 05-18 retune confirmed broad-based win (+28.8pp 1d hit-rate, sign-flipped return, all 4 sectors improved, none hurt); current era `d95e3096` (first signal 2026-05-29) not yet evaluable (0/110 resolved); cron 32/30 days archived; drift cap 0%; pulse skip 6.7%; universe 1d hit 0.524 / 3d 0.585; first-ever live extended_watchlist promotions XOM/CVX (Energy Transition, 2026-05-31)

---

## Top Insight Theme-Membership Floor

### Date

2026-06-01

### Area

output_contract

### Files / Functions

- `watchlist_scanner/daily_memo.py` — `_build_memo_top_insight`
- `tests/test_daily_memo.py` — `TestTopInsightThemeMembership` (4 tests)

### Decision

`_build_memo_top_insight` unconditionally appended `"{ticker} remains the lead opportunity inside the {theme} theme"` whenever a theme name and a ticker both existed — it never checked whether the ticker was a member of `top_theme.tickers`. On 2026-06-01 this rendered "MSFT … inside the Energy Transition theme" though Energy Transition = `[XOM, CVX]` and MSFT maps to Technology / theme "Unspecified". The "inside the {theme}" clause is now gated on case-insensitive membership in `top_theme.tickers`. Non-members and empty/unknown ticker lists render both facts un-linked: `"{ticker} remains the lead opportunity; {theme} remains the dominant theme."`

### Why

Surfaced by the always-on `portfolio-memo-reviewer` agent during the 2026-06-01 daily-tool-analysis run. The memo was stating a theme link the data does not support, which can mislead the operator about why a ticker is the lead opportunity. Commit `72800c06`.

### Invariants Preserved

- No scoring / decision / allocation / recommendation behavior changed — render layer only
- Membership-positive output is byte-identical to before (backward compatible)
- Memo compact contract unchanged (max 5 decisions / 3 risk / 3 changes); no list-count change

### Downstream Impact

- `outputs/latest/daily_memo.md` and `daily_memo.txt` Top Insight line stops asserting theme membership for a non-member lead opportunity; both renderers share the single fixed function
- Verified next-run by the always-on memo-reviewer dispatch (cross-artifact: `daily_memo.md` text vs `system_decision_summary.json` `top_theme.tickers` membership)

## Observe-Only Documentation Auditor System

### Date

2026-06-01

### Area

architecture

### Files / Functions

- `portfolio_automation/doc_audit.py` — `Anchor`, `Finding`, `ANCHOR_REGISTRY`, `resolve_source`, `find_drift`, `find_coverage_gaps`, `find_dead_refs`, `find_cross_doc_inconsistency`, `run_doc_audit`, `write_doc_audit_status`, `apply_auto_fix`
- `portfolio_automation/doc_audit_state.py` — `load_state`, `save_state`, `state_path`
- `.agent/doc_audit_state.yaml` — committed cross-workstation auditor state
- `.claude/commands/doc-audit.md` — weekly `/doc-audit` skill (producer + guardrailed auto-fix + state advance)
- `.claude/commands/doc-audit-monthly.md` — monthly `/doc-audit-monthly` skill (producer + read-only judgment dispatch)
- `.claude/agents/portfolio-doc-auditor.md` — read-only judgment agent (clarity / conciseness / redundancy / decomposition lens)
- `scripts/run_doc_audit.sh`, `scripts/run_doc_audit_monthly.sh` — VPS cron wrapper scripts
- `tests/test_doc_audit.py` (21 tests), `tests/test_doc_audit_state.py` (3 tests)
- `docs/doc_audit.md`, `docs/doc_audit_state.md` — module documentation

### Decision

Shipped a complete observe-only documentation-auditor system.  The producer
(`doc_audit.py`) runs four check families: factual drift against machine-readable
source artifacts (auto-fixable, bounded to the anchor registry); coverage gaps
(new source module without a `docs/<stem>.md`); dead references (backtick
`.py` mentions that no longer resolve on disk); cross-document consistency
(anchor value disagreement across docs).  Auto-fix is capped at 10/run, gated
by `apply_enabled`, limited to pure captured-value substitution, and protected
by path-containment and staleness guards.  State is committed to `.agent/`
(tracked, cross-workstation portable) rather than `data/` (gitignored); rollback
= `git revert`.

Two cadence tiers: weekly Mon 09:45 UTC (`/doc-audit`, auto-fix + state advance)
and monthly 1st 09:15 UTC (`/doc-audit-monthly`, read-only judgment via
`portfolio-doc-auditor` agent).

`daily-tool-analysis` was extended to consume `doc_audit_status.json` and signal
AMBER on `coverage_gap` or unfixed drift.  `monthly-tool-analysis` received a
documentation-lens hook.

This is an operator-requested feature, not a roadmap step.  `next_official_step`
remains `observe_and_iterate`.

### Why

Documentation drift is an invisible liability in an advisory system: a stale
policy number in a doc becomes the operator's mental model even after the live
configuration changes.  The anchor registry provides a permanent, low-noise
bridge between machine-readable artifacts and prose documentation.  The weekly
auto-fix tier handles rote numeric updates (caps, budgets, stage counts) without
operator involvement; the monthly judgment tier catches structural problems
(redundancy, missing sections, decomposition) that no regex can catch.  Committing
the state file ensures the coverage gap check is reproducible on every workstation
without manual SHA bookkeeping.

### Invariants Preserved

- `observe_only: true` hardcoded in all output artifacts
- No scoring, decision, allocation, recommendation, or schema behavior changed
- Auto-fix restricted to the anchor registry and to pure in-place substitution
- All pipeline integration wrapped in `try/except` (non-blocking)
- OutputNamespace.LATEST used for artifact writes; no writes to HISTORICAL or
  other namespaces

### Downstream Impact

- `outputs/latest/doc_audit_status.json` and `.md` produced on each weekly run
- `daily-tool-analysis` AMBER signal on coverage_gap or unfixed drift
- `portfolio-doc-auditor` agent dispatched monthly for judgment-lens review
- VPS cron entries: `scripts/run_doc_audit.sh` (Mon 09:45 UTC) + `scripts/run_doc_audit_monthly.sh` (1st 09:15 UTC)


---

## 2026-06-05 — Pattern-Improvement Loop end-to-end driver (`backtesting/run_loop.py`)

### Decision

Add a thin observe-only CLI driver that chains the loop's Steps 1→4 into one
command (`python -m backtesting.run_loop`), so the loop is runnable end-to-end
rather than only as composable library pieces. The missing connective tissue
between Step 2 (walk-forward OOS) and Step 4 (tuning proposals).

### What it does

- Loads real emitted signals (single artifact or aggregated `outputs/history`).
- Runs the POC simulation (`run_poc`) → `outputs/backtest/poc_simulation_results.json`.
- Computes per-signal OOS efficacy via `walk_forward`, grouped by registry
  `signal_id` (`STRONG_MOVE` direction-resolved to UP/DOWN).
- Feeds OOS efficacy into `propose_weight_changes` → writes
  `outputs/policy/signal_weight_proposals.json`.

### Guarantees

- Observe-only / proposes-only. **Step 5 (governed apply) is never invoked** —
  `registry_apply.py` stays inert/owner-gated.
- `config/signal_registry.yaml` byte-identical before/after (asserted in tests).
- No protected scoring/decision/allocation logic touched; no schema changed.
- `run_poc` gained one additive optional param (`signals=`); backward compatible.

### Downstream impact

- Writing the proposals artifact flips `backtest_health` off `proposals_missing`
  (→ `no_proposals` when there's no real edge, the honest state).
- New contracts documented in `docs/OUTPUT_ARTIFACT_CONTRACTS.md`.
- Tests: `tests/test_run_loop.py` (11). Real-edge evidence still requires a
  `--live` run with FMP access + sufficient history (operator's complete env).

---

## 2026-06-05 — Step 5 protected-score value-regression gate (`backtesting/score_invariance_gate.py`)

### Decision

Build the documented Step 5 precondition #2 — a value-regression gate proving a
registry `default_weight` apply is semantically safe for the six protected
scores — WITHOUT enabling or executing any live apply (owner chose "build the
gate"; a live apply was not authorized and has no evidence basis yet).

### What it does

- Copies the registry to a temp file, applies a candidate delta to the temp copy
  via `registry_apply`, recomputes the protected scores over a fixed offline
  fixture (real `scanner`/`confidence`/`alert_ranking` functions) before/after,
  and asserts **bit-identical**. GREEN = weight moved, scores unchanged; RED =
  coupling regression; inconclusive = apply no-op.
- Wired opt-in into `backtest_health.assess_backtest_health(run_score_gate=True)`
  → RED flag `score_coupling_regression` (yearly Quant-lens cadence).

### Key architectural finding

The registry `default_weight` is **decoupled from all six protected scores** —
no scoring function reads it (`final_rank_score` uses `config/base.json`
"ranking" coefficients; the 0.45 match with STRONG_MOVE_UP's weight is
coincidental). Consequence: a Step 5 apply changes the YAML value but **changes
no decision**. The loop's weight proposals have no scoring consumer yet. Wiring
`default_weight` into scoring is protected scope requiring explicit owner
approval — intentionally **not** done.

### Guarantees

- Observe-only; operates on a temp copy; live `config/signal_registry.yaml`
  byte-identical (asserted). No scoring/decision logic modified.
- `backtest_health` gained an opt-in param (default off); artifact-only path
  unchanged.
- Step 5 live apply remains owner-gated + inert; precondition #1 (owner-signed,
  evidence-backed `approved_weight_changes.json`) still unmet.

### Tests

`tests/test_score_invariance_gate.py` (6) + 2 in `tests/test_backtest_health.py`.
Full backtesting suite: 252 passed.
