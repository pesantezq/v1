# Promotion-Overlay Durability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a human-approved production change durable — approve a fact once and it stays applied until explicitly revoked — without weakening the human gate.

**Architecture:** Two additive changes. (1) Approval records gain the already-stable `candidate_id`, and approval matching accepts either id, so an unchanged fact does not need daily re-approval. (2) Watchlist ops (durable membership) are rebuilt from the append-only audit log minus revocations, while advisory ops (current-state annotations) keep today's refresh semantics. A new explicit revoke path is the only thing that un-applies a watchlist op.

**Tech Stack:** Python 3.12, pytest, `.venv/bin/python`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-28-promotion-overlay-durability-design.md`

## Global Constraints

- Interpreter is `/opt/stockbot/.venv/bin/python`. Bare `python` is not on PATH.
- **HUMAN GATE — the highest-priority invariant.** Only human-approved ops may ever be applied. Do NOT modify `schemas.is_human_approver`, and do NOT add any type to `auto_approval._WATCHLIST_ELIGIBLE_TYPES`. Matching on `candidate_id` must never let an *unapproved* candidate through: a candidate id must be matched exactly, never inferred from symbol, type, or proximity.
- **Backward compatibility is load-bearing.** `outputs/promotion_approvals/approved_proposals.json` holds 43 real records that carry NO `candidate_id`. `is_valid_approval_record` must keep accepting them, and `proposal_id` matching must keep working unchanged.
- **A rejected or revoked op must NEVER be resurrected** by the audit-log rebuild. This is the primary risk of Task 3; reject/revoke always beats approve regardless of record order.
- **Advisory refresh must not change.** No stale annotation may survive a state change. Advisory ops keep replace-from-pending semantics.
- Observe-only elsewhere: do NOT change `decision_engine.py`, scoring logic, `_TRACKED_KNOBS`, or any score semantics.
- `record_approval`'s `base_dir` guard stays: a repo-root `base_dir` must still be refused (`_looks_like_repo_root`). Pass `<root>/outputs`.
- Every function that reads a log or artifact must degrade to empty on a missing/corrupt file and never raise.
- Commit with explicit paths (`git add <path>`), never `git commit -am` — the tree carries unrelated modified artifacts. Verify with `git diff --cached --stat`.
- Every commit message ends with:
  `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`
- **Do NOT run the full test suite** except in Task 5. It takes ~4-5 minutes; run it in the BACKGROUND with a durable log path and poll, never as a blocking foreground call. Baseline is **9 pre-existing failures** (`tuning_proposals` x2, `run_loop`, `operator_control` x2, `operator_worker_runner` x2, `data_budget_governor`, `social_sentiment/quality_gates`). Anything beyond those 9 is a regression.
- The full suite mutates `config/signal_registry.yaml`. Back it up first, restore if it differs.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `portfolio_automation/sim_governance/promotion_approvals.py` | approval log; gains candidate-keyed folds + revoke | modify (~60 lines) |
| `portfolio_automation/sim_governance/production_application.py` | overlay application; either-id matching + durable watchlist rebuild | modify (~70 lines) |
| `tests/test_promotion_approval_identity.py` | Task 1 tests | create |
| `tests/test_overlay_either_id_matching.py` | Task 2 tests | create |
| `tests/test_overlay_watchlist_durability.py` | Task 3 tests | create |
| `tests/test_overlay_revoke.py` | Task 4 tests | create |
| `.claude/commands/daily-tool-analysis.md` | durability health check | modify |

**Verified facts (do not re-derive):**
- `record_approval(proposal_id, decision, approver, now, *, base_dir, notes=None, review_date=None, write_files=True) -> dict` returning `{"ok", "reason", "record"}`.
- `is_valid_approval_record(record) -> (ok, reason)` requires only `proposal_id`, a decision in `HUMAN_DECISIONS`, a human `approver`, and a `timestamp`. It does NOT look at `candidate_id`.
- `effective_approvals(base_dir) -> dict[proposal_id, decision]` folds `load_valid_approvals` in file order, last record wins (`promotion_approvals.py:131-140`).
- `_overlay_entry(proposal)` (`production_application.py:75-84`) already includes `candidate_id`.
- The audit writer (`production_application.py:165-173`) does NOT write `candidate_id` — adding it is a one-line additive fix.
- Audit rows are `{ts, event, proposal_id, proposal_type, change, rollback_plan, snapshots}` with `event == "applied_to_production"`.
- `S.workflow_for_proposal_type(ptype)` returns `S.WORKFLOW_WATCHLIST` for all watchlist types.

---

## Task 1: Candidate-keyed approval identity

**Files:**
- Modify: `portfolio_automation/sim_governance/promotion_approvals.py` (`record_approval` at `:63`; new folds after `:149`)
- Test: `tests/test_promotion_approval_identity.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `record_approval(..., candidate_id: str | None = None)` — stores `candidate_id` in the record. Keyword-only, defaults `None`, so every existing call site is unaffected.
  - `effective_approvals_by_candidate(base_dir) -> dict[str, str]` — `{candidate_id: 'approve'|'reject'}`, folding valid records that carry a `candidate_id`, last record wins.
  - `approved_candidate_ids(base_dir) -> set[str]`, `rejected_candidate_ids(base_dir) -> set[str]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_promotion_approval_identity.py`:

