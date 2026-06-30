# Experiment Registry & Research Integrity (Phase 8)

Status: **shipped** on `feat/complete-simulation-quant-governance-loop`.
Observe-only. Records research; never mutates production/scores/weights;
promotion stays human-gated (Phase 10).

The biggest greenfield gap in the program: before this, walk-forward results and
leaderboards were scattered with no durable hypothesis -> commit -> windows ->
result audit trail. `experiment_registry.py` provides it.

## Record (`new_experiment`)
`experiment_id, hypothesis, rationale, owner, source_commit, data_sources,
pit_policy, discovery_window, calibration_window, validation_window, oos_window,
benchmark, cost_assumptions, variants_tested, selected_variant, status, result,
failure_conditions, promotion_state, supersedes, created_at, updated_at`.

Statuses: `proposed · running · inconclusive · rejected · validated · promoted ·
degraded · retired · superseded`.

## Two guarantees
1. **Failures are retained.** `update_experiment` advances status/result in
   place; a rejected/inconclusive/degraded experiment is never deleted, so
   negative results stay visible.
2. **In-sample discovery can't masquerade as OOS validation.**
   `validate_research_controls` flags:
   - `discovery_validation_overlap` (windows not disjoint) → not validatable,
   - `pit_unsupported_dataset` (dataset can't provide point-in-time) →
     `recommended_status: degraded`,
   - `multiple_testing_risk` (≥10 variants),
   - `insufficient_oos_sample` (OOS n < 30).

## Persistence
`outputs/sandbox/experiment_registry.json` (durable; idempotent register;
in-place update retains history).

## Consumed by
- **Phase 10** governance proposals link to `source_experiment_ids` and read
  OOS/validation status from here.
- **Phase 12** monthly review audits the registry (failed-experiment review,
  multiple-testing review, promotion eligibility).

## Tests
`tests/test_experiment_registry.py` (10) — schema/statuses, window-disjointness,
PIT-degrade, multiple-testing + sample-size disclosure, retain-on-reject,
idempotent register, supersession links.
