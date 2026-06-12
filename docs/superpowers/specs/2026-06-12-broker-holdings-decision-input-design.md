# Part B — Broker Holdings as the Decision-Core Input (overlay-at-boundary)

Date: 2026-06-12
Status: Approved (brainstorm) — pending spec review → implementation plan
Owner: Enrique Pesantez (explicitly authorized the §23.10 broker→decision_plan crossing via "part b")
Depends on: Part A (config synced to Schwab + reconciliation fix, shipped 9b59550a); live Schwab read-only sync.
Related: [[project_schwab_source_of_truth]], [[project_strategy_tax_hardening]], §23.10.

## Goal

Make the live Schwab snapshot the source of truth for **actual holdings** in the
decision core, automatically — so the daily pipeline reasons about the operator's real
positions (shares + cash) without a manual `config.json` sync. Achieved by overlaying
broker holdings onto the `portfolio_context` at the single pipeline boundary, leaving
the scoring engine untouched.

## Design intent

- **holdings (shares) + cash = broker** (Schwab), when fresh.
- **target_weights / strategy = config** (Schwab does not provide these — preserved via merge).
- **Stale/missing broker → config fallback** (existing `resolve_holdings` behavior + `confidence_modifier`).

## The boundary (why this is small + safe)

Holdings enter the decision pipeline through a `portfolio_context` dict assembled at TWO
call sites, then consumed by every downstream module:
- `main.py:1569` (live daily pipeline) — `portfolio_context={'holdings': holdings, 'cash_available': config.cash_available, 'target_cash_weight': config.target_cash_weight}`.
- `watchlist_scanner/__main__.py:513` (CLI/standalone) — `portfolio_context=full_cfg.get("portfolio")`.

Downstream consumers (`decision_engine.py`, `watchlist_scanner/portfolio_fit.py`,
`postprocess.py`, `memo_enrichment.py`) read *that context*, not config directly.
Therefore the change is injected at the boundary; **`decision_engine.py` and all scoring
math are NOT edited.**

## Components / changes

1. **`portfolio_automation/holdings_resolver.py` — new `broker_overlaid_portfolio(portfolio_block, root, now=None)`:**
   Pure-ish function returning a COPY of the config `portfolio` block with `holdings`
   (shares) + `cash_available` overlaid from `resolve_holdings(root)` when
   `holdings_source == "broker"`; otherwise returns the block unchanged (config fallback).
   - Merge rule (broker path): for each broker position set `shares`; PRESERVE existing
     config per-symbol metadata (`target_weight`, `asset_class`, `is_leveraged`,
     `leverage_factor`) for matched symbols; add broker-only symbols with safe defaults
     (`target_weight: 0`, `asset_class: "us_equity"`, `is_leveraged: False`,
     `leverage_factor: 1`); keep config-only 0-share target entries (e.g. VFH/VXUS).
   - Set `cash_available` to the broker snapshot cash.
   - Stamp an observe field on the returned block: `holdings_source` ∈ {broker, config}
     + `confidence_modifier` (carried from the resolver), so downstream/telemetry can see
     which source drove the run. RUNTIME ONLY — never writes `config.json`.
   - Never raises; on any error returns the original block (config fallback).

2. **`main.py` (~1569)** — build the live-pipeline `portfolio_context` from
   `broker_overlaid_portfolio(...)` instead of raw config holdings/cash. (Gate: only
   overlays when `portfolio.broker_aware.enabled` is true — the existing resolver flag;
   off → unchanged config behavior.)

3. **`watchlist_scanner/__main__.py` (~513)** — same: pass
   `broker_overlaid_portfolio(full_cfg.get("portfolio"), root)` as `portfolio_context`.

4. **Telemetry (observe-only):** record the run's `holdings_source` +
   `confidence_modifier` to a small artifact / existing decision telemetry so the daily
   health check can see whether a given decision run used broker or config holdings.
   (Wire into the existing `decision_plan_portfolio_context` already surfaced at
   `main.py:242` if it carries through; otherwise a one-line addition.)

## Invariants / safety

- **Scoring untouched:** `decision_engine.py`, `signal_score`/`conviction_score`/etc
  semantics, and recommendation logic are NOT modified. Only the holdings INPUT to the
  existing `portfolio_context` changes.
- **No auto-config-write:** runtime overlay only; `config.json` is mutated solely by the
  Part-A `tools.manual_portfolio_update` safe writer. The Observe-Only / no-auto-write
  invariant for config holds.
- **§23.10 crossing (owner-approved):** broker data now feeds `decision_plan` via the
  overlaid context. This is the explicit owner-approved wiring step; gated behind
  `portfolio.broker_aware.enabled` (already true) so it's reversible via that flag.
- **Staleness safety:** stale/missing/`broker_no_positions` → config fallback with a
  lowered `confidence_modifier`; a stale Schwab snapshot can never silently drive
  decisions on wrong holdings.
- **No trades:** read-only throughout.

## Testing

- `broker_overlaid_portfolio`: broker-fresh overlays shares+cash & preserves
  target_weight metadata; config-only 0-share targets retained; broker-only symbol added
  with defaults; stale/missing → original config block returned unchanged; never raises
  on bad input.
- Boundary integration: with `broker_aware.enabled=true` + fresh Schwab fixture, the
  `portfolio_context` passed into `run()` carries broker shares; with stale fixture it
  carries config; with `broker_aware.enabled=false` it is unchanged config.
- Regression: existing `decision_engine` / `portfolio_fit` / `postprocess` tests stay
  green (they receive the same context shape). Full `pytest -q`.
- Preserve `config/signal_registry.yaml default_weight: 0.4947`.

## Health coverage

Daily cadence → extend `.claude/commands/daily-tool-analysis.md`: surface the decision
run's `holdings_source` (broker|config) + `confidence_modifier`. AMBER
`decision_on_config_while_broker_ok` = `broker_aware` enabled AND
`broker_sync_status.overall_status == ok` AND the run used `holdings_source: config`
(broker live but decisions fell back to config — investigate freshness). Never RED.

## Out of scope

- Editing `decision_engine.py` scoring/recommendation logic.
- Auto-writing `config.json` from the overlay (rejected — keeps the safe-writer as sole
  config mutator).
- Target_weight strategy changes (strategy stays config-owned).
- Other observe-only consumers (risk advisor, GUI) — already covered by Part A / the
  side-panel.

## Rollback

Flip `portfolio.broker_aware.enabled=false` → overlay no-ops, pipeline reverts to config
holdings instantly. Code is additive (one helper + two call-site lines); revert commits
otherwise. No data migration.