```python
"""Candidate-keyed approval identity (Task 1).

make_candidate_id is stable-when-unchanged by design (schemas.py:245); the flock
producer salts it with the flock STATE. make_proposal_id hashes candidate_id|now,
so the proposal id churns every run even when the fact is identical. Approvals key
on proposal_id, so an unchanged fact needs daily re-approval. Recording
candidate_id lets an approval outlive the proposal id it was filed against.

Backward compatibility is load-bearing: 43 real records carry no candidate_id.
"""
from __future__ import annotations

import json
from pathlib import Path

from portfolio_automation.sim_governance import promotion_approvals as PA
from portfolio_automation.sim_governance import schemas as S

_NOW = "2026-07-28T17:00:00+00:00"


def _outputs(tmp_path: Path) -> str:
    d = tmp_path / "outputs"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _write_log(base_dir: str, approvals: list[dict]) -> None:
    d = Path(base_dir) / "promotion_approvals"
    d.mkdir(parents=True, exist_ok=True)
    (d / "approved_proposals.json").write_text(
        json.dumps({"generated_at": _NOW, "schema": "approved_proposals.v1",
                    "approvals": approvals}), encoding="utf-8")


def _rec(pid: str, decision: str = "approve", *, cid: str | None = None) -> dict:
    r = {"proposal_id": pid, "decision": decision, "approver": "pesantez",
         "timestamp": _NOW, "notes": None, "review_date": None}
    if cid is not None:
        r["candidate_id"] = cid
    return r


def test_record_approval_persists_candidate_id(tmp_path):
    base = _outputs(tmp_path)
    res = PA.record_approval("prop_a", "approve", "pesantez", _NOW,
                             base_dir=base, candidate_id="cand_x")
    assert res["ok"] is True, res["reason"]
    assert res["record"]["candidate_id"] == "cand_x"


def test_legacy_record_without_candidate_id_is_still_valid(tmp_path):
    """The 43 historical records carry no candidate_id and must keep working."""
    ok, reason = S.is_valid_approval_record(_rec("prop_legacy"))
    assert ok is True, reason


def test_fold_by_candidate_ignores_records_without_candidate_id(tmp_path):
    base = _outputs(tmp_path)
    _write_log(base, [_rec("prop_legacy"), _rec("prop_b", cid="cand_b")])

    by_cand = PA.effective_approvals_by_candidate(base)

    assert by_cand == {"cand_b": "approve"}
    # proposal-id folding is untouched and still sees both
    assert PA.approved_proposal_ids(base) == {"prop_legacy", "prop_b"}


def test_last_record_wins_per_candidate(tmp_path):
    """A later reject supersedes an earlier approve for the same candidate."""
    base = _outputs(tmp_path)
    _write_log(base, [
        _rec("prop_1", "approve", cid="cand_x"),
        _rec("prop_2", "reject", cid="cand_x"),
    ])

    assert PA.effective_approvals_by_candidate(base) == {"cand_x": "reject"}
    assert PA.approved_candidate_ids(base) == set()
    assert PA.rejected_candidate_ids(base) == {"cand_x"}


def test_approve_under_a_new_proposal_id_keeps_the_candidate_approved(tmp_path):
    """The treadmill case: same fact, new proposal id each run."""
    base = _outputs(tmp_path)
    _write_log(base, [_rec("prop_day1", "approve", cid="cand_same")])

    assert PA.approved_candidate_ids(base) == {"cand_same"}


def test_missing_log_degrades_to_empty(tmp_path):
    base = _outputs(tmp_path)
    assert PA.effective_approvals_by_candidate(base) == {}
    assert PA.approved_candidate_ids(base) == set()
    assert PA.rejected_candidate_ids(base) == set()


def test_ai_approver_still_rejected_with_candidate_id(tmp_path):
    """candidate_id must not become a bypass for the human gate."""
    base = _outputs(tmp_path)
    res = PA.record_approval("prop_ai", "approve", "auto_approval", _NOW,
                             base_dir=base, candidate_id="cand_x")
    assert res["ok"] is False
    assert "human" in res["reason"].lower() or "approver" in res["reason"].lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_promotion_approval_identity.py -v`
Expected: FAIL — `record_approval() got an unexpected keyword argument 'candidate_id'` and `AttributeError: effective_approvals_by_candidate`. The two backward-compat tests (`test_legacy_record_without_candidate_id_is_still_valid`, `test_missing_log_degrades_to_empty`) will fail only on the missing attribute, not on validation.

- [ ] **Step 3: Add `candidate_id` to `record_approval`**

In `promotion_approvals.py`, the signature currently reads:

```python
def record_approval(
    proposal_id: str,
    decision: str,
    approver: str,
    now: str,
    *,
    base_dir: str,
    notes: str | None = None,
    review_date: str | None = None,
    write_files: bool = True,
) -> dict:
```

Add one keyword-only parameter (keeping every existing call site valid):

```python
def record_approval(
    proposal_id: str,
    decision: str,
    approver: str,
    now: str,
    *,
    base_dir: str,
    notes: str | None = None,
    review_date: str | None = None,
    candidate_id: str | None = None,
    write_files: bool = True,
) -> dict:
```

Then the record construction currently reads:

```python
    record = {
        "proposal_id": proposal_id,
        "decision": decision,
        "approver": approver,
        "timestamp": now,
        "notes": notes,
        "review_date": review_date,
    }
```

Add the candidate id. Write the key only when supplied, so records stay
byte-comparable with the historical shape when it is omitted:

```python
    record = {
        "proposal_id": proposal_id,
        "decision": decision,
        "approver": approver,
        "timestamp": now,
        "notes": notes,
        "review_date": review_date,
    }
    if candidate_id:
        # The durable identity. make_proposal_id is clock-salted, so proposal_id
        # churns every run for an unchanged fact; candidate_id does not. Recording
        # it lets one approval outlive the proposal id it was filed against.
        record["candidate_id"] = candidate_id
```

Do NOT change `is_valid_approval_record` — it must keep accepting records without
a `candidate_id`.

- [ ] **Step 4: Add the candidate-keyed folds**

Append to `promotion_approvals.py` after `rejected_proposal_ids`:

```python
def effective_approvals_by_candidate(base_dir: str) -> dict[str, str]:
    """Fold the approval log to the latest valid decision per candidate_id.

    Mirrors ``effective_approvals`` but keys on the DURABLE identity. Records
    without a ``candidate_id`` (every record written before this field existed)
    are skipped here — they remain fully effective via ``effective_approvals``.

    Returns {candidate_id: 'approve'|'reject'}; file order is chronological, so
    the last record wins.
    """
    latest: dict[str, str] = {}
    for rec in load_valid_approvals(base_dir):
        cid = rec.get("candidate_id")
        if cid:
            latest[str(cid)] = rec["decision"]
    return latest


def approved_candidate_ids(base_dir: str) -> set[str]:
    """candidate_ids whose latest valid human decision is 'approve'."""
    return {cid for cid, dec in effective_approvals_by_candidate(base_dir).items()
            if dec == S.HUMAN_APPROVE}


def rejected_candidate_ids(base_dir: str) -> set[str]:
    """candidate_ids whose latest valid human decision is 'reject'."""
    return {cid for cid, dec in effective_approvals_by_candidate(base_dir).items()
            if dec == S.HUMAN_REJECT}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_promotion_approval_identity.py -v`
Expected: 7 passed.

- [ ] **Step 6: Verify against the real 43-record log**

Run:

```bash
.venv/bin/python -c "
from portfolio_automation.sim_governance import promotion_approvals as PA
b='/opt/stockbot/outputs'
print('valid approvals   :', len(PA.load_valid_approvals(b)))
print('approved by pid   :', len(PA.approved_proposal_ids(b)))
print('approved by cand  :', len(PA.approved_candidate_ids(b)))
"
```

Expected: `valid approvals` and `approved by pid` are non-zero and unchanged from
before this task (the historical log still resolves); `approved by cand` is `0`
because no historical record carries a `candidate_id`. A non-zero
`approved by cand` here would mean the fold is inventing ids — stop and report.

