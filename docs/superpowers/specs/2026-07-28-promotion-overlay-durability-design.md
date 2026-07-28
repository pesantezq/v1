# Promotion-Overlay Durability — Design

**Date:** 2026-07-28
**Status:** approved (operator, 2026-07-28) — not yet implemented
**Scope:** `sim_governance` promotion pipeline: approval identity + overlay persistence
**Branch:** `fix/promotion-overlay-durability` (off `main`; independent of the held
`feat/watchlist-decay-removals`)

---

## 1. Problem

Two coupled defects in the human-gated promotion pipeline. Both are
**pre-existing** — neither was introduced by the watchlist-removal work, which
merely exercised the path hard enough to expose them.

### Defect A — the re-approval treadmill

`schemas.make_candidate_id` (`schemas.py:245`) is documented as *"Stable candidate
id derived from its identity, not from a clock"* and is genuinely stable: the
flock-advisory producer salts it with the **flock state**
(`simulation_lane.py:268`, `salt=state`), so the same symbol in the same state
yields the same `candidate_id` on every run, and a genuine state change yields a
new one. That is exactly the right identity semantics.

But `schemas.make_proposal_id(candidate_id, stamp)` (`schemas.py:252`) hashes
`candidate_id|stamp`, and every caller passes `now`. So the **proposal** id is
new on every run even when the underlying fact is identical. Approval records are
keyed on `proposal_id` and carry no `candidate_id`:

```
{proposal_id, decision, approver, timestamp, notes, review_date}
```

Consequence: an approval can never match a later regeneration of the same
unchanged candidate. The operator must re-approve an identical annotation every
single run, forever.

Evidence: `outputs/promotion_approvals/approved_proposals.json` holds **43
approvals**, while `outputs/latest/approved_watchlist_proposals.json` is
`ops: []`.

### Defect B — durable state rebuilt from an ephemeral flow

`production_application.apply_approved_proposals`
(`production_application.py:104-146`) initializes `watchlist_ops` and
`advisory_ops` as **empty lists** and repopulates them from the *current*
`pending_proposals.json` intersected with the approvals log, then writes both
overlays as full replacements. `promotion_proposals.generate_proposals`
(`promotion_proposals.py:124-137`) overwrites that pending file with only
*today's* READY candidates.

So any applied op whose candidate is absent from today's pending set silently
disappears from production.

Evidence: XOM/CVX applied `2026-07-27T15:32:40` (per
`production_application_audit.jsonl`) are absent from the current
`approved_advisory_proposals.json`, which holds only GOOGL/MSFT applied
`2026-07-28T13:38:32`.

---

## 2. The key insight: two workflows, opposite requirements, one policy

The pipeline applies a single persistence policy to two workflows that need
opposite behavior. This is why the current replace-everything semantics looks
correct in one place and broken in the other.

| Workflow | Consumer | Nature | Correct policy |
|---|---|---|---|
| `WORKFLOW_ADVISORY` | `main.py:2100-2111` — annotates *today's* decision rows with `overlay_context` / `overlay_rank_hint` / `overlay_strategy` | A **current-state annotation**, derived from live flock state (the candidate salt *is* the state) | **Refresh.** A stale "flock confirmed" label after the state changed would actively mislead. |
| `WORKFLOW_WATCHLIST` | `watchlist_scanner/__main__.py:224` — mutates the scanned universe | **Durable membership state** | **Persist until revoked.** A removal is a decision, not an observation. |

So Defect B is not "the overlay should accumulate." It is: *advisory refresh is
correct and must be kept; watchlist membership must become durable.*

This also refines Defect A per workflow:
- Advisory: the annotation refreshing is right, but demanding **re-approval of an
  unchanged fact** is the bug.
- Watchlist: the op vanishing at all is the bug.

---

## 3. Design

### Part 1 — durable approval identity

Record `candidate_id` in the approval record alongside `proposal_id`, and match
approvals on **either**. `candidate_id` already carries the correct semantics:
stable while the fact is unchanged, new when it changes.

