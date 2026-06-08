# artifact_registry

Single machine-readable contract for every tracked artifact + an observe-only
validator. The governance layer that turns ~52 artifacts into one governed corpus.

## Files
- `portfolio_automation/artifact_registry.yaml` — the contract (one row per artifact).
- `portfolio_automation/artifact_registry.py` — loader, `required_artifacts()`,
  `validate_registry()`, `run_artifact_registry()` (never raises).
- `outputs/latest/artifact_registry_status.json` — observe-only governance snapshot.

## Row schema
`lens` (developer | quant_learning | market_discovery | risk_action | decision_core |
meta_governance) · `role` (source_of_truth | advisor | probe | telemetry | narrative) ·
`required` · `cadence` (daily | weekend | weekly | monthly | yearly | on_demand) ·
`producer` · `consumers` (or `[UNATTRIBUTED]`) · `severity_if_missing`
(critical | warning | info) · optional `staleness_hours_override` / `notes`.

## Staleness
Cadence-derived: daily 30h · weekend 100h · weekly 192h · monthly 768h · yearly 9000h ·
on_demand never. Per-row override available. (This is why weekly/monthly artifacts
don't false-alarm at a flat 30h.)

## Severity → status
Any critical missing/stale → red · any warning → amber · else green.

## The invariant
Only `role: source_of_truth` artifacts represent official portfolio actions.
probe/advisor/telemetry/narrative artifacts inform analysis, warnings, explanations,
and confidence — they never independently create or override a buy/sell/hold.

## Single source of truth
`daily_run_status.py` reads `required_artifacts()` (no hardcoded list); the daily
skill reads `artifact_registry_status.json` first and gates confidence by `role`.

Observe-only: mutates only its status artifact; never decision/score/allocation state.
