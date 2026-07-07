# Design — Glide-in Excess Cash + Weekly Deployment Pacing

**Date:** 2026-07-07
**Status:** Approved (design), pending spec review → implementation plan
**Author:** Claude Code (operator-directed)
**Scope:** Advisory capital-sizing only. Observe-only. No execution, no broker writes,
`decision_engine.py` and the six protected scores untouched.

## Problem

The daily memo tells the operator *which* names to buy but, on a day like 2026-07-07,
shows **no per-stock dollar amounts** — every opportunity renders
`DEFERRED_BY_MONTHLY_BUDGET`. Root cause: the monthly-capital-envelope layer
(`portfolio_automation/cash_deployment_plan.py`) defines each cycle's deployable budget
as the **monthly contribution only** ($1,000), ignoring accumulated cash. With
~$3,151 cash on hand (≈$2,627 deployable above the 5% reserve), the $1,000/month pace
never draws down the idle pile:

```
net_investable = max(0, monthly_contribution_gross - reserve_shortfall)   # excludes excess cash
```

and the envelope's own rule states:

> `no_rollover: undeployed net-investable is not carried forward; it remains cash and
> contributes to next cycle's excess.`

So idle cash accumulates indefinitely and the operator is never told how much of it to
deploy per name.

## Goals

1. Put idle cash to work **gradually** — deploy the monthly contribution **plus a capped
   slice of excess cash** each cycle, without abandoning dollar-cost-averaging discipline.
2. **Pace deployment weekly** (default) so buys aren't lump-summed or over-fragmented.
3. Always show the operator **per-stock buy amounts** for the cash available right now.
4. Always protect the cash reserve floor.

## Non-goals

- No trade execution, order placement, broker writes, or money movement (advisory only).
- No change to `decision_engine.py`, scoring, ranking, or the six protected scores.
- No change to how opportunities are *ranked* — only how much capital each funded name gets.

## Design

### A. Budget math — "monthly + glide excess in"

Changed inside `compute_monthly_envelope` (and the mirrored inline calc in
`run_cash_deployment_plan`). Inputs are the existing live values (Schwab-resolved
`portfolio_value` PV, `cash_on_hand` C, `config.portfolio.monthly_contribution` M,
`config.portfolio.target_cash_weight` r):

```
reserve_target    = round(r * PV, 2)                              # protected floor, e.g. $524
reserve_shortfall = max(0, reserve_target - C)                    # e.g. $0
deployable_cash   = max(0, C - reserve_target)                    # all touchable cash, e.g. $2,627
idle_excess       = max(0, deployable_cash - M)                   # dry powder beyond this month, e.g. $1,627
glide_slice       = round(idle_excess * glide_fraction, 2)        # 0.25 -> ~$407
cycle_net_investable = max(0, M - reserve_shortfall) + glide_slice  # e.g. $1,407
```

Rationale for subtracting `M` when computing `idle_excess`: the monthly contribution is
already part of `cash_on_hand`; subtracting it prevents double-counting so the added
slice is genuinely *excess* dry powder. At `glide_fraction = 0.25` the idle pile draws
down over ~4 cycles. `glide_fraction = 0` reproduces today's contribution-only behavior
exactly (back-compat guarantee).

New envelope fields (additive; existing fields preserved):
- `deployable_cash`, `idle_excess`, `excess_glide_fraction`, `glide_slice`
- `monthly_contribution_net_investable_base` = `max(0, M - reserve_shortfall)` (contribution-only, for transparency/back-compat)
- `monthly_contribution_net_investable` = `cycle_net_investable` (now includes glide) — the value downstream sizing consumes

### B. Weekly pacing

The contribution cycle is monthly; deployment is paced across the ISO weeks spanning it,
self-correcting so a skipped week's budget rolls into the remaining weeks of the cycle:

```
weeks_remaining_in_cycle = ISO weeks from today .. cycle_end (>= 1)
deployed_this_cycle      = sum of prior funded capital this cycle (existing ledger)
deployed_this_week       = sum of funded capital dated within the current ISO week (derived from ledger)
weekly_tranche           = round((cycle_net_investable - deployed_this_cycle) / weeks_remaining_in_cycle, 2)
weekly_remaining         = max(0, weekly_tranche - deployed_this_week)
```

- `deployed_this_week` is **derived** by filtering the existing append-only cycle ledger to
  the current ISO week — **no new state file**.
