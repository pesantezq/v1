# Design — Glide-in Excess Cash + Weekly Deployment Pacing

**Date:** 2026-07-07
**Status:** Reviewed 2026-07-07 (spec review complete; contribution model resolved — see §A) → ready for implementation plan
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

**Naming convention:** in the formulas above, `glide_fraction` is shorthand for the single
canonical config key `excess_cash_glide_fraction` (§D); there is no separately-named field.

Rationale for subtracting `M` when computing `idle_excess`: the monthly contribution is
**deposited into Schwab** (operator-confirmed 2026-07-07), so it is already part of the
live `cash_on_hand` that `resolve_capital_basis` reads from `totals.cash`. Subtracting it
prevents double-counting so the added slice is genuinely *excess* dry powder. At
`excess_cash_glide_fraction = 0.25` the idle pile draws down over ~4 cycles.
`excess_cash_glide_fraction = 0` reproduces today's contribution-only *budget* — but exact
legacy *behavior* also requires `deploy_cadence = "monthly"` (see Back-compat note in §D).

**Reconcile the legacy sibling (required, not optional).** `compute_available_cash` in the
same module models the contribution as additive
(`total_deployable_pct = excess_cash_pct + incoming_pct`) — i.e. as money *not yet* in
cash, the opposite convention. Under the confirmed deposited model that double-counts `M`.
Its `cash_summary` block is still emitted in the plan, so if left unaligned its deployable
figure will disagree with the envelope. Implementation MUST align `compute_available_cash`
to the deposited model (contribution already inside `cash_available`, not added on top) and
cover the change with a test.

New envelope fields (additive; existing fields preserved):
- `deployable_cash`, `idle_excess`, `excess_cash_glide_fraction`, `glide_slice`
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

- `deployed_this_cycle` and `deployed_this_week` are both computed **inclusive of today's**
  funding (`deployed_before_today + capital_funded_today`, filtered by cycle / ISO week),
  so a same-day re-run is idempotent via the ledger's last-wins read.
- `weekly_tranche` is a **live residual, not a fixed weekly allowance**: because
  `deployed_this_cycle` already includes the current week's spend, an intra-week partial
  deploy recomputes the tranche slightly downward within that week (it re-levels at the
  start of the next ISO week and still totals `cycle_net_investable` across the cycle). This
  is deliberately conservative — it never over-deploys — but the memo MUST label the figure
  as "remaining this week", not a static "$X tranche", so the operator doesn't read the
  drift as a bug.
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
- `daily`: `weekly_tranche` further divided by the **weekday (Mon–Fri) days remaining** in
  the current ISO week, counted with the stdlib `calendar`/`date` — no market-holiday
  calendar dependency (a holiday simply leaves that day's slice undeployed and self-corrects
  into the remaining days, like a skipped week).

### C. Memo rendering (`watchlist_scanner/daily_memo.py`)

Funded actions already render `$X` + `% of portfolio` + `% of net investable` via
`_funded_action_line_text` / `_funded_action_line_md`. With a positive weekly tranche,
funded names reappear with amounts. Add a compact **Weekly Deployment** block (text + md)
ahead of Funded Actions:

```
## Weekly Deployment
- This week: $350 target · $0 deployed · $350 remaining this week
- Cycle: $1,407 net-investable ($1,000 contribution + $407 glide) · reserve $524 protected
```

The Deferred/Blocked section distinguishes the two statuses and keeps the
`...and N more` overflow line (shipped 2026-07-07, commit c678dd4f).

### D. Config + safety

`config.json` `portfolio` block — two additive keys with defaults:
- `excess_cash_glide_fraction`: `0.25`
- `deploy_cadence`: `"weekly"`

Ships enabled (prod-ready observe-only advisory). Absent keys default to the above.
**Back-compat:** exact legacy behavior requires BOTH `excess_cash_glide_fraction = 0` (no
glide) AND `deploy_cadence = "monthly"` (no weekly sub-cap). With the shipped default
`deploy_cadence = "weekly"`, a zero glide still paces the $1,000 contribution across the
cycle's ISO weeks — that is a rendering/pacing change, not a change to the total cycle
budget. All artifacts retain `observe_only: true` / `no_trade: true`.

### E. Tests + monitoring

Unit tests (`tests/test_cash_deployment_plan.py` or the existing envelope test module):
- glide math: `idle_excess`, `glide_slice`, `cycle_net_investable` on a known PV/cash/M/r tuple
- `glide_fraction = 0` reproduces contribution-only `net_investable` (back-compat)
- reserve protection: `reserve_shortfall > 0` reduces the base before glide is added
- `compute_available_cash` reconciliation: contribution treated as already-in-cash (no
  double-count) so its `total_deployable_amount` agrees with the envelope's `deployable_cash`
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
  `rank_deployable_decisions` (weekly-remaining plumb-through), new `DEFERRED_BY_WEEKLY_PACING`,
  and `compute_available_cash` (align its additive `excess + incoming` model to the confirmed
  deposited-contribution model so its `cash_summary` deployable figure matches the envelope).
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
  advisory-only, reserve always protected, `excess_cash_glide_fraction` tunable, and
  `excess_cash_glide_fraction = 0` + `deploy_cadence = "monthly"` = exact legacy behavior.
- Weekly-tranche derivation depends on the cycle ledger being complete; if
  `monthly_history_status == "unavailable"`, fall back to cycle-level budget (no weekly sub-cap)
  and note it — never over-deploy on missing history.
- Double-count guard (`idle_excess = deployable_cash - M`) must be covered by a test.

## Backward compatibility

Additive fields only; existing consumers unaffected. `excess_cash_glide_fraction = 0` +
`deploy_cadence = "monthly"` = current behavior. New deferral status is additive. The one
non-additive change is aligning `compute_available_cash` to the deposited-contribution
model (§A) — covered by a dedicated test so its `cash_summary` output stays consistent.
