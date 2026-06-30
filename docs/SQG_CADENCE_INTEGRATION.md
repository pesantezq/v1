# Cadence Integration (Phase 12)

Status: **shipped** on `feat/complete-simulation-quant-governance-loop`.

How the SQG program's producers are wired across cadences.

## Daily (`run_daily_safe.sh`) — wired as each phase landed
| Stage | Producer | Phase |
|---|---|---|
| Stage 00 | `run_manifest.begin_run` (status=running) | 1 |
| Stage 7b2 | `scenario_risk.build_scenario_risk` | 11 |
| Stage 7g | `daily_input_snapshot.run_daily_input_snapshot` | 2 |
| Stage 7h | `decision_context_capture.run_decision_context_capture` | 4 |
| Stage 7i | `quant_feedback.run_quant_feedback` | 5 |
| Stage 13b | `semantic_liveness.run_semantic_liveness` | 6 |
| Stage 14 | `run_manifest.complete_run` (status=complete) | 1 |

Ordering guarantees (tested): begin < complete; snapshot (7g) < context capture
(7h); both bind to the frozen snapshot. The Phase 3 sim lane (Stage 10e) reads
the snapshot written at 7g. All stages are non-blocking (`run_aux_stage`),
observe-only, and write SANDBOX/POLICY/LATEST only.

## Weekly (`run_weekly_safe.sh`) — added
- `strategy_mandate.build_strategy_mandates` (Phase 9 — mandate coverage)
- `experiment_registry.read_registry` review (Phase 8 — status rollup)
(alongside the existing portfolio_sim backtest / projection / strategy lab).

## Monthly / Yearly
`monthly_check.sh` and `yearly_check.sh` are skill-dispatch wrappers
(`claude --print /monthly-tool-analysis` / `/yearly-tool-analysis`). The new
artifacts (run_manifest, daily_input_snapshot, quant_feedback,
semantic_liveness_status, scenario_risk, experiment_registry, strategy_mandates)
are produced and on disk; extending those analysis skills to read them is a
documentation/skill update (follow-up), not a code-wiring gap.

## Wiring guard
`tests/test_sqg_pipeline_wiring.py` asserts every new stage is present in the
daily/weekly scripts and that the ordering invariants hold — so a producer can
never exist in code but silently not run.
