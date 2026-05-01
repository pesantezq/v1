"""
Candidate scoring and status assignment for discovery candidates.

Allowed statuses in v1: DISCOVERED, WATCH, REJECTED.

Not allowed (and never produced):
  PROMOTED, VALIDATED, ACTIONABLE, BUY, SELL

Every candidate carries:
  corroboration_required = True
  corroboration_met = False
  corroboration_sources = []

Discovery candidates are NOT buy/sell recommendations.
Discovery candidates are NOT official portfolio actions.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Sequence

from portfolio_automation.discovery.news_ticker_discovery import DiscoveredTicker
from portfolio_automation.discovery.event_classifier import ClassificationResult, EventType


# ---------------------------------------------------------------------------
# Candidate status — v1 only
# ---------------------------------------------------------------------------

class CandidateStatus(str, Enum):
    DISCOVERED = "discovered"
    WATCH      = "watch"
    REJECTED   = "rejected"
    # NOT_ALLOWED: PROMOTED, VALIDATED, ACTIONABLE, BUY, SELL


# ---------------------------------------------------------------------------
# Candidate dataclass
# ---------------------------------------------------------------------------

@dataclass
class DiscoveryCandidate:
    """A scored, status-assigned discovery candidate. Research-lane only."""
    ticker: str
    status: CandidateStatus
    score: float
    mention_count: int
    unique_source_count: int
    event_type: EventType
    event_confidence: float
    risk_flag: bool
    rejection_reason: str | None

    # Hard governance flags — always True/False in v1
    discovery_only: bool = True
    sandbox_only: bool = True
    corroboration_required: bool = True
    corroboration_met: bool = False
    corroboration_sources: list[str] = field(default_factory=list)

    # Timestamps
    first_seen: str | None = None
    last_seen: str | None = None

    # Evidence snippets for context
    evidence_snippets: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Scoring constants
# ---------------------------------------------------------------------------

# Per-event-type score adjustments
_EVENT_TYPE_BONUS: dict[EventType, float] = {
    EventType.EARNINGS:           0.5,
    EventType.MERGER_ACQUISITION: 0.5,
    EventType.ANALYST_ACTION:     0.3,
    EventType.GUIDANCE:           0.3,
    EventType.PRODUCT_LAUNCH:     0.2,
    EventType.PARTNERSHIP:        0.2,
    EventType.REGULATORY:         0.1,
    EventType.FINANCING:          0.1,
    EventType.MACRO_THEME:        0.0,
    EventType.MANAGEMENT_CHANGE:  0.1,
    EventType.LEGAL_RISK:        -0.5,
    EventType.UNKNOWN:           -0.2,
}


# ---------------------------------------------------------------------------
# Internal scoring
# ---------------------------------------------------------------------------

def _compute_score(
    discovered: DiscoveredTicker,
    classification: ClassificationResult,
) -> float:
    """
    Compute a research-lane relevance score. Not a buy/sell signal.

    Components:
    - Mention count contribution (log-scaled, max ~1.0 for 3+ mentions)
    - Source diversity bonus (up to 0.5)
    - Event classification confidence (up to 1.0)
    - Event type bonus/penalty
    - Risk flag penalty (-0.5)
    """
    # Mention count: log2(count+1) scaled — 1 mention = 1.0, 3 = ~2.0, 7 = ~3.0
    mention_score = math.log2(discovered.mention_count + 1)

    # Source diversity: reward unique sources, cap at 5
    source_score = min(len(discovered.unique_sources), 5) * 0.1

    # Event confidence: direct contribution
    confidence_score = classification.confidence

    # Event type adjustment
    type_bonus = _EVENT_TYPE_BONUS.get(classification.event_type, 0.0)

    # Risk penalty
    risk_penalty = -0.5 if classification.risk_flag else 0.0

    total = mention_score + source_score + confidence_score + type_bonus + risk_penalty
    return max(total, 0.0)


def _determine_status(
    score: float,
    classification: ClassificationResult,
    watch_threshold: float,
    reject_risk_below: float,
) -> tuple[CandidateStatus, str | None]:
    """Return (status, rejection_reason)."""
    if classification.risk_flag and classification.confidence < reject_risk_below:
        return CandidateStatus.REJECTED, (
            f"Risk flag with low event confidence "
            f"({classification.confidence:.2f} < {reject_risk_below:.2f})"
        )
    if score >= watch_threshold:
        return CandidateStatus.WATCH, None
    return CandidateStatus.DISCOVERED, None


def _best_classification(
    discovered: DiscoveredTicker,
    record_classifications: Sequence[ClassificationResult],
) -> ClassificationResult:
    """
    Pick the most confident classification from records where this ticker appeared.

    Falls back to the highest-confidence classification overall if no per-record
    match is available.
    """
    from portfolio_automation.discovery.event_classifier import EventType, ClassificationResult

    record_indices = {e.record_index for e in discovered.evidence}
    relevant = [
        cls
        for i, cls in enumerate(record_classifications)
        if i in record_indices
    ]

    if not relevant:
        if record_classifications:
            return max(record_classifications, key=lambda c: c.confidence)
        return ClassificationResult(
            event_type=EventType.UNKNOWN,
            confidence=0.0,
            matched_keywords=[],
            risk_flag=False,
        )

    # Prefer non-unknown with highest confidence
    non_unknown = [c for c in relevant if c.event_type != EventType.UNKNOWN]
    pool = non_unknown if non_unknown else relevant
    return max(pool, key=lambda c: c.confidence)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_candidate(
    discovered: DiscoveredTicker,
    classification: ClassificationResult,
    *,
    now: datetime | None = None,
    watch_threshold: float = 2.0,
    reject_risk_below: float = 0.3,
) -> DiscoveryCandidate:
    """
    Produce a :class:`DiscoveryCandidate` from a single :class:`DiscoveredTicker`
    and its associated :class:`ClassificationResult`.

    The returned candidate always has:
    - ``discovery_only = True``
    - ``sandbox_only = True``
    - ``corroboration_required = True``
    - ``corroboration_met = False``
    - ``corroboration_sources = []``
    """
    ts = (now or datetime.now(timezone.utc)).isoformat()
    score = _compute_score(discovered, classification)
    status, rejection_reason = _determine_status(
        score, classification, watch_threshold, reject_risk_below
    )
    snippets = list({
        e.context for e in discovered.evidence if e.context
    })[:5]

    return DiscoveryCandidate(
        ticker=discovered.ticker,
        status=status,
        score=round(score, 4),
        mention_count=discovered.mention_count,
        unique_source_count=len(discovered.unique_sources),
        event_type=classification.event_type,
        event_confidence=round(classification.confidence, 4),
        risk_flag=classification.risk_flag,
        rejection_reason=rejection_reason,
        first_seen=ts,
        last_seen=ts,
        evidence_snippets=snippets,
    )


def evaluate_candidates(
    discovered_tickers: list[DiscoveredTicker],
    record_classifications: list[ClassificationResult],
    *,
    now: datetime | None = None,
    watch_threshold: float = 2.0,
    reject_risk_below: float = 0.3,
) -> list[DiscoveryCandidate]:
    """
    Score and assign statuses to all discovered tickers.

    Parameters
    ----------
    discovered_tickers:
        Output of :func:`~portfolio_automation.discovery.news_ticker_discovery.extract_tickers`.
    record_classifications:
        One :class:`ClassificationResult` per input record (aligned by index).
    now:
        Timestamp override for testing.
    watch_threshold:
        Minimum score for WATCH status.
    reject_risk_below:
        Confidence floor below which a risk-flagged event triggers REJECTED.

    Returns
    -------
    List of :class:`DiscoveryCandidate`, sorted: WATCH first, then DISCOVERED,
    then REJECTED; within each group sorted by score descending.
    """
    candidates: list[DiscoveryCandidate] = []
    for discovered in discovered_tickers:
        classification = _best_classification(discovered, record_classifications)
        candidate = score_candidate(
            discovered,
            classification,
            now=now,
            watch_threshold=watch_threshold,
            reject_risk_below=reject_risk_below,
        )
        candidates.append(candidate)

    # Sort: WATCH > DISCOVERED > REJECTED, then by score descending
    _ORDER = {
        CandidateStatus.WATCH: 0,
        CandidateStatus.DISCOVERED: 1,
        CandidateStatus.REJECTED: 2,
    }
    candidates.sort(key=lambda c: (_ORDER.get(c.status, 9), -c.score))
    return candidates
