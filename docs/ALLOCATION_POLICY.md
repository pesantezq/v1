# Allocation Policy

Last verified against `watchlist_scanner/conviction.py`, `watchlist_scanner/portfolio_construction.py`, `allocation_engine.py`, and `portfolio_decision_engine.py`.

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

Defaults:

- `baseline_position_pct = 0.02`
- `max_total_allocation = 0.10`
- `max_ticker_allocation = 0.02`
- `max_sector_allocation = 0.04`

Suggested allocation:

- `baseline_position_pct * sizing_multiplier`

Normalized allocation:

- starts as suggested allocation
- scales down if total suggested exceeds `max_total_allocation`
- then applies per-ticker cap
- then applies per-sector cap

### Concentration warnings

Warnings are emitted when:

- top sector exceeds `0.40`
- top 3 tickers exceed `0.70`
- too many `high_conviction` names share a theme
- degraded mode still carries positive normalized exposure

### Degraded mode behavior

- conviction can be capped by `degraded_mode_cap`
- degraded mode adds a conviction penalty
- portfolio construction adds `degraded_mode_exposure_risk` warning when exposure remains non-zero

## Broader-Market Portfolio Action Sizing

### Base sizing

`allocation_engine.py:suggest_allocation`

Defaults:

- `compounder_base_pct = 0.05`
- `momentum_base_pct = 0.03`

### Confidence multipliers

- `high >= 0.75 -> 1.00`
- `medium >= 0.60 -> 0.75`
- `low -> 0.50`

### Risk-off adjustments

When regime is `risk_off`, `significant_dip`, or `severe_dip`:

- compounder multiplier: `0.85`
- momentum multiplier: `0.55`

### Degraded-mode adjustment

- degraded penalty multiplier: `0.65`

### Caps

- `max_position_cap = 0.08`
- `sector_cap = 0.20`
- `cash_reserve_pct = 0.05`
- `min_position_pct = 0.01`

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

## Invariants

- Allocation is advisory-only everywhere in this repository.
- Base sizing must remain explainable from score/confidence/regime inputs.
- Caps must remain explicit in output fields such as `allocation_capped` and `allocation_cap_reason`.
- Degraded mode should reduce exposure or increase warnings, never invent certainty.
