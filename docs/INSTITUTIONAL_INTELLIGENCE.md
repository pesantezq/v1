# Institutional Intelligence (SEC 13F)

**Package:** `portfolio_automation/institutional_intelligence/`
**Pipeline stage:** `scripts/run_daily_safe.sh` Stage 7f2 (before the daily input snapshot)
**Status:** observe-only, sandbox-first, production human-gated. Ships **inert**.

Extracts point-in-time-safe signals from large institutional-manager 13F
disclosures and tests bounded institutional strategies inside the Strategy Lab
and simulation-governance architecture. It does **not** blindly copy hedge
funds — it weighs conviction, independence, cloneability, freshness, strategy
fit, crowding, and options ambiguity.

## Hard invariants (enforced in code + tests + health)

- Never writes `outputs/latest/decision_plan.json`; never changes the six
  protected scores; never mutates production allocations, brokerage state, or
  production watchlist state.
- `feeds_decision_engine` is always `false` on every artifact.
- **No look-ahead:** a signal is available no earlier than the NEXT market
  session after the filing's public `filed_at` — never the quarter-end. Amendments
  are invisible before they are filed.
- **Options are never auto-directional:** puts are not bearish, calls not bullish;
  an option's directional contribution is always 0; a possible hedge is labeled
  `sector_beta_hedge_possible` (an inference, never a confirmed short).
- **Never guesses a ticker:** FIGI → CUSIP → app map → unambiguous issuer →
  unresolved (with an explicit reason).
- No raw network outside the governed SEC client (AST-enforced); the SEC
  User-Agent (a contact value) is never persisted to any artifact/ledger.
- XML parsed with `defusedxml` (XXE / billion-laughs hardened).

## Modules

| Module | Responsibility |
|---|---|
| `schemas.py` | Registry + filing/holding contracts, strict validation, vocabularies |
| `manager_registry.py` | Load + validate the version-controlled, PIT-versionable manager registry |
| `sec_client.py` | Governed EDGAR client (env UA, rate limit, cache, ledger, fixtures, backoff) |
| `filing_discovery.py` | Submissions → 13F FilingRefs (PIT filed_at; notice ≠ holdings) |
| `filing_parser.py` | Defensive, namespace-agnostic information-table XML parser |
| `holdings_store.py` | Point-in-time SQLite store; amendments supersede; anti-look-ahead queries |
| `security_identity.py` | Deterministic FIGI/CUSIP/issuer resolution; unresolved reasons |
| `position_changes.py` | New/inc/unchanged/reduced/exit (+ option) events; split-aware |
| `options_interpretation.py` | Options taxonomy; directional contribution always 0 |
| `manager_scoring.py` | Pure signed score = direction × 8 components × penalties |
| `crowding.py` | Dual-natured crowding score (consensus AND reversal risk) |
| `consensus.py` | Independence-adjusted symbol consensus; crowded ≠ more bullish |
| `evidence_alignment.py` | Additive retail/market/institutional agreement layer |
| `institutional_backtest.py` | Point-in-time forward-return backtest + attribution |
| `sim_candidates.py` | Governed sim-lane candidates; stable dedup IDs |
| `institutional_memo.py` | Compact, honest memo section (material-only) |
| `health.py` | Health assessor + semantic-liveness detectors |
| `context_loader.py` | Orchestrator (`run_institutional_intelligence`) |
| `../portfolio_sim/institutional_tilt.py` | InstitutionalTactic + Strategy Lab variants |

## Artifacts

`outputs/latest/institutional_intelligence.json` (+ `_status.json`,
`institutional_consensus.json`) — all with the invariant envelope
(`observe_only`, `no_trade`, `simulation_active`, `production_gated`,
`feeds_decision_engine: false`, `sandbox_only`, `source_limitations`). Status ∈
`ok | degraded | insufficient_data | stale | failed | disabled`; a valid
no-new-filings run is `insufficient_data`, never `failed`.

## Configuration (`config/base.json:institutional_intelligence`)

Ships with `enabled: true` but `live_sec_ingestion_enabled: false`, and every
seed manager is `enabled: false` / `cik_verified: false`, so the subsystem is
inert. Strategy caps: total sleeve 10%, per-position 2%, distribution trim 2%,
min consensus confidence 0.55, min effective managers 1.5.

## Activating live SEC ingestion (operator runbook)

1. Set `SEC_EDGAR_USER_AGENT` env to a descriptive contact string (never in config).
2. Verify each manager's CIK against EDGAR; set `cik_verified: true` then
   `enabled: true` for the managers you want (validation refuses enabling an
   unverified CIK).
3. Set `institutional_intelligence.live_sec_ingestion_enabled: true`.
4. Run the pipeline; confirm `institutional_intelligence_status.json` and the
   health check. Strategy activation stays behind the existing simulation-only
   auto-approval bounds; production promotion stays human-gated.
