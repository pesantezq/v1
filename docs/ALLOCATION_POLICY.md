# Allocation Policy

Last verified against `watchlist_scanner/conviction.py`, `watchlist_scanner/portfolio_construction.py`, `allocation_engine.py`, `portfolio_decision_engine.py`, `decision_engine.py`, `portfolio_automation/cash_deployment_plan.py`, `watchlist_scanner/allocation_preview.py`, and `config.json:growth_mode`. Last updated 2026-07-01 (refreshed cap values after the 2026-06-26/27 targeted
partial revert: `sector_cap 0.35→0.25`, `max_position_cap 0.15→0.12`; gauge era
`5687885c`). Prior: 2026-05-20 (tactical gauge retune + structural cap widening,
operator-approved 2026-05-18).

## Scope

There are two advisory allocation layers:

1. Watchlist allocation preview
   Observe-only sizing bands and normalized exposure preview.
2. Broader-market portfolio action sizing
   Advisory sizing for `BUY` and `PROMOTE_TO_PORTFOLIO` actions.

Neither layer has execution authority.

## Watchlist Allocation Preview

### Base sizing source

`watchlist_scanner/conviction.py`

Conviction bands map to multipliers:

- `defer -> 0.00`
- `observe -> 0.00`
- `starter -> 0.25`
- `normal -> 0.50`
- `high_conviction -> 1.00`

Target allocation bands:

- `defer -> 0%`
- `observe -> 0%`
- `starter -> 0.25-0.50%`
- `normal -> 0.50-1.00%`
- `high_conviction -> 1.00-2.00%`

### Base position sizing

`watchlist_scanner/portfolio_construction.py`

Defaults (`DEFAULT_PORTFOLIO_CONSTRUCTION_CONFIG`):

- `baseline_position_pct = 0.04`
- `max_total_allocation = 0.30`
- `max_ticker_allocation = 0.05`
- `max_sector_allocation = 0.10`

These were widened from the pre-retune baseline (`0.02 / 0.10 / 0.02 / 0.04`) on
2026-05-18 as part of the tactical gauge retune. The change is gauge-only — band
selection, conviction multipliers, and warning logic are unchanged.

Suggested allocation:

- `baseline_position_pct * sizing_multiplier`

Normalized allocation:

- starts as suggested allocation
- scales down if total suggested exceeds `max_total_allocation`
- then applies per-ticker cap
- then applies per-sector cap

### Concentration warnings

Warnings are emitted when:

- top sector exceeds the structural sector cap (`growth_mode.concentration_cap`,
  currently `0.60`)
- top 3 tickers exceed `0.70`
- too many `high_conviction` names share a theme
- degraded mode still carries positive normalized exposure

The portfolio_construction warning threshold for "top sector exceeds N%" now
references the structural cap in `config.json:growth_mode.concentration_cap`
rather than a hardcoded `0.40`. The daily memo's Portfolio Pulse section reads
the suggested-deployment cap reference from `allocation_engine.DEFAULT_CONFIG.sector_cap`
(currently `0.25`, after the 2026-06-26/27 targeted partial revert that tightened
`sector_cap 0.35→0.25` and `max_position_cap 0.15→0.12`).

### Degraded mode behavior

- conviction can be capped by `degraded_mode_cap`
- degraded mode adds a conviction penalty
- portfolio construction adds `degraded_mode_exposure_risk` warning when exposure remains non-zero

## Broader-Market Portfolio Action Sizing

### Base sizing

`allocation_engine.py:suggest_allocation`

Defaults (`allocation_engine.DEFAULT_CONFIG`):

- `compounder_base_pct = 0.10`
- `momentum_base_pct = 0.06`

Pre-retune baseline (recorded in `portfolio_automation/retune_impact_tracker.py`):
`compounder_base_pct = 0.05`, `momentum_base_pct = 0.03`.

### Confidence multipliers

- `high >= 0.75 -> 1.00`
- `medium >= 0.60 -> 0.75`
- `low -> 0.65`

Pre-retune `low_confidence_multiplier` was `0.50`; it was eased to `0.65` on
2026-05-18 so weakly-confident signals still get a modest sizing instead of
being snapped to half.

### Risk-off adjustments

When regime is `risk_off`, `significant_dip`, or `severe_dip`:

- compounder multiplier: `0.85`
- momentum multiplier: `0.55`

### Degraded-mode adjustment

- degraded penalty multiplier: `0.65`

### Caps

- `max_position_cap = 0.12`
- `sector_cap = 0.25`
- `cash_reserve_pct = 0.05`
- `min_position_pct = 0.01`

Pre-retune baseline (2026-05-18 widen): `max_position_cap = 0.08`, `sector_cap = 0.20`.
The 2026-05-18 retune widened these to `0.15 / 0.35`; the 2026-06-26/27 targeted
partial revert then tightened them to the current `0.12 / 0.25` to cap sector
overload (Energy) — see the header note in `allocation_engine.py`. The current
values are mirrored in the downstream sizing surfaces so the ceiling is
consistent across the system:

- `portfolio_automation/cash_deployment_plan._MAX_POSITION_PCT = 0.12`
- `watchlist_scanner/allocation_preview._DEFAULT_MAX_TICKER_PCT = 0.12`
- `watchlist_scanner/allocation_preview._DEFAULT_MAX_SECTOR_PCT = 0.25`

Weak-fundamentals cap:

- if fundamentals score `< 30`
- cap size at `0.02`

### Cash behavior

- reserve target = `portfolio_value * cash_reserve_pct`
- deployable cash = `cash_available - reserve_target`
- if no deployable cash remains, size becomes zero

### Action thresholds

`portfolio_decision_engine.py`

For new names:

- `PROMOTE_TO_PORTFOLIO`
  Requires strong score plus confidence threshold.
- `BUY`
  Starter position only; amount is multiplied by `buy_starter_multiplier` (`0.70` default).
- `ADD_TO_WATCHLIST`
  Used when capital, confidence, or score are insufficient.

## Approved Rank-Aware Policy

The broader allocation engine can annotate approved rank-aware policy metadata, but it does not directly rewrite live suggested percentage semantics.

Key point:

- Approved policy adds advisory metadata such as `rank_multiplier` and `rank_aware_suggested_pct`.
- Baseline sizing output remains explicit and inspectable.

## Structural Caps (config.json:growth_mode)

The two structural caps below are independent of the sizing gauges above. They
live in `config.json:growth_mode` and are enforced by `adjustment.py` and
`guardrails.py`, which emit SELL recommendations when a holding breaches them.

- `concentration_cap = 0.60` — max share of portfolio in a single position
- `leverage_cap = 0.25` — max share of portfolio in leveraged exposure

Pre-retune baseline (2026-05-18): `concentration_cap = 0.40`, `leverage_cap = 0.15`.
These were widened operator-approved for profit maximization. The risk delta
advisor (`portfolio_automation/risk_delta_advisor.py`) compares live exposure
against these caps every run and writes `outputs/latest/risk_delta.{json,md}`.

## Invariants

- Allocation is advisory-only everywhere in this repository.
- Base sizing must remain explainable from score/confidence/regime inputs.
- Caps must remain explicit in output fields such as `allocation_capped` and `allocation_cap_reason`.
- Degraded mode should reduce exposure or increase warnings, never invent certainty.
