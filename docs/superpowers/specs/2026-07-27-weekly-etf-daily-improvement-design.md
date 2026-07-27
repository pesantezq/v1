# Weekly ETF Bundles — Daily Continuous-Improvement (Split Cadence) — Design

**Date:** 2026-07-27
**Status:** Design approved (guardrails added by operator); implementation plan pending
**Scope:** Additive, observe-only. No change to `decision_engine.py`, score semantics, or the daily decision core.

## 1. Context

The weekly ETF bundle watchlist subsystem (`portfolio_automation/weekly_etf_bundles/`)
was merged to main (`7a36dc37`) and **activated** on 2026-07-27:

- 23 bundle-ticker 5y archives refreshed; first immutable prediction frozen
  (`outputs/weekly_etf_bundles/predictions/2026-07-27.json`, 23 rows).
- `WEEKLY_ETF_BUNDLES_ENABLED=1`; Monday 08:30 UTC cron (`run_weekly_etf_bundles.sh`).
- Email stays dry-run. Invariants hold: `observe_only=true`, `simulation_active=true`,
  `production_gated=true`, `feeds_decision_engine=false`.

The subsystem is architecturally **weekly**: predictions are frozen once per week,
immutable, keyed on `market_data_date`, with a conflict guard that refuses to
re-freeze a date. Outcomes mature at 1/4/12/26-week horizons.

## 2. Goal

Let the subsystem **improve continuously** without violating weekly immutability, by
running the *non-freezing* learning steps daily and routing scoring changes through a
**human-gated, anti-overfit** champion-swap workflow.

## 3. Architecture — split cadence

| Cadence | Runner | Does | Freezes? |
|---|---|---|---|
| **Weekly** (Mon 08:30) | `run_weekly_etf_bundles.sh` (unchanged) | full run incl. **freeze** new prediction + digest | ✅ once/week |
| **Daily** (new) | new stage in `run_daily_safe.sh` | mature outcomes → refresh scorecard/calibration/attribution → Strat Lab champion/challenger comparison → health → champion-swap proposer | ❌ never |

The daily lane reads the immutable ledger + fresh prices and rewrites the *derived*
observe artifacts only. It never writes `predictions/<date>.json`.

## 4. Components

### 4.1 New non-freezing run mode
Add `--daily-observe` to `weekly_etf_bundles/run.py` that runs steps: mature outcomes,
evaluate (scorecard/calibration/attribution), Strat Lab comparison, health, champion-swap
proposer — with `do_freeze=False` hard-wired. Reuses existing step functions; no new
analysis math.

### 4.2 Daily pipeline stage
New stage in `run_daily_safe.sh`, after the existing observe-only stages:
- Wrapped in `try/except` (non-blocking; never affects the daily exit code).
- Gated by `WEEKLY_ETF_BUNDLES_ENABLED` **and** a new daily sub-flag
  `WEEKLY_ETF_BUNDLES_DAILY_ENABLED` (default 0 — ships inert).
- Its own in-stage guard/log; writes only to the `WEEKLY_ETF_BUNDLES` namespace.

### 4.3 Human-gated champion-swap proposer  *(the "auto-tune")*
When a challenger's **matured** performance beats the champion, emit a champion-swap
**proposal** through the existing human approval path
(`portfolio_automation/sim_governance/promotion_approvals.record_approval`, `base_dir=<root>/outputs`).
**Never auto-applied.** AI/heuristic may recommend; only a human approver promotes.
Inert for weeks until outcomes mature.

## 5. Guardrails (operator-mandated)

### 5.1 Timezone guardrail
`market_data_date` currently snaps to `last_on_or_before(panel.dates, as_of)` where
`as_of`/`generated_at` derive from **UTC now** — an off-by-one risk near the UTC/ET
boundary (a UTC-cron firing after 20:00 ET could pick the wrong calendar date).
- Compute the "as-of trading date" reference in one **explicit, documented market
  timezone** (US/Eastern), not UTC-naive `now()`.
