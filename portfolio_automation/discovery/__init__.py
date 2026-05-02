"""
Discovery Engine Foundation — Public API
=========================================

Sandbox-only, research-only ticker discovery and candidate tracking.

Discovery candidates are NOT buy/sell recommendations.
Discovery candidates are NOT official portfolio actions.
This module does NOT mutate official watchlists or recommendations.
All outputs are written to outputs/sandbox/discovery/ only.

Entry point::

    from portfolio_automation.discovery import run_discovery_engine

    summary = run_discovery_engine(
        records=[{"title": "$NVDA beats earnings", "source": "example"}],
        run_mode="discovery",
        run_id="2026-05-01_discovery",
    )
"""
from portfolio_automation.discovery.news_ticker_discovery import (
    DiscoveredTicker,
    TickerEvidence,
    extract_tickers,
)
from portfolio_automation.discovery.event_classifier import (
    ClassificationResult,
    EventType,
    classify_event,
    classify_record,
)
from portfolio_automation.discovery.candidate_promotion_engine import (
    CandidateStatus,
    DiscoveryCandidate,
    evaluate_candidates,
)
from portfolio_automation.discovery.corroboration import (
    CorroborationResult,
    compute_corroboration,
    CORROBORATION_MET_THRESHOLD,
)
from portfolio_automation.discovery.discovery_memory import (
    DiscoveryMemory,
    MemoryEntry,
)
from portfolio_automation.discovery.discovery_reports import (
    run_discovery_engine,
    write_discovery_reports,
)
from portfolio_automation.discovery.approval_workflow import (
    ApprovalDecision,
    DiscoveryApprovalDecision,
    make_approval_decision,
    record_approval_decision,
    load_approval_decisions,
    build_approval_summary,
)

__all__ = [
    # ticker extraction
    "DiscoveredTicker",
    "TickerEvidence",
    "extract_tickers",
    # event classification
    "ClassificationResult",
    "EventType",
    "classify_event",
    "classify_record",
    # candidate scoring
    "CandidateStatus",
    "DiscoveryCandidate",
    "evaluate_candidates",
    # corroboration
    "CorroborationResult",
    "compute_corroboration",
    "CORROBORATION_MET_THRESHOLD",
    # memory
    "DiscoveryMemory",
    "MemoryEntry",
    # reports / orchestration
    "run_discovery_engine",
    "write_discovery_reports",
    # approval workflow (sandbox audit layer)
    "ApprovalDecision",
    "DiscoveryApprovalDecision",
    "make_approval_decision",
    "record_approval_decision",
    "load_approval_decisions",
    "build_approval_summary",
]