- [ ] **Step 7: Run the targeted regression**

Run: `.venv/bin/python -m pytest tests/test_sim_governance.py tests/test_promotion_approval_identity.py -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add portfolio_automation/sim_governance/promotion_approvals.py tests/test_promotion_approval_identity.py
git commit -m "feat(sim-gov): record candidate_id on approvals for durable identity

make_candidate_id is stable-when-unchanged by design (schemas.py:245) — the
flock producer salts it with the flock state — while make_proposal_id hashes
candidate_id|now, so the proposal id churns every run for an identical fact.
Approvals key on proposal_id, so an unchanged fact needed re-approval daily.

record_approval gains a keyword-only candidate_id (default None, written only
when supplied, so historical record shape is unchanged and every existing call
site is unaffected). Adds effective_approvals_by_candidate /
approved_candidate_ids / rejected_candidate_ids, mirroring the proposal-id folds
with the same last-record-wins semantics.

is_valid_approval_record is deliberately UNCHANGED: the 43 existing records carry
no candidate_id and must stay valid. Records without one are skipped by the
candidate fold and remain fully effective via the proposal-id fold.

The human gate is untouched — a record with a candidate_id and an AI approver is
still rejected (test covers it).

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Either-id approval matching + audit carries `candidate_id`

**Files:**
- Modify: `portfolio_automation/sim_governance/production_application.py` (`apply_approved_proposals` at `:87`; audit row at `:165-173`)
- Test: `tests/test_overlay_either_id_matching.py`

**Interfaces:**
- Consumes: `approved_candidate_ids` / `rejected_candidate_ids` from Task 1.
- Produces: `apply_approved_proposals` gains keyword-only `approved_candidate_ids: set[str] | None = None` and `rejected_candidate_ids: set[str] | None = None` (defaulting to the loaded log), and treats a proposal as approved when EITHER its `proposal_id` or its `candidate_id` is approved. **Reject always wins.** Audit rows gain `candidate_id`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_overlay_either_id_matching.py`:

```python
"""Either-id approval matching (Task 2).

A proposal is applied when its proposal_id OR its candidate_id carries a valid
human approval, so an unchanged fact re-proposed under a fresh proposal_id does
not need re-approval. Reject always beats approve — the human gate must never
loosen.
"""
from __future__ import annotations

from pathlib import Path

from portfolio_automation.sim_governance import production_application as PAP
from portfolio_automation.sim_governance import schemas as S


def _outputs(tmp_path: Path) -> str:
    d = tmp_path / "outputs"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _proposal(pid: str, cid: str, sym: str = "RIOT") -> dict:
    return {
        "proposal_id": pid,
        "candidate_id": cid,
        "proposal_type": S.PROPOSAL_WATCHLIST_REMOVE,
        "proposed_production_change": {"op": "remove", "symbol": sym},
        "rollback_plan": "delete the op and re-run the loader",
    }


_NOW = "2026-07-28T17:00:00+00:00"


def test_candidate_approval_applies_a_new_proposal_id(tmp_path):
    """The treadmill fix: yesterday's approval covers today's regenerated proposal."""
    res = PAP.apply_approved_proposals(
        _NOW, base_dir=_outputs(tmp_path),
        proposals=[_proposal("prop_TODAY", "cand_stable")],
        approved_ids=set(),                       # yesterday's proposal id is gone
        approved_candidate_ids={"cand_stable"},   # but the candidate is approved
        write_files=False,
    )
    assert res["applied_count"] == 1
    assert res["watchlist_applied"] == 1


def test_proposal_id_approval_still_works(tmp_path):
    """Backward compatibility: the 43 historical records key on proposal_id only."""
    res = PAP.apply_approved_proposals(
        _NOW, base_dir=_outputs(tmp_path),
        proposals=[_proposal("prop_X", "cand_X")],
        approved_ids={"prop_X"},
        approved_candidate_ids=set(),
        write_files=False,
    )
    assert res["applied_count"] == 1


def test_unapproved_candidate_is_not_applied(tmp_path):
    res = PAP.apply_approved_proposals(
        _NOW, base_dir=_outputs(tmp_path),
        proposals=[_proposal("prop_X", "cand_X")],
        approved_ids=set(), approved_candidate_ids=set(),
        write_files=False,
    )
    assert res["applied_count"] == 0
    assert res["ignored"][0]["reason"] == "pending_or_unapproved"


def test_candidate_reject_beats_proposal_id_approve(tmp_path):
    """Reject always wins — the gate must never loosen."""
    res = PAP.apply_approved_proposals(
        _NOW, base_dir=_outputs(tmp_path),
        proposals=[_proposal("prop_X", "cand_X")],
        approved_ids={"prop_X"},
        rejected_candidate_ids={"cand_X"},
        write_files=False,
    )
    assert res["applied_count"] == 0
    assert res["ignored"][0]["reason"] == "rejected"


def test_proposal_id_reject_beats_candidate_approve(tmp_path):
    res = PAP.apply_approved_proposals(
        _NOW, base_dir=_outputs(tmp_path),
        proposals=[_proposal("prop_X", "cand_X")],
        rejected_ids={"prop_X"},
        approved_candidate_ids={"cand_X"},
        write_files=False,
    )
    assert res["applied_count"] == 0
    assert res["ignored"][0]["reason"] == "rejected"


def test_proposal_without_candidate_id_is_unaffected(tmp_path):
    p = _proposal("prop_X", "cand_X")
    del p["candidate_id"]
    res = PAP.apply_approved_proposals(
        _NOW, base_dir=_outputs(tmp_path), proposals=[p],
        approved_ids={"prop_X"}, write_files=False,
    )
    assert res["applied_count"] == 1


def test_audit_row_carries_candidate_id(tmp_path):
    import json
    base = _outputs(tmp_path)
    PAP.apply_approved_proposals(
        _NOW, base_dir=base,
        proposals=[_proposal("prop_X", "cand_X")],
        approved_ids={"prop_X"}, write_files=True,
    )
    audit = Path(base) / "promotion_approvals" / "production_application_audit.jsonl"
    rows = [json.loads(l) for l in audit.read_text().splitlines() if l.strip()]
    applied = [r for r in rows if r.get("event") == "applied_to_production"]
    assert applied, "no applied_to_production row written"
    assert applied[-1]["candidate_id"] == "cand_X"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_overlay_either_id_matching.py -v`
Expected: FAIL — `apply_approved_proposals() got an unexpected keyword argument 'approved_candidate_ids'`, and `test_audit_row_carries_candidate_id` fails with `KeyError: 'candidate_id'`.

