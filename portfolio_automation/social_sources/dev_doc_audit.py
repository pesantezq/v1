"""
Crowd-source developer-doc audit — the validated, research-backed source catalog.

Each entry was verified against OFFICIAL developer documentation on 2026-06-14
(blogs / scraper marketplaces were rejected as non-authoritative). This module is
the single source of truth for the audit; it emits both the JSON artifact and the
docs/CROWD_SOURCE_DEV_DOC_AUDIT.md table.

No-extra-cost policy: a source is only ``active`` if it is reachable with no paid
plan and no approval. Paid / approval-gated sources are probe-only or blocked.
"""
from __future__ import annotations

from typing import Any

SCHEMA_VERSION = "1"

# Verified audit. Field order mirrors the spec's required audit schema.
SOURCE_AUDIT: list[dict[str, Any]] = [
    {
        "source_name": "apewisdom",
        "official_docs_url_or_source": "https://apewisdom.io/api/ (+ live no-auth fetch)",
        "endpoint_candidates": [
            "https://apewisdom.io/api/v1.0/filter/{filter}",
            "https://apewisdom.io/api/v1.0/filter/{filter}/page/{pageNbr}",
        ],
        "auth_required": False,
        "known_params": {"filter": ["all-stocks", "wallstreetbets", "stocks", "options", "investing", "4chan"], "pageNbr": "int (optional)"},
        "response_fields": ["rank", "ticker", "name", "mentions", "upvotes", "rank_24h_ago", "mentions_24h_ago", "(top-level) count", "pages", "current_page"],
        "pagination_model": "inline top-level pages/current_page/count; ~100 results/page; /page/{n}",
        "rate_limit_notes_if_available": "none published — be polite client-side (bounded pages + caching)",
        "cost_or_entitlement_status": "free, no API key (de facto; no written ToS/pricing)",
        "allowed_under_no_extra_cost": True,
        "implementation_status": "active",
        "risk_notes": "no published rate limit/ToS or versioning guarantee — parse defensively, cache, throttle",
        "recommended_connector_behavior": "read-only GET, no auth, bounded pages, degrade on missing keys, store aggregate counts only (no raw post text)",
    },
    {
        "source_name": "fmp_social_sentiment",
        "official_docs_url_or_source": "https://site.financialmodelingprep.com/developer/docs/social-sentiment-api (Legacy label) + FMP example repo",
        "endpoint_candidates": [
            "/stable/historical/social-sentiment (current)",
            "/api/v4/historical/social-sentiment (legacy fallback)",
            "/stable/social-sentiments/trending", "/stable/social-sentiments/change",
        ],
        "auth_required": True,
        "known_params": {"symbol": "AAPL", "page": "0-indexed", "limit": "optional", "apikey": "required"},
        "response_fields": ["date", "symbol", "stocktwitsPosts", "twitterPosts", "stocktwitsSentiment", "twitterSentiment", "sentiment", "absoluteIndex", "relativeIndex", "generalPerception"],
        "pagination_model": "page-based (0-indexed), optional limit",
        "rate_limit_notes_if_available": "plan-level (repo posture: 250/min subscription, daily cap removed)",
        "cost_or_entitlement_status": "PAID — Starter+ entitlement; NOT on free/Basic tier",
        "allowed_under_no_extra_cost": False,
        "implementation_status": "probe_only",
        "risk_notes": "assume not_entitled until a live probe proves otherwise; never upgrade; registered in fmp_endpoint_registry (P3 premium_optional)",
        "recommended_connector_behavior": "single entitlement probe vs /stable/ (v4 fallback); 200+rows=entitled, 402/403/empty/Error Message=not_entitled; budget-guard first; observe-only",
    },
    {
        "source_name": "stocktwits",
        "official_docs_url_or_source": "https://api.stocktwits.com/developers (registrations CLOSED) + Firestream partner docs",
        "endpoint_candidates": [
            "legacy /api/2/streams/symbol/{S}.json + /api/2/trending/symbols.json (docs now 404 — unsanctioned)",
            "current Firestream/Sentiment v2 (partner-gated, Basic-auth provisioned)",
        ],
        "auth_required": True,
        "known_params": {"symbol": "AAPL", "seq_id": "stream resume cursor"},
        "response_fields": ["messageVolume", "sentiment", "timeframes", "(legacy) symbol stream messages"],
        "pagination_model": "streaming SSE cursor (seq_id, 24h) / point-in-time sentiment snapshot",
        "rate_limit_notes_if_available": "not officially documented (community '200/hr' is unverified)",
        "cost_or_entitlement_status": "partner/commercial, approval-gated; no published free tier",
        "allowed_under_no_extra_cost": False,
        "implementation_status": "requires_manual_review",
        "risk_notes": "free program suspended; legacy endpoints undocumented (private-endpoint use forbidden); storage/redistribution terms unpublished",
        "recommended_connector_behavior": "NO network call; inert probe; manual partner inquiry (developers@stocktwits.com) is the only sanctioned path",
    },
    {
        "source_name": "finnhub_social",
        "official_docs_url_or_source": "https://finnhub.io/docs/api/social-sentiment + official SDK + issue tracker #557",
        "endpoint_candidates": ["GET /api/v1/stock/social-sentiment?symbol=AAPL&token=KEY"],
        "auth_required": True,
        "known_params": {"symbol": "AAPL", "from": "YYYY-MM-DD", "to": "YYYY-MM-DD", "token": "required"},
        "response_fields": ["data[] (or legacy reddit[]/twitter[]): atTime, mention, positiveMention, negativeMention, positiveScore, negativeScore, score"],
        "pagination_model": "none (date-window bounded single object)",
        "rate_limit_notes_if_available": "free tier 60 calls/min (account-wide)",
        "cost_or_entitlement_status": "PREMIUM-only; free key → HTTP 403 'You don't have access to this resource.'",
        "allowed_under_no_extra_cost": False,
        "implementation_status": "probe_only",
        "risk_notes": "403 is the EXPECTED non-entitled state, not an error; skip entirely unless FINNHUB_API_KEY set; never buy premium",
        "recommended_connector_behavior": "probe only if key present; 200+rows=entitled else not_entitled; cache verdict; stay dormant unless entitled",
    },
    {
        "source_name": "quiver_wsb",
        "official_docs_url_or_source": "https://www.quiverquant.com/api-setup/ + pricing + official python-api source",
        "endpoint_candidates": ["/beta/live/wallstreetbets", "/beta/historical/wallstreetbets/{ticker}"],
        "auth_required": True,
        "known_params": {"ticker": "path", "date_from": "YYYYMMDD", "date_to": "YYYYMMDD", "count_all": "true"},
        "response_fields": ["Date/Time", "Ticker", "Mentions (unverified field name)", "Rank (unverified)", "Sentiment (unverified)"],
        "pagination_model": "none (full array per call, client-side date filter)",
        "rate_limit_notes_if_available": "not documented",
        "cost_or_entitlement_status": "PAID — Trader tier ~$75/mo for WSB; no free entitlement; no key in repo",
        "allowed_under_no_extra_cost": False,
        "implementation_status": "blocked_no_extra_cost",
        "risk_notes": "recurring billing; auth scheme is 'Authorization: Token' (not Bearer); never auto-subscribe",
        "recommended_connector_behavior": "inert; no network; activate only with a pre-existing QUIVER_API_KEY AND explicit allow_paid_sources opt-in",
    },
]


