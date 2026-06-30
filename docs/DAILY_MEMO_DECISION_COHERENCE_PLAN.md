# Daily Memo Decision-Coherence Plan

Status: **active** · Branch: `feat/daily-memo-decision-coherence` · Started 2026-06-30
Owner: memo-coherence upgrade · Scope: **advisory-only, additive, observe-only**

This plan upgrades the Daily Investment Memo from a stack of independent
subsystem outputs into a single, portfolio-aware, internally-consistent decision
document. It is a **memo-coherence and decision-presentation** change. It does
**not** alter scoring, allocation, recommendation, or any production trading
behavior. Production promotion stays human-gated.

---

## 0. Audit summary (source-to-artifact map)

Verified against live `outputs/latest/` on 2026-06-30 (47-decision plan).

| Concern | Canonical producer | Artifact | Authoritative? |
|---|---|---|---|
| Decisions (source of truth) | `portfolio_automation/decision_engine.py::build_decision_plan()` | `outputs/latest/decision_plan.json` | **Authoritative** |
| Verdict / theme / opp / fit | `decision_engine.py::summarize_decision_plan()` + `watchlist_scanner/system_summary.py::compute_top_theme/compute_top_opportunity/compute_best_portfolio_fit` | `system_decision_summary.json` | Authoritative for narrative selection |
| Available cash + deployable | `portfolio_automation/cash_deployment_plan.py::compute_available_cash()` (5% reserve, `_SAFETY_FLOOR_PCT`) | `cash_deployment_plan.json` | **Authoritative for funding** |
| Holdings/cash source | `portfolio_automation/holdings_resolver.py::resolve_holdings()` (broker-preferred, freshness-graded) | feeds context | Authoritative |
| Concentration/leverage/VaR | `portfolio_automation/risk_delta_advisor.py` | `risk_delta.json` | Authoritative (observe-only) |
| Correlation / overlap | `portfolio_automation/correlation_risk_advisor.py` + `sector_mapping.normalize_sector()` | `correlation_risk_advisor.json` | Authoritative (observe-only) |
| Kelly sizing | `portfolio_automation/kelly_sizing_advisor.py` | `kelly_sizing_advisor.json` | Advisory (observe-only) |
| Hit-rate (calibration) | `confidence_calibration.json` + `outputs/policy/decision_outcomes.jsonl` | rendered by `watchlist_scanner/memo_enrichment.py::render_hit_rate_*` | Advisory |
| Signal outcomes (producer) | `watchlist_scanner/performance_feedback.py::evaluate_pending_signal_feedback()` | `outputs/performance/signal_outcomes.csv`, `performance_summary.json` | Authoritative (frozen — not touched) |
| Crowd (unified) | `flock_intelligence` + `social_intelligence/crowd_state_classifier.py` | `unified_crowd_intelligence_status.json` | **Sandbox / production-gated** |
| Strategy comparison | `portfolio_automation/strategy/strategy_comparator.py` | `strategy_comparison.json` | Sandbox-only |
| Memo render | `watchlist_scanner/daily_memo.py` (`build_daily_memo`, `build_daily_memo_md`, `generate_daily_memo`) + `memo_enrichment.py` | `daily_memo.txt`, `daily_memo.md` | Consumer/renderer only |

**Protected (must not change):** `decision_engine.py`, `compute_priority()`,
`conviction.py`, `allocation_engine.py`, and the score semantics `signal_score`,
`confidence_score`, `effective_score`, `conviction_score`, `final_rank_score`,
`recommendation_score`, `priority_score`. The producer `performance_feedback.py`
win-rate is also left untouched to preserve tuning baselines + historical compat.

---

## 1. Current-State Diagnosis

Each problem below was reproduced against the live 47-decision run.

1. **"Cautious" verdict alongside risk-increasing actions.** The verdict mood
   ladder (`daily_memo.py::_build_verdict`) and the BUY/SCALE action mix are
   computed independently and never reconciled. 18 BUY + 3 SCALE were present
   with no statement of how they square with a cautious posture.
   *Origin: aggregation/rendering (no reconciliation step).*

