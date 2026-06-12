# Strategy + Tax-Aware Hardening — Consume Live Schwab Data

Date: 2026-06-12
Status: Approved (brainstorm) — pending spec review → implementation plan
Owner: Enrique Pesantez
Depends on: live Schwab read-only sync (Stage 10c) + re-auth auto-capture (shipped 2026-06-12)
Roadmap: owner-agreed post-activation step 4 ("strategy/tax-aware hardening"); `next_official_step` stays `observe_and_iterate` (GPT owns formal advancement).

## Goal

Now that Schwab read-only sync is live (real positions + aggregate cost basis), lift
the tax + strategy advisory layer out of `config`/`degraded` mode so it reflects the
operator's ACTUAL holdings. Pull per-lot tax data in where the broker exposes it so
holding-period and wash-sale advice can go fully live; degrade honestly where it
cannot. Fully additive, observe-only, never feeds `decision_plan.json`, never touches
scoring or the decision core.

## Build boundary ("build everything until we need to simulate")

Build all deterministic components end-to-end. The only places we stop and leave an
explicit gated/degraded output (never a fabricated value) are where a result genuinely
needs evidence that does not exist yet:
- strategy efficacy that prefers sandbox/backtest evidence over heuristic estimates
  (matures with the existing shadow/backtest layer);
- tax fields that require data the broker does not return (see degradation contract).
These follow the same honest-gating pattern as the pattern-loop OOS-maturity countdown.

## The lynchpin

`holdings_resolver.resolve_holdings` gates on `config.json portfolio.broker_aware.enabled`
(`_broker_aware_enabled`, currently absent → False), so it falls back to `config`
(`holdings_source: config`, `degraded_mode: true`). Setting `enabled: true` (the
owner-approved §23.10 wiring step) makes the resolver consume `schwab_positions.json`
→ `holdings_source: "broker"`, which cascades to strategy (broker context) and tax
(real cost basis). The resolver's existing fallbacks (`broker_data_missing`,
`broker_data_stale`, `broker_no_positions` → `config` with a `confidence_modifier`)
stay intact — a stale/missing Schwab snapshot can never silently drive advice.

Per operator policy (2026-06-12): a prod-ready observe-only feature ships ENABLED, not
inert. This flip is applied once the work is built + tested + green.

## Components / changes

