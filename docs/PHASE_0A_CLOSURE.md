# Phase 0A Closure — R&D Control Plane Foundation

Date: 2026-08-09. Narrow closure review of the Phase 0A foundation against the
operator's review findings. Small corrective patches were applied to satisfy
Phase 0A's own contract (concurrency, schema fail-closed, read-only health).

## 1–5. Git topology & working tree
- Branch: `feature/rd-control-foundation`
- HEAD SHA (pre-closure): `3173325` (Phase 0A), parent `5d79c2b`
- origin/main: `af10d8e`
- Divergence: HEAD **2 ahead / 7 behind** origin/main (2 ahead = `5d79c2b` export + `3173325` rd_control; 7 behind = main commits since `dc0070a`). **Intentionally unresolved** — no pull/rebase/merge. Integration onto current main is an explicit future operator decision.
- Working tree at review start: **clean** (no runtime DB, no stray files).

## 6. Files inspected
`portfolio_automation/rd_control/{contracts,registry,health,__main__,__init__}.py`,
`tests/test_rd_control.py`, `portfolio_automation/artifact_registry.yaml`,
`run_manifest.py`, `next_stage/contracts.py`, existing SQLite modules, git topology.

## 7. Concurrency assessment (finding #2) — PATCHED
**Before:** `transition()` did `get_job` (read status) then `UPDATE ... WHERE job_id = ?` (unconditional). Two writers that both validated against the same old status could both UPDATE → logical lost update / illegal double transition. WAL + busy_timeout serialize the *writes* but do not re-check the precondition.
**After:** transitions go through `_apply_cas_transition()`, an atomic compare-and-swap: `UPDATE ... WHERE job_id = ? AND status = <expected_old>`; if `rowcount != 1` it raises `ConcurrentTransitionError` and the transaction rolls back (no state change, no audit row). The loser of a race is refused; the winner is preserved.
**Evidence:** `test_concurrent_transition_cas_prevents_lost_update` (two connections; c1 wins RUNNING→RESULT_RECEIVED; c2 with a stale RUNNING view is refused; winner preserved; no phantom audit) and `test_cas_transition_on_missing_job_refused`.

## 8. Schema migration / version assessment (finding #3) — PATCHED
Phase 0A had **version detection + first-open creation**, NOT a multi-version migration engine (accurate wording). It did **not** fail closed on a *newer* schema.
**After:** `_migrate()` raises `RDControlError` when the stored `schema_meta.version` exceeds the supported version (`1`). Incompatible (future) DBs are refused, not silently used. There is still no v1→v2 migration path because only v1 exists — deliberately not built.
**Evidence:** `test_connect_fails_closed_on_newer_schema`, `test_health_red_on_newer_schema`.

## 9. Recovery semantics assessment (finding #4) — WORDING QUALIFIED (no code change)
`recover_stale_running()` is **stale-job reconciliation**: RUNNING jobs whose `updated_at` is older than an injected threshold are moved to `INTERRUPTED` (a legal RUNNING edge). It is explicitly **not** a worker lease/heartbeat system and makes no automatic retries. Leases are a future phase; not built here.

## 10. Health read-only assessment (finding #5) — PATCHED
**Before:** `build_health()` called `connect()`, which runs `_migrate()` (creates schema) and `PRAGMA journal_mode=WAL` (a write) — so health could initialize/alter state (a read-only violation, masked by a test that pre-created the DB).
**After:** health uses new `connect_readonly()` — opens `file:...?mode=ro` (fails if the DB doesn't exist), sets `PRAGMA query_only=ON`, and does **no** create/migrate/journal-change. A missing/uninitialised registry reports `db_accessible=False`, status **RED** (never GREEN when authoritative state can't be read).
**Evidence:** `test_health_does_not_create_db` (missing path stays absent, RED), `test_health_does_not_migrate_or_change_journal` (repeated health leaves schema/journal/job-count unchanged).

## 11. Artifact-registry consumer assessment (finding #6)
`rd_control_health.json`: `required: false`, `cadence: on_demand`, `consumer_status: diagnostic_only`, `consumers: []`, `lens: meta_governance`. Schema-valid (the registry validator only requires non-empty `consumers` for `consumer_status: consumed`). Satisfies the producer/consumer rule as an observe-only diagnostic — the operator + registry validator are its consumers. **No daily production coupling added**; a dedicated analysis consumer can be added in a later phase when a real reader exists.

## 12. Production-impact wording (finding #7)
Precise statement: **No production decision, scoring, broker, portfolio, or execution behavior changed. `decision_engine.py` and the protected score semantics are untouched, and `outputs/latest/decision_plan.json` remains the decision source of truth.** The shared governance/observability registry (`artifact_registry.yaml`) was extended **additively** with one optional, observe-only telemetry row.

## 13–14. Test results
- Targeted `tests/test_rd_control.py`: **32 passed** (26 original + 6 closure). `py_compile` clean.
- Broad `pytest -q`: see final report — the only failures are the known pre-existing environment-coupled set; **zero new regressions** attributable to this closure.

## 15. Final working-tree state
After tests/CLI: only the intended source changes staged; **no `data/rd_control.db`** or other runtime artifact present or committed.

## 16. Exact changes made
- `contracts.py`: add `ConcurrentTransitionError`.
- `registry.py`: add `connect_readonly()`; `_migrate()` fail-closed on newer schema; refactor `transition()` → atomic `_apply_cas_transition()` (CAS + affected-rows check).
- `health.py`: `build_health()` uses `connect_readonly()`.
- `__init__.py`: export `ConcurrentTransitionError`.
- `tests/test_rd_control.py`: +6 closure tests.
- `PHASE_0A_CLOSURE.md`: this document.
(No change to `decision_engine.py`, scoring, or any production path. `artifact_registry.yaml` unchanged in this closure — the row was added in the Phase 0A commit.)

## 17. Verdict
```
RD_CONTROL_FOUNDATION_READY_WITH_QUALIFICATIONS
```
The foundation is correct and its contract now holds under concurrency, incompatible schemas, and read-only health. Standing qualifications (documented, not defects to fix now): (a) validated on the `5d79c2b`/`3173325` lineage, **not** against current `origin/main af10d8e` — integration is a deferred operator decision; (b) recovery is stale-job reconciliation, **not** a lease/heartbeat system; (c) no worker, sandbox, or lifecycle beyond `jobs`/`job_events` exists yet (by design). Phase 0B not started.
