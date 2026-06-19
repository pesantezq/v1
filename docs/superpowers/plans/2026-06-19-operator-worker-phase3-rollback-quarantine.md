# Operator Worker — Phase 3: Rollback + Quarantine Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** Add an explicit `cancel` verb + a quarantine-review/salvage/discard path (the explicit rollback for contained changes) + a daily-check pending signal, to round out the operator-worker's precondition-4 (rollback behavior).

**Architecture:** All runner logic lands in existing `operator_control/worker_runner.py` (new functions `cancel`, `quarantine_review`, `quarantine_discard`, `quarantine_salvage`, a `status()` field, and CLI subcommands). Quarantine state is DERIVED live (failed order + existing `.worktrees/<id>` + diff vs main), no new artifact. Daily-check skill gets a pending-count line.

**Tech Stack:** Python 3.12 stdlib, pytest. Reuses `operator_control.worktree` (`list_worktrees`, `changed_files`, `remove_worktree`), `work_orders` (`transition_work_order`, `list_work_orders`), `audit_log.record_event`.

## Global Constraints

- Additive + reversible. Does NOT enable autonomous execution (`autonomous_worker.enabled` stays false — that's Phase 4, human-gated). No `decision_engine.py` / score / `decision_plan.json` change. Never merges/pushes.
- No state-machine change: `cancel` uses the EXISTING `cancelled` terminal status (reachable from queued/awaiting_approval/approved/claimed via `repair_policies._TRANSITIONS`). Invalid transitions must raise `WorkOrderValidationError` (let `policy.validate_transition` enforce — do not bypass it).
- Quarantine worktrees are removed ONLY by explicit `quarantine_discard`. Default never-auto-delete behavior is preserved.
- Worktree path for an order = `<root>/.worktrees/<work_order_id>`; branch = `operator/<work_order_id>`.
- Test runner: `.venv/bin/python -m pytest`. Targeted tests per task; do NOT run the full suite mid-plan (it mutates the protected `config/signal_registry.yaml`; preserve `default_weight: 0.4947`).
- Commits stage EXPLICIT paths (never `git commit -am`). End each commit message with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. Do NOT push.

---

### Task 1: `cancel` verb + CLI

**Files:**
- Modify: `operator_control/worker_runner.py` — add `cancel()` after `fail()` (~line 624); add CLI subparser + main() handler.
- Test: `tests/test_operator_worker_quarantine.py` (new)

**Interfaces:**
- Produces: `cancel(root, work_order_id, actor="cli", note="") -> dict` — transitions to `cancelled`; raises `WorkerRunnerError` on unknown id, `WorkOrderValidationError` on illegal transition.

- [ ] **Step 1: Write failing tests**

Create `tests/test_operator_worker_quarantine.py`:

```python
import json
from pathlib import Path

import pytest

from operator_control import worker_runner as wr
from operator_control import work_orders as wo
from operator_control.repair_policies import WorkOrderValidationError


def _seed(root: Path, status: str) -> str:
    """Create a work order record at the given status by appending a folded record."""
    root.mkdir(parents=True, exist_ok=True)
    wid = f"wo_{status}"
    rec = {"work_order_id": wid, "status": status, "probe_id": "p1",
           "skill_id": "s1", "mode": "safe_repair", "requested_action": "fix",
           "created_at": "2026-06-19T00:00:00+00:00",
           "status_history": [{"status": status, "at": "2026-06-19T00:00:00+00:00",
                               "actor": "test", "note": "seed"}]}
    p = root / "outputs" / "operator_control" / "work_orders.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    return wid


def _audit_events(root: Path) -> list[dict]:
    p = root / "outputs" / "operator_control" / "audit_log.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def test_cancel_queued_order(tmp_path):
    wid = _seed(tmp_path, "queued")
    out = wr.cancel(tmp_path, wid, actor="test", note="dead")
    assert out["status"] == "cancelled"
    assert wo.get_work_order(tmp_path, wid)["status"] == "cancelled"
    assert any(e["event_type"] == "work_order_cancelled" for e in _audit_events(tmp_path))


def test_cancel_claimed_order_ok(tmp_path):
    wid = _seed(tmp_path, "claimed")
    assert wr.cancel(tmp_path, wid, actor="test")["status"] == "cancelled"


def test_cancel_running_order_rejected(tmp_path):
    wid = _seed(tmp_path, "running")
    with pytest.raises(WorkOrderValidationError):
        wr.cancel(tmp_path, wid, actor="test")


def test_cancel_completed_order_rejected(tmp_path):
    wid = _seed(tmp_path, "completed")
    with pytest.raises(WorkOrderValidationError):
        wr.cancel(tmp_path, wid, actor="test")


def test_cancel_unknown_id_raises(tmp_path):
    _seed(tmp_path, "queued")
    with pytest.raises(wr.WorkerRunnerError):
        wr.cancel(tmp_path, "wo_does_not_exist", actor="test")
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_operator_worker_quarantine.py -q -k cancel`
Expected: FAIL — `module 'operator_control.worker_runner' has no attribute 'cancel'`.

- [ ] **Step 3: Implement `cancel`**

In `operator_control/worker_runner.py`, add immediately after the `fail()` function (which ends ~line 624, before `def drain(`):

```python
def cancel(root, work_order_id, actor="cli", note="") -> dict:
    """Clear a dead/abandoned order: transition to 'cancelled' (NOT 'failed', which
    would inflate the failure count). The policy graph permits cancelled from
    queued/awaiting_approval/approved/claimed; an illegal source status (running /
    terminal) raises WorkOrderValidationError via transition_work_order."""
    cur = wo.get_work_order(root, work_order_id)
    if cur is None:
        raise WorkerRunnerError(f"unknown work order {work_order_id}")
    return wo.transition_work_order(
        root, work_order_id, new_status="cancelled", actor=actor,
        note=note or "cancelled by operator",
    )
```

- [ ] **Step 4: Add CLI subcommand**

In `_build_parser()`, after the `fail` subparser block (the `spf` block ending with `spf.add_argument("--note", ...)`), add:

```python
    spc = sub.add_parser("cancel")
    spc.add_argument("--id", required=True)
    spc.add_argument("--actor", default="cli")
    spc.add_argument("--note", default="")
```

In `main()`, after the `if args.command == "fail":` block, add:

```python
        if args.command == "cancel":
            print(json.dumps(cancel(root, args.id, actor=args.actor, note=args.note), indent=2))
            return 0
```

Add `"cancel"` to the `__all__` list.

- [ ] **Step 5: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_operator_worker_quarantine.py -q -k cancel`
Expected: PASS (5 passed).

- [ ] **Step 6: Compile + commit**

Run: `.venv/bin/python -m py_compile operator_control/worker_runner.py`

```bash
git add operator_control/worker_runner.py tests/test_operator_worker_quarantine.py
git commit -m "feat(operator-worker): cancel verb to clear dead orders without inflating failed

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Quarantine review / discard / salvage + status field

**Files:**
- Modify: `operator_control/worker_runner.py` — add `quarantine_review`, `quarantine_discard`, `quarantine_salvage`; add `quarantine_pending` to `status()`; CLI subcommands; `__all__`.
- Test: `tests/test_operator_worker_quarantine.py` (append)

**Interfaces:**
- Consumes: `worktree.list_worktrees`, `worktree.changed_files`, `worktree.remove_worktree`, `wo.list_work_orders`, `audit_log.record_event`.
- Produces:
  - `quarantine_review(root) -> {"pending": int, "items": [{work_order_id, worktree, branch, changed_file_count, salvageable, report_path}]}`
  - `quarantine_discard(root, work_order_id, actor="cli") -> {"work_order_id", "worktree", "removed": bool}` (audits `worker_quarantine_discarded`; order status unchanged)
  - `quarantine_salvage(root, work_order_id, actor="cli") -> {"work_order_id", "branch", "worktree", "integration_command"}` (audits `worker_quarantine_salvaged`; removes nothing)
  - `status(root)` dict gains `"quarantine_pending": int`.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_operator_worker_quarantine.py`:

```python
def _seed_failed_with_worktree(root: Path, wid: str, with_diff: bool):
    """Seed a failed order and a fake worktree dir; optionally with a changed file."""
    rec = {"work_order_id": wid, "status": "failed", "probe_id": "p", "skill_id": "s",
           "mode": "safe_repair", "requested_action": "x",
           "created_at": "2026-06-19T00:00:00+00:00",
           "status_history": [{"status": "failed", "at": "2026-06-19T00:00:00+00:00",
                               "actor": "t", "note": "quarantined"}]}
    p = root / "outputs" / "operator_control" / "work_orders.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    wt = root / ".worktrees" / wid
    wt.mkdir(parents=True, exist_ok=True)
    return wt


def test_quarantine_review_identifies_salvageable(tmp_path, monkeypatch):
    wt_diff = _seed_failed_with_worktree(tmp_path, "wo_diff", with_diff=True)
    _seed_failed_with_worktree(tmp_path, "wo_nodiff", with_diff=False)
    # No worktree at all for this failed order:
    p = tmp_path / "outputs" / "operator_control" / "work_orders.jsonl"
    with p.open("a") as fh:
        fh.write(json.dumps({"work_order_id": "wo_nowt", "status": "failed",
                             "created_at": "2026-06-19T00:00:00+00:00"}) + "\n")

    def fake_changed(wtpath, base="main"):
        return ["some/file.py"] if str(wtpath).endswith("wo_diff") else []
    monkeypatch.setattr(wr.worktree, "changed_files", fake_changed)

    review = wr.quarantine_review(tmp_path)
    ids = {it["work_order_id"]: it for it in review["items"]}
    assert ids["wo_diff"]["salvageable"] is True
    assert ids["wo_diff"]["changed_file_count"] == 1
    assert ids["wo_nodiff"]["salvageable"] is False
    assert "wo_nowt" not in ids  # no worktree -> not listed
    assert review["pending"] == 1  # only wo_diff


def test_quarantine_discard_removes_worktree(tmp_path, monkeypatch):
    _seed_failed_with_worktree(tmp_path, "wo_kill", with_diff=True)
    removed = {}
    monkeypatch.setattr(wr.worktree, "remove_worktree",
                        lambda root, path, force=False: removed.update(path=str(path), force=force))
    out = wr.quarantine_discard(tmp_path, "wo_kill", actor="test")
    assert out["removed"] is True
    assert removed["force"] is True
    assert wo.get_work_order(tmp_path, "wo_kill")["status"] == "failed"  # status unchanged
    assert any(e["event_type"] == "worker_quarantine_discarded" for e in _audit_events(tmp_path))


def test_quarantine_salvage_reports_only(tmp_path, monkeypatch):
    _seed_failed_with_worktree(tmp_path, "wo_keep", with_diff=True)
    called = {"removed": False}
    monkeypatch.setattr(wr.worktree, "remove_worktree",
                        lambda *a, **k: called.update(removed=True))
    out = wr.quarantine_salvage(tmp_path, "wo_keep", actor="test")
    assert out["branch"] == "operator/wo_keep"
    assert "wo_keep" in out["integration_command"]
    assert called["removed"] is False  # salvage NEVER removes
    assert any(e["event_type"] == "worker_quarantine_salvaged" for e in _audit_events(tmp_path))


def test_status_includes_quarantine_pending(tmp_path, monkeypatch):
    _seed_failed_with_worktree(tmp_path, "wo_p", with_diff=True)
    monkeypatch.setattr(wr.worktree, "changed_files", lambda w, base="main": ["f.py"])
    monkeypatch.setattr(wr.worktree, "list_worktrees", lambda root: [])
    st = wr.status(tmp_path)
    assert st["quarantine_pending"] == 1
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_operator_worker_quarantine.py -q -k "quarantine or status_includes"`
Expected: FAIL — `quarantine_review` not defined.

- [ ] **Step 3: Implement the three functions**

In `operator_control/worker_runner.py`, add after `cancel()`:

```python
def quarantine_review(root) -> dict:
    """Enumerate quarantined worktrees: failed orders whose .worktrees/<id> still
    exists. Computes the diff vs main per item (salvageable = non-empty diff).
    Derived live — no persisted artifact. Removes nothing."""
    root = Path(root)
    items = []
    for o in wo.list_work_orders(root):
        if o.get("status") != "failed":
            continue
        wid = o["work_order_id"]
        wt = root / ".worktrees" / wid
        if not wt.exists():
            continue
        try:
            changed = worktree.changed_files(wt, base="main")
        except Exception:
            changed = []
        rp = report_path(root, wid)
        items.append({
            "work_order_id": wid,
            "worktree": str(wt),
            "branch": f"operator/{wid}",
            "changed_file_count": len(changed),
            "salvageable": bool(changed),
            "report_path": str(rp) if rp.exists() else None,
        })
    return {"pending": sum(1 for it in items if it["salvageable"]), "items": items}


def quarantine_discard(root, work_order_id, actor="cli") -> dict:
    """Explicit rollback: remove the quarantined worktree for a failed order. The
    order's status stays 'failed' (the record of what happened); only the contained
    worktree is discarded. Audits worker_quarantine_discarded."""
    root = Path(root)
    wt = root / ".worktrees" / work_order_id
    existed = wt.exists()
    if existed:
        worktree.remove_worktree(root, wt, force=True)
    audit_log.record_event(
        root, event_type="worker_quarantine_discarded", actor=actor,
        work_order_id=work_order_id,
        details={"worktree": str(wt), "existed": existed},
        safety_result="quarantine worktree removed (contained change discarded)",
    )
    return {"work_order_id": work_order_id, "worktree": str(wt), "removed": existed}


def quarantine_salvage(root, work_order_id, actor="cli") -> dict:
    """Report-only: return the MANUAL integration command for a human to review and
    integrate the contained branch. Never merges/pushes/edits. Audits
    worker_quarantine_salvaged."""
    root = Path(root)
    branch = f"operator/{work_order_id}"
    wt = root / ".worktrees" / work_order_id
    integration_command = (
        f"# review first, then from the repo root:\n"
        f"git -C {root} checkout main && git -C {root} merge --no-ff {branch}"
    )
    audit_log.record_event(
        root, event_type="worker_quarantine_salvaged", actor=actor,
        work_order_id=work_order_id,
        details={"branch": branch, "worktree": str(wt)},
        safety_result="quarantine salvage reported (manual integration; no auto-merge)",
    )
    return {"work_order_id": work_order_id, "branch": branch,
            "worktree": str(wt), "integration_command": integration_command}
```

- [ ] **Step 4: Add `quarantine_pending` to `status()`**

In `status()`, change the returned dict to include the pending count. Add this key to the dict literal (e.g. after `"operational_runs": len(cost_log),`):

```python
        "quarantine_pending": quarantine_review(root)["pending"],
```

- [ ] **Step 5: Add CLI subcommands + handlers + `__all__`**

In `_build_parser()`, after the `cancel` subparser, add:

```python
    sub.add_parser("quarantine-review")
    sqd = sub.add_parser("quarantine-discard")
    sqd.add_argument("--id", required=True)
    sqd.add_argument("--actor", default="cli")
    sqs = sub.add_parser("quarantine-salvage")
    sqs.add_argument("--id", required=True)
    sqs.add_argument("--actor", default="cli")
```

In `main()`, after the `cancel` handler, add:

```python
        if args.command == "quarantine-review":
            print(json.dumps(quarantine_review(root), indent=2))
            return 0
        if args.command == "quarantine-discard":
            print(json.dumps(quarantine_discard(root, args.id, actor=args.actor), indent=2))
            return 0
        if args.command == "quarantine-salvage":
            print(json.dumps(quarantine_salvage(root, args.id, actor=args.actor), indent=2))
            return 0
```

Add `"quarantine_review"`, `"quarantine_discard"`, `"quarantine_salvage"` to `__all__`.

- [ ] **Step 6: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_operator_worker_quarantine.py -q`
Expected: PASS (all cancel + quarantine + status tests).

- [ ] **Step 7: Compile + run the existing runner suite (no regression) + commit**

Run: `.venv/bin/python -m py_compile operator_control/worker_runner.py && .venv/bin/python -m pytest tests/test_operator_worker_runner.py tests/test_operator_worker_cost_cap.py -q`
Expected: PASS (existing runner + cost-cap tests still green — confirms `status()` change didn't break callers).

```bash
git add operator_control/worker_runner.py tests/test_operator_worker_quarantine.py
git commit -m "feat(operator-worker): quarantine review/discard/salvage + status pending

quarantine_review derives quarantined worktrees (failed order + existing worktree +
diff vs main); discard is the explicit rollback (remove worktree, audit), salvage is
report-only (manual integration command, audit). status() surfaces quarantine_pending.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Daily-check pending signal + docs

**Files:**
- Modify: `.claude/commands/daily-tool-analysis.md` — operator-control body line (6g) + artifacts-read entry + AMBER trigger.
- Modify: `docs/operator_worker_hardening_spec.md` — mark Phase 3 shipped.

**Interfaces:**
- Consumes: `status(root)["quarantine_pending"]` and the new audit events.

- [ ] **Step 1: Extend the operator-control body line (6g, ~line 328)**

Find the 6g operator-control template line. Append `· quarantine-pending {quarantine_pending}` to the rendered counts, and add `quarantine_pending ≥ 1` to its AMBER clause. Make the minimal fragment edit; preserve the observe-only / never-RED wording. The covering signal is `worker_runner status`'s new `quarantine_pending`.

- [ ] **Step 2: Extend the AMBER-triggers section (~line 198)**

Append to the operator-control AMBER bullet: `OR quarantine_pending ≥ 1 (a contained candidate fix is awaiting human salvage/discard — review /dashboard/operator/report/<id> then quarantine-salvage or quarantine-discard). Still observe-only, never RED.`

- [ ] **Step 3: Extend the artifacts-read entry (~line 66)**

Append to the operator-control audit-log bullet: `Also fold the Phase 3 events: work_order_cancelled (dead order cleared, not a failure), worker_quarantine_discarded (contained worktree rolled back), worker_quarantine_salvaged (branch reported for manual integration).`

- [ ] **Step 4: Verify the markdown**

Run: `grep -n "quarantine_pending\|worker_quarantine_discarded\|worker_quarantine_salvaged\|work_order_cancelled" .claude/commands/daily-tool-analysis.md`
Expected: ≥3 matching lines across the three locations.

- [ ] **Step 5: Mark Phase 3 shipped in the hardening spec**

In `docs/operator_worker_hardening_spec.md`, update the precondition #4 row and the Phase 3 section to reflect shipped state (cancel + quarantine-review/salvage/discard implemented; rollback path now explicit). Reference this plan.

- [ ] **Step 6: Commit**

```bash
git add .claude/commands/daily-tool-analysis.md docs/operator_worker_hardening_spec.md
git commit -m "feat(daily-check)+docs: operator-worker quarantine-pending signal + Phase 3 shipped

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Verification

- [ ] **Step 1: Run all operator-worker suites**

Run: `.venv/bin/python -m pytest tests/test_operator_worker_quarantine.py tests/test_operator_worker_runner.py tests/test_operator_worker_cost_cap.py tests/test_worker_runner_container.py -q`
Expected: all PASS.

- [ ] **Step 2: Smoke the CLI**

Run: `.venv/bin/python -m operator_control.worker_runner status` and `.venv/bin/python -m operator_control.worker_runner quarantine-review`
Expected: JSON with `quarantine_pending` and `{"pending":..., "items":[...]}` — no traceback.