1. **`config.json`** — `portfolio.broker_aware: {enabled: true}` (+ optional
   `freshness_max_age_s` if the resolver doesn't already default one). Observe-only,
   reversible. Applied at the end, after the feature is green.

2. **`portfolio_automation/holdings_resolver.py`** — carry **cost basis through** the
   resolved broker holdings: each holding gains `average_cost` and derived
   `cost_basis` (= `quantity × average_cost`) sourced from `schwab_positions.json`, so
   downstream tax modules read one source of truth instead of re-reading the positions
   artifact. `holdings_source`/staleness logic unchanged.

3. **`portfolio_automation/brokers/` (per-lot tax data — NEW, defensive)** — add a
   READ-ONLY client method + normalizer that consumes Schwab tax-lot / acquisition-date
   detail **if the API returns it** (e.g. lot-level cost basis when present in the
   accounts/positions payload or an available read-only lots field). It is AST-safe
   (no trade verbs; covered by the existing
   `test_no_trading_capability_anywhere_in_brokers_package`). If lot data is absent or
   the field is unavailable, the layer emits an explicit "no per-lot data" marker and
   everything downstream degrades honestly. Writes `schwab_tax_lots.json`
   (observe_only, no_trade) when data is present.

4. **`portfolio_automation/strategy/tax_scorecard.py`** — compute **unrealized
   gain/loss** per holding + portfolio aggregate from broker cost basis → exit
   `degraded_mode` for those fields. When per-lot data (component 3) is present,
   compute the **LTCG/STCG split** (holding period from lot acquisition dates) and a
   **wash-sale window flag**. When per-lot data is absent, those specific fields stay
   in an explicit `degraded_fields` list with reason "aggregate cost basis only, no
   per-lot acquisition dates" — never a coarse guess.

5. **`portfolio_automation/tax_harvest_advisor.py`** — source cost basis from the
   resolved broker holdings (`average_cost`) when broker-aware, else config entry
   prices; keep the `is_taxable_account` gate and the 30-day wash-sale replacement
   note; expose `basis_source: broker|config`. When per-lot data is present, refine
   harvest candidates by lot (harvest only loss lots) instead of the aggregate.

6. **`portfolio_automation/strategy/strategy_comparator.py`** — becomes broker-aware
   automatically once the resolver returns `holdings_source: broker` (it already
   branches on this). Verify the 8-profile comparison scores against real holdings;
   add cost-basis-aware metrics to the scorecard join only where cleanly additive (no
   objective-function math changes).

7. **Memo surfacing** (`watchlist_scanner/daily_memo.py` — NOT `gui_v2`): one compact
   tax/strategy line within the memo contract, e.g.
   `"Tax: $X unrealized G/L · N harvest candidates (basis: broker) · Strategy: closest profile <name> (broker-aware)"`,
   with degraded wording when a field is degraded. Respects the max-sections contract.

## Data flow

```
config.broker_aware.enabled=true
  → resolve_holdings reads schwab_positions.json (fresh; daily Stage 10c)
      → holdings_source: "broker" + average_cost/cost_basis carried per holding
  → (optional) schwab tax-lot fetch → schwab_tax_lots.json (if API returns lots)
  → tax_scorecard: unrealized G/L (live); LTCG/STCG + wash-sale (live IF lots, else degraded_fields)
  → tax_harvest_advisor: broker-basis harvest candidates (lot-refined IF lots)
  → strategy_comparator: 8-profile comparison in broker context
  → daily_memo: compact tax/strategy line
```

## Safety / invariants

- Observe-only throughout: every artifact keeps `observe_only: true` / `no_trade: true`;
  `broker_aware_portfolio` stays `side_panel_only`, `feeds_decision_plan: false`.
- No scoring / `decision_engine.py` / decision-core change; nothing writes
  `decision_plan.json`.
- Resolver staleness/missing fallback to config unchanged — stale Schwab data degrades,
  never silently drives tax/strategy advice.
- Honest degradation: tax fields lacking source data are listed in `degraded_fields`
  with a reason; never guessed.
- No trade capability added to `brokers/` (AST test enforces).

## Health coverage (CLAUDE.md Analysis + Health Coverage requirement)

These producers run daily → extend `.claude/commands/daily-tool-analysis.md`
(quant + market-expert lens). New artifacts read + signals:
- `strategy_tax_scorecard.json` → `degraded_mode`, `degraded_fields`, `basis_source`.
- `tax_harvest_advisor.json` → `basis_source`, candidate count.
- `strategy_comparison.json` → `context_source`.
- `schwab_tax_lots.json` (if present) → lot coverage.
- AMBER `tax_scorecard_unexpectedly_degraded` = broker-aware enabled AND
  `broker_sync_status.overall_status == ok` AND scorecard still degraded for
  unrealized G/L (data plumbing broke).
- AMBER `strategy_context_not_broker` = broker-aware enabled AND Schwab `ok` AND
  `strategy_comparison.context_source == config` (resolver not flowing through).
- Both never RED (observe-only/advisory).

## Testing

- `holdings_resolver`: cost basis carried into broker holdings; staleness/missing
  fallback to config intact; config path unchanged.
- per-lot layer: lots normalized when present; explicit no-lot marker when absent;
  AST no-trade test still passes.
- `tax_scorecard`: unrealized G/L computed from broker basis (exits degraded);
  LTCG/STCG + wash-sale computed when lots present; `degraded_fields` populated with
  reason when lots absent.
- `tax_harvest_advisor`: `basis_source` switches broker↔config; lot-refined candidates
  when lots present; `is_taxable_account` gate respected.
- `strategy_comparator`: `context_source: broker` when resolver returns broker.
- `daily_memo`: tax/strategy line renders, incl. degraded wording.
- daily-tool-analysis signals: both healthy and degraded fixture states asserted.
- Targeted first, then full `pytest -q` (preserve `config/signal_registry.yaml`
  `default_weight: 0.4947`).

## GUI panel (IN scope — operator approved 2026-06-12 "everything in scope")

8. **GUI tax/strategy panel** (`gui_v2`). Added defensively to dodge the live
   concurrent-session collision: a NEW isolated data loader
   (`gui_v2/data/dash_strategy_tax.py`) + a NEW template
   (`gui_v2/templates/dashboard/strategy_tax.html`), with only minimal shared-file
   touches (one route registration in `gui_v2/app.py`, one nav link in
   `gui_v2/templates/base.html`). Read-only: renders `strategy_tax_scorecard.json`,
   `tax_harvest_advisor.json`, `strategy_comparison.json`, `schwab_tax_lots.json`
   (degraded states shown honestly). Implemented as the LAST task; if a concurrent
   session has touched `app.py`/`base.html` at execution time, rebase the two shared
   lines (the new files never conflict). Requires a dashboard restart to serve
   (operator step, per [[project_dashboard_service_no_autoreload]]).

## Deferred (NOT in this spec)

- New strategy profiles or objective-function math changes (YAGNI).
- Strategy efficacy that requires backtest/shadow maturity beyond what exists — those
  fields prefer existing sandbox evidence and mature on their own cadence.

## Rollback

Config flip is a one-line revert (`broker_aware.enabled=false` → resolver returns to
config; all downstream auto-degrades). Module changes are additive; revert the commits.
No data migration; the manual/config path remains the always-available fallback.
