# Strategy Mandates & Champion/Challenger (Phase 9)

Status: **shipped** on `feat/complete-simulation-quant-governance-loop`.
Observe-only — research contestants + scoring; never production instructions;
promotion human-gated (Phase 10).

## What already existed
The 8 materialized profiles (`portfolio_automation/strategy/profiles.py`) +
a score-based leaderboard. Phase 9 adds the **structured mandate** each profile
was missing and the formal champion/challenger/control framing.

## Mandate (`strategy_mandate.MANDATES`)
Per profile: `objective, benchmark, permitted_inputs, risk_budget,
turnover_budget, leverage_limit, concentration_limit, holding_period,
success_regime, failure_regime, promotion_criteria, rollback_criteria, role`.
All 8 profiles (`aggressive_growth, short_term_tactical, long_term_compounding,
tax_aware, defensive_capital_preservation, income_dividend,
balanced_core_satellite, boom_bucket`) have complete mandates;
`mandate_complete()` / `mandate_missing_fields()` flag gaps and
`build_strategy_mandates()` sets `coverage_complete=False` + lists `unmandated`
for any profile without one.

## Roles (`assign_roles`)
- **champion** = `production_baseline`
- **control** = `overlays_off` (production with overlays disabled)
- **challengers** = the materialized profiles + overlays

## Leaderboard scoring (`leaderboard_score`)
Multi-factor in [0,1] — **not CAGR/Sharpe alone**: rewards OOS excess +
consistency + regime stability, penalizes drawdown + turnover. Two entries with
identical CAGR but worse drawdown/consistency/regime-stability score lower.
`promotion_eligible()` requires OOS sample ≥ 30 — insufficient evidence blocks
promotion regardless of score.

## Cadence + consumers
Research/weekly cadence (no daily stage). Consumed by the Strategy Lab
(standardized evidence) and Phase 10 governance (a promotion proposal carries
the mandate's promotion/rollback criteria + OOS-eligibility gate).

## Tests
`tests/test_strategy_mandate.py` (6) — complete mandate per profile, incomplete
flagged, roles, multi-factor scoring, OOS promotion gate, unmandated detection.