- [ ] **Step 3: Extend the signature and the decision logic**

In `production_application.py`, add two keyword-only params after `rejected_ids`:

```python
def apply_approved_proposals(
    now: str,
    *,
    base_dir: str,
    proposals: list[dict] | None = None,
    approved_ids: set[str] | None = None,
    rejected_ids: set[str] | None = None,
    approved_candidate_ids: set[str] | None = None,
    rejected_candidate_ids: set[str] | None = None,
    write_files: bool = True,
) -> dict:
```

Then the loader block currently reads:

```python
    proposals = proposals if proposals is not None else promotion_proposals.load_pending_proposals(base_dir)
    approved = approved_ids if approved_ids is not None else promotion_approvals.approved_proposal_ids(base_dir)
    rejected = rejected_ids if rejected_ids is not None else promotion_approvals.rejected_proposal_ids(base_dir)
```

Add the candidate-keyed sets:

```python
    proposals = proposals if proposals is not None else promotion_proposals.load_pending_proposals(base_dir)
    approved = approved_ids if approved_ids is not None else promotion_approvals.approved_proposal_ids(base_dir)
    rejected = rejected_ids if rejected_ids is not None else promotion_approvals.rejected_proposal_ids(base_dir)
    # Durable identity: proposal_id is clock-salted and churns every run, so an
    # unchanged fact would otherwise need re-approval daily. candidate_id is
    # stable-when-unchanged, so an approval filed against it stays effective.
    approved_cands = (approved_candidate_ids if approved_candidate_ids is not None
                      else promotion_approvals.approved_candidate_ids(base_dir))
    rejected_cands = (rejected_candidate_ids if rejected_candidate_ids is not None
                      else promotion_approvals.rejected_candidate_ids(base_dir))
```

Then the per-proposal decision currently reads:

```python
        if pid not in approved:
            reason = "rejected" if pid in rejected else "pending_or_unapproved"
            ignored.append({"proposal_id": pid, "reason": reason})
            continue
```

Replace it with either-id matching where reject always wins:

```python
        cid = p.get("candidate_id")
        # Reject wins over approve on BOTH identities — the gate never loosens.
        if pid in rejected or (cid and cid in rejected_cands):
            ignored.append({"proposal_id": pid, "reason": "rejected"})
            continue
        if pid not in approved and not (cid and cid in approved_cands):
            ignored.append({"proposal_id": pid, "reason": "pending_or_unapproved"})
            continue
```

- [ ] **Step 4: Add `candidate_id` to the audit row**

The audit row currently reads:

```python
                    fh.write(json.dumps({
                        "ts": now,
                        "event": "applied_to_production",
                        "proposal_id": o["proposal_id"],
                        "proposal_type": o["proposal_type"],
                        "change": o["change"],
                        "rollback_plan": o["rollback_plan"],
                        "snapshots": snapshots,
                    }, default=str) + "\n")
```

Add the candidate id (`_overlay_entry` already carries it), so the audit log is a
complete rebuild source for Task 3:

```python
                    fh.write(json.dumps({
                        "ts": now,
                        "event": "applied_to_production",
                        "proposal_id": o["proposal_id"],
                        "candidate_id": o.get("candidate_id"),
                        "proposal_type": o["proposal_type"],
                        "change": o["change"],
                        "rollback_plan": o["rollback_plan"],
                        "snapshots": snapshots,
                    }, default=str) + "\n")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_overlay_either_id_matching.py -v`
Expected: 7 passed.

- [ ] **Step 6: Run the targeted regression**

Run: `.venv/bin/python -m pytest tests/test_sim_governance.py tests/test_overlay_either_id_matching.py tests/test_promotion_approval_identity.py -q`
Expected: all pass. If an existing test asserted the old `pending_or_unapproved` reason for a *rejected* proposal, read it — the reason string for a rejected proposal is unchanged (`"rejected"`), so no existing expectation should move. If one does, report it rather than editing the test silently.

- [ ] **Step 7: Commit**

```bash
git add portfolio_automation/sim_governance/production_application.py tests/test_overlay_either_id_matching.py
git commit -m "feat(sim-gov): match approvals on either proposal_id or candidate_id

A proposal is now applied when EITHER its proposal_id or its candidate_id carries
a valid human approval, so an unchanged fact re-proposed under a fresh
clock-salted proposal_id no longer needs daily re-approval.

Reject always wins, on both identities — a reject against either id blocks the
apply even if the other id is approved. The gate cannot loosen: candidate ids are
matched exactly, never inferred from symbol or type, and an unapproved candidate
is still ignored as pending_or_unapproved.

Backward compatible: proposal_id matching is unchanged, so the 43 historical
records (which carry no candidate_id) keep working, and a proposal without a
candidate_id behaves exactly as before.

Also adds candidate_id to the applied_to_production audit row — _overlay_entry
already carried it, and it makes the audit log a complete rebuild source for the
durable-watchlist work.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Durable watchlist ops rebuilt from the audit log

**Files:**
- Modify: `portfolio_automation/sim_governance/production_application.py`
- Test: `tests/test_overlay_watchlist_durability.py`

**Interfaces:**
- Consumes: Task 2's either-id matching and the audit row's `candidate_id`.
- Produces: `_prior_watchlist_ops(base_dir, *, rejected, rejected_cands, revoked) -> list[dict]` reading `production_application_audit.jsonl`, and `apply_approved_proposals` unions today's approved watchlist ops with those prior ops (deduped, today's winning). Advisory ops are unchanged.

**Why:** watchlist ops are durable membership state; rebuilding them from today's pending set loses them. Advisory ops are current-state annotations and must keep refreshing.

- [ ] **Step 1: Write the failing test**

Create `tests/test_overlay_watchlist_durability.py`:

```python
"""Durable watchlist ops (Task 3).

Watchlist ops are durable membership state and must persist across runs even when
their candidate stops being proposed — which is exactly what happens after a
removal is applied and the producer correctly self-suppresses. Advisory ops are
current-state annotations and must keep refreshing.

A rejected or revoked op must NEVER be resurrected by the rebuild.
"""
from __future__ import annotations

import json
from pathlib import Path

from portfolio_automation.sim_governance import production_application as PAP
from portfolio_automation.sim_governance import schemas as S

_NOW = "2026-07-28T17:00:00+00:00"


def _outputs(tmp_path: Path) -> str:
    d = tmp_path / "outputs"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _audit(base_dir: str, rows: list[dict]) -> None:
    d = Path(base_dir) / "promotion_approvals"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "production_application_audit.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _applied_row(pid: str, cid: str, sym: str, ptype: str = S.PROPOSAL_WATCHLIST_REMOVE) -> dict:
    return {"ts": "2026-07-27T15:00:00+00:00", "event": "applied_to_production",
            "proposal_id": pid, "candidate_id": cid, "proposal_type": ptype,
            "change": {"op": "remove", "symbol": sym},
            "rollback_plan": "delete the op", "snapshots": {}}


