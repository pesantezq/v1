# Operator / Research Surfaces (Phase 13)

Status: **operator surface shipped**; GUI panels = documented follow-up.

## Operator surface (`daily_run_status`)
The operator status artifact now carries the whole SQG program at a glance, each
with **graceful degrade** (`present=False` when absent — no crash):
- `run_manifest` — run_id / status / complete (Phase 1)
- `input_snapshot` — snapshot_hash / valid / stale / missing / future_rejected (Phase 2)
- `sqg_surfaces.scenario_risk` — worst_case_scenario / degraded (Phase 11)
- `sqg_surfaces.quant_feedback` — evidence_status / fallback_rate (Phase 5)
- `sqg_surfaces.semantic_liveness` — overall_status / finding_count (Phase 6)

These ride the existing daily-run-status consumer (GUI System tab + the
`/daily-tool-analysis` skill already read this artifact), so the new producers
are operator-visible immediately without new GUI templates.

## Research surface
The research artifacts (`daily_input_snapshot`, `experiment_registry`,
`strategy_mandates`, `quant_feedback`, `scenario_risk`, the sim bundle's
`input_snapshot_hash`) are all on disk under sandbox/latest and clearly labeled
research/observe-only with `is_forecast=False` / `observe_only=true` envelopes.

## Follow-up (not blocking)
Dedicated `gui_v2` panels (investor / research / operator cockpit cards) for the
new artifacts. gui_v2's `_read_json` + `card()` already degrade gracefully on
absent artifacts, so adding panels is additive template work — deferred so the
functional + verification phases land first.

## Tests
`tests/test_daily_input_snapshot.py::test_daily_run_status_sqg_surfaces_block` —
the operator surface exposes all three with graceful degrade.
