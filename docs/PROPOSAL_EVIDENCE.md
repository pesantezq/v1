# Governance Proposal Hardening — Evidence Cards & Power Classes (Phase 10)

Status: **shipped** on `feat/complete-simulation-quant-governance-loop`.
Observe-only helpers; nothing self-activates; production stays human-gated.

## What already existed
`sim_governance/` already has the full propose -> AI/heuristic review -> approve
-> apply machinery, the $0.50/day AI cap, and the structural no-self-approve
guard. Phase 10 adds the **power-class gating + evidence cards + lifecycle
checks** that were missing.

## Overlay power classes (`OVERLAY_POWER_CLASSES`, 1-6)
1 explanation_only · 2 ranking_only · 3 eligibility_guard · 4 sizing_modifier ·
5 allocation_overlay · 6 decision_override. Increasing production reach.

`required_evidence(power_class)` returns a monotonic evidence floor
(min OOS sample, min regime stability, require-validated-OOS) — **higher power
needs stronger evidence** — and always `human_approval=True`.

## Evidence card (`evidence_card`)
Full card: proposal_id, type, hypothesis, affected_component, proposed_effect,
power_class(+label), simulation_result, baseline_comparison, oos_status,
sample_size, cost_adjusted_result, regime_stability, risk_impact,
max_production_impact, failure_conditions, rollback_plan, expiration,
evidence_freshness, source_experiment_ids, conflicts, supersedes, owner,
created_at, `approval_status="pending"` (never self-approves).

## Gating + lifecycle
- `gate_proposal()` — eligible-for-**review** only when evidence meets the
  power-class floor AND evidence is fresh (not expired/stale). Never approves;
  `human_approval_required` is invariant. A class-5 overlay with weak evidence
  is blocked; the same evidence is fine for a class-1 explanation overlay.
- `dedupe_proposals()` — collapses duplicate (component, effect, power) cards.
- `is_expired()` — expiration timestamp check.
- `detect_conflicts()` — flags same-component contradictory effects.

## Cadence + consumers
Governance cadence. The proposal builder + the one consolidated daily AI review
(Stage 10e) use these to build/gate cards; `sim_governance.promotion_approvals`
remains the human approval gate; `production_application` remains default-OFF.

## Tests
`tests/test_proposal_evidence.py` (8) — 6 power classes, escalating evidence,
high-power-needs-strong-evidence gate, complete card stays pending, dedupe,
expiration, conflict detection, stale-evidence block.