def test_prior_applied_removal_persists_with_no_pending_proposal(tmp_path):
    """The core case: the producer self-suppresses, so nothing is proposed today."""
    base = _outputs(tmp_path)
    _audit(base, [_applied_row("prop_yesterday", "cand_riot", "RIOT")])

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[],       # nothing proposed today
        approved_ids={"prop_yesterday"},
        write_files=True,
    )

    assert res["watchlist_applied"] == 1
    ov = json.loads((Path(base) / "latest" / "approved_watchlist_proposals.json").read_text())
    assert [o["change"]["symbol"] for o in ov["ops"]] == ["RIOT"]


def test_rejected_prior_op_is_not_resurrected(tmp_path):
    base = _outputs(tmp_path)
    _audit(base, [_applied_row("prop_old", "cand_riot", "RIOT")])

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[],
        approved_ids=set(), rejected_ids={"prop_old"},
        write_files=False,
    )
    assert res["watchlist_applied"] == 0


def test_candidate_rejected_prior_op_is_not_resurrected(tmp_path):
    base = _outputs(tmp_path)
    _audit(base, [_applied_row("prop_old", "cand_riot", "RIOT")])

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[],
        approved_ids={"prop_old"}, rejected_candidate_ids={"cand_riot"},
        write_files=False,
    )
    assert res["watchlist_applied"] == 0


def test_unapproved_prior_op_is_not_resurrected(tmp_path):
    """An audit row alone is not authority — approval must still be present."""
    base = _outputs(tmp_path)
    _audit(base, [_applied_row("prop_old", "cand_riot", "RIOT")])

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[],
        approved_ids=set(), approved_candidate_ids=set(),
        write_files=False,
    )
    assert res["watchlist_applied"] == 0


def test_today_and_prior_ops_are_deduped_by_candidate(tmp_path):
    base = _outputs(tmp_path)
    _audit(base, [_applied_row("prop_old", "cand_riot", "RIOT")])
    today = {"proposal_id": "prop_new", "candidate_id": "cand_riot",
             "proposal_type": S.PROPOSAL_WATCHLIST_REMOVE,
             "proposed_production_change": {"op": "remove", "symbol": "RIOT"},
             "rollback_plan": "delete the op"}

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[today],
        approved_candidate_ids={"cand_riot"},
        write_files=False,
    )
    assert res["watchlist_applied"] == 1, "the same candidate must not be applied twice"


def test_advisory_ops_are_not_made_durable(tmp_path):
    """Advisory annotations must still refresh — a stale label would mislead."""
    base = _outputs(tmp_path)
    _audit(base, [{
        "ts": "2026-07-27T15:00:00+00:00", "event": "applied_to_production",
        "proposal_id": "prop_adv", "candidate_id": "cand_adv",
        "proposal_type": S.PROPOSAL_FLOCK_ADVISORY_CONTEXT,
        "change": {"op": "flock_context", "symbol": "GOOGL", "label": "stale"},
        "rollback_plan": "", "snapshots": {},
    }])

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[],
        approved_ids={"prop_adv"}, write_files=False,
    )
    assert res["advisory_applied"] == 0, "advisory ops must not persist from the audit log"


def test_missing_audit_log_degrades_to_empty(tmp_path):
    res = PAP.apply_approved_proposals(
        _NOW, base_dir=_outputs(tmp_path), proposals=[], write_files=False,
    )
    assert res["watchlist_applied"] == 0


def test_corrupt_audit_lines_are_skipped(tmp_path):
    base = _outputs(tmp_path)
    d = Path(base) / "promotion_approvals"
    d.mkdir(parents=True, exist_ok=True)
    (d / "production_application_audit.jsonl").write_text(
        "not json\n" + json.dumps(_applied_row("prop_ok", "cand_ok", "RIOT")) + "\n",
        encoding="utf-8")

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[], approved_ids={"prop_ok"}, write_files=False,
    )
    assert res["watchlist_applied"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_overlay_watchlist_durability.py -v`
Expected: `test_prior_applied_removal_persists_with_no_pending_proposal`,
`test_today_and_prior_ops_are_deduped_by_candidate` and
`test_corrupt_audit_lines_are_skipped` FAIL with `watchlist_applied == 0` — the
rebuild does not read the audit log yet. The negative tests pass already.

- [ ] **Step 3: Add the prior-ops reader**

Insert into `production_application.py` above `apply_approved_proposals`:

```python
def _prior_watchlist_ops(
    base_dir: str,
    *,
    approved: set[str],
    approved_cands: set[str],
    rejected: set[str],
    rejected_cands: set[str],
    revoked: set[str],
) -> list[dict]:
    """Previously-applied WATCHLIST ops, from the append-only audit log.

    Watchlist ops are durable membership state: once applied they must survive
    runs in which their candidate is no longer proposed — which is precisely what
    happens after a removal is applied and the producer self-suppresses. Advisory
    ops are deliberately NOT included: they annotate today's decision rows from
    live state and must refresh.

    An audit row is not authority on its own. A prior op is carried forward only
    while it is still backed by a valid human approval and has not been rejected
    or revoked.

    Tolerant: a missing or partly-corrupt log yields whatever rows parse.
    """
    path = Path(get_output_path(OutputNamespace.PROMOTION_APPROVALS, _AUDIT_FILE,
                                base_dir=base_dir))
    if not path.exists():
        return []

    seen: dict[str, dict] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict) or row.get("event") != "applied_to_production":
                continue
            ptype = row.get("proposal_type")
            if not S.is_valid_proposal_type(ptype):
                continue
            if S.workflow_for_proposal_type(ptype) != S.WORKFLOW_WATCHLIST:
                continue
            pid = row.get("proposal_id")
            cid = row.get("candidate_id")
            if pid in revoked or (cid and cid in revoked):
                continue
            if pid in rejected or (cid and cid in rejected_cands):
                continue
            if pid not in approved and not (cid and cid in approved_cands):
                continue
            key = str(cid or pid)
            seen[key] = {
                "proposal_id": pid,
                "candidate_id": cid,
                "proposal_type": ptype,
                "change": row.get("change", {}),
                "rollback_plan": row.get("rollback_plan", ""),
                "applied_from": "human_approved_promotion_proposal",
            }
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("production_application: prior-ops read failed: %s", exc)
        return []
    return list(seen.values())
