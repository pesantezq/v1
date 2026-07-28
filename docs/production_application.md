# Simulation Governance — Production Application

## Purpose

`portfolio_automation/sim_governance/production_application.py` materializes
human-approved proposals into the production overlay artifacts that the live
watchlist/advisory loaders consume. This is the one place where an approved
promotion becomes a (gated, default-OFF) production input.

---

## Two-Lane Governance

It applies **only** human-approved proposals. By construction it IGNORES: raw
simulation artifacts, pending proposals, rejected proposals, and invalid
approvals (bad metadata / AI self-approval). Every applied change carries its
originating `proposal_id` and a rollback plan; every application event is
appended to an audit trail; and before overwriting an overlay the prior version
is snapshotted so a single-call rollback can restore it (mirrors
`backtesting/registry_apply`'s snapshot-then-write discipline). The overlays
themselves only take effect when the corresponding `production_overlays` loader
flag is turned on. `decision_engine.py` and score semantics are never touched.

---

## Artifacts Written

| File | Namespace | Path |
|------|-----------|------|
| `approved_watchlist_proposals.json` | LATEST | `outputs/latest/` (consumed by prod loader) |
| `approved_advisory_proposals.json` | LATEST | `outputs/latest/` (consumed by prod loader) |
| `production_application_audit.jsonl` | PROMOTION_APPROVALS | `outputs/promotion_approvals/` (append-only) |
| `production_application_state.json` | PROMOTION_APPROVALS | `outputs/promotion_approvals/` (current state) |
| `production_revocations.jsonl` | PROMOTION_APPROVALS | `outputs/promotion_approvals/` (append-only; written by `promotion_approvals.revoke_application`) |
| `snapshots/<overlay>.<stamp>.json` | PROMOTION_APPROVALS | `outputs/promotion_approvals/snapshots/` (rollback) |

The two overlay artifacts carry `feeds_production: true`, the
`applied_proposal_ids`, and the `ops` (each with provenance + `rollback_plan`).

---

## Durability — which ops persist, and which refresh

Durability is a property of the **proposal type**, not of the workflow (operator
decision 2026-07-28: the staleness rule follows the data, not the folder).

| Class | Types | Policy |
|---|---|---|
| Membership **decision** | `watchlist_add`, `watchlist_remove`, `watchlist_rank_change`, `watchlist_tag_change` (`_DURABLE_PROPOSAL_TYPES`) | **Durable.** Rebuilt from the append-only audit log every run, so an applied op survives runs in which its candidate is no longer proposed. Reversed only by a recorded human decision. |
| State-derived **label** | `flock_watchlist_candidate_logic`, all advisory / crowd / flock advisory+risk+scoring types | **Refresh.** Rebuilt from the *current* pending set only. Their `candidate_id` is salted by the state that produced them, so persisting one would keep a stale label alive past the state change. |

`discovery_candidate_promotion` is a membership add in shape but is not in the
durable set and has no producer today; it keeps refresh semantics.

Conflicts are resolved **before** the overlay is written (`_resolve_durable_ops`),
not left to the loader's last-writer-wins fold: one op per
`(symbol, proposal_type)` with the most recent winning, one membership direction
per symbol (add vs remove resolved by recency), and survivors emitted oldest-first.
Today's op always beats a carried one for the same fact.

Fail-closed: if `approved_proposals.json` **exists but cannot be parsed**, the
rebuild is REFUSED — the existing overlays are left untouched and the state reports
`overlay_rebuild_skipped: true` + `approvals_log_unreadable`. An *absent* log still
means "no approvals yet". This exists because an empty approval set would otherwise
drop every carried op and write `ops: []`, silently reversing established
production membership.

---

## Reversing an applied op — `revoke_application`

**This is the only working reversal path for a durable op.** Deleting the overlay
entry or hand-editing `approved_watchlist_proposals.json` is silently undone on the
next run, because durable ops are rebuilt from the audit log.

```
from portfolio_automation.sim_governance import promotion_approvals as PA
PA.revoke_application("<candidate_id or proposal_id>", "<human_approver>",
                      "<iso-now>", base_dir="outputs", notes="why")
```

- Human-gated exactly like approval: `schemas.is_human_approver` must pass, so the
  AI reviewer and the `auto_approval` marker are rejected.
- `target_id` may be a `proposal_id` OR a `candidate_id`; both are matched when the
  durable overlay is rebuilt. The id is matched EXACTLY — never inferred from symbol
  or type.
- Appends one line to `production_revocations.jsonl` (append-only, so a concurrent
  revocation cannot be lost and one corrupt line cannot destroy history).
- Applies to BOTH paths: a revoked target is skipped in the carry-forward *and* in
  today's per-proposal loop, so a revoked target still sitting in
  `pending_proposals.json` is not re-applied.
- Evidence going stale never restores a symbol — production membership changes only
  on a recorded human decision.
- To re-establish a revoked op, approve a NEW proposal through the normal flow.

`rollback_last` is the other reversal, at overlay granularity: it restores the most
recent snapshot and records a `rolled_back` audit event carrying `rolled_back_ids`,
which the carry-forward honours so the rollback is not undone on the next run.

---

## Key Functions

- `apply_approved_proposals(now, *, base_dir, proposals=None, approved_ids=None,
  rejected_ids=None, approved_candidate_ids=None, rejected_candidate_ids=None,
  write_files=True) -> dict` — routes each approved proposal to the watchlist or
  advisory overlay (by `workflow_for_proposal_type`), carries forward + supersedes
  durable ops, snapshots the prior overlays, writes the new overlays, appends audit
  rows, and writes the application state. Inputs default to the persisted pending
  set + validated approval log. Approvals match on `proposal_id` OR the durable
  `candidate_id`; reject and revoke win over approve on both identities.
- `is_durable_proposal_type(ptype) -> bool` — the durability predicate. Use this,
  never `workflow_for_proposal_type(...) == WORKFLOW_WATCHLIST`.
- `rollback_last(filename, base_dir, now) -> dict` — restores the most recent
  snapshot of an overlay and records a `rolled_back` audit event with
  `rolled_back_ids`.
- `_prior_durable_ops(...)` / `_resolve_durable_ops(...)` — audit-log carry-forward
  and explicit conflict resolution. An audit row is not authority: a carried op must
  still be backed by a valid human approval and not be rejected/revoked/rolled back.
- `_overlay_entry(proposal)` / `_snapshot_existing(...)` — provenance entry
  construction and rollback snapshotting.

### State fields for operator surfaces

`applied_count` / `applied_today_count` count only **this run's** approvals — they
read 0 on a quiet day while durable ops are still in force. `durably_live_count`
(= `watchlist_applied`) is the honest "live in production" figure;
`watchlist_applied_today` and `watchlist_carried_forward` split it. The GUI
Governance page shows both, labelled.

---

## Tests

Covered under `tests/` with the sim-governance suite
(`python -m pytest -q tests -k sim_governance`).