2. **Recommended capital can exceed available cash unlabeled.** `decision_plan`
   emits capital decisions (`recommended_amount` is often `null`);
   `cash_deployment_plan` separately funds only the **top 10** ranked rows from a
   `$758` deployable budget while `cash_available` is `$150.60`
   (`below_safety_floor: true`). The other 11 capital decisions are silently
   omitted, never labeled "unfunded". And deployment ($392.60) exceeds cash on
   hand because it counts **incoming contributions** — never disclosed.
   *Origin: aggregation (no funded/unfunded join, no cash-vs-incoming split).*

3. **Identical `0.550` priorities.** 19 of 47 decisions share `priority == 0.55`.
   Root cause: market/`watch`-sourced decisions have all-zero drivers
   (`conviction_score=signal_score=confidence_score=0.0`) so `compute_priority`
   would yield `0.0`; a **default fallback of 0.55** is injected upstream. The
   ranking is therefore a flat plateau with no tie-break.
   *Origin: producer default + rendering (no tie detection/break).*

4. **Noise-level moves scored as hit/miss.** `performance_feedback.py:238` uses
   `direction_correct = return_pct > 0` with **no neutral band**, so `+0.06%`
   counts "correct" and `−0.09%` "incorrect". (Note: `outcome_evaluator`
   already uses a ±1% flat band elsewhere — the convention exists but isn't
   applied to correctness.) *Origin: producer rule; we correct at the memo
   evaluation layer to avoid disturbing tuning baselines.*

5. **Momentum BUYs after large one-day moves lack entry context.** PANW carried
   `momentum: +9.14% today` with a plain BUY and no "extended / starter-only /
   pullback" framing. *Origin: rendering (no entry-context derivation).*

6. **Risk caps pass while portfolio is highly correlated.**
   `correlation_risk_advisor` shows QQQ/QLD corr `0.999` (combined 53%),
   QQQ/CHAT `0.895`, `effective_independent_bets: 1.386`,
   `low_effective_independent_bets` — but per-position concentration caps still
   "pass". The memo surfaced concentration, not economic overlap.
   *Origin: rendering (overlap data computed but not surfaced).*

7. **Theme / opportunity / fit / Top-Decisions can contradict.** These are four
   independent `max()` selections over different artifacts; nothing checks that
   the dominant theme is represented in displayed decisions, or that the top
   opportunity / best fit appear in Top Decisions (or explains why not).
   *Origin: aggregation (independent selectors, partial existing check in
   `_build_memo_top_insight`).*

8. **Crowd Radar self-contradiction.** `unified_crowd_intelligence_status.json`
   reports `confirmed_attention: 11` and `divergent_attention: 9` **and**
   `insufficient_data: 22` with `social_sentiment: PLAN_LOCKED`. "Confirmed
   attention" is *cross-source attention overlap*, not a *classified
   crowd-knowledge buy state* — the memo conflates the two definitions.
   *Origin: rendering (definitions not surfaced).*

9. **Counts refer to different universes.** Position-cap counts (risk_delta),
   recommendation counts (decision_plan: 21 capital actions), funded counts
   (cash_deployment: 10 rows) refer to different universes with no bridge.
   *Origin: rendering (no reconciliation of counts).*

10. **Telemetry drowns the decision.** FMP usage, probe health, retune
    validation, artifact population sit inline with investor content.
    *Origin: rendering (no investor/operator split).*

---

## 2. Target Architecture

A deterministic, pure-function pipeline producing one reconciled artifact that
the renderer consumes. New module: **`portfolio_automation/memo_coherence.py`**.

```
1. load_sources()        → read decision_plan, system_decision_summary,
                           cash_deployment_plan, risk_delta, correlation_risk_advisor,
                           kelly_sizing_advisor, confidence_calibration,
                           unified_crowd_intelligence_status, portfolio_snapshot.
2. normalize_freshness() → per-source generated_at, age, stale flag, max skew.
3. build_candidates()    → one enriched record per decision_plan decision:
                           presentation_state, priority_breakdown + basis,
                           entry_context, thesis/risk, portfolio-fit reason, eligibility.
4. compute_funding()     → join with cash_deployment rows: funded vs unfunded,
                           cash-on-hand vs incoming, gross/funded/unfunded capital,
                           blocking_reason per unfunded action. Reuse 5% reserve.
5. reconcile_fields()    → verdict, theme, opportunity, fit, top-decisions, risk,
                           cash, deployment, concentration, leverage, conviction
                           allocation, model confidence, data quality,
                           sandbox-vs-actionable → each {value, source, status, note}.
6. split_actions()       → funded_actions[], deferred_actions[].
7. run_guards()          → deterministic coherence guards → issues.
8. build_investor_view() → posture paragraph, main opp, main risk, what-changed.
9. build_crowd_narrative / build_overlap / evaluate_hit_rate (neutral band).
10. emit diagnostics     → memo_coherence.json (+ optional .md operator appendix).
```

