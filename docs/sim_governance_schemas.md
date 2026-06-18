# Simulation Governance — Schemas

## Purpose

`portfolio_automation/sim_governance/schemas.py` is the shared vocabulary, data
model, and validator layer for the two-lane simulation-governance promotion
workflow. It is a **pure** module: no I/O, no clock reads, no randomness —
timestamps and ids are passed in by callers so the whole lane is deterministic
and testable. (One of two `schemas.py` modules; see
[`docs/schemas.md`](schemas.md) for disambiguation.)

---

## Two-Lane Governance

This module encodes the contract that makes the system **production-gated /
simulation-active / human-approved**:

- The simulation lane may actively produce `SimulationCandidate`s and change
  SANDBOX/SIMULATION outputs.
- The AI/product review (`ReviewVerdict`) may **recommend** readiness
  (`ready_for_production_review`) but can never approve production.
- `is_human_approver()` makes "AI cannot self-approve" a **structural** invariant
  rather than a convention: any approver token matching an AI-reviewer marker
  (`ai`, `gpt`, `openai`, `claude`, `auto`, `system`, …) is rejected.

Nothing here touches `decision_engine.py` or score semantics.

---

## Vocabulary

- **Workflows:** `WORKFLOW_ADVISORY`, `WORKFLOW_WATCHLIST` (a daily review must
  cover both together).
- **Proposal types (`PROPOSAL_TYPES`):** advisory strategy/ranking/context,
  watchlist add/remove/rank/tag, crowd context, discovery promotion, and the
  five flock types (`flock_context_production_display`,
  `flock_watchlist_candidate_logic`, `flock_advisory_context_logic`,
  `flock_simulation_scoring_adjustment`, `flock_risk_overlay`).
- **Review decisions (`REVIEW_DECISIONS`):** `reject`, `continue_testing`,
  `ready_for_production_review`.
- **Approval lifecycle (`APPROVAL_STATUSES`):** `pending`, `approved`,
  `rejected`; human decisions (`HUMAN_DECISIONS`): `approve`, `reject`.

---

## Data Model

- `@dataclass SimulationCandidate` — a change the sim lane produced and wants
  reviewed (`what_changed` / `why_changed` / `production_baseline` →
  `simulated_value`, `risk_impact`, `confidence`, `data_quality`,
  `ready_for_production_review` hint, `proposed_production_change`). `to_dict()`
  renders `before`/`after` keys.
- `@dataclass ReviewVerdict` — the AI/product classification of one candidate.
- `@dataclass PromotionProposal` — a pending production-change proposal; defaults
  to `approval_status="pending"`.

---

## Key Functions

- `workflow_for_proposal_type(proposal_type) -> str` — routes a proposal to the
  advisory or watchlist overlay.
- `make_candidate_id(...)` / `make_proposal_id(...)` — deterministic SHA-256
  ids (caller supplies the salt/stamp; no clock).
- `is_valid_proposal_type(x) -> bool`.
- `is_human_approver(approver) -> bool` — rejects empty, non-string, and any
  AI-reviewer-marker approver.
- `is_valid_approval_record(record) -> (ok, reason)` — a record is valid only
  with a `proposal_id`, a known human `decision`, a real human `approver`, and a
  `timestamp`. Invalid approvals are ignored by production application.

---

## Tests

Covered under `tests/` with the sim-governance suite
(`python -m pytest -q tests -k sim_governance`).
