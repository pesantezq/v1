# Daily Incremental Simulation (Phase 3)

Status: **shipped** on `feat/complete-simulation-quant-governance-loop`.
Active-in-simulation, production-isolated (writes SANDBOX only).

## What already existed

The daily simulation engine was largely built before this phase
(`portfolio_automation/sim_governance/`):

- `simulation_lane.py` — `run_simulation_lane()` applies a set of deterministic,
  injectable *experiments* to one **production baseline** (`load_production_baseline`):
  flock-intelligence, watchlist discovery adds, watchlist rerank, advisory crowd
  context. Emits `SimulationCandidate`s + the actively-changed simulated
  views. `lane_active=True` (allowed to change SANDBOX outputs) but
  `production_safe=True` (never writes outside SANDBOX).
- `daily_simulation_bundle.py` — `build_daily_simulation_bundle()` rolls the lane
  result into one bundle with the comparison-vs-production-baseline, risk /
  data-quality / confidence summaries, and the unified-crowd evidence the single
  daily AI review reads.
- The 8 materialized strategy profiles + 6 shadow portfolios + crowd tactic live
  in `portfolio_automation/portfolio_sim/` and `strategy/profiles.py`.

Pipeline: the lane runs as `run_daily_safe.sh` **Stage 10e**
(simulation-governance daily lane).

## What Phase 3 added — binding to the frozen input snapshot

Every experiment in one `run_simulation_lane` call already operates on the
**same** baseline, so they share inputs by construction. Phase 3 makes that
**provable + auditable** by binding the lane (and the bundle) to the Phase 2
immutable input snapshot:

- `simulation_lane._input_snapshot_binding(base_dir)` reads
  `sandbox/daily_input_snapshot.json` and records `input_snapshot_hash` +
  `input_snapshot_run_id` on the lane result.
- `daily_simulation_bundle` propagates both fields.

So a reviewer can verify that the production decision and **every** shadow
strategy in a given run evaluated one identical frozen input identity
(`snapshot_hash`) — and that no sim read later information (Iron rules 4, 5).
Degrades to `None` when no snapshot exists (e.g. Phase 2 stage skipped).

Ordering: Stage 7g (Phase 2 snapshot) runs **before** Stage 10e (this lane), so
the snapshot is frozen when the lane reads it.

## Invariants

- No production write (SANDBOX only; asserted by test).
- No score/allocation/decision mutation; `decision_engine.py` untouched.
- Deterministic for fixed inputs (injected `now`; idempotent).
- Candidates remain proposals — promotion stays human-gated (Phase 10).

## Tests

`tests/test_sim_governance.py` (Phase 3 section) — lane binds to the frozen
snapshot, binding-absent is safe, bundle propagates the binding, and the lane
stays sandbox-only / production-safe. Plus the pre-existing lane/bundle/
promotion suites (43 tests) remain green.

## Remaining (later phases)

Per-decision capital metrics (funded/unfunded/turnover/divergence) and pre/post
risk per simulated action land in Phases 4–5 + 11, which consume this lane's
candidates + the snapshot.
