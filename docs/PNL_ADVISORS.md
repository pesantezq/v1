# P&L Advisors

Status: Phase 1 + Phase 2 + Phase 3 shipped, observe-only, wired into the
daily pipeline. Phase 4 (semantic changes to scoring / conviction /
allocation) remains gated behind explicit user approval per CLAUDE.md.

## Purpose

Each advisor is a single-purpose observe-only layer that consumes existing
artifacts (and optionally FMP-cached price data) and writes its own
namespace-aware JSON + Markdown artifact. None of them mutates a protected
score, modifies a decision, or executes a trade.

The advisors exist to surface gaps that the core decision engine does not
see: trailing-stop / time-stop on positions, cash that has accrued and
needs deployment, hidden correlation, earnings windows, vol regime, taxable
account loss-harvesting opportunities, Kelly-fractional sizing
recommendations, and per-alpha-source risk-adjusted attribution.

## Catalogue

| # | Module | Source | Artifacts (LATEST) | Inputs |
|---|---|---|---|---|
| P1.1 | `portfolio_automation/exit_advisor.py` | Phase 1 | `exit_advisor.{json,md}` | config holdings + FMP history |
| P1.2 | `portfolio_automation/cash_deployment_plan.py` | Phase 1 | `cash_deployment_plan.{json,md}` | config + decision_plan + system_decision_summary |
| P1.3 | `portfolio_automation/correlation_risk_advisor.py` | Phase 1 | `correlation_risk_advisor.{json,md}` | config holdings + FMP history |
| P2.1 | `portfolio_automation/earnings_gate.py` | Phase 2 | `earnings_gate.{json,md}` | config holdings + injected earnings lookup |
| P2.2 | `portfolio_automation/vol_regime_advisor.py` | Phase 2 | `vol_regime_advisor.{json,md}` | FMP benchmark history (default SPY) |
| P2.3 | `portfolio_automation/tax_harvest_advisor.py` | Phase 2 | `tax_harvest_advisor.{json,md}` | config holdings + cost basis + FMP current price |
| P3.1 | `portfolio_automation/kelly_sizing_advisor.py` | Phase 3 | `kelly_sizing_advisor.{json,md}` | `outputs/policy/decision_outcomes.jsonl` |
| P3.2 | `portfolio_automation/alpha_attribution_report.py` | Phase 3 | `alpha_attribution_report.{json,md}` | `outputs/policy/decision_outcomes.jsonl` |

All artifacts carry `observe_only: true` hardcoded. Every advisor is
non-blocking: a failure inside any single advisor is caught at the pipeline
integration site (`main.py`) and downgraded to a warning log.

## Hard guarantees (every advisor)

- `observe_only: true` is hardcoded in the written artifact.
- Failure is non-fatal — the daily pipeline always continues.
- Never modifies `signal_score`, `confidence_score`, `effective_score`,
  `conviction_score`, `final_rank_score`, or `recommendation_score`.
- Never modifies `decision_plan.json`, `ai_decision_validation.json`, or
  any output of an upstream layer.
- Writes only to the LATEST namespace via `safe_write_json` /
  `safe_write_text`.

## Pipeline integration

The advisors run inside `main.py`'s `_write_decision_engine_outputs` call
chain after the existing observe-only layers (outcome tracker, triage,
calibration, performance attribution) and before the AI budget summary.
Each advisor has its own `try/except` block so a single failure cannot
affect any other layer.

The decision_plan.json envelope written by main.py was extended in this
phase with a new top-level `portfolio_context` field carrying
`total_portfolio_value`, `cash`, `degraded_mode`, `data_mode`, and
`drawdown_regime`. This is purely additive; existing consumers ignore
unknown keys.

## Per-module details

### P1.1 Exit Advisor

Trailing-stop / time-stop / signal-decay layer. For each active holding it
emits one of `EXIT_FULL`, `EXIT_HALF`, `TIGHTEN_STOP`, or `HOLD`.

Strategy classification: leveraged holdings and assets explicitly tagged
`momentum` use tighter drawdown thresholds (5% / 10% / 18%). Everything
else is treated as a compounder (10% / 18% / 28%).

Time-stop applies only to momentum positions held > 120 days without a new
high. Signal-decay escalates a TIGHTEN to EXIT_HALF when current
`signal_score` has dropped at least 0.20 from entry AND drawdown is at or
above the soft threshold.

The output is advisory only — no SELL decision is ever emitted into the
decision plan.

### P1.2 Cash Deployment Plan

Calculates deployable cash from `excess_above_target + monthly_contribution`
respecting a 5% safety floor. Ranks BUY/SCALE decisions from the current
decision plan and distributes the budget across them subject to:

- the decision's own `recommended_allocation_pct` ceiling
- the per-position cap (8% of portfolio, mirrors `allocation_engine`)
- the conviction-band sizing multiplier (1.00 / 0.50 / 0.25 / 0.00 / 0.00)
- the running remaining budget

`portfolio_value` is read from the new top-level `decision_plan.portfolio_context`
field (added in this phase). Falls back to per-row `inputs_used.portfolio_context`
on older artifacts, and finally to cash-only when no other source exists.

Suspends deployment entirely when `degraded_mode` is true.

### P1.3 Correlation Risk Advisor

Computes a 90-day daily-return correlation matrix across active holdings,
flags any pair with `|corr| > 0.85` and combined weight > 25%, and reports
the effective number of independent bets:

    effective_bets = 1 / (w^T C w)

Triggers `low_effective_independent_bets` when the value falls below 4.0.
This catches concentration that `sector_cap` does not — e.g. QQQ + QLD +
NASA all driven by the same NDX factor.