```

- [ ] **Step 4: Union prior ops into the watchlist overlay**

After the per-proposal loop finishes building `watchlist_ops` / `advisory_ops`,
and BEFORE `watchlist_overlay` is constructed, add:

```python
    # Watchlist membership is durable: carry forward previously-applied ops that
    # are still approved and not rejected/revoked. Today's op wins on a clash, so
    # a re-proposed candidate is not applied twice. Advisory ops are untouched —
    # they must refresh from the current pending set.
    _today_keys = {str(o.get("candidate_id") or o.get("proposal_id")) for o in watchlist_ops}
    _revoked = promotion_approvals.revoked_ids(base_dir) if hasattr(
        promotion_approvals, "revoked_ids") else set()
    for _prior in _prior_watchlist_ops(
        base_dir,
        approved=approved, approved_cands=approved_cands,
        rejected=rejected, rejected_cands=rejected_cands,
        revoked=_revoked,
    ):
        if str(_prior.get("candidate_id") or _prior.get("proposal_id")) not in _today_keys:
            watchlist_ops.append(_prior)
```

The `hasattr` guard keeps this task independently runnable; Task 4 adds
`revoked_ids` and the guard then resolves to the real set.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_overlay_watchlist_durability.py -v`
Expected: 8 passed.

- [ ] **Step 6: Prove the durability end-to-end against a real scenario**

Run:

```bash
.venv/bin/python - <<'PY'
import json, tempfile, pathlib
from portfolio_automation.sim_governance import production_application as PAP
from portfolio_automation.sim_governance import schemas as S
with tempfile.TemporaryDirectory() as td:
    base = str(pathlib.Path(td) / "outputs")
    p = {"proposal_id": "prop_d1", "candidate_id": "cand_riot",
         "proposal_type": S.PROPOSAL_WATCHLIST_REMOVE,
         "proposed_production_change": {"op": "remove", "symbol": "RIOT"},
         "rollback_plan": "delete the op"}
    # Day 1: proposed + approved + applied
    r1 = PAP.apply_approved_proposals("2026-07-28T09:00:00+00:00", base_dir=base,
            proposals=[p], approved_ids={"prop_d1"}, write_files=True)
    # Day 2: producer self-suppresses -> nothing proposed
    r2 = PAP.apply_approved_proposals("2026-07-29T09:00:00+00:00", base_dir=base,
            proposals=[], approved_ids={"prop_d1"}, write_files=True)
    ov = json.loads((pathlib.Path(base)/"latest"/"approved_watchlist_proposals.json").read_text())
    print("day1 watchlist_applied:", r1["watchlist_applied"])
    print("day2 watchlist_applied:", r2["watchlist_applied"])
    print("day2 overlay symbols  :", [o["change"]["symbol"] for o in ov["ops"]])
    assert r2["watchlist_applied"] == 1, "REGRESSION: the op did not persist"
    print("OK - removal survived a run with no pending proposal")
PY
```

Expected: both days report `1`, day-2 overlay still contains `RIOT`, and the
final `OK` line prints. Before this task, day 2 would have been `0`.

- [ ] **Step 7: Run the targeted regression**

Run: `.venv/bin/python -m pytest tests/test_sim_governance.py tests/test_overlay_watchlist_durability.py tests/test_overlay_either_id_matching.py tests/test_promotion_approval_identity.py -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add portfolio_automation/sim_governance/production_application.py tests/test_overlay_watchlist_durability.py
git commit -m "feat(sim-gov): watchlist overlay ops are durable

apply_approved_proposals rebuilt BOTH overlays from empty lists out of today's
pending set, so an applied op whose candidate was no longer proposed vanished
from production silently. For watchlist membership that is data loss; for
advisory annotations it is correct refresh behavior. One policy was applied to
two workflows with opposite requirements.

Watchlist ops are now unioned with previously-applied ops read from the
append-only audit log (_prior_watchlist_ops), deduped by candidate_id with
today's op winning. Advisory ops are deliberately excluded from the rebuild and
keep replace-from-pending semantics.

An audit row is NOT authority on its own: a prior op is carried forward only
while still backed by a valid human approval and not rejected or revoked, so a
rejected or unapproved op can never be resurrected (three tests cover those
cases). Tolerant of a missing or partly-corrupt log.

Proven end-to-end: a removal applied on day 1 survives a day-2 run in which the
producer correctly self-suppresses and proposes nothing.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Explicit revoke path

**Files:**
- Modify: `portfolio_automation/sim_governance/promotion_approvals.py`
- Test: `tests/test_overlay_revoke.py`

**Interfaces:**
- Consumes: Task 3's `revoked` parameter (currently reached via a `hasattr` guard).
- Produces:
  - `revoke_application(target_id: str, approver: str, now: str, *, base_dir: str, notes: str | None = None, write_files: bool = True) -> dict` returning `{"ok", "reason", "record"}`. `target_id` may be a `proposal_id` OR a `candidate_id`. Requires a human approver.
  - `revoked_ids(base_dir) -> set[str]` — all target ids with a valid revoke record.
- After this task, remove the `hasattr` guard added in Task 3 Step 4 and call `promotion_approvals.revoked_ids(base_dir)` directly.

**Why:** operator decision (spec §3) is **explicit revoke only** — evidence going stale never auto-restores a symbol. Without a revoke path an applied removal is irreversible in practice.

- [ ] **Step 1: Write the failing test**

Create `tests/test_overlay_revoke.py`:

```python
"""Explicit revoke path (Task 4).

Operator decision: an applied watchlist op persists until a recorded human
decision reverses it. Data drift never restores a symbol. Revocation must itself
be human-gated — an AI marker cannot revoke, just as it cannot approve.
"""
from __future__ import annotations

import json
from pathlib import Path

from portfolio_automation.sim_governance import production_application as PAP
from portfolio_automation.sim_governance import promotion_approvals as PA
from portfolio_automation.sim_governance import schemas as S

_NOW = "2026-07-29T09:00:00+00:00"


def _outputs(tmp_path: Path) -> str:
    d = tmp_path / "outputs"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _audit(base_dir: str, rows: list[dict]) -> None:
    d = Path(base_dir) / "promotion_approvals"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "production_application_audit.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _applied_row(pid: str, cid: str, sym: str) -> dict:
    return {"ts": "2026-07-28T09:00:00+00:00", "event": "applied_to_production",
            "proposal_id": pid, "candidate_id": cid,
            "proposal_type": S.PROPOSAL_WATCHLIST_REMOVE,
            "change": {"op": "remove", "symbol": sym},
            "rollback_plan": "delete the op", "snapshots": {}}


