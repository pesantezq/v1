"""
Simulation-Governance — shared vocabulary, data model, and validators.

Pure module: no I/O, no clock reads, no randomness. Timestamps and run ids are
passed in by callers so the whole lane is deterministic and testable. This keeps
the contract auditable and lets every downstream module agree on the exact shape
of candidates, verdicts, proposals, and approvals.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Workflows — every candidate belongs to exactly one workflow. The daily AI
# review MUST cover both together (advisory + watchlist) in one call.
# ---------------------------------------------------------------------------

WORKFLOW_ADVISORY = "advisory"
WORKFLOW_WATCHLIST = "watchlist"
WORKFLOWS = frozenset({WORKFLOW_ADVISORY, WORKFLOW_WATCHLIST})

# ---------------------------------------------------------------------------
# Proposal types (spec §5). A candidate's proposal_type pins exactly what kind
# of production change it would become.
# ---------------------------------------------------------------------------

PROPOSAL_ADVISORY_STRATEGY = "advisory_strategy_change"
PROPOSAL_ADVISORY_RANKING = "advisory_ranking_change"
PROPOSAL_ADVISORY_CONTEXT = "advisory_context_change"
PROPOSAL_WATCHLIST_ADD = "watchlist_add"
PROPOSAL_WATCHLIST_REMOVE = "watchlist_remove"
PROPOSAL_WATCHLIST_RANK = "watchlist_rank_change"
PROPOSAL_WATCHLIST_TAG = "watchlist_tag_change"
PROPOSAL_CROWD_CONTEXT = "crowd_context_change"
PROPOSAL_DISCOVERY_PROMOTION = "discovery_candidate_promotion"

# Flock Intelligence proposal types (simulation-only until human-approved).
PROPOSAL_FLOCK_CONTEXT_DISPLAY = "flock_context_production_display"
PROPOSAL_FLOCK_WATCHLIST_LOGIC = "flock_watchlist_candidate_logic"
PROPOSAL_FLOCK_ADVISORY_CONTEXT = "flock_advisory_context_logic"
PROPOSAL_FLOCK_SCORING_ADJUSTMENT = "flock_simulation_scoring_adjustment"
PROPOSAL_FLOCK_RISK_OVERLAY = "flock_risk_overlay"

PROPOSAL_TYPES = frozenset({
    PROPOSAL_ADVISORY_STRATEGY,
    PROPOSAL_ADVISORY_RANKING,
    PROPOSAL_ADVISORY_CONTEXT,
    PROPOSAL_WATCHLIST_ADD,
    PROPOSAL_WATCHLIST_REMOVE,
    PROPOSAL_WATCHLIST_RANK,
    PROPOSAL_WATCHLIST_TAG,
    PROPOSAL_CROWD_CONTEXT,
    PROPOSAL_DISCOVERY_PROMOTION,
    PROPOSAL_FLOCK_CONTEXT_DISPLAY,
    PROPOSAL_FLOCK_WATCHLIST_LOGIC,
    PROPOSAL_FLOCK_ADVISORY_CONTEXT,
    PROPOSAL_FLOCK_SCORING_ADJUSTMENT,
    PROPOSAL_FLOCK_RISK_OVERLAY,
})

# Which workflow each proposal type belongs to. Used by production application
# to route an approved proposal to the right overlay artifact.
_ADVISORY_PROPOSALS = frozenset({
    PROPOSAL_ADVISORY_STRATEGY,
    PROPOSAL_ADVISORY_RANKING,
    PROPOSAL_ADVISORY_CONTEXT,
    PROPOSAL_CROWD_CONTEXT,
    PROPOSAL_FLOCK_CONTEXT_DISPLAY,
    PROPOSAL_FLOCK_ADVISORY_CONTEXT,
    PROPOSAL_FLOCK_SCORING_ADJUSTMENT,
    PROPOSAL_FLOCK_RISK_OVERLAY,
})
_WATCHLIST_PROPOSALS = frozenset({
    PROPOSAL_WATCHLIST_ADD,
    PROPOSAL_WATCHLIST_REMOVE,
    PROPOSAL_WATCHLIST_RANK,
    PROPOSAL_WATCHLIST_TAG,
    PROPOSAL_DISCOVERY_PROMOTION,
    PROPOSAL_FLOCK_WATCHLIST_LOGIC,
})


def workflow_for_proposal_type(proposal_type: str) -> str:
    """Return the workflow ('advisory'|'watchlist') a proposal type applies to."""
    if proposal_type in _WATCHLIST_PROPOSALS:
        return WORKFLOW_WATCHLIST
    return WORKFLOW_ADVISORY


# ---------------------------------------------------------------------------
# AI/product review decisions (spec §4). The review classifies each candidate.
# 'ready_for_production_review' is a RECOMMENDATION only — it triggers a *pending*
# proposal; it never approves production.
# ---------------------------------------------------------------------------

DECISION_REJECT = "reject"
DECISION_CONTINUE_TESTING = "continue_testing"
DECISION_READY = "ready_for_production_review"
REVIEW_DECISIONS = frozenset({DECISION_REJECT, DECISION_CONTINUE_TESTING, DECISION_READY})

# ---------------------------------------------------------------------------
# Proposal approval lifecycle (spec §5/§6). Proposals default to 'pending'.
# ---------------------------------------------------------------------------

APPROVAL_PENDING = "pending"
APPROVAL_APPROVED = "approved"
APPROVAL_REJECTED = "rejected"
APPROVAL_STATUSES = frozenset({APPROVAL_PENDING, APPROVAL_APPROVED, APPROVAL_REJECTED})

# Human approval decisions recorded against a proposal.
HUMAN_APPROVE = "approve"
HUMAN_REJECT = "reject"
HUMAN_DECISIONS = frozenset({HUMAN_APPROVE, HUMAN_REJECT})

# An approval is only valid if its approver is a real human marker — never the
# AI/product reviewer. This is the structural guarantee that "AI cannot
# self-approve production" (spec §4, §11).
AI_REVIEWER_MARKERS = frozenset({
    "ai", "ai_review", "ai_product_review", "gpt", "openai", "anthropic",
    "claude", "model", "llm", "auto", "automatic", "system",
})


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class SimulationCandidate:
    """A change the simulation lane actively produced and wants reviewed.

    The simulation lane is allowed to *apply* this change to simulation outputs.
    It becomes a production change only via the gated promotion workflow.
    """
    candidate_id: str
    workflow: str                       # WORKFLOW_ADVISORY | WORKFLOW_WATCHLIST
    proposal_type: str                  # one of PROPOSAL_TYPES
    symbol: str | None
    what_changed: str
    why_changed: str
    source_evidence: list[str] = field(default_factory=list)   # artifact refs / sources
    production_baseline: Any = None     # value in production today (before)
    simulated_value: Any = None         # value the simulation produced (after)
    risk_impact: str = "unknown"        # low | medium | high | unknown
    confidence: float = 0.0             # 0..1
    data_quality: str = "unknown"       # ok | degraded | stale | unknown
    ready_for_production_review: bool = False  # simulation-side readiness hint
    proposed_production_change: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "workflow": self.workflow,
            "proposal_type": self.proposal_type,
            "symbol": self.symbol,
            "what_changed": self.what_changed,
            "why_changed": self.why_changed,
            "source_evidence": list(self.source_evidence),
            "before": self.production_baseline,
            "after": self.simulated_value,
            "risk_impact": self.risk_impact,
            "confidence": round(float(self.confidence), 4),
            "data_quality": self.data_quality,
            "ready_for_production_review": bool(self.ready_for_production_review),
            "proposed_production_change": dict(self.proposed_production_change),
            "metadata": dict(self.metadata),
        }


@dataclass
class ReviewVerdict:
    """The AI/product review's classification of one candidate (spec §4)."""
    candidate_id: str
    workflow: str
    decision: str                       # one of REVIEW_DECISIONS
    reason: str = ""
    evidence_strength: str = "unknown"  # weak | moderate | strong | unknown
    risk_level: str = "unknown"         # low | medium | high | unknown
    missing_evidence: list[str] = field(default_factory=list)
    required_human_review: bool = True
    rollback_readiness: str = "unknown"  # ready | partial | none | unknown

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "workflow": self.workflow,
            "decision": self.decision,
            "reason": self.reason,
            "evidence_strength": self.evidence_strength,
            "risk_level": self.risk_level,
            "missing_evidence": list(self.missing_evidence),
            "required_human_review": bool(self.required_human_review),
            "rollback_readiness": self.rollback_readiness,
        }


