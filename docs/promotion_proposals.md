# Simulation Governance — Promotion Proposals

## Purpose

`portfolio_automation/sim_governance/promotion_proposals.py` generates *pending*
production-change proposals. When (and only when) the AI/product review marks a
candidate `ready_for_production_review`, a pending `PromotionProposal` is created
carrying everything a human needs to approve: the concrete production change,
evidence/review/simulation refs, a risk summary, and a rollback plan.

---

## Two-Lane Governance

Every generated proposal defaults to `approval_status: pending` and therefore has
**NO effect on production until a human approves it**. AI cannot self-approve.
The module only emits proposal records; it never applies them (that is
`production_application`), never touches production loaders directly, and never
touches score semantics.

---

## Artifacts Written (OutputNamespace.PROMOTION_REVIEW → `outputs/promotion_review/`)

| File | Contents |
|------|----------|
| `pending_proposals.json` | The full current pending set (schema `pending_proposals.v1`) |
| `proposals_log.jsonl` | Append-only history of every generated proposal |

---

## Key Functions

- `generate_proposals(candidates_by_id, review_result, now, *, base_dir,
  write_files=True) -> dict` — for each verdict whose decision is
  `DECISION_READY`, builds a `PromotionProposal` (validating the proposal type),
  attaches a type-specific rollback plan, and writes the pending set + appends to
  the log.
- `_rollback_plan_for(proposal_type, symbol) -> str` — type-aware rollback text;
  for advisory/flock-context/risk types it explicitly notes the decision engine
  is untouched (display/input-boundary only).
- `load_pending_proposals(base_dir) -> list[dict]` — best-effort read of the
  current pending set.

---

## Tests

Covered under `tests/` with the sim-governance suite
(`python -m pytest -q tests -k sim_governance`).