### P2.1 Earnings Calendar Gate

Flags positions within 5 days (`REVIEW_BEFORE_EARNINGS`) or within 15 days
(`EARNINGS_APPROACHING`) of an earnings report. Also flags positions whose
earnings happened in the last 3 days (`POST_EARNINGS_REVIEW`).

Earnings data is provided via an injected `earnings_lookup` callable so
that FMP compliance is honoured: no new FMP endpoint is called from this
module. When the lookup is `None` (current default), every position
reports `status="no_earnings_source"` and `gate="HOLD"`. Wire the lookup
when an FMP-compliant earnings-calendar endpoint is registered.

### P2.2 Volatility Regime Advisor

Computes 20-day realized volatility of a benchmark (default SPY) and maps
annualised σ to a regime label:

| Regime | annualised stdev | Suggested aggregate sizing |
|---|---|---|
| calm | < 12% | ×1.10 |
| normal | 12% – 18% | ×1.00 |
| elevated | 18% – 28% | ×0.75 |
| risk_off | 28% – 45% | ×0.50 |
| crisis | ≥ 45% | ×0.25 |

The `sizing_multiplier_suggested` is purely advisory. Live allocations are
NOT modified by this layer (that would require a Phase-4-class change to
allocation_engine.py).

### P2.3 Tax-Loss Harvest Advisor

Activates only when `config.portfolio.is_taxable_account` is true. For
each holding with a known `cost_basis`, computes unrealized loss and flags
positions where the loss is at least $25 and (optionally) at least 5% of
basis (`material loss`).

A `replacement_map` argument can supply like-exposure substitutes (e.g.
QQQ → VGT) for wash-sale-safe rotation. The default replacement map is
empty by design: the operator decides what counts as substantially
identical for IRS purposes.

The advisor emits advisory notes only — never a SELL decision.

### P3.1 Kelly Sizing Advisor

Reads resolved outcomes from `outputs/policy/decision_outcomes.jsonl` and
computes a fractional Kelly recommendation per decision type
(BUY / SCALE / SELL):

    f_full = (b·p − q) / b
    f_recommended = clamp(0.5 · f_full, 0, 0.25)     # half-Kelly + 25% cap

  p = hit rate
  b = mean(positive return) / |mean(negative return)|

Gated at 20 judgeable resolved rows per decision type. Below the gate the
row reports `status="insufficient_data"` and the multiplier is `None`.

Calibration data is still accumulating; this layer mostly emits
insufficient_data today. That is the intended behaviour — it becomes
informative as the system collects resolved outcomes.

### P3.2 Alpha Attribution Report

Reads resolved outcomes and computes risk-adjusted metrics per alpha
source (`structural`, `portfolio`, `finance`, `watchlist`, `market`):

- hit_rate
- mean_return_pct
- return_stdev_pct
- downside_stdev_pct
- sharpe_proxy = mean / stdev
- sortino_proxy = mean / downside_stdev
- information_ratio_proxy (currently equal to sharpe_proxy against a
  zero benchmark)

Gated at 20 resolved returns per source. Surfaces best- and worst-Sharpe
sources to help the operator see which source actually earns
risk-adjusted returns.

## Validation

A single paste-safe command runs every advisor and lists every artifact:

```
python scripts/validate_pnl_advisors.py
```

Add `--no-fmp` to skip the FMP client construction (advisors degrade to
`insufficient_data` for layers that need price data). Add `--strict` to
exit with code 2 when any advisor reports `insufficient_data` (default is
to treat insufficient_data as success — only exceptions return non-zero).

## Phase 4 (NOT shipped — requires explicit user approval per CLAUDE.md)

These changes are designed in `docs/superpowers/specs/2026-05-15-pnl-maximization-roadmap.md`
but are blocked by the `forbidden_changes` list in
`.agent/project_state.yaml`:

- **P4.1**: replace static sizing multipliers in `conviction.py` with
  Kelly-calibrated values (changes `conviction_score` sizing semantics).
- **P4.2**: have the decision engine consume exit_advisor as a downgrade
  source (modifies `decision_engine.py`).
- **P4.3**: add weekly + monthly trend confirmation to the scanner
  signal_score composite (changes `signal_score` semantics).
- **P4.4**: feed `vol_regime_advisor.sizing_multiplier_suggested` into
  `allocation_engine` (changes allocation behaviour).

Any of these requires an explicit single-item approval before scope is
unlocked. The advisory layers above produce all the inputs each Phase 4
change would consume, so the operator can review the data first.

## File index

| Module | Tests | Doc section |
|---|---|---|
| `portfolio_automation/exit_advisor.py` | `tests/test_exit_advisor.py` | P1.1 above |
| `portfolio_automation/cash_deployment_plan.py` | `tests/test_cash_deployment_plan.py` | P1.2 above |
| `portfolio_automation/correlation_risk_advisor.py` | `tests/test_correlation_risk_advisor.py` | P1.3 above |
| `portfolio_automation/earnings_gate.py` | `tests/test_earnings_gate.py` | P2.1 above |
| `portfolio_automation/vol_regime_advisor.py` | `tests/test_vol_regime_advisor.py` | P2.2 above |
| `portfolio_automation/tax_harvest_advisor.py` | `tests/test_tax_harvest_advisor.py` | P2.3 above |
| `portfolio_automation/kelly_sizing_advisor.py` | `tests/test_kelly_sizing_advisor.py` | P3.1 above |
| `portfolio_automation/alpha_attribution_report.py` | `tests/test_alpha_attribution_report.py` | P3.2 above |
| `scripts/validate_pnl_advisors.py` | (validation runner) | this doc |
