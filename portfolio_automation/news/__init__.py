"""
News Intelligence — Public API
================================

Observe-only, rules-first FMP news intelligence layer.

This module ingests raw FMP news articles and emits structured evidence
artifacts for official holdings, watchlist symbols, ETFs/sectors/themes, and
sandbox discovery candidates.

Safety constraints (hardcoded):
  - observe_only: true
  - no_trade: true
  - not_recommendation: true
  - No BUY/SELL/HOLD/ACTIONABLE/PROMOTED statuses emitted.
  - No official portfolio or watchlist mutation.
  - No discovery candidate promotion.
  - No LLM or AI calls — deterministic rules only.

Artifacts produced:
  - outputs/latest/news_intelligence.json   (LATEST namespace)
  - outputs/latest/news_intelligence.md     (LATEST namespace)
  - outputs/sandbox/discovery/news_candidate_evidence.json  (SANDBOX, optional)

Entry point::

    from portfolio_automation.news import run_fmp_news_intelligence

    result = run_fmp_news_intelligence(
        raw_articles=[...],
        holdings=["NVDA", "MSFT"],
        watchlist=["AAPL", "AMZN"],
        base_dir="outputs",
    )
"""
from portfolio_automation.news.fmp_news_intelligence import (
    NormalizedArticle,
    ThemeMatch,
    EvidencePacket,
    normalize_news_articles,
    dedupe_news_articles,
    extract_news_entities,
    classify_news_themes,
    build_news_evidence_packets,
    write_news_intelligence_report,
    run_fmp_news_intelligence,
)

__all__ = [
    "NormalizedArticle",
    "ThemeMatch",
    "EvidencePacket",
    "normalize_news_articles",
    "dedupe_news_articles",
    "extract_news_entities",
    "classify_news_themes",
    "build_news_evidence_packets",
    "write_news_intelligence_report",
    "run_fmp_news_intelligence",
]
