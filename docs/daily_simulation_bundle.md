# Simulation Governance — Daily Simulation Bundle

## Purpose

`portfolio_automation/sim_governance/daily_simulation_bundle.py` consolidates the
active simulation lane's output into ONE evidence bundle. The bundle is the
single source the AI/product review reads: it carries the before/after
comparison against the production baseline plus aggregate risk, confidence, and
data-quality summaries so the downstream review packet can be compressed without
losing the decision-relevant signal.

---

## Two-Lane Governance

Read/aggregate-only over the active simulation lane's result. It writes to the
SIMULATION namespace only and never touches production. The bundle explicitly
asserts `lane_active: true` and embeds a compact unified-crowd summary as
*context* for the single daily review — no extra AI call, and never feeding the
production decision engine. Production changes require human approval.

---

## Artifacts Written (OutputNamespace.SIMULATION → `outputs/simulation/`)

| File | Contents |
|------|----------|
| `daily_simulation_bundle.json` | The consolidated evidence bundle (schema `daily_simulation_bundle.v1`) |

Bundle fields include: `candidate_count`, `ready_count`,
`advisory_experiment_results`, `watchlist_experiment_results`,
`crowd_experiment_results`, `discovery_candidates`,
`comparison_vs_production_baseline` (watchlist added/removed + advisory counts),
`unified_crowd_summary`, `data_quality` / `risk_summary` buckets,
`confidence_summary` (avg/min/max), and `artifact_refs`.

---

## Key Functions

- `build_daily_simulation_bundle(lane_result, now, *, base_dir,
  write_files=True) -> dict` — splits candidates by workflow/category, computes
  the production-baseline diff and aggregate summaries, and writes the bundle.
  Write failures are logged and recorded in `bundle["write_error"]`.
- `_summarize_unified_crowd(base_dir) -> dict` — compact, capped read of
  `outputs/latest/unified_crowd_intelligence_status.json` (ticker lists only) so
  it adds negligible tokens to the one consolidated review.

---

## Tests

Covered under `tests/` with the sim-governance suite
(`python -m pytest -q tests -k sim_governance`).
