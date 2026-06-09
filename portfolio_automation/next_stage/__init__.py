"""Next-stage portfolio intelligence package.

Implements the workstreams specified in
``docs/NEXT_STAGE_PORTFOLIO_INTELLIGENCE_SPEC.md`` — universe scanning,
opportunity scoring, sandbox shadow tracking, the multi-strategy objective
engine, the daily system-improvement skill, the learning-loop event store, and
their artifact contracts.

Hard invariants for everything in this package (asserted by tests):

* Advisory-only / observe-only. Every artifact carries ``observe_only: true``.
* No trading, no broker writes, no order placement, no money movement, no
  automatic portfolio allocation changes.
* Research/sandbox writers never write ``outputs/latest/decision_plan.json``.
* New layers degrade gracefully (return a valid degraded payload on failure)
  and never break the daily pipeline.

Phase 1 ships only :mod:`portfolio_automation.next_stage.contracts` (schemas +
artifact descriptors). Producers arrive in later phases.
"""
