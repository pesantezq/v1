# Operator Worker — Phase 3: Explicit Rollback + Quarantine Review (design)

Status: **approved (autonomous goal-driven)** · Date: 2026-06-19
Predecessor: `docs/operator_worker_hardening_spec.md` (Phase 3 row), Phase 2 cost cap
(shipped b000db89).

## Why

Phase 3 rounds out precondition #4 (rollback behavior). Today the worker's only
containment is "never merge / never push / quarantine on protected-path diff", and
the only terminal-clearing verb is `fail` — so abandoned/dead orders inflate the
`failed` count, and a quarantined worktree that holds a *good* candidate diff has no
review/salvage path (it's left in place, manually). This phase adds:

1. An explicit **`cancel`** verb (uses the existing `cancelled` terminal status) to
   clear dead/abandoned orders WITHOUT inflating `failed`.
2. A **quarantine-review** path: enumerate quarantined worktrees, show their diff vs
   `main`, and offer **salvage** (report the integration command for a human) or
   **discard** (remove the worktree — the explicit rollback of a contained change).
3. A daily-check **quarantine-review-pending** signal.

Ships additive + reversible. Does NOT enable autonomous execution (that is Phase 4,
human-gated). No `decision_engine.py` / score / `decision_plan.json` touch.

## State machine (already present — `operator_control/repair_policies.py`)

Statuses: queued, awaiting_approval, approved, claimed, running, completed, failed,
rejected, cancelled. `cancelled` is TERMINAL and reachable from
queued/awaiting_approval/approved/claimed (NOT from running or failed). Terminal set:
{completed, rejected, cancelled}. **No state-machine change is needed** — Phase 3
uses the existing `cancelled` transitions.

## Design decisions

1. **`cancel` scope = the existing graph.** `cancel` transitions queued / awaiting_approval /
   approved / claimed → `cancelled`. It is intentionally NOT allowed from `running`
   (a running order that died is a genuine `failed`, handled by run()'s crash path) or
   from terminal states. Invalid transitions raise `WorkOrderValidationError` (the
   existing `policy.validate_transition` enforces this — `cancel` just calls
   `transition_work_order(new_status="cancelled")`).
2. **"archive" is realized as quarantine `discard`, not a new status.** The hardening
   spec's loose "cancel/archive" becomes: `cancel` (clear a not-yet-run order) +
   `discard` (remove a quarantined worktree). Adding an `archive` status would expand
   the state machine without a concrete consumer — YAGNI. Documented as the deliberate
   interpretation.
3. **Quarantine identification is derived, not a new persisted field.** A "quarantined"
   item = a work order in `failed` status whose `operator/<id>` worktree still exists
   (worktrees are never auto-deleted on failure). `quarantine_review` cross-references
   `work_orders` (folded) with `worktree.list_worktrees` and computes the diff stat vs
   `main` for each. No new artifact; computed live (mirrors `status()`).
4. **Discard is the explicit rollback; salvage is report-only.** `discard` removes the
   worktree (`worktree.remove_worktree(force=True)`) and audits `worker_quarantine_discarded`
   — this is the reversible "roll back the contained change". `salvage` does NOT touch
   files; it returns the manual integration command + audits `worker_quarantine_salvaged`
   (a human integrates from the repo root). Neither merges nor pushes.

## Components — `operator_control/worker_runner.py`

```python
def cancel(root, work_order_id, actor="cli", note="") -> dict:
    """Clear a dead/abandoned order: transition to 'cancelled' (not 'failed').
    Raises WorkOrderValidationError if the current status can't go to cancelled."""
    cur = wo.get_work_order(root, work_order_id)
    if cur is None:
        raise WorkerRunnerError(f"unknown work order {work_order_id}")
    return wo.transition_work_order(root, work_order_id, new_status="cancelled",
                                    actor=actor, note=note or "cancelled by operator")


def quarantine_review(root) -> dict:
    """List quarantined worktrees (failed orders whose worktree still exists),
    with diff-vs-main stat + salvageable flag. Computed live; no persisted artifact."""
    # fold orders; for each failed order, check worktree path exists; compute
    # changed_files(wt, base="main"); item = {work_order_id, worktree, branch,
    # changed_file_count, salvageable: bool(changed_files), report_path}
    # return {"pending": <count of salvageable>, "items": [...]}


def quarantine_discard(root, work_order_id, actor="cli") -> dict:
    """Explicit rollback: remove the quarantined worktree for a failed order.
    Audits worker_quarantine_discarded. Does not change the order's status
    (it stays 'failed' — the record of what happened); only the worktree is removed."""


def quarantine_salvage(root, work_order_id, actor="cli") -> dict:
    """Report-only: return the manual integration command for a human + audit
    worker_quarantine_salvaged. Never merges/pushes/edits files."""
```

CLI subcommands (mirror the existing `complete`/`fail` pattern in `_build_parser`):
`cancel --id [--note]`, `quarantine-review`, `quarantine-discard --id`,
`quarantine-salvage --id`.

`status()` gains a `quarantine_pending` count (from `quarantine_review(root)["pending"]`)
so the daily check can read it without a second call path.

## Daily-check — `.claude/commands/daily-tool-analysis.md`

Extend the operator-control body line (6g) and the artifacts-read entry: surface
`quarantine_pending` (count of failed orders with a salvageable worktree diff). AMBER
when `quarantine_pending >= 1` (a contained candidate fix is awaiting human
salvage/discard — review it). Observe-only, never RED (unchanged contract). Mention
the new audit events `worker_quarantine_discarded` / `worker_quarantine_salvaged` /
`work_order_cancelled`.

## Tests — `tests/test_operator_worker_quarantine.py`

1. `cancel` transitions a queued order → cancelled (status + `work_order_cancelled` audit).
2. `cancel` from `claimed` → cancelled OK; from `running` raises WorkOrderValidationError;
   from `completed`/`failed` raises (invalid transition).
3. `cancel` unknown id → WorkerRunnerError.
4. `quarantine_review` identifies a failed order whose worktree has a diff as
   `salvageable=True` and counts it in `pending`; a failed order with no worktree, or a
   worktree with no diff, is not pending.
5. `quarantine_discard` removes the worktree (worktree.remove_worktree called),
   audits `worker_quarantine_discarded`, leaves the order status `failed`.
6. `quarantine_salvage` returns the integration command, audits
   `worker_quarantine_salvaged`, does NOT remove the worktree.
7. `status()` includes `quarantine_pending`.

## Invariants preserved
- Additive; no autonomous enablement; never merges/pushes; no decision_engine/score
  touch. `cancel`/`discard`/`salvage` are operator verbs (CLI), not auto-fired.
- Quarantine worktrees are removed ONLY by explicit `quarantine_discard` (today's
  never-auto-delete behavior is preserved by default).
