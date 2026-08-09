"""StockBot R&D Control Plane — deterministic foundation (Phase 0A).

StockBot-owned, local-first R&D lifecycle control. Authority belongs to this
deterministic code, never to a worker/LLM. Phase 0A ships: versioned contracts,
an authoritative SQLite registry with a validated state machine + audit trail,
bounded restart recovery, and observe-only health. No worker, sandbox, LLM,
scheduler, model router, or engineering gateway is included here.

See docs/RD_CONTROL_PLANE.md.
"""
from __future__ import annotations

from portfolio_automation.rd_control.contracts import (
    SCHEMA_VERSION,
    JobType,
    JobStatus,
    WorkerAuthority,
    JobRecord,
    LEGAL_TRANSITIONS,
    TERMINAL_STATUSES,
    is_legal_transition,
    assert_legal_transition,
    compute_input_manifest_hash,
    RDControlError,
    IllegalTransitionError,
    JobNotFoundError,
)
from portfolio_automation.rd_control import registry, health

__all__ = [
    "SCHEMA_VERSION",
    "JobType", "JobStatus", "WorkerAuthority", "JobRecord",
    "LEGAL_TRANSITIONS", "TERMINAL_STATUSES",
    "is_legal_transition", "assert_legal_transition", "compute_input_manifest_hash",
    "RDControlError", "IllegalTransitionError", "JobNotFoundError",
    "registry", "health",
]
