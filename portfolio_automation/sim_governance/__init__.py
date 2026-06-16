"""
Simulation-Governance Lane
==========================

Implements the operator's two-lane governance model (set 2026-06-16):

* **Simulation / Test Lane — ACTIVE.** Experimental advisory, watchlist, crowd,
  discovery, ranking, and strategy logic is fully implemented and *allowed to
  change simulation outputs* once its tests pass. All simulation artifacts live
  in the ``SANDBOX`` / ``SIMULATION`` namespaces and never touch production.

* **Production Lane — PROTECTED + HUMAN-GATED.** Production behavior changes
  only after a promotion proposal is approved by a human. Production loaders
  apply *only* approved proposals; they ignore raw simulation artifacts, pending
  proposals, rejected proposals, and invalid approvals.

Daily workflow (all non-blocking, wrapped in try/except by the orchestrator)::

    idea/feature/candidate
      -> implement in simulation lane (simulation_lane.run_simulation_lane)
      -> run tests (pytest)
      -> run daily simulation (orchestrator step 2, after production baseline)
      -> generate evidence (daily_simulation_bundle.build_daily_simulation_bundle)
      -> ONE consolidated AI/product review <= $0.50/day (daily_ai_review.run_daily_ai_review)
      -> if ready: pending production proposal (promotion_proposals.generate_proposals)
      -> human approval (promotion_approvals.record_approval)
      -> production integration (production_application.apply_approved_proposals)

The AI/product review can *recommend* readiness but can never approve production
by itself; human approval is the production gate.

This package is the one sanctioned place (alongside the pre-existing
``backtesting/auto_apply.py``) where the system is *not* observe-only: approved
proposals are applied to production behavior. Everything upstream of human
approval remains simulation-only.
"""

from __future__ import annotations

__all__ = [
    "schemas",
    "simulation_lane",
    "daily_simulation_bundle",
    "ai_review_packet",
    "daily_ai_review",
    "promotion_proposals",
    "promotion_approvals",
    "production_application",
    "daily_governance_run",
]