- The daily lane must **never** change an already-frozen `market_data_date`'s prediction;
  the freeze conflict-guard remains the backstop. Add a test asserting a daily-observe run
  on the same market date does not mutate the frozen prediction file (content-hash stable).

### 5.2 Rollback evidence
Every champion-swap **apply** (post human approval) captures **before/after** state
(active champion variant + its config snapshot) into an append-only audit
(`outputs/weekly_etf_bundles/champion_swap_audit.jsonl`) — mirroring the sim-gov
reversible pattern. A human veto rolls back via compare-and-swap (never overwriting
newer state → `rollback_conflict`). No apply without a captured before-state.

### 5.3 Anti-overfitting controls for champion swaps
A challenger may be *proposed* to replace the champion only if **all** hold:
1. **Minimum matured sample** — ≥ `min_matured_n` resolved predictions per evaluated
   horizon (config; e.g. 20), never on 0/near-0 samples.
2. **Out-of-sample / walk-forward** — outperformance measured on walk-forward folds
   (`list_prediction_dates`), not the in-sample window that selected the variant.
3. **Sustained margin** — challenger beats champion by ≥ `min_margin` over **K
   consecutive** evaluation periods (config; e.g. K=4), not a single lucky window.
4. **Multiple-comparison correction** — because "best of 4 variants" is selection-biased,
   apply a Holm/Bonferroni-style adjustment (or a raised significance bar) across the
   variant set; record the adjusted p-value/threshold in the proposal.
5. **Economic + statistical significance** — margin must clear both a minimum effect size
   and the corrected significance bar; a Sharpe-style haircut/deflation is recorded.
A proposal that fails any control is **not** emitted; the reason is logged for the health
skill. All thresholds live in `config/weekly_etf_bundles.yaml` with recorded rationale
(Strategy Documentation Requirement).

## 6. Analysis + Health coverage (mandatory pairing)
The daily lane runs at **daily cadence** → extend `.claude/commands/daily-tool-analysis.md`:
- read the daily ETF artifacts (health, scorecard, calibration, champion-swap proposal);
- signals: `weekly_etf_daily_ran`, `champion_swap_pending` (a proposal awaiting human
  approval → AMBER, actionable), content_liveness (`status==ok` but 0 tickers scored);
- RED only on an invariant breach (`feeds_decision_engine` flips true) — otherwise AMBER-max.
`/weekly-etf-analysis` remains the deep readout. Add tests asserting healthy vs degraded
fixture states, per the Analysis+Health Coverage Requirement.

## 7. Error handling
Non-blocking daily stage; fail-closed champion-swap (no proposal on missing/short data,
invariant doubt, or failed rollback-precondition). Circuit-breaker on repeated
apply/rollback failure, matching sim-gov.

## 8. Testing
- Daily-observe mode: matures + evaluates + does NOT freeze (content-hash of the frozen
  file unchanged) — healthy + degraded fixtures.
- Timezone: as-of date resolved in market tz; boundary case near UTC midnight.
- Anti-overfit: each of the 5 controls blocks a proposal when violated; a clean fixture
  emits exactly one proposal.
- Rollback: apply captures before-state; veto restores it; CAS conflict path.
- Health/analysis: `champion_swap_pending` AMBER; invariant-breach RED.

## 9. Phasing
1. Daily-observe mode + daily stage (inert sub-flag) + health/analysis coverage + tests.
2. Champion-swap proposer with the 5 anti-overfit controls + timezone guardrail (proposal
   only; inert until data matures).
3. Human-gated apply + rollback-evidence audit + veto/CAS.
Each phase is independently shippable and observe-only until its gate is flipped.

## 10. Boundaries
Observe-only; simulation/ sandbox namespaces only; production changes only via
human-approved champion-swap; `feeds_decision_engine=false` throughout; no
`decision_engine.py` / scoring / allocation changes.
