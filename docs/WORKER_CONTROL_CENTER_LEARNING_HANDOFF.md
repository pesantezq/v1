# Worker Control Center — Learning Projections Handoff (integration checkpoint 2)

**From:** the controller session (`feature/ew-0a-safe-operations`)
**To:** the GUI session (`feature/worker-control-center-gui`)

Supplements `docs/WORKER_CONTROL_CENTER_INTERFACE.md`, which remains the base
contract. This document adds the LEARNING surfaces and restates what is now LIVE.

The controller session did **not** modify the GUI worktree.

## Base state

| | |
|---|---|
| Controller branch | `feature/ew-0a-safe-operations` |
| Base SHA (0B.3 certified candidate) | `4d7aab68cb8be917f6509f99364048d3bbf46dc2` |
| `origin/main` | `0ef307a3e8682173f780fbc3a19b683f96e1249b` |
| Learning Kernel commit | see `git log` on this branch (added after `4d7aab6`) |
| Interface version | `engineering.readmodel.v0` + `engineering.learning_readmodel.v0` |

⚠ **0B.3 is NOT yet merged to `origin/main`.** The certified candidate is 30 commits
ahead and the branch is not on the remote — push is blocked on GitHub write
credentials (see the final report). Reconcile against the branch, not `origin/main`,
and expect the base SHA to change once the merge happens.

## Projection modules

| Module | Entry point | Status |
|---|---|---|
| `portfolio_automation/engineer_worker/ew0a_readmodels.py` | `build_dashboard(repo_root, now=...)` | extended |
| `portfolio_automation/engineer_worker/learning/readmodels.py` | `build_learning_dashboard(repo_root, worker_id, now)` | **new** |

`build_dashboard` gained an optional `now` argument (injected, never read from the
clock — the no-fabricated-time discipline) and a new `"learning"` key. If the
learning store is absent, `dashboard["learning"]` degrades to `PENDING_BACKEND`
rather than failing the whole dashboard.

**Existing GUI code keeps working**: `now` defaults to `None`, and every previously
published field is unchanged.

## New learning read models

### `RecentLessonsSummary`
`active_count`, `candidate_count`, `superseded_count`, `contradicted_count`,
`retired_count`, `recent[]` (lesson_id, capability, task_class, subsystem, status,
confidence, evidence_refs count, principle).

Show CANDIDATE and CONTRADICTED counts — a rejected lesson is evidence about the
extractor's calibration, not an embarrassment to hide.

### `CapabilityCompetenceSummary`
`worker_id`, `capabilities[]` (per-capability `TaskClassPerformanceV0` including
`success_rate` and `lesson_transfer_rate`), `total_observations`, `total_unsafe`.

**There is no aggregate score and the GUI must not compute one.** Averaging
capabilities would hide danger in one behind competence in another.

### `LessonTransferSummary`
`retrievals`, `lessons_supplied`, `successful_transfers`, `failed_transfers`,
`retrieved_but_ignored`, `repeated_error_after_lesson`, `transfer_rate`
(`PENDING_BACKEND` when no lesson has yet been retrieved — never `0.0`, which would
read as measured failure rather than absent data).

### `GraduationReadinessSummary`
`capabilities[]` (`CapabilityReadinessV0` per capability), `ready_for_certification[]`,
`c1_enabled` (structurally `false`), plus the explicit
`readiness_is_not_certification` / `certification_is_not_authority` flags.

**Rendering rule:** `READY_FOR_CERTIFICATION` must never be rendered as a promotion,
a grant, or an actionable control. It means only that a separate certification
mission may examine the capability.

## Fields now LIVE (were PENDING_BACKEND or absent)

* mission deliverables — all six 0B.3 contracts project `VERIFIED`, `is_complete=true`
* lesson store state (active / candidate / superseded / contradicted / retired)
* per-capability competence statistics, including unsafe counts
* lesson transfer statistics
* per-capability graduation readiness, hard blockers, unmet thresholds
* apprenticeship metrics (5 shadowed decisions — was 1)

## Fields STILL PENDING_BACKEND

Unchanged from the base interface — no backend was added for these, and none is
fabricated:

* worker heartbeat / `operational_state` / `current_task` / `queue_size` / `next_action`
* supervisor `availability` / `measured_latency_ms` / `verification_queue` / `outage_state`
* component health: `gpt_supervisor`, `engineer_runtime`, `sandbox`, `evidence_bridge`
* `controller_since`
* `LessonTransferSummary.transfer_rate` when no retrieval has occurred yet

## What the GUI must never do

Everything in the base interface, plus: **no projection may alter lessons,
competence, readiness policy, or authority.** There is no action endpoint in this
path, and none may be added. The GUI must not offer "activate lesson", "approve
readiness", "adjust threshold", or "certify capability" controls — those are
controller-owned operations that do not exist as GUI-reachable code.

## Suggested panel (all fields LIVE today)

```
ENGINEER
  Authority        A1_ASSISTED_ENGINEERING
  Controller       C0.5_SHADOW

  Capability: canonical_contract_risk_routing
    Observations              5
    Correct                   4
    Unsafe                    1  (historical — cdc-exs-1)
    Lesson transfers          4 / 4
    Repeated unsafe after lesson  0
    Readiness                 LEARNING
    Unmet: observations 5 < 40 · consecutive_safe 4 < 20 · success_rate 0.80 < 0.95
```

Note the honest divergence from the earlier `CANDIDATE` expectation: the new gate
applies high-risk multipliers (this capability is high-risk), which did not exist
when `CANDIDATE` was first stated. The number went down because the bar went up.

## Tests required after reconciliation

```bash
.venv/bin/python -m pytest \
  tests/test_ew0a_readmodels.py \
  tests/test_ew0a_learning.py \
  tests/test_ew0a_evals_registry.py \
  tests/test_ew0a.py tests/test_ew0a_authority.py tests/test_ew0a_loop.py \
  tests/test_gpt_supervisor.py \
  tests/test_worker_control_center.py    # GUI-side, in the GUI worktree
```

Plus the GUI session's own control-plane boundary tests (GET/HEAD only, CSP,
no-shell/credential imports, secret-shape redaction, unknown-run 404).

## Reconciliation guidance

The GUI stays **PARTIAL**. Several surfaces are LIVE now, but worker heartbeat,
supervisor health, and component health still have no authoritative backend. Do not
mark `READY` because pages exist or because the learning panels populate.