- Additive and backward compatible: the 43 existing records carry no
  `candidate_id`, so `proposal_id` matching must continue to work unchanged.
- Effect: approve a fact once and it stays approved while the fact holds. A
  genuine change mints a new `candidate_id` that requires fresh human approval.

### Part 2 — persistence policy per workflow

- **Advisory:** unchanged. Rebuilt each run from the current pending set
  (refresh semantics preserved).
- **Watchlist:** rebuilt from the append-only
  `production_application_audit.jsonl`, which already carries everything needed —
  `proposal_id`, `proposal_type`, the full `change` payload, and `rollback_plan`
  — minus anything explicitly revoked.

### Part 3 — revocation (operator decision, 2026-07-28)

**Explicit revoke only.** An applied watchlist op persists indefinitely until a
recorded human decision reverses it: a reject/veto against that candidate, or a
rollback. **Evidence going stale never auto-restores a symbol.** Production
membership changes only on a recorded human decision, never on data drift.

Requires a revoke path plus an `applied_to_production`-symmetric audit event so
the rebuild can subtract it.

---

## 4. Invariants that must hold

1. **Human gate preserved.** Only human-approved ops are ever applied.
   `schemas.is_human_approver` unchanged; `auto_approval._WATCHLIST_ELIGIBLE_TYPES`
   unchanged. Matching on `candidate_id` does **not** weaken the gate — a changed
   fact is a different candidate and needs fresh approval.
2. **A rejected or revoked op is never resurrected** by the audit-log rebuild.
   This is the primary risk of Part 2 and needs a dedicated test.
3. **Observe-only elsewhere.** `decision_engine.py`, scoring logic,
   `_TRACKED_KNOBS`, and all score semantics untouched.
4. **Additive / backward compatible.** Existing approval records keep working;
   overlay schemas keep their current shape and `feeds_production` flag.
5. **Advisory refresh unchanged.** No stale annotation may persist past a state
   change.

---

## 5. Risks

| Risk | Mitigation |
|---|---|
| Audit-log rebuild resurrects a rejected/revoked op | Subtract rejected ids + revoke events; dedicated test per case |
| Watchlist ops accumulate unbounded | Bounded by universe size; log-derived so it is replayable and auditable |
| No revoke path exists today | Part 3 adds one; without it a removal is irreversible in practice |
| Existing 43 records lack `candidate_id` | Match-on-either; regression test over the real historical shape |
| Approval matching becomes looser than intended | `candidate_id` must be matched exactly, never by symbol or type alone |

---

## 6. Out of scope

- The watchlist-removal gate's statistical rule — that is the held
  `feat/watchlist-decay-removals` branch and its own superseding revision.
- Changing `make_proposal_id`'s signature or dropping its clock salt.
  Part 1 achieves durability without touching existing id semantics, so
  historical ids stay valid.
- The `>7 days pending` health trigger, which cannot currently fire because
  `created_at` is re-stamped every run. It becomes fixable once identity is
  durable; recorded as a follow-up rather than bundled here.

---

## 7. Follow-ups

1. Re-point the `6n4` / backlog-age health checks at a durable timestamp once
   Part 1 lands (derive age from `proposals_log.jsonl`, or from the earliest
   approval for the candidate).
2. `promotion_proposals._rollback_plan_for` for `watchlist_remove`
   (`promotion_proposals.py:41-42`) reads *"Restore {sym} to the approved-watchlist
   overlay"* — the inverse of the actual mechanic. An operator following it
   literally would author an ADD op needing its own approval; the real reversal is
   deleting the op or rolling back the overlay. Pre-existing text, first made
   load-bearing by the removal work.
3. `register_universe_composition_break` does a blind read-modify-write of the
   shared quant-watch ledger with no compare-and-swap; a manual invocation racing
   the 09:11 UTC quant-watch run loses the probe while still returning
   `registered: True`.
