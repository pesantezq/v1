"""EvidenceGateway (Northstar Phase 0C) — the point-in-time admission boundary.

The 0B evidence kernel ends with the note that "a consumer arrives with the
Phase 0C EvidenceGateway". This is that consumer. It is deliberately a SEPARATE
package from ``portfolio_automation.northstar``: the canonical contracts define
what evidence IS, and the gateway decides what a reader may SEE. Collapsing the
two would let an admission rule quietly redefine a contract.

Responsibility, narrowly:

    evidence candidate + as-of instant
            -> admissibility decision + explicit reason

What the gateway is NOT, and must never absorb:

    * a persistence backend or research store
    * a vendor adapter (no vendor schema may enter this interface)
    * a prediction, allocation, exit, certification or portfolio authority

Phase 0C's exit gate is "lookahead-audited PIT reads over the research store":
evidence available at a historical time T must be restricted to what was
genuinely knowable at T, and that restriction must be AUDITED rather than
asserted. The audit trail is the reason code on every decision.
"""
from __future__ import annotations

from portfolio_automation.evidence_gateway.admissibility import (
    AdmissibilityDecision,
    AdmissibilityReason,
    is_admissible,
    require_admissible,
)

__all__ = [
    "AdmissibilityDecision",
    "AdmissibilityReason",
    "is_admissible",
    "require_admissible",
]