The **renderer** (`daily_memo.py`) then reads `memo_coherence.json` and renders:

- **Investor-facing core** (first): posture · what the portfolio should do today ·
  funded actions · deferred/blocked · main opportunity · main risk · what changed ·
  performance · what to monitor.
- **Quant/analyst context**: thesis, risk, entry, fit, confidence, cluster, limits.
- **Operator/system appendix** (last): FMP usage, probe health, retune validation,
  model readiness, crowd connector status, degraded fields, timestamps, diagnostics.

All new calls are `try/except`-wrapped and non-blocking; missing inputs produce
honest degraded states. No renderer makes network calls.

---

## 3. Scope

### Required (this change)
- `portfolio_automation/memo_coherence.py` reconciliation layer + artifact.
- Funded vs unfunded action split (reuse existing 5% reserve + deployable).
- New memo-layer `presentation_state` vocabulary (additive; protected `decision`
  untouched).
- Priority transparency: breakdown, default-fallback detection, deterministic
  tie-break (presentation order only).
- Hit-rate neutral band at the memo evaluation layer (reuse ±1% convention).
- Portfolio overlap/cluster context (reuse correlation_risk_advisor + sector_mapping).
- Crowd narrative consistency (surface definitions; no new sources).
- Investor/operator memo split in `daily_memo.py` (additive; preserve headers).
- Coherence guards + `quant.daily_memo_coherence` probe + validator tool.
- Tests, docs, artifact contract, pipeline + preflight wiring,
  daily-tool-analysis hook.

### Optional follow-up (documented, not built now)
- Richer hit-rate metrics: MAE/MFE, payoff ratio, cost-adjusted return,
  benchmark-relative accuracy by regime/horizon.
- True ETF look-through using a constituent dataset (none available; no paid dep).
- Correlated-cluster backtesting in the sim lane.

### Explicit non-goals
- No change to `decision_engine.py`, `compute_priority`, conviction, allocation,
  or any protected score semantics.
- No broker/execution/auto-trade logic. No rebalance. No credentials. No paid APIs.
- No change to `performance_feedback.py` stored win-rate.
- Production promotion of any new decision behavior (stays human-gated).

### Production behavior that remains gated
- Crowd/flock/strategy outputs stay `observe_only` / sandbox / production-gated.
- The memo and the new artifact are advisory; `no_trade: true`, `observe_only: true`.

---

## 4. Acceptance Criteria → Tests

