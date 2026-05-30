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

