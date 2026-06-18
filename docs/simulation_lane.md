# Simulation Governance — Active Simulation Lane

## Purpose

`portfolio_automation/sim_governance/simulation_lane.py` is the **active**
simulation/test lane of the two-lane governance model. Experimental advisory,
watchlist, crowd, discovery, and flock logic is applied here and is *allowed to
change simulation outputs*. The lane transforms a production *baseline* (a
snapshot of what production looks like today) into a *simulated* view, emitting
one `SimulationCandidate` per change.

---

## Two-Lane Governance

This is the lane the operator wants ACTIVE. It emits `lane_active: true`,
`observe_only: false` (active by design, sandbox-scoped), and
`production_safe: true`. Every write lands in the **SANDBOX** namespace; nothing
here touches production. The only path to production is the human-approved
promotion workflow. `decision_engine.py` and score semantics are never touched.

---

## Artifacts Written (OutputNamespace.SANDBOX → `outputs/sandbox/sim_governance/`)

| File | Contents |
|------|----------|
| `simulation_candidates.json` | Full lane result + candidates + simulated views |
| `simulated_watchlist.json` | The actively-changed simulated watchlist |
| `simulated_advisory.json` | The actively-changed simulated advisory |

---

## Key Functions

- `run_simulation_lane(root, now, *, baseline=None, experiments=None,
  write_files=True, base_dir=None) -> dict` — runs every experiment over the
  baseline, materializes the simulated views, and writes the SANDBOX artifacts.
  Inputs are injectable for tests; never raises on missing artifacts.
- `load_production_baseline(root) -> dict` — tolerant snapshot of
  `{watchlist, advisory, crowd, flock}`. Crowd context is sourced from the
  unified crowd bus; advisory from `decision_plan.json`; flock from the
  `outputs/simulation/flock_*` artifacts.
- **Built-in experiments (`DEFAULT_EXPERIMENTS`):**
  `experiment_watchlist_discovery_adds`, `experiment_watchlist_rerank`,
  `experiment_advisory_crowd_context`, `experiment_flock_intelligence`. Each is
  `baseline-dict -> list[SimulationCandidate]`; a failing experiment is logged
  and skipped (it never sinks the lane).
- `materialize_simulated_views(baseline, candidates) -> dict` — applies the
  candidates' `proposed_production_change` ops (add/remove/context/rank/flock_*)
  to produce the actively-changed `simulated_watchlist` / `simulated_advisory`.

Each candidate's `ready_for_production_review` is a HINT only; the AI/product
review decides, and only `DECISION_READY` later yields a *pending* proposal.

---

## Tests

Covered under `tests/` with the sim-governance suite
(`python -m pytest -q tests -k sim_governance`).