| # | Criterion | Test |
|---|---|---|
| AC1 | Cautious verdict + all-BUY mix → explanation or coherence warning | `test_reconcile_cautious_with_buys_warns` |
| AC2 | Top opportunity absent from Top Decisions → reason emitted | `test_top_opportunity_missing_reason` |
| AC3 | Best-fit symbol not funded → blocking reason stated | `test_best_fit_not_funded_explained` |
| AC4 | Theme/action mismatch detected | `test_theme_not_represented_flagged` |
| AC5 | Legitimate differences don't create false failures | `test_consistent_inputs_status_ok` |
| AC6 | Recommendations > cash split funded/unfunded | `test_funding_split_exceeds_cash` |
| AC7 | Funded never exceeds deployable | `test_funded_never_exceeds_deployable` |
| AC8 | Zero-cash honest | `test_zero_cash_honest` |
| AC9 | No-sale plans don't assume sale proceeds | `test_no_phantom_sale_proceeds` |
| AC10 | Missing cash data → degraded | `test_missing_cash_degraded` |
| AC11 | Default/fallback priority identified | `test_default_priority_detected` |
| AC12 | Genuine ties get deterministic tie-break | `test_tie_break_deterministic` |
| AC13 | Score breakdown preserved | `test_priority_breakdown_preserved` |
| AC14 | Rounding doesn't collapse distinct ranks | `test_rounding_does_not_collapse` |
| AC15 | Noise moves → neutral/unresolved | `test_hit_rate_neutral_band` |
| AC16 | Missing prices ≠ correct/incorrect | `test_missing_price_not_scored` |
| AC17 | Large daily move → extension context | `test_entry_extended_context` |
| AC18 | Normal move → no false warning | `test_entry_normal_no_warning` |
| AC19 | Correlated semis grouped | `test_overlap_semiconductor_cluster` |
| AC20 | ETF overlap degrades honestly when no constituents | `test_overlap_etf_degraded` |
| AC21 | QQQ/CHAT overlap surfaced when present | `test_overlap_existing_exposure` |
| AC22 | Raw overlap not mislabeled as classified state | `test_crowd_raw_vs_classified` |
| AC23 | No-credential crowd state non-blocking | `test_crowd_no_credentials_nonblocking` |
| AC24 | Insufficient data rendered consistently | `test_crowd_insufficient_consistent` |
| AC25 | Investor core precedes operator telemetry | `test_investor_core_before_appendix` |
| AC26 | Markdown + text consistent | `test_txt_md_consistent` |
| AC27 | Advisory/sandbox labels visible | `test_advisory_labels_present` |
| AC28 | Existing consumers don't break | existing `tests/test_daily_memo*.py` pass |
| AC29 | Guards never crash pipeline | `test_run_memo_coherence_degraded_never_raises` |
| AC30 | No-mutation invariant | `test_no_mutation_of_decision_plan` |

---

## 5. Governance

- Advisory only; `observe_only: true`, `no_trade: true` hardcoded in the artifact.
- Simulation lane active; production gated; human approval required for production.
- No threshold tuning to flatter output; the hit-rate neutral band makes results
  **more** honest (more "neutral", fewer false "correct").
- Deterministic fallbacks preserved; degraded inputs → honest degraded states.
- Reuses existing reserve policy, ±1% band, correlation advisor, sector mapping,
  probe registry, data-governance writers — no parallel system.

---

## 6. Follow-up shipped: Monthly Capital Envelope (2026-06-30)

Funding was upgraded from "full net-investable deployable every day" to a **monthly capital
envelope** in the canonical capital producer (`cash_deployment_plan.py`, schema v1→v2) — not a
parallel engine.

- **Formulas (amount-based, decimal-safe).** `reserve_target = reserve_pct × portfolio_value`;
  `reserve_shortfall = max(0, reserve_target − cash_on_hand)`;
  `net_investable = max(0, gross_contribution − reserve_shortfall)`;
  `remaining = max(0, net_investable − deployed_before_today − funded_today)`.
- **Reserve denominator** = `portfolio_value`; reserve % is canonical `portfolio.target_cash_weight`.
- **Cycle** = calendar month; **no rollover**. Prior deployment from an append-only ledger
  (`outputs/policy/monthly_deployment_ledger.jsonl`), idempotent via last-wins-per-date.
- **Degraded**: `INSUFFICIENT_CAPITAL_DATA`; `monthly_history_status` ok/partial/unavailable —
  never silently assumes zero prior deployment.
- **New statuses** replace blanket `BLOCKED_BY_CASH`: FUNDED_STARTER/FUNDED_STANDARD/
  RESERVED_FOR_CASH_FLOOR/HELD_FOR_PULLBACK/DEFERRED_BY_MONTHLY_BUDGET/DEFERRED_BY_THEME_CAP/…
- **Config** `config.daily_memo_capital`: starter 0.005, standard 0.01, max/cycle 0.015,
  theme cap 0.40 of net investable.
- **Memo**: Monthly Capital Plan, Funded Actions (% of portfolio + % of net investable + tranche
  + entry basis), Capital Held Back, Concentration Check (honest degrade). Extension language now
  names its basis ("Session move: +X%"), never "today".

Full field-level contract: `docs/OUTPUT_ARTIFACT_CONTRACTS.md` →
`cash_deployment_plan.json — monthly_capital_envelope (schema v2)`.