def test_revoke_is_recorded(tmp_path):
    base = _outputs(tmp_path)
    res = PA.revoke_application("cand_riot", "pesantez", _NOW, base_dir=base)
    assert res["ok"] is True, res["reason"]
    assert PA.revoked_ids(base) == {"cand_riot"}


def test_ai_cannot_revoke(tmp_path):
    """Revocation is human-gated, exactly like approval."""
    base = _outputs(tmp_path)
    res = PA.revoke_application("cand_riot", "auto_approval", _NOW, base_dir=base)
    assert res["ok"] is False
    assert PA.revoked_ids(base) == set()


def test_revoked_op_is_dropped_from_the_overlay(tmp_path):
    base = _outputs(tmp_path)
    _audit(base, [_applied_row("prop_old", "cand_riot", "RIOT")])
    PA.revoke_application("cand_riot", "pesantez", _NOW, base_dir=base)

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[], approved_ids={"prop_old"},
        write_files=False,
    )
    assert res["watchlist_applied"] == 0, "a revoked op must not persist"


def test_revoke_by_proposal_id_also_works(tmp_path):
    base = _outputs(tmp_path)
    _audit(base, [_applied_row("prop_old", "cand_riot", "RIOT")])
    PA.revoke_application("prop_old", "pesantez", _NOW, base_dir=base)

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[], approved_ids={"prop_old"},
        write_files=False,
    )
    assert res["watchlist_applied"] == 0


def test_unrevoked_sibling_op_survives_a_revoke(tmp_path):
    """Revoking one symbol must not drop the others."""
    base = _outputs(tmp_path)
    _audit(base, [_applied_row("prop_a", "cand_riot", "RIOT"),
                  _applied_row("prop_b", "cand_tsla", "TSLA")])
    PA.revoke_application("cand_riot", "pesantez", _NOW, base_dir=base)

    res = PAP.apply_approved_proposals(
        _NOW, base_dir=base, proposals=[],
        approved_ids={"prop_a", "prop_b"}, write_files=False,
    )
    assert res["watchlist_applied"] == 1


def test_missing_revoke_log_degrades_to_empty(tmp_path):
    assert PA.revoked_ids(_outputs(tmp_path)) == set()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_overlay_revoke.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'revoke_application'`.

- [ ] **Step 3: Implement the revoke path**

Append to `promotion_approvals.py`:

```python
_REVOCATIONS_FILE = "production_revocations.json"


def revoke_application(
    target_id: str,
    approver: str,
    now: str,
    *,
    base_dir: str,
    notes: str | None = None,
    write_files: bool = True,
) -> dict:
    """Record a human revocation of a previously-applied production op.

    ``target_id`` may be a proposal_id OR a candidate_id — both are matched when
    the durable watchlist overlay is rebuilt.

    Revocation is the ONLY thing that un-applies a durable watchlist op: evidence
    going stale never restores a symbol, so production membership changes only on
    a recorded human decision. Human-gated exactly like approval.

    Returns {"ok": bool, "reason": str, "record": dict|None}.
    """
    if not target_id:
        return {"ok": False, "reason": "missing target_id", "record": None}
    if not S.is_human_approver(approver):
        logger.warning("promotion_approvals: rejecting non-human revocation by %r", approver)
        return {"ok": False,
                "reason": f"approver {approver!r} is not a valid human approver",
                "record": None}
    if not now:
        return {"ok": False, "reason": "missing timestamp", "record": None}

    record = {"target_id": target_id, "approver": approver,
              "timestamp": now, "notes": notes}

    if write_files and _looks_like_repo_root(base_dir):
        return {"ok": False,
                "reason": f"base_dir_is_repo_root: {base_dir!r}; pass <root>/outputs",
                "record": None}

    if write_files:
        existing = []
        try:
            path = Path(get_output_path(OutputNamespace.PROMOTION_APPROVALS,
                                        _REVOCATIONS_FILE, base_dir=base_dir))
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    existing = list(data.get("revocations", []) or [])
        except Exception:
            existing = []
        payload = {"generated_at": now, "schema": "production_revocations.v1",
                   "note": "Human revocations only. Revoking un-applies a durable "
                           "production op; evidence going stale never does.",
                   "revocations": existing + [record]}
        try:
            safe_write_json(OutputNamespace.PROMOTION_APPROVALS, _REVOCATIONS_FILE,
                            payload, base_dir=base_dir)
        except Exception as exc:
            logger.warning("promotion_approvals: revocation write failed: %s", exc)
            return {"ok": False, "reason": f"write_failed: {exc}", "record": record}

    return {"ok": True, "reason": "ok", "record": record}


def revoked_ids(base_dir: str) -> set[str]:
    """Target ids (proposal_id or candidate_id) with a valid human revocation."""
    try:
        path = Path(get_output_path(OutputNamespace.PROMOTION_APPROVALS,
                                    _REVOCATIONS_FILE, base_dir=base_dir))
        if not path.exists():
            return set()
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return set()
    if not isinstance(data, dict):
        return set()
    out: set[str] = set()
    for rec in data.get("revocations", []) or []:
        if not isinstance(rec, dict):
            continue
        tid = rec.get("target_id")
        if tid and rec.get("timestamp") and S.is_human_approver(rec.get("approver")):
            out.add(str(tid))
    return out
```

If `json`, `Path`, `safe_write_json`, `get_output_path` or `OutputNamespace` are
not already imported in this module, add the missing imports alongside the
existing ones (`record_approval` already uses `safe_write_json` and
`OutputNamespace`).

- [ ] **Step 4: Remove the `hasattr` guard from Task 3**

In `production_application.py`, this line was written defensively so Task 3 could
run standalone:

```python
    _revoked = promotion_approvals.revoked_ids(base_dir) if hasattr(
        promotion_approvals, "revoked_ids") else set()
```

Replace it with the direct call now that the function exists:

```python
    _revoked = promotion_approvals.revoked_ids(base_dir)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_overlay_revoke.py -v`
Expected: 6 passed.

- [ ] **Step 6: Run the targeted regression**

Run: `.venv/bin/python -m pytest tests/test_sim_governance.py tests/test_overlay_revoke.py tests/test_overlay_watchlist_durability.py tests/test_overlay_either_id_matching.py tests/test_promotion_approval_identity.py -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add portfolio_automation/sim_governance/promotion_approvals.py portfolio_automation/sim_governance/production_application.py tests/test_overlay_revoke.py
git commit -m "feat(sim-gov): explicit revoke path for applied production ops

Durable watchlist ops persist until a recorded human decision reverses them
(operator decision: evidence going stale must never auto-restore a symbol, so
production membership changes only on a recorded human decision). Without a
revoke path an applied removal was irreversible in practice.

