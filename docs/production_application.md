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
| `snapshots/<overlay>.<stamp>.json` | PROMOTION_APPROVALS | `outputs/promotion_approvals/snapshots/` (rollback) |

The two overlay artifacts carry `feeds_production: true`, the
`applied_proposal_ids`, and the `ops` (each with provenance + `rollback_plan`).

---

## Key Functions

- `apply_approved_proposals(now, *, base_dir, proposals=None, approved_ids=None,
  rejected_ids=None, write_files=True) -> dict` — routes each approved proposal
  to the watchlist or advisory overlay (by `workflow_for_proposal_type`),
  snapshots the prior overlays, writes the new overlays, appends audit rows, and
  writes the application state. Inputs default to the persisted pending set +
  validated approval log.
- `rollback_last(filename, base_dir, now) -> dict` — restores the most recent
  snapshot of an overlay and records a `rolled_back` audit event.
- `_overlay_entry(proposal)` / `_snapshot_existing(...)` — provenance entry
  construction and rollback snapshotting.

---

## Tests

Covered under `tests/` with the sim-governance suite
(`python -m pytest -q tests -k sim_governance`).
