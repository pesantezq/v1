# Memo Coherence Reconciliation Layer

**Module:** `portfolio_automation/memo_coherence.py`
**Artifact:** `outputs/latest/memo_coherence.json` (+ `.md` operator appendix)
**Pipeline stage:** `run_daily_safe.sh` Stage 9e (non-blocking, before the memo)
**Probe:** `quant.daily_memo_coherence` · **Validator:** `tools/validate_daily_memo_coherence.py`
**Status:** advisory · observe-only · never feeds `decision_plan.json` · no trades

## Purpose

Turn the daily memo from a stack of independent subsystem outputs into one internally
consistent decision document. The layer is a **deterministic, pure-function reconciliation
step** that reads already-produced artifacts and reconciles them; it never recomputes a
decision or any protected score and never mutates production state.

## What it reconciles

Reads `decision_plan.json`, `system_decision_summary.json`, `cash_deployment_plan.json`,
`risk_delta.json`, `correlation_risk_advisor.json`, `kelly_sizing_advisor.json`,
`confidence_calibration.json`, `unified_crowd_intelligence_status.json`,
`portfolio_snapshot.json`, and `outputs/policy/decision_outcomes.jsonl`.

Pipeline (pure functions): `load_sources → build_freshness → build_candidates →
compute_funding → finalize_actions (presentation_state + tie-break) → reconcile_fields →
build_overlap → build_crowd_narrative → evaluate_hit_rate → run_guards →
build_investor_summary → build_memo_coherence`.

## Key behaviors

- **Funded vs unfunded.** Joins capital decisions with `cash_deployment_plan` rows. Reuses the
  existing 5% cash reserve (`cash_reserve_pct`) — invents no policy. Distinguishes
  *deployable from cash on hand* vs *from incoming contributions*; capital decisions ranked
  out of the budget become deferred with a `blocking_reason`.
- **Presentation states (additive).** The protected `decision` is unchanged; a memo-layer
  `presentation_state` (BUY_NOW/STARTER/ADD/ADD_ON_PULLBACK/WATCH/HOLD/TRIM/BLOCKED_BY_CASH/
  BLOCKED_BY_CONCENTRATION/BLOCKED_BY_RISK/RESEARCH_ONLY/INSUFFICIENT_DATA) is derived from
  decision + conviction band + funding + entry context + eligibility.
- **Priority transparency.** Surfaces the read-only weighted breakdown of `compute_priority`
  (0.45 conviction + 0.35 signal + 0.20 confidence), flags the default-fallback `0.55` plateau,
  and applies a deterministic tie-break (priority → today's momentum → confidence → symbol).
- **Hit-rate neutral band.** Re-evaluates `decision_outcomes.jsonl` with an economically
  meaningful ±1% band (reused from `outcome_evaluator._label_return`). Sub-band noise moves are
  **neutral**, not hit/miss. `return_pct` there is a decimal fraction — converted to percent
  before banding. The producer win-rate is left unchanged.
- **Overlap context.** Groups proposed buys into thesis clusters from
  `correlation_risk_advisor` high-correlation pairs + `sector_mapping.normalize_sector`.
  ETF look-through is honestly reported as degraded (no constituent dataset).
- **Crowd narrative.** Surfaces distinct definitions: cross-source *attention* confirmation vs
  retail-only vs divergent vs *classified* crowd-knowledge state vs insufficient data. Crowd is
  always `production_eligible: false`.
- **Coherence guards.** Deterministic checks (cautious-verdict-vs-action-mix, top-opportunity /
  best-fit / theme not represented, identical priorities, count reconciliation, crowd
  attention-vs-classified, stale-mixed-with-fresh, model readiness, etc.). Each is an explained
  issue, `resolved` when legitimate; unresolved issues set `coherence_status` to warning.

## Memo rendering

`watchlist_scanner/daily_memo.py` attaches the result as `summary["_memo_coherence"]` and
renders an **investor-facing core** (posture · funded actions · deferred/blocked · main
opportunity · main risk · clusters) before the existing analyst sections, then an
**OPERATOR / SYSTEM APPENDIX** divider ahead of the telemetry. Both `.txt` and `.md` are
updated symmetrically; sections are skipped (not errored) when coherence data is absent.

## Governance

Advisory only; `observe_only`/`no_trade` hardcoded; degraded inputs yield honest
`{available: false, reason}` sections; the top-level entry point never raises. Production
behavior remains human-gated. See `docs/DAILY_MEMO_DECISION_COHERENCE_PLAN.md` for the full
diagnosis, acceptance criteria, and follow-up scope.
