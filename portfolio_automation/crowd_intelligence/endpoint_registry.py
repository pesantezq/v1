"""Crowd-intelligence FMP endpoint registry (probe/adapter source of truth).

Rich candidate list across social / news / analyst / insider / congress / market-
attention categories. The capability probe confirms which are actually reachable on
the current FMP plan; Phase-2 adapters read ``enabled_after_probe`` (set from the
persisted capability map). Paths for the net-new categories are best-effort — a
``NOT_FOUND`` from the probe flags a path to correct, not a plan lock.

Compliance: every path here that is not already in ``fmp_endpoint_registry.REGISTRY``
is mirrored there (NOT in STABLE_METHOD_MAP) so the canonical compliance layer
governs it. See docs/CROWD_INTELLIGENCE.md.
"""
from __future__ import annotations

from typing import Any

_DAY = 86400

# Endpoint ids already proven on Starter (registered + called today) — the probe
# SKIPS these; they form Phase-2's guaranteed working floor.
CONFIRMED_BASELINE: set[str] = {"stock_news_search", "ratings_snapshot", "ratings_historical"}

# Each entry: endpoint_id, provider, path, params_template, category, priority,
# expected_fields, min_plan_assumption, legacy, enabled_after_probe, ttl_seconds, run_modes.
ENTRIES: list[dict[str, Any]] = [
    # ── A. Direct social sentiment (legacy, probe-only) ──────────────────────
    {"endpoint_id": "historical_social_sentiment", "provider": "fmp",
     "path": "/api/v4/historical/social-sentiment",
     "params_template": {"symbol": "{symbol}", "page": 0},
     "category": "social", "priority": "P3",
     "expected_fields": ["stocktwitsPosts", "twitterPosts", "date"],
     "min_plan_assumption": "premium", "legacy": True,
     "enabled_after_probe": False, "ttl_seconds": _DAY, "run_modes": ["discovery"]},
    {"endpoint_id": "social_sentiment_legacy", "provider": "fmp",
     "path": "/api/v4/social-sentiment",
     "params_template": {"symbol": "{symbol}", "limit": 1},
     "category": "social", "priority": "P3",
     "expected_fields": ["sentiment", "date"],
     "min_plan_assumption": "legacy", "legacy": True,
     "enabled_after_probe": False, "ttl_seconds": _DAY, "run_modes": ["discovery"]},
    {"endpoint_id": "stock_news_sentiment_rss", "provider": "fmp",
     "path": "/api/v4/stock-news-sentiments-rss-feed",
     "params_template": {"page": 0},
     "category": "news_sentiment", "priority": "P2",
     "expected_fields": ["sentiment", "title"],
     "min_plan_assumption": "legacy", "legacy": True,
     "enabled_after_probe": False, "ttl_seconds": 3600, "run_modes": ["daily", "discovery"]},

    # ── B. Starter-assumed news ──────────────────────────────────────────────
    {"endpoint_id": "fmp_articles", "provider": "fmp",
     "path": "/stable/fmp-articles", "params_template": {"page": 0, "limit": 1},
     "category": "news", "priority": "P2", "expected_fields": ["title", "date"],
     "min_plan_assumption": "starter", "legacy": False,
     "enabled_after_probe": False, "ttl_seconds": 3600, "run_modes": ["daily", "discovery"]},
    {"endpoint_id": "general_news", "provider": "fmp",
     "path": "/stable/news/general-latest", "params_template": {"page": 0, "limit": 1},
     "category": "news", "priority": "P2", "expected_fields": ["title", "publishedDate"],
     "min_plan_assumption": "starter", "legacy": False,
     "enabled_after_probe": False, "ttl_seconds": 3600, "run_modes": ["daily", "discovery"]},
    {"endpoint_id": "stock_news_latest", "provider": "fmp",
     "path": "/stable/news/stock-latest", "params_template": {"page": 0, "limit": 1},
     "category": "news", "priority": "P1", "expected_fields": ["symbol", "title"],
     "min_plan_assumption": "starter", "legacy": False,
     "enabled_after_probe": False, "ttl_seconds": 1800, "run_modes": ["daily", "discovery"]},
    {"endpoint_id": "stock_news_search", "provider": "fmp",
     "path": "/stable/news/stock", "params_template": {"symbols": "{symbol}"},
     "category": "news", "priority": "P0", "expected_fields": ["symbol", "title"],
     "min_plan_assumption": "starter", "legacy": False,
     "enabled_after_probe": True, "ttl_seconds": 1800, "run_modes": ["daily", "discovery"]},
    {"endpoint_id": "crypto_news", "provider": "fmp",
     "path": "/stable/news/crypto-latest", "params_template": {"page": 0, "limit": 1},
     "category": "news", "priority": "P3", "expected_fields": ["title"],
     "min_plan_assumption": "starter", "legacy": False,
     "enabled_after_probe": False, "ttl_seconds": 3600, "run_modes": ["discovery"]},
    {"endpoint_id": "forex_news", "provider": "fmp",
     "path": "/stable/news/forex-latest", "params_template": {"page": 0, "limit": 1},
     "category": "news", "priority": "P3", "expected_fields": ["title"],
     "min_plan_assumption": "starter", "legacy": False,
     "enabled_after_probe": False, "ttl_seconds": 3600, "run_modes": ["discovery"]},

    # ── C. Analyst sentiment ─────────────────────────────────────────────────
    {"endpoint_id": "ratings_snapshot", "provider": "fmp",
     "path": "/stable/ratings-snapshot", "params_template": {"symbol": "{symbol}"},
     "category": "analyst", "priority": "P1", "expected_fields": ["rating", "symbol"],
     "min_plan_assumption": "starter", "legacy": False,
     "enabled_after_probe": True, "ttl_seconds": _DAY, "run_modes": ["daily", "discovery"]},
    {"endpoint_id": "ratings_historical", "provider": "fmp",
     "path": "/stable/historical-ratings", "params_template": {"symbol": "{symbol}", "limit": 1},
     "category": "analyst", "priority": "P2", "expected_fields": ["rating", "date"],
     "min_plan_assumption": "starter", "legacy": False,
     "enabled_after_probe": True, "ttl_seconds": _DAY, "run_modes": ["daily", "discovery"]},
    {"endpoint_id": "stock_grades", "provider": "fmp",
     "path": "/stable/grades", "params_template": {"symbol": "{symbol}"},
     "category": "analyst", "priority": "P2", "expected_fields": ["newGrade", "gradingCompany"],
     "min_plan_assumption": "starter", "legacy": False,
     "enabled_after_probe": False, "ttl_seconds": _DAY, "run_modes": ["daily", "discovery"]},
    {"endpoint_id": "grades_consensus", "provider": "fmp",
     "path": "/stable/grades-consensus", "params_template": {"symbol": "{symbol}"},
     "category": "analyst", "priority": "P2", "expected_fields": ["consensus", "strongBuy"],
     "min_plan_assumption": "starter", "legacy": False,
     "enabled_after_probe": False, "ttl_seconds": _DAY, "run_modes": ["daily", "discovery"]},

    # ── D. Insider / congress behavior ───────────────────────────────────────
    {"endpoint_id": "latest_insider_trading", "provider": "fmp",
     "path": "/stable/insider-trading/latest", "params_template": {"page": 0, "limit": 1},
     "category": "insider", "priority": "P2", "expected_fields": ["symbol", "transactionType"],
     "min_plan_assumption": "starter", "legacy": False,
     "enabled_after_probe": False, "ttl_seconds": _DAY, "run_modes": ["daily", "discovery"]},
    {"endpoint_id": "search_insider_trades", "provider": "fmp",
     "path": "/stable/insider-trading/search", "params_template": {"symbol": "{symbol}", "limit": 1},
     "category": "insider", "priority": "P2", "expected_fields": ["symbol", "transactionType"],
     "min_plan_assumption": "starter", "legacy": False,
     "enabled_after_probe": False, "ttl_seconds": _DAY, "run_modes": ["daily", "discovery"]},
    {"endpoint_id": "insider_trade_statistics", "provider": "fmp",
     "path": "/stable/insider-trading/statistics", "params_template": {"symbol": "{symbol}"},
     "category": "insider", "priority": "P3", "expected_fields": ["symbol", "buySellRatio"],
     "min_plan_assumption": "starter", "legacy": False,
     "enabled_after_probe": False, "ttl_seconds": _DAY, "run_modes": ["discovery"]},
    {"endpoint_id": "senate_trading", "provider": "fmp",
     "path": "/stable/senate-trades", "params_template": {"symbol": "{symbol}"},
     "category": "congress", "priority": "P3", "expected_fields": ["symbol", "office"],
     "min_plan_assumption": "starter", "legacy": False,
     "enabled_after_probe": False, "ttl_seconds": _DAY, "run_modes": ["discovery"]},
    {"endpoint_id": "senate_trading_by_name", "provider": "fmp",
     "path": "/stable/senate-trades-by-name", "params_template": {"name": "Pelosi"},
     "category": "congress", "priority": "P3", "expected_fields": ["symbol", "office"],
     "min_plan_assumption": "starter", "legacy": False,
     "enabled_after_probe": False, "ttl_seconds": _DAY, "run_modes": ["discovery"]},
    {"endpoint_id": "house_trading", "provider": "fmp",
     "path": "/stable/house-trades", "params_template": {"symbol": "{symbol}"},
     "category": "congress", "priority": "P3", "expected_fields": ["symbol", "office"],
     "min_plan_assumption": "starter", "legacy": False,
     "enabled_after_probe": False, "ttl_seconds": _DAY, "run_modes": ["discovery"]},
    {"endpoint_id": "house_trading_by_name", "provider": "fmp",
     "path": "/stable/house-trades-by-name", "params_template": {"name": "Pelosi"},
     "category": "congress", "priority": "P3", "expected_fields": ["symbol", "office"],
     "min_plan_assumption": "starter", "legacy": False,
     "enabled_after_probe": False, "ttl_seconds": _DAY, "run_modes": ["discovery"]},

    # ── E. Market attention ──────────────────────────────────────────────────
    {"endpoint_id": "biggest_gainers", "provider": "fmp",
     "path": "/stable/biggest-gainers", "params_template": {},
     "category": "attention", "priority": "P2", "expected_fields": ["symbol", "changesPercentage"],
     "min_plan_assumption": "starter", "legacy": False,
     "enabled_after_probe": False, "ttl_seconds": 1800, "run_modes": ["daily", "discovery"]},
    {"endpoint_id": "biggest_losers", "provider": "fmp",
     "path": "/stable/biggest-losers", "params_template": {},
     "category": "attention", "priority": "P2", "expected_fields": ["symbol", "changesPercentage"],
     "min_plan_assumption": "starter", "legacy": False,
     "enabled_after_probe": False, "ttl_seconds": 1800, "run_modes": ["daily", "discovery"]},
    {"endpoint_id": "most_active", "provider": "fmp",
     "path": "/stable/most-actives", "params_template": {},
     "category": "attention", "priority": "P2", "expected_fields": ["symbol", "volume"],
     "min_plan_assumption": "starter", "legacy": False,
     "enabled_after_probe": False, "ttl_seconds": 1800, "run_modes": ["daily", "discovery"]},
    {"endpoint_id": "sector_performance_snapshot", "provider": "fmp",
     "path": "/stable/sector-performance-snapshot", "params_template": {"date": "2026-06-12"},
     "category": "attention", "priority": "P2", "expected_fields": ["sector", "changesPercentage"],
     "min_plan_assumption": "starter", "legacy": False,
     "enabled_after_probe": False, "ttl_seconds": 3600, "run_modes": ["daily", "discovery"]},
    {"endpoint_id": "industry_performance_snapshot", "provider": "fmp",
     "path": "/stable/industry-performance-snapshot", "params_template": {"date": "2026-06-12"},
     "category": "attention", "priority": "P3", "expected_fields": ["industry", "changesPercentage"],
     "min_plan_assumption": "starter", "legacy": False,
     "enabled_after_probe": False, "ttl_seconds": 3600, "run_modes": ["discovery"]},
]

_REQUIRED_KEYS = {"endpoint_id", "provider", "path", "params_template", "category",
                  "priority", "expected_fields", "min_plan_assumption", "legacy",
                  "enabled_after_probe", "ttl_seconds", "run_modes"}


def all_entries() -> list[dict]:
    return list(ENTRIES)


def probe_targets() -> list[dict]:
    """Candidates to probe — everything except the already-confirmed baseline."""
    return [e for e in ENTRIES if e["endpoint_id"] not in CONFIRMED_BASELINE]


def by_category(category: str) -> list[dict]:
    return [e for e in ENTRIES if e["category"] == category]


_BY_ID = {e["endpoint_id"]: e for e in ENTRIES}


def entry(endpoint_id: str) -> dict | None:
    return _BY_ID.get(endpoint_id)
