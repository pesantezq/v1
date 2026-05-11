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
from portfolio_automation.discovery.discovery_replay import (
    run_discovery_replay,
    write_discovery_replay_report,
    load_discovery_replay_inputs,
    evaluate_discovery_candidate_outcomes,
    summarize_discovery_replay_results,
)
from portfolio_automation.discovery.news_integration import (
    load_news_intelligence,
    load_news_candidate_evidence,
    load_emerging_candidates,
    load_rejected_candidates,
    match_evidence_to_candidates,
    enrich_candidates,
    build_integration_summary,
    write_news_integration_artifacts,
    run_discovery_news_integration,
)
from portfolio_automation.discovery.automatic_promotion_governance import (
    PromotionGates,
    PromotionEligibilityResult,
    PromotionDecision,
    AutomaticPromotionReport,
    UnsafeAutomaticPromotionArtifactError,
    ALLOWED_STATUSES,
    FORBIDDEN_STATUSES,
    load_automatic_promotion_inputs,
    evaluate_candidate_promotion,
    build_automatic_promotion_report,
    render_automatic_promotion_markdown,
    write_automatic_promotion_report,
    run_automatic_promotion_governance,
    validate_automatic_promotion_safety,
    sanitize_automatic_promotion_text,
    sanitize_nested_automatic_promotion_payload,
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
    # discovery replay (sandbox backtest evaluation)
    "run_discovery_replay",
    "write_discovery_replay_report",
    "load_discovery_replay_inputs",
    "evaluate_discovery_candidate_outcomes",
    "summarize_discovery_replay_results",
    # discovery news integration
    "load_news_intelligence",
    "load_news_candidate_evidence",
    "load_emerging_candidates",
    "load_rejected_candidates",
    "match_evidence_to_candidates",
    "enrich_candidates",
    "build_integration_summary",
    "write_news_integration_artifacts",
    "run_discovery_news_integration",
    # automatic promotion governance
    "PromotionGates",
    "PromotionEligibilityResult",
    "PromotionDecision",
    "AutomaticPromotionReport",
    "UnsafeAutomaticPromotionArtifactError",
    "ALLOWED_STATUSES",
    "FORBIDDEN_STATUSES",
    "load_automatic_promotion_inputs",
    "evaluate_candidate_promotion",
    "build_automatic_promotion_report",
    "render_automatic_promotion_markdown",
    "write_automatic_promotion_report",
    "run_automatic_promotion_governance",
    "validate_automatic_promotion_safety",
    "sanitize_automatic_promotion_text",
    "sanitize_nested_automatic_promotion_payload",
]