@dataclass
class PromotionProposal:
    """A pending production-change proposal (spec §5). Defaults to pending."""
    proposal_id: str
    candidate_id: str
    proposal_type: str
    workflow: str
    proposed_production_change: dict
    evidence_refs: list[str] = field(default_factory=list)
    ai_review_refs: list[str] = field(default_factory=list)
    simulation_result_refs: list[str] = field(default_factory=list)
    risk_summary: str = ""
    rollback_plan: str = ""
    approval_status: str = APPROVAL_PENDING
    approved_by: str | None = None
    approved_at: str | None = None
    approval_notes: str | None = None
    created_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "candidate_id": self.candidate_id,
            "proposal_type": self.proposal_type,
            "workflow": self.workflow,
            "proposed_production_change": dict(self.proposed_production_change),
            "evidence_refs": list(self.evidence_refs),
            "ai_review_refs": list(self.ai_review_refs),
            "simulation_result_refs": list(self.simulation_result_refs),
            "risk_summary": self.risk_summary,
            "rollback_plan": self.rollback_plan,
            "approval_status": self.approval_status,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "approval_notes": self.approval_notes,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# Deterministic id helpers (no clock / randomness — caller passes a stamp)
# ---------------------------------------------------------------------------


def make_candidate_id(proposal_type: str, symbol: str | None, salt: str) -> str:
    """Stable candidate id derived from its identity, not from a clock."""
    raw = f"{proposal_type}|{symbol or '-'}|{salt}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"cand_{digest}"