- Sizing funds ranked names up to `weekly_remaining`; a name that fits the cycle budget but
  exceeds the week's tranche gets the new precise status `DEFERRED_BY_WEEKLY_PACING`
  (distinct from `DEFERRED_BY_MONTHLY_BUDGET`, which means the whole cycle budget is spent).
- Funding may occur **any day** up to `weekly_remaining`, preserving the existing
  `entry_extended` / `held_for_pullback` logic that waits for a better entry.

`config.portfolio.deploy_cadence`:
- `weekly` (default): budget = `weekly_tranche`.
- `monthly`: whole `cycle_net_investable` available any day (glide, no weekly sub-cap).
- `daily`: `weekly_tranche` further divided by trading days remaining in the week.

### C. Memo rendering (`watchlist_scanner/daily_memo.py`)

Funded actions already render `$X` + `% of portfolio` + `% of net investable` via
`_funded_action_line_text` / `_funded_action_line_md`. With a positive weekly tranche,
funded names reappear with amounts. Add a compact **Weekly Deployment** block (text + md)
ahead of Funded Actions:

```
## Weekly Deployment
- This week: $350 tranche · $0 deployed · $350 available
- Cycle: $1,407 net-investable ($1,000 contribution + $407 glide) · reserve $524 protected
```

The Deferred/Blocked section distinguishes the two statuses and keeps the
`...and N more` overflow line (shipped 2026-07-07, commit c678dd4f).

### D. Config + safety

`config.json` `portfolio` block — two additive keys with defaults:
- `excess_cash_glide_fraction`: `0.25`
- `deploy_cadence`: `"weekly"`

Ships enabled (prod-ready observe-only advisory). Absent keys default to the above.
`excess_cash_glide_fraction = 0` = exact legacy behavior. All artifacts retain
`observe_only: true` / `no_trade: true`.

### E. Tests + monitoring

Unit tests (`tests/test_cash_deployment_plan.py` or the existing envelope test module):
- glide math: `idle_excess`, `glide_slice`, `cycle_net_investable` on a known PV/cash/M/r tuple
- `glide_fraction = 0` reproduces contribution-only `net_investable` (back-compat)
- reserve protection: `reserve_shortfall > 0` reduces the base before glide is added
- weekly tranche math + `weekly_remaining` decrement as `deployed_this_week` grows
- deferral-status split: cycle-exhausted → `DEFERRED_BY_MONTHLY_BUDGET`; week-exhausted but
  cycle-available → `DEFERRED_BY_WEEKLY_PACING`
- `deploy_cadence` = monthly/daily variants size correctly
- memo renders the Weekly Deployment block and per-stock funded amounts

Monitoring (Analysis + Health Coverage Requirement — daily cadence):
- Extend `daily-tool-analysis` memo-coherence / funding heartbeat (`6j`) + the daily-check
  runner funding line to surface `weekly_tranche`, `glide_slice`, and the
  `DEFERRED_BY_WEEKLY_PACING` count, so a stuck/zero glide is observable.

## Affected modules / artifacts

- `portfolio_automation/cash_deployment_plan.py` — `compute_monthly_envelope`,
  `run_cash_deployment_plan` (inline net_investable), `allocate_within_envelope`,
  `rank_deployable_decisions` (weekly-remaining plumb-through), new `DEFERRED_BY_WEEKLY_PACING`.
- `watchlist_scanner/daily_memo.py` — `_investor_core_text` / `_investor_core_md`
  (Weekly Deployment block), deferral-status labels.
- `portfolio_automation/memo_coherence.py` — funding block surfaces weekly tranche.
- `.claude/commands/daily-tool-analysis.md` + `portfolio_automation/daily_check_runner.py`
  — funding heartbeat line.
- `config.json` — two new `portfolio` keys.
- Artifacts: `outputs/latest/cash_deployment_plan.json` (new envelope fields),
  `daily_memo.md/.txt`, `memo_coherence.json` (all `OutputNamespace.LATEST`).

## Risks

- **Behavioral change to the advisory capital plan** (deploys more than $1,000/cycle). Mitigated:
  advisory-only, reserve always protected, `glide_fraction` tunable, `0` = legacy behavior.
- Weekly-tranche derivation depends on the cycle ledger being complete; if
  `monthly_history_status == "unavailable"`, fall back to cycle-level budget (no weekly sub-cap)
  and note it — never over-deploy on missing history.
- Double-count guard (`idle_excess = deployable_cash - M`) must be covered by a test.

## Backward compatibility

Additive fields only; existing consumers unaffected. `glide_fraction = 0` +
`deploy_cadence = "monthly"` = current behavior. New deferral status is additive.
