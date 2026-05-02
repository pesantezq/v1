"""
Discovery Approval Workflow — Sandbox audit layer for research candidate review.

Allows operators to record sandbox review decisions for discovery candidates.

Safety invariants (hard-coded, not configurable):
  - All decisions are sandbox-only research notes.
  - No trade execution.
  - No official watchlist mutation.
  - No official portfolio mutation.
  - No buy/sell/actionable/promoted/validated decisions.
  - Artifacts written exclusively to outputs/sandbox/discovery/.

Allowed decisions:
  APPROVE_FOR_RESEARCH_REVIEW  — candidate is worth tracking in the research lane
  KEEP_WATCHING                — continue monitoring; not ready for review
  NEEDS_MORE_EVIDENCE          — corroboration score too low; wait for more data
  REJECT_CANDIDATE             — not worth further research attention

Forbidden decisions (never accepted):
  buy, sell, actionable, promoted, validated
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import (
    OutputNamespace,
    get_output_path,
    validate_output_path,
)

logger = logging.getLogger(__name__)

# Sandbox sub-paths — consistent with discovery_reports.py conventions
_APPROVAL_DECISIONS_SUBPATH = "discovery/approval_decisions.jsonl"
_APPROVAL_SUMMARY_SUBPATH   = "discovery/approval_summary.json"

# ---------------------------------------------------------------------------
# Forbidden decision values — never accepted, checked at write time
# ---------------------------------------------------------------------------

_FORBIDDEN_DECISIONS: frozenset[str] = frozenset({
    "buy", "sell", "actionable", "promoted", "validated",
})


# ---------------------------------------------------------------------------
# Decision enum — allowed values only
# ---------------------------------------------------------------------------

class ApprovalDecision(str, Enum):
    """Allowed sandbox review decisions. No buy/sell/trade/promotion semantics."""
    APPROVE_FOR_RESEARCH_REVIEW = "approve_for_research_review"
    KEEP_WATCHING               = "keep_watching"
    REJECT_CANDIDATE            = "reject_candidate"
    NEEDS_MORE_EVIDENCE         = "needs_more_evidence"
    # NOT ALLOWED: buy, sell, actionable, promoted, validated


# ---------------------------------------------------------------------------
# Approval decision dataclass
# ---------------------------------------------------------------------------

@dataclass
class DiscoveryApprovalDecision:
    """
    A single sandbox research review decision for a discovery candidate.

    This is an audit/research note only. It does not:
      - create buy/sell recommendations
      - update the official watchlist
      - mutate portfolio state
      - trigger any trade
    """
    generated_at: str
    symbol: str
    company_name: str
    candidate_status: str
    corroboration_score: float
    corroboration_level: str
    decision: ApprovalDecision
    decision_reason: str
    operator: str
    source_artifact: str
    run_id: str

    # Hard-coded governance flags — always True, never configurable
    observe_only: bool = True
    sandbox_only: bool = True
    no_trade: bool = True
    no_official_promotion: bool = True

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["decision"] = self.decision.value  # serialize enum as its string value
        return d


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_ALLOWED_DECISION_VALUES: frozenset[str] = frozenset(d.value for d in ApprovalDecision)


def _validate_decision(decision: ApprovalDecision | str) -> ApprovalDecision:
    """
    Validate and coerce a decision value.

    Raises ValueError for forbidden values (buy/sell/…) or unknown values.
    """
    if isinstance(decision, ApprovalDecision):
        return decision  # already a valid enum member

    raw = str(decision).strip().lower()
    if raw in _FORBIDDEN_DECISIONS:
        raise ValueError(
            f"Forbidden decision {decision!r}. "
            "Discovery approval decisions must not be buy/sell/actionable/promoted/validated."
        )
    try:
        return ApprovalDecision(raw)
    except ValueError:
        raise ValueError(
            f"Unknown decision {decision!r}. "
            f"Allowed values: {sorted(_ALLOWED_DECISION_VALUES)}"
        )


def _validate_governance_flags(decision: DiscoveryApprovalDecision) -> None:
    """Raise ValueError if any governance flag has been tampered with."""
    if not decision.observe_only:
        raise ValueError("DiscoveryApprovalDecision.observe_only must be True.")
    if not decision.sandbox_only:
        raise ValueError("DiscoveryApprovalDecision.sandbox_only must be True.")
    if not decision.no_trade:
        raise ValueError("DiscoveryApprovalDecision.no_trade must be True.")
    if not decision.no_official_promotion:
        raise ValueError("DiscoveryApprovalDecision.no_official_promotion must be True.")


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------

def make_approval_decision(
    *,
    symbol: str,
    decision: ApprovalDecision | str,
    decision_reason: str = "",
    candidate_status: str = "watch",
    corroboration_score: float = 0.0,
    corroboration_level: str = "none",
    company_name: str = "",
    operator: str = "operator",
    source_artifact: str = "",
    run_id: str = "",
    now: datetime | None = None,
) -> DiscoveryApprovalDecision:
    """
    Build a :class:`DiscoveryApprovalDecision`, validating the decision value.

    Governance flags are hard-coded and cannot be overridden.
    """
    validated_decision = _validate_decision(decision)
    ts = (now or datetime.now(timezone.utc)).isoformat()
    return DiscoveryApprovalDecision(
        generated_at=ts,
        symbol=symbol.upper().strip(),
        company_name=company_name or "",
        candidate_status=candidate_status,
        corroboration_score=float(corroboration_score),
        corroboration_level=corroboration_level,
        decision=validated_decision,
        decision_reason=decision_reason or "",
        operator=operator or "operator",
        source_artifact=source_artifact or "",
        run_id=run_id or "",
    )


# ---------------------------------------------------------------------------
# Write — append-only JSONL
# ---------------------------------------------------------------------------

def record_approval_decision(
    decision: DiscoveryApprovalDecision,
    *,
    base_dir: str | Path = "outputs",
) -> Path:
    """
    Append a single sandbox review decision to the approval JSONL artifact.

    This is append-only — existing decisions are never modified or deleted.
    The file is created if it does not exist.

    Data governance is enforced: the path is validated to be within
    OutputNamespace.SANDBOX before any write occurs.

    Parameters
    ----------
    decision:
        A :class:`DiscoveryApprovalDecision` instance.
    base_dir:
        Root outputs directory (default: "outputs").

    Returns
    -------
    Path written.

    Raises
    ------
    ValueError
        If governance flags are invalid or the decision value is forbidden.
    DataGovernanceError
        If the resolved path is outside the SANDBOX namespace.
    """
    _validate_governance_flags(decision)
    _validate_decision(decision.decision)

    out_path = get_output_path(
        OutputNamespace.SANDBOX, _APPROVAL_DECISIONS_SUBPATH, base_dir=base_dir
    )
    validate_output_path(OutputNamespace.SANDBOX, out_path, base_dir=base_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    line = json.dumps(decision.to_dict(), default=str)
    with out_path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")

    logger.info("approval_workflow: recorded %s decision for %s → %s",
                decision.decision.value, decision.symbol, out_path)
    return out_path


# ---------------------------------------------------------------------------
# Read — JSONL loader
# ---------------------------------------------------------------------------

def load_approval_decisions(
    base_dir: str | Path = "outputs",
) -> list[dict[str, Any]]:
    """
    Load all sandbox review decisions from the approval JSONL artifact.

    Malformed lines are silently skipped.
    Returns an empty list if the file does not exist or cannot be read.

    Parameters
    ----------
    base_dir:
        Root outputs directory (default: "outputs").
    """
    path = get_output_path(
        OutputNamespace.SANDBOX, _APPROVAL_DECISIONS_SUBPATH, base_dir=base_dir
    )
    if not path.exists():
        return []
    decisions: list[dict[str, Any]] = []
    try:
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
                if isinstance(obj, dict):
                    decisions.append(obj)
            except json.JSONDecodeError:
                pass
    except Exception:
        pass
    return decisions


# ---------------------------------------------------------------------------
# Summary builder
# ---------------------------------------------------------------------------

def build_approval_summary(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Compute an in-memory summary from a list of raw decision dicts.

    Parameters
    ----------
    decisions:
        Output of :func:`load_approval_decisions`.

    Returns
    -------
    Summary dict with counts, per-symbol latest decision, and governance flags.
    """
    counts: dict[str, int] = {}
    by_symbol: dict[str, dict[str, Any]] = {}

    for d in decisions:
        decision_val = d.get("decision", "unknown")
        counts[decision_val] = counts.get(decision_val, 0) + 1
        symbol = d.get("symbol", "?")
        by_symbol[symbol] = d  # last decision per symbol wins (append-only log)

    return {
        "total_decisions": len(decisions),
        "unique_symbols_reviewed": len(by_symbol),
        "decision_counts": counts,
        "latest_per_symbol": by_symbol,
        "observe_only": True,
        "sandbox_only": True,
        "no_trade": True,
        "no_official_promotion": True,
        "disclaimer": (
            "Approval decisions are sandbox research notes only. "
            "They do not update the official watchlist, portfolio, or recommendations."
        ),
    }