def build_dev_doc_audit(*, run_id: str, run_mode: str, created_at: str) -> dict[str, Any]:
    """Build the crowd_source_dev_doc_audit.json payload."""
    active = [s["source_name"] for s in SOURCE_AUDIT if s["implementation_status"] == "active"]
    probe = [s["source_name"] for s in SOURCE_AUDIT if s["implementation_status"] == "probe_only"]
    blocked = [s["source_name"] for s in SOURCE_AUDIT if s["implementation_status"] in ("blocked_no_extra_cost", "requires_manual_review")]
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "crowd_source_dev_doc_audit",
        "run_id": run_id,
        "run_mode": run_mode,
        "created_at": created_at,
        "source_status": "ok",
        "observe_only": True,
        "sandbox_only": True,
        "no_trade": True,
        "cost_policy": "no_extra_cost",
        "audited_count": len(SOURCE_AUDIT),
        "active_sources": active,
        "probe_only_sources": probe,
        "blocked_sources": blocked,
        "warnings": [],
        "records": SOURCE_AUDIT,
    }


def render_dev_doc_audit_md(payload: dict[str, Any]) -> str:
    """Render docs/CROWD_SOURCE_DEV_DOC_AUDIT.md from the audit payload."""
    lines = [
        "# Crowd Source Dev-Doc Audit",
        "",
        "_Validated against official developer documentation on 2026-06-14. Blogs and "
        "scraper marketplaces were rejected as non-authoritative. No-extra-cost policy: "
        "a source is `active` only if reachable with no paid plan and no approval._",
        "",
        f"- Active: **{', '.join(payload['active_sources']) or 'none'}**",
        f"- Probe-only: **{', '.join(payload['probe_only_sources']) or 'none'}**",
        f"- Blocked / manual-review: **{', '.join(payload['blocked_sources']) or 'none'}**",
        "",
    ]
    for s in payload["records"]:
        lines.append(f"## {s['source_name']} — `{s['implementation_status']}`")
        lines.append(f"- **Docs:** {s['official_docs_url_or_source']}")
        lines.append(f"- **Endpoints:** {'; '.join(s['endpoint_candidates'])}")
        lines.append(f"- **Auth required:** {s['auth_required']}")
        lines.append(f"- **Cost / entitlement:** {s['cost_or_entitlement_status']}")
        lines.append(f"- **Allowed under no-extra-cost:** {s['allowed_under_no_extra_cost']}")
        lines.append(f"- **Response fields:** {', '.join(s['response_fields'])}")
        lines.append(f"- **Pagination:** {s['pagination_model']}")
        lines.append(f"- **Rate limit:** {s['rate_limit_notes_if_available']}")
        lines.append(f"- **Risk notes:** {s['risk_notes']}")
        lines.append(f"- **Connector behavior:** {s['recommended_connector_behavior']}")
        lines.append("")
    lines.append("_Sandbox research intelligence only. Not a trade recommendation. "
                 "No paid data sources enabled._")
    return "\n".join(lines) + "\n"