revoke_application(target_id, approver, now, base_dir=...) accepts either a
proposal_id or a candidate_id and appends to production_revocations.json;
revoked_ids() feeds the durable-overlay rebuild. Human-gated exactly like
approval — is_human_approver must pass, so an AI marker cannot revoke. Keeps
record_approval's repo-root base_dir guard.

Also drops the hasattr shim Task 3 used so it could run before this existed.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Health pairing and full-suite verification

**Files:**
- Modify: `.claude/commands/daily-tool-analysis.md`
- Modify: `docs/superpowers/specs/2026-07-28-promotion-overlay-durability-design.md` (status)

**Interfaces:** none — operator-facing surface only.

**Why:** CLAUDE.md's Analysis + Health Coverage Requirement. The pipeline runs daily, so `daily-tool-analysis` owns the check.

- [ ] **Step 1: Read the current 6n / 6q sections**

Run: `grep -n "6n. Sim-governance\|6q. Operator approval packet" -A 8 .claude/commands/daily-tool-analysis.md`

Note the exact heartbeat grammar so the addition matches.

- [ ] **Step 2: Add the durability heartbeat**

Insert after the `6q.` block in `.claude/commands/daily-tool-analysis.md`:

```markdown
6r. Overlay durability (always when `outputs/promotion_approvals/production_application_audit.jsonl` exists; else `"Overlay-durability: inert (nothing applied yet)"`): `"Overlay-durability: {n} durable watchlist op(s) live · {k} revoked · {m} approval(s) with no live op"` — the durable-overlay layer (added 2026-07-28). Watchlist ops are membership state and persist across runs until explicitly revoked; advisory ops refresh by design and are NOT expected to persist. **AMBER** when: an `applied_to_production` watchlist op is absent from the current `approved_watchlist_proposals.json` while carrying no revocation (durability regression — the exact defect this layer fixed), OR a valid approval exists whose candidate has produced no live op for > 2 runs (approval not taking effect). **RED** only on a contract breach: a live overlay op with NO matching valid human approval (`is_human_approver` must pass), or a revoked target still present in the overlay. Advisory ops refreshing away is the expected steady state — never alert on it. Source: `production_application_audit.jsonl` + `production_revocations.json` + `approved_proposals.json` + `outputs/latest/approved_watchlist_proposals.json`.
```

- [ ] **Step 3: Mark the spec implemented**

Change the spec's status line from:

```markdown
**Status:** approved (operator, 2026-07-28) — not yet implemented
```

to:

```markdown
**Status:** implemented 2026-07-28 (plan: `docs/superpowers/plans/2026-07-28-promotion-overlay-durability.md`)
```

- [ ] **Step 4: Verify every symbol the docs name actually exists**

Run:

```bash
.venv/bin/python -c "
from portfolio_automation.sim_governance import promotion_approvals as PA
from portfolio_automation.sim_governance import production_application as PAP
from portfolio_automation.sim_governance import schemas as S
from portfolio_automation.sim_governance.auto_approval import _WATCHLIST_ELIGIBLE_TYPES
for n in ('revoke_application','revoked_ids','effective_approvals_by_candidate',
          'approved_candidate_ids','rejected_candidate_ids'):
    assert hasattr(PA, n), n
assert hasattr(PAP, '_prior_watchlist_ops')
assert S.PROPOSAL_WATCHLIST_REMOVE not in _WATCHLIST_ELIGIBLE_TYPES, 'authority invariant broken'
print('all symbols exist; authority invariant holds')
"
```

Expected: the confirmation line, no assertion error.

- [ ] **Step 5: Confirm the real approval log still resolves**

Run:

```bash
.venv/bin/python -c "
from portfolio_automation.sim_governance import promotion_approvals as PA
b='/opt/stockbot/outputs'
print('valid approvals :', len(PA.load_valid_approvals(b)))
print('approved by pid :', len(PA.approved_proposal_ids(b)))
print('revoked ids     :', len(PA.revoked_ids(b)))
"
```

Expected: `valid approvals` and `approved by pid` non-zero (the 43 historical
records still resolve); `revoked ids` is `0`. A drop in the first two means
backward compatibility broke — stop and report.

- [ ] **Step 6: Back up the registry, then run the full suite**

```bash
cp config/signal_registry.yaml /tmp/sr_durability.bak
LOG=/tmp/durability_full_suite.log
nohup .venv/bin/python -m pytest -q > "$LOG" 2>&1 &
# then poll: until grep -qE "passed|failed|error" "$LOG"; do sleep 5; done
```

Do NOT run it as a blocking foreground call. When it finishes, compare against
the 9-failure baseline and restore the registry if it differs:

```bash
diff -q /tmp/sr_durability.bak config/signal_registry.yaml || cp /tmp/sr_durability.bak config/signal_registry.yaml
```

Expected: 9 failures, all from the documented baseline set. Report exact counts
and name every failure. Anything beyond the 9 is a regression this branch caused.

- [ ] **Step 7: Commit**

```bash
git add .claude/commands/daily-tool-analysis.md docs/superpowers/specs/2026-07-28-promotion-overlay-durability-design.md
git commit -m "docs(health): pair overlay durability with a daily check

CLAUDE.md Analysis + Health Coverage Requirement: the promotion pipeline runs
daily, so daily-tool-analysis owns the check.

Adds a 6r heartbeat reporting live durable watchlist ops, revocations, and
approvals with no corresponding live op. AMBER on a durability regression (an
applied watchlist op missing from the overlay with no revocation) or an approval
that never takes effect. RED reserved for contract breaches: a live op with no
valid human approval, or a revoked target still present. Advisory ops refreshing
away is the expected steady state and never alerts.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Post-implementation (operator actions, not tasks)

1. Re-applying the GOOGL/MSFT advisory annotations is unnecessary — advisory refresh is by design. If they are still wanted tomorrow the sim lane will re-propose them, and after Task 1+2 an unchanged fact stays approved rather than needing a fresh decision.
2. The durable path only takes effect for approvals recorded *with* a `candidate_id`, i.e. those made after Task 1 ships. Historical approvals keep working through `proposal_id`.
3. `revoke_application` is deliberately CLI-only in this plan; a GUI control is a follow-up.

## Follow-ups (out of scope, from the spec)

1. Re-point the `>7 days pending` backlog-age check at a durable timestamp — it currently cannot fire because `created_at` is re-stamped every run.
2. `promotion_proposals._rollback_plan_for` for `watchlist_remove` (`promotion_proposals.py:41-42`) describes the inverse of the real mechanic.
3. `register_universe_composition_break` does a blind read-modify-write of the quant-watch ledger with no compare-and-swap.
