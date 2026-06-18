# Simulation Governance — Promotion Approvals

## Purpose

`portfolio_automation/sim_governance/promotion_approvals.py` is the **human
approval mechanism** — the production gate of the two-lane model. A human records
an approve/reject decision against a pending proposal; the decision is validated
(real human approver, known decision, timestamp) and appended to the approval
log. The log is folded to the latest valid decision per proposal.

---

## Two-Lane Governance

This module is where production promotion is gated on a real human. Structural
guarantees (enforced here and re-checked at application time):

- **AI cannot self-approve** — an approver that looks like the AI reviewer is
  rejected by `schemas.is_human_approver`.
- **Invalid approval metadata is ignored** — never counted as an approval.

It only records/reads approval records; it does not itself change production
(that is `production_application`, which consumes the approved ids).

---

## Artifacts Written (OutputNamespace.PROMOTION_APPROVALS → `outputs/promotion_approvals/`)

| File | Contents |
|------|----------|
| `approved_proposals.json` | The append-style approval log (schema `approved_proposals.v1`) |

---

## Key Functions

- `record_approval(proposal_id, decision, approver, now, *, base_dir,
  notes=None, review_date=None, write_files=True) -> dict` — validates and
  appends one human decision. Returns `{ok, reason, record}`; when invalid
  (e.g. AI self-approval) nothing is written.
- `load_valid_approvals(base_dir) -> list[dict]` — all structurally-valid
  records (invalid metadata filtered out).
- `effective_approvals(base_dir) -> {proposal_id: 'approve'|'reject'}` — folds
  the log to the latest valid decision per proposal (last record wins).
- `approved_proposal_ids(base_dir) -> set[str]` /
  `rejected_proposal_ids(base_dir) -> set[str]` — the effective approve/reject
  sets consumed by production application.

---

## Tests

Covered under `tests/` with the sim-governance suite
(`python -m pytest -q tests -k sim_governance`).