def make_proposal_id(candidate_id: str, stamp: str) -> str:
    raw = f"{candidate_id}|{stamp}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"prop_{digest}"


# ---------------------------------------------------------------------------
# Validators — the structural guards production application relies on.
# ---------------------------------------------------------------------------


def is_valid_proposal_type(proposal_type: Any) -> bool:
    return isinstance(proposal_type, str) and proposal_type in PROPOSAL_TYPES


def is_human_approver(approver: Any) -> bool:
    """True only when *approver* is a real human marker (not the AI reviewer).

    Empty / non-string / AI-reviewer markers are all rejected. This is what
    makes "AI cannot self-approve production" a structural invariant rather than
    a convention.
    """
    if not isinstance(approver, str):
        return False
    cleaned = approver.strip().lower()
    if not cleaned:
        return False
    # Reject if any AI-reviewer marker is a standalone token in the approver id.
    tokens = set(cleaned.replace(":", " ").replace("-", " ").replace("_", " ").split())
    if tokens & AI_REVIEWER_MARKERS:
        return False
    return True


def is_valid_approval_record(record: Any) -> tuple[bool, str]:
    """Validate a human-approval record (spec §6, §7, §11).

    Returns (ok, reason). A record is valid only if:
      - it is a dict with a proposal_id and a known human decision
      - the approver is a real human (not the AI reviewer)
      - it carries a timestamp

    Invalid approvals are *ignored* by production application — never applied.
    """
    if not isinstance(record, dict):
        return False, "approval record is not an object"
    if not record.get("proposal_id"):
        return False, "missing proposal_id"
    decision = record.get("decision")
    if decision not in HUMAN_DECISIONS:
        return False, f"decision {decision!r} not in {sorted(HUMAN_DECISIONS)}"
    approver = record.get("approver")
    if not is_human_approver(approver):
        return False, f"approver {approver!r} is not a valid human approver (AI cannot self-approve)"
    if not record.get("timestamp"):
        return False, "missing timestamp"
    return True, "ok"
