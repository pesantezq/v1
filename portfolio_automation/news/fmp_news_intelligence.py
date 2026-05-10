"""
FMP News Intelligence Layer
============================

Observe-only, rules-first news evidence foundation.

Safety invariants (all hardcoded):
  - observe_only: true
  - no_trade: true
  - not_recommendation: true
  - No BUY/SELL/HOLD/ACTIONABLE/PROMOTED statuses.
  - No official portfolio, watchlist, allocation, recommendation, or scoring mutation.
  - No discovery candidate promotion.
  - No LLM/AI calls — deterministic keyword rules only.

Public API:
  normalize_news_articles(raw_articles)
  dedupe_news_articles(articles)
  extract_news_entities(article)
  classify_news_themes(article)
  build_news_evidence_packets(articles, holdings, watchlist, discovery_candidates)
  write_news_intelligence_report(base_dir, raw_articles, ...)
  run_fmp_news_intelligence(raw_articles, holdings, watchlist, discovery_candidates, ...)
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import (
    OutputNamespace,
    safe_write_json,
    safe_write_text,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Safety constants
# ---------------------------------------------------------------------------

_OBSERVE_ONLY = True
_NO_TRADE = True
_NOT_RECOMMENDATION = True
_SOURCE_LABEL = "fmp_news_intelligence_layer"

_DISCLAIMER = (
    "News Intelligence is observe-only research context. "
    "It is not a buy/sell recommendation and does not modify "
    "official portfolio/watchlist state."
)

# ---------------------------------------------------------------------------
# Company / entity alias map  →  canonical ticker
# Keys are lowercased for case-insensitive matching.
# ---------------------------------------------------------------------------

COMPANY_ALIAS_MAP: dict[str, str] = {
    # Mega-cap tech
    "nvidia": "NVDA",
    "nvdia": "NVDA",  # common typo
    "apple": "AAPL",
    "microsoft": "MSFT",
    "amazon": "AMZN",
    "alphabet": "GOOGL",
    "google": "GOOGL",
    "meta": "META",
    "facebook": "META",
    "tesla": "TSLA",
    "netflix": "NFLX",
    "salesforce": "CRM",
    "adobe": "ADBE",
    "oracle": "ORCL",
    "intel": "INTC",
    "amd": "AMD",
    "advanced micro devices": "AMD",
    "qualcomm": "QCOM",
    "broadcom": "AVGO",
    "texas instruments": "TXN",
    "micron": "MU",
    "applied materials": "AMAT",
    "lam research": "LRCX",
    "asml": "ASML",
    "taiwan semiconductor": "TSM",
    "tsmc": "TSM",
    # Finance
    "jpmorgan": "JPM",
    "jp morgan": "JPM",
    "bank of america": "BAC",
    "goldman sachs": "GS",
    "morgan stanley": "MS",
    "wells fargo": "WFC",
    "citigroup": "C",
    "citi": "C",
    "berkshire hathaway": "BRK.B",
    "blackrock": "BLK",
    "visa": "V",
    "mastercard": "MA",
    "paypal": "PYPL",
    # Energy / commodity
    "exxonmobil": "XOM",
    "exxon": "XOM",
    "chevron": "CVX",
    "conocophillips": "COP",
    # Healthcare
    "johnson & johnson": "JNJ",
    "johnson and johnson": "JNJ",
    "unitedhealth": "UNH",
    "pfizer": "PFE",
    "abbvie": "ABBV",
    "merck": "MRK",
    "eli lilly": "LLY",
    "lilly": "LLY",
    # Consumer
    "walmart": "WMT",
    "target": "TGT",
    "costco": "COST",
    "home depot": "HD",
    "mcdonalds": "MCD",
    "mcdonald's": "MCD",
    "starbucks": "SBUX",
    "coca-cola": "KO",
    "coca cola": "KO",
    "pepsico": "PEP",
    "pepsi": "PEP",
    # ETFs / indices (map to ticker)
    "nasdaq 100": "QQQ",
    "nasdaq-100": "QQQ",
    "qqq": "QQQ",
    "s&p 500": "SPY",
    "s&p500": "SPY",
    "spy": "SPY",
}

# ---------------------------------------------------------------------------
# Theme keyword tables
# Keys are theme names; values are keyword lists (lowercased).
# ---------------------------------------------------------------------------

THEME_KEYWORDS: dict[str, list[str]] = {
    "ai_infrastructure": [
        "artificial intelligence", "ai infrastructure", "large language model",
        "llm", "generative ai", "gpu cluster", "data center ai", "ai chips",
        "ai accelerator", "model training", "inference", "foundation model",
        "openai", "anthropic", "nvidia ai", "ai workload",
    ],
    "semiconductors": [
        "semiconductor", "chip", "wafer", "fab", "foundry", "node",
        "advanced packaging", "hbm", "memory chip", "chip shortage",
        "chip demand", "chipmaker", "integrated circuit",
    ],
    "cloud": [
        "cloud computing", "cloud services", "aws", "azure", "google cloud",
        "cloud revenue", "cloud migration", "saas", "paas", "iaas",
        "hybrid cloud", "multi-cloud", "cloud growth",
    ],
    "earnings_guidance": [
        "earnings", "quarterly results", "beat estimates", "missed estimates",
        "eps beat", "eps miss", "revenue beat", "revenue miss",
        "guidance", "raised guidance", "lowered guidance", "outlook",
        "forecast", "full-year", "q1", "q2", "q3", "q4", "fiscal year",
        "profit", "net income", "operating income",
    ],
    "rates_inflation": [
        "inflation", "cpi", "ppi", "interest rate", "rate hike", "rate cut",
        "rate decision", "yield curve", "treasury yield", "bond yield",
        "10-year yield", "fed funds rate", "real rate", "disinflation",
        "deflation", "consumer prices", "producer prices",
    ],
    "fed_policy": [
        "federal reserve", "fed", "fomc", "powell", "central bank",
        "monetary policy", "quantitative tightening", "qt",
        "quantitative easing", "qe", "tapering", "balance sheet",
        "fed minutes", "rate decision", "fed chair",
    ],
    "legal_regulatory_risk": [
        "lawsuit", "litigation", "regulatory", "investigation", "sec",
        "ftc", "doj", "antitrust", "fine", "penalty", "settlement",
        "class action", "probe", "subpoena", "enforcement action",
        "compliance", "regulatory scrutiny", "fraud", "misconduct",
    ],
    "mna": [
        "merger", "acquisition", "takeover", "buyout", "deal",
        "strategic acquisition", "m&a", "acquires", "acquired by",
        "merger agreement", "definitive agreement", "all-cash deal",
        "all-stock deal", "hostile bid", "due diligence",
    ],
    "geopolitical_risk": [
        "tariff", "trade war", "sanctions", "geopolitical", "export controls",
        "china trade", "supply chain disruption", "taiwan strait",
        "nato", "russia", "ukraine", "middle east", "conflict",
        "de-risking", "reshoring", "onshoring",
    ],
    "energy": [
        "oil price", "crude oil", "brent", "wti", "natural gas", "lng",
        "energy transition", "renewable energy", "solar", "wind energy",
        "battery storage", "ev charging", "electric vehicle", "opec",
        "refinery", "upstream", "downstream",
    ],
    "financials": [
        "bank earnings", "net interest margin", "loan growth", "credit quality",
        "nonperforming loans", "capital ratio", "stress test", "bank sector",
        "financial services", "insurance", "asset management",
        "wealth management", "investment banking", "trading revenue",
    ],
    "gold_safe_haven": [
        "gold", "gold price", "precious metals", "gold miners",
        "safe haven", "flight to safety", "gld", "gdx",
        "gold rally", "bullion", "gold demand",
    ],
    "sector_rotation": [
        "sector rotation", "rotation into", "rotation out of",
        "defensive", "cyclical", "value rotation", "growth to value",
        "sector leadership", "risk-off", "risk-on",
    ],
    "consumer_demand": [
        "consumer spending", "retail sales", "consumer confidence",
        "consumer sentiment", "discretionary spending", "demand outlook",
        "holiday sales", "e-commerce", "online retail",
    ],
    "valuation": [
        "overvalued", "undervalued", "pe ratio", "price-to-earnings",
        "valuation concern", "expensive", "stretched valuation",
        "fair value", "intrinsic value", "discount", "premium",
    ],
    "market_sentiment": [
        "market rally", "market selloff", "bull market", "bear market",
        "volatility", "vix", "fear and greed", "overbought", "oversold",
        "market breadth", "risk appetite", "investor sentiment",
        "momentum", "correction", "rebound",
    ],
}

# Risk flag keywords (negative signals)
_RISK_KEYWORDS: list[str] = [
    "lawsuit", "fraud", "fine", "penalty", "investigation", "probe",
    "recall", "safety concern", "regulatory action", "class action",
    "bankruptcy", "default", "debt crisis", "downgrade", "miss",
    "missed estimates", "revenue miss", "guidance cut", "lowered guidance",
    "supply chain issue", "shortage", "tariff impact", "sanctions",
    "geopolitical risk", "trade war", "data breach", "cybersecurity incident",
]

# Catalyst flag keywords (positive signals)
_CATALYST_KEYWORDS: list[str] = [
    "beat estimates", "revenue beat", "raised guidance", "record revenue",
    "record profit", "new product", "product launch", "partnership",
    "acquisition", "strategic deal", "buyback", "dividend increase",
    "upgrade", "price target raised", "analyst upgrade",
    "market share gain", "breakthrough", "approval", "fda approval",
    "patent", "contract win", "major contract",
]

# Noise words — not tickers
_NOISE_WORDS: frozenset[str] = frozenset({
    "CEO", "CFO", "COO", "CTO", "CMO", "EVP", "SVP",
    "USA", "US", "UK", "EU", "UN", "NATO", "OPEC", "IMF",
    "GDP", "CPI", "PPI", "PCE", "EPS", "PE", "EV",
    "FED", "SEC", "IRS", "FDIC", "CFTC", "FINRA", "FTC", "DOJ",
    "FOMC", "YOY", "QOQ", "MOM", "TTM",
    "ETF", "IPO", "NAV", "AUM", "REIT",
    "AI", "API", "ML", "NLP", "AR", "VR",
    "LLC", "INC", "LTD", "PLC", "CORP",
    "AM", "PM", "EST", "PST", "UTC", "GMT",
    "THE", "AND", "FOR", "NOT", "BUT", "NEW", "TOP",
    "SET", "OUT", "LOW", "HIGH", "ALL", "ONE",
    "BUY", "SELL", "HOLD",
})

_CASHTAG_RE = re.compile(r'\$([A-Z]{1,5})\b')
_PAREN_TICKER_RE = re.compile(r'\b([A-Z][a-z].{1,30}?)\s+\(([A-Z]{1,5})\)')

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class NormalizedArticle:
    """Normalized representation of one news article."""
    title: str
    text: str
    url: str
    published_at: str
    source: str
    site: str
    tickers: list[str]
    symbols: list[str]
    image: str
    normalized_at: str
    dedup_key: str
    raw: dict = field(default_factory=dict, repr=False)


@dataclass
class ThemeMatch:
    """Result of theme classification for one article."""
    theme: str
    score: float          # 0.0–1.0, proportional to matched terms
    matched_terms: list[str]
    evidence_titles: list[str]


@dataclass
class EvidencePacket:
    """Structured evidence grouped by entity (ticker/ETF/theme)."""
    entity_key: str
    entity_type: str      # "ticker" | "etf" | "theme" | "sector"
    related_tickers: list[str]
    article_count: int
    source_count: int
    latest_published_at: str
    themes: list[str]
    risk_flags: list[str]
    catalyst_flags: list[str]
    sentiment_hint: str   # "positive" | "negative" | "mixed" | "neutral"
    article_refs: list[dict]
    summary_bullets: list[str]
    evidence_lane: str    # "official_monitoring" | "sandbox_discovery_research"
    observe_only: bool = True
    no_trade: bool = True
    not_recommendation: bool = True


# ---------------------------------------------------------------------------
# 1. Normalization
# ---------------------------------------------------------------------------

def _parse_published_at(raw: Any) -> str:
    """Extract a string timestamp from various field names/formats."""
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return ""


def normalize_news_articles(raw_articles: list[dict]) -> list[NormalizedArticle]:
    """
    Normalize a list of raw FMP-style article dicts into NormalizedArticle objects.

    Handles missing/malformed fields gracefully.  Does not mutate input dicts.
    Accepts both raw FMP API responses and the normalized shape returned by
    FMPClient.get_stock_news().
    """
    now_str = datetime.now(timezone.utc).isoformat()
    results: list[NormalizedArticle] = []

    for raw in raw_articles:
        if not isinstance(raw, dict):
            continue

        title = str(raw.get("title") or "").strip()
        if not title:
            continue

        # Text / summary — accept multiple field names
        text = str(
            raw.get("text") or raw.get("summary") or raw.get("description") or ""
        ).strip()

        url = str(raw.get("url") or raw.get("link") or "").strip()
        image = str(raw.get("image") or raw.get("image_url") or "").strip()
        source = str(raw.get("source") or raw.get("site") or "").strip()
        site = str(raw.get("site") or raw.get("source") or "").strip()

        # Published timestamp
        published_at = _parse_published_at(
            raw.get("publishedDate")
            or raw.get("published_at")
            or raw.get("time_published")
            or raw.get("date")
        )

        # Source-provided symbols
        raw_syms: list[str] = []
        for key in ("symbols", "tickers", "symbol"):
            val = raw.get(key)
            if isinstance(val, list):
                raw_syms.extend(str(s).upper().strip() for s in val if s)
            elif isinstance(val, str) and val.strip():
                raw_syms.append(val.upper().strip())
        # ticker_sentiment list (FMPClient normalized shape)
        ts_list = raw.get("ticker_sentiment")
        if isinstance(ts_list, list):
            for ts in ts_list:
                if isinstance(ts, dict):
                    t = str(ts.get("ticker") or "").upper().strip()
                    if t and t not in _NOISE_WORDS and t.isalpha() and len(t) <= 5:
                        raw_syms.append(t)

        symbols = sorted(set(raw_syms))
        tickers = [s for s in symbols if _is_valid_ticker(s)]

        dedup_key = _make_dedup_key(title, published_at, url, source)

        results.append(NormalizedArticle(
            title=title,
            text=text,
            url=url,
            published_at=published_at,
            source=source,
            site=site,
            tickers=tickers,
            symbols=symbols,
            image=image,
            normalized_at=now_str,
            dedup_key=dedup_key,
            raw=dict(raw),
        ))

    return results


def _is_valid_ticker(ticker: str) -> bool:
    if not ticker or not (1 <= len(ticker) <= 5):
        return False
    if not ticker.isalpha():
        return False
    if ticker in _NOISE_WORDS:
        return False
    return True


def _make_dedup_key(title: str, published_at: str, url: str, source: str) -> str:
    """Stable dedup key: URL if present, else title+date+source hash."""
    if url:
        return hashlib.md5(url.encode("utf-8")).hexdigest()
    combined = f"{title.lower().strip()}|{published_at[:10]}|{source.lower()}"
    return hashlib.md5(combined.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 2. Deduplication
# ---------------------------------------------------------------------------

def dedupe_news_articles(articles: list[NormalizedArticle]) -> list[NormalizedArticle]:
    """
    Remove duplicate articles using stable dedup keys.

    Preserves the first occurrence when duplicates exist.
    Returns articles sorted newest-first when published_at is parseable;
    otherwise preserves original order.
    """
    seen: set[str] = set()
    unique: list[NormalizedArticle] = []
    for art in articles:
        if art.dedup_key not in seen:
            seen.add(art.dedup_key)
            unique.append(art)

    # Sort newest-first where dates are available
    def _sort_key(a: NormalizedArticle) -> str:
        return a.published_at or ""

    unique.sort(key=_sort_key, reverse=True)
    return unique


# ---------------------------------------------------------------------------
# 3. Entity extraction
# ---------------------------------------------------------------------------

def extract_news_entities(article: NormalizedArticle) -> list[str]:
    """
    Extract tickers/entities from one article using deterministic rules.

    Priority order:
    1. Source-provided tickers (already on article.tickers)
    2. Cashtag patterns: $NVDA
    3. Parenthetical patterns: NVIDIA (NVDA)
    4. Company alias map matches in title/text

    Returns deduplicated, sorted list of canonical tickers.  Does not mutate
    the article.
    """
    found: set[str] = set(article.tickers)
    text = f"{article.title} {article.text}"

    # Cashtag: $NVDA
    for m in _CASHTAG_RE.finditer(text):
        t = m.group(1).upper()
        if _is_valid_ticker(t):
            found.add(t)

    # Parenthetical: "Apple (AAPL)"
    for m in _PAREN_TICKER_RE.finditer(text):
        t = m.group(2).upper()
        if _is_valid_ticker(t):
            found.add(t)

    # Company alias map — match on lowercased combined text
    text_lower = text.lower()
    for alias, ticker in COMPANY_ALIAS_MAP.items():
        if alias in text_lower:
            found.add(ticker)

    return sorted(found)


# ---------------------------------------------------------------------------
# 4. Theme classification
# ---------------------------------------------------------------------------

def classify_news_themes(article: NormalizedArticle) -> list[ThemeMatch]:
    """
    Classify news themes using deterministic keyword scoring.

    Returns a list of ThemeMatch objects for themes with at least one keyword
    match, sorted by score descending.
    """
    text_lower = f"{article.title} {article.text}".lower()
    matches: list[ThemeMatch] = []

    for theme, keywords in THEME_KEYWORDS.items():
        matched = [kw for kw in keywords if kw in text_lower]
        if not matched:
            continue
        score = min(1.0, len(matched) / max(1, len(keywords) // 3))
        matches.append(ThemeMatch(
            theme=theme,
            score=round(score, 3),
            matched_terms=matched[:10],
            evidence_titles=[article.title],
        ))

    matches.sort(key=lambda m: m.score, reverse=True)
    return matches


def _detect_flags(articles: list[NormalizedArticle]) -> tuple[list[str], list[str]]:
    """Return (risk_flags, catalyst_flags) from a list of articles."""
    risk: set[str] = set()
    catalyst: set[str] = set()
    for art in articles:
        text_lower = f"{art.title} {art.text}".lower()
        for kw in _RISK_KEYWORDS:
            if kw in text_lower:
                risk.add(kw)
        for kw in _CATALYST_KEYWORDS:
            if kw in text_lower:
                catalyst.add(kw)
    return sorted(risk), sorted(catalyst)


def _sentiment_hint(risk_flags: list[str], catalyst_flags: list[str]) -> str:
    r, c = len(risk_flags), len(catalyst_flags)
    if r == 0 and c == 0:
        return "neutral"
    if r > c:
        return "negative"
    if c > r:
        return "positive"
    return "mixed"


# ---------------------------------------------------------------------------
# 5. Evidence packets
# ---------------------------------------------------------------------------

def build_news_evidence_packets(
    articles: list[NormalizedArticle],
    holdings: list[str] | None = None,
    watchlist: list[str] | None = None,
    discovery_candidates: list[str] | None = None,
) -> list[EvidencePacket]:
    """
    Build structured evidence packets grouped by ticker/entity.

    Lane assignment:
      "official_monitoring"       — ticker in holdings or watchlist
      "sandbox_discovery_research" — ticker in discovery_candidates or otherwise unknown

    Does not promote, score, allocate, or recommend.
    """
    official_set: set[str] = set()
    if holdings:
        official_set.update(t.upper() for t in holdings)
    if watchlist:
        official_set.update(t.upper() for t in watchlist)

    discovery_set: set[str] = set()
    if discovery_candidates:
        discovery_set.update(t.upper() for t in discovery_candidates)

    # Group articles by ticker
    ticker_articles: dict[str, list[NormalizedArticle]] = {}
    for art in articles:
        entities = extract_news_entities(art)
        for ticker in entities:
            ticker_articles.setdefault(ticker, []).append(art)

    packets: list[EvidencePacket] = []
    for ticker, arts in ticker_articles.items():
        sources = {a.source for a in arts if a.source}
        dates = [a.published_at for a in arts if a.published_at]
        latest = max(dates) if dates else ""

        # Theme aggregation
        theme_counts: dict[str, float] = {}
        for art in arts:
            for tm in classify_news_themes(art):
                theme_counts[tm.theme] = theme_counts.get(tm.theme, 0) + tm.score
        top_themes = sorted(theme_counts, key=lambda t: theme_counts[t], reverse=True)[:5]

        risk_flags, catalyst_flags = _detect_flags(arts)

        # Summary bullets — top 3 titles
        summary_bullets = [
            f"{a.title[:120]}" for a in arts[:3]
        ]

        # Article refs
        article_refs = [
            {
                "title": a.title[:120],
                "url": a.url,
                "published_at": a.published_at,
                "source": a.source,
            }
            for a in arts[:10]
        ]

        # Lane
        t_upper = ticker.upper()
        if t_upper in official_set:
            lane = "official_monitoring"
        elif t_upper in discovery_set:
            lane = "sandbox_discovery_research"
        else:
            lane = "sandbox_discovery_research"

        packets.append(EvidencePacket(
            entity_key=ticker,
            entity_type="ticker",
            related_tickers=[ticker],
            article_count=len(arts),
            source_count=len(sources),
            latest_published_at=latest,
            themes=top_themes,
            risk_flags=risk_flags[:5],
            catalyst_flags=catalyst_flags[:5],
            sentiment_hint=_sentiment_hint(risk_flags, catalyst_flags),
            article_refs=article_refs,
            summary_bullets=summary_bullets,
            evidence_lane=lane,
        ))

    # Sort: official first, then by article count desc
    packets.sort(key=lambda p: (p.evidence_lane != "official_monitoring", -p.article_count))
    return packets


# ---------------------------------------------------------------------------
# 6. Markdown report builder
# ---------------------------------------------------------------------------

def _build_markdown_report(
    packets: list[EvidencePacket],
    article_count: int,
    generated_at: str,
) -> str:
    lines: list[str] = []
    lines.append("# News Intelligence Report")
    lines.append("")
    lines.append(f"**Generated:** {generated_at}")
    lines.append(f"**Articles processed:** {article_count}")
    lines.append("")
    lines.append(f"> **{_DISCLAIMER}**")
    lines.append("")

    official = [p for p in packets if p.evidence_lane == "official_monitoring"]
    sandbox = [p for p in packets if p.evidence_lane == "sandbox_discovery_research"]

    if official:
        lines.append("## Official Monitoring")
        lines.append("")
        for p in official[:10]:
            lines.append(f"### {p.entity_key}")
            lines.append(f"- Articles: {p.article_count} | Sources: {p.source_count}")
            lines.append(f"- Themes: {', '.join(p.themes) if p.themes else 'none'}")
            lines.append(f"- Sentiment hint: {p.sentiment_hint}")
            if p.catalyst_flags:
                lines.append(f"- Catalysts: {', '.join(p.catalyst_flags[:3])}")
            if p.risk_flags:
                lines.append(f"- Risks: {', '.join(p.risk_flags[:3])}")
            for bullet in p.summary_bullets[:3]:
                lines.append(f"  - {bullet}")
            lines.append("")

    if sandbox:
        lines.append("## Sandbox Discovery Research")
        lines.append("")
        lines.append(
            "_Sandbox research only. Not official watchlist. "
            "No promotion action taken._"
        )
        lines.append("")
        for p in sandbox[:10]:
            lines.append(f"### {p.entity_key} _(sandbox)_")
            lines.append(f"- Articles: {p.article_count} | Sources: {p.source_count}")
            lines.append(f"- Themes: {', '.join(p.themes) if p.themes else 'none'}")
            if p.summary_bullets:
                lines.append(f"  - {p.summary_bullets[0]}")
            lines.append("")

    lines.append("---")
    lines.append(f"*Source: {_SOURCE_LABEL}*")
    lines.append(f"*observe_only: {_OBSERVE_ONLY} | no_trade: {_NO_TRADE} | not_recommendation: {_NOT_RECOMMENDATION}*")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 7. Artifact writer
# ---------------------------------------------------------------------------

def write_news_intelligence_report(
    base_dir: str | Path,
    raw_articles: list[dict],
    holdings: list[str] | None = None,
    watchlist: list[str] | None = None,
    discovery_candidates: list[str] | None = None,
    run_mode: str = "daily",
) -> dict[str, Any]:
    """
    Full pipeline: normalize → dedupe → build evidence → write artifacts.

    Writes to LATEST namespace (outputs/latest/).
    Sandbox-lane evidence optionally written to SANDBOX namespace.

    Returns a summary dict with artifact paths and counts.
    """
    generated_at = datetime.now(timezone.utc).isoformat()
    base = Path(base_dir)

    normalized = normalize_news_articles(raw_articles)
    unique = dedupe_news_articles(normalized)
    packets = build_news_evidence_packets(
        unique,
        holdings=holdings,
        watchlist=watchlist,
        discovery_candidates=discovery_candidates,
    )

    official_packets = [p for p in packets if p.evidence_lane == "official_monitoring"]
    sandbox_packets = [p for p in packets if p.evidence_lane == "sandbox_discovery_research"]

    payload: dict[str, Any] = {
        "generated_at": generated_at,
        "observe_only": _OBSERVE_ONLY,
        "no_trade": _NO_TRADE,
        "not_recommendation": _NOT_RECOMMENDATION,
        "source": _SOURCE_LABEL,
        "run_mode": run_mode,
        "article_count_raw": len(raw_articles),
        "article_count_normalized": len(normalized),
        "article_count_deduped": len(unique),
        "evidence_packet_count": len(packets),
        "official_monitoring_count": len(official_packets),
        "sandbox_count": len(sandbox_packets),
        "disclaimer": _DISCLAIMER,
        "evidence_packets": [_packet_to_dict(p) for p in packets],
    }

    json_path = safe_write_json(
        OutputNamespace.LATEST,
        "news_intelligence.json",
        payload,
        base_dir=base,
    )

    md_content = _build_markdown_report(packets, len(unique), generated_at)
    md_path = safe_write_text(
        OutputNamespace.LATEST,
        "news_intelligence.md",
        md_content,
        base_dir=base,
    )

    sandbox_path: str | None = None
    if sandbox_packets:
        sandbox_payload: dict[str, Any] = {
            "generated_at": generated_at,
            "observe_only": _OBSERVE_ONLY,
            "no_trade": _NO_TRADE,
            "not_recommendation": _NOT_RECOMMENDATION,
            "source": _SOURCE_LABEL,
            "lane": "sandbox_discovery_research",
            "disclaimer": _DISCLAIMER,
            "evidence_packets": [_packet_to_dict(p) for p in sandbox_packets],
        }
        sp = safe_write_json(
            OutputNamespace.SANDBOX,
            "discovery/news_candidate_evidence.json",
            sandbox_payload,
            base_dir=base,
        )
        sandbox_path = str(sp)

    return {
        "generated_at": generated_at,
        "article_count_raw": len(raw_articles),
        "article_count_deduped": len(unique),
        "evidence_packet_count": len(packets),
        "official_monitoring_count": len(official_packets),
        "sandbox_count": len(sandbox_packets),
        "artifacts": {
            "news_intelligence_json": str(json_path),
            "news_intelligence_md": str(md_path),
            "news_candidate_evidence_json": sandbox_path,
        },
        "observe_only": _OBSERVE_ONLY,
        "no_trade": _NO_TRADE,
        "not_recommendation": _NOT_RECOMMENDATION,
    }


def _packet_to_dict(p: EvidencePacket) -> dict[str, Any]:
    return {
        "entity_key": p.entity_key,
        "entity_type": p.entity_type,
        "related_tickers": p.related_tickers,
        "article_count": p.article_count,
        "source_count": p.source_count,
        "latest_published_at": p.latest_published_at,
        "themes": p.themes,
        "risk_flags": p.risk_flags,
        "catalyst_flags": p.catalyst_flags,
        "sentiment_hint": p.sentiment_hint,
        "article_refs": p.article_refs,
        "summary_bullets": p.summary_bullets,
        "evidence_lane": p.evidence_lane,
        "observe_only": p.observe_only,
        "no_trade": p.no_trade,
        "not_recommendation": p.not_recommendation,
    }


# ---------------------------------------------------------------------------
# 8. Top-level orchestrator
# ---------------------------------------------------------------------------

def run_fmp_news_intelligence(
    raw_articles: list[dict],
    holdings: list[str] | None = None,
    watchlist: list[str] | None = None,
    discovery_candidates: list[str] | None = None,
    base_dir: str | Path = "outputs",
    run_mode: str = "daily",
    write_files: bool = True,
) -> dict[str, Any]:
    """
    Orchestrate news normalization, entity extraction, theme classification,
    evidence packet building, and artifact writing.

    Parameters
    ----------
    raw_articles:
        Raw FMP-style article dicts.  Can be empty — produces safe empty artifacts.
    holdings:
        Current official portfolio holdings (tickers).  Observe-only; not mutated.
    watchlist:
        Official watchlist tickers.  Observe-only; not mutated.
    discovery_candidates:
        Sandbox discovery candidate tickers.  Not promoted; evidence only.
    base_dir:
        Output root directory (parent of outputs/).
    run_mode:
        Run mode string (informational only).
    write_files:
        If False, skip file writes (useful for dry-run / test).

    Returns a summary dict.  On any error, returns a safe degraded state.
    """
    try:
        if write_files:
            return write_news_intelligence_report(
                base_dir=base_dir,
                raw_articles=raw_articles,
                holdings=holdings,
                watchlist=watchlist,
                discovery_candidates=discovery_candidates,
                run_mode=run_mode,
            )

        # write_files=False: run pipeline but skip writes
        normalized = normalize_news_articles(raw_articles)
        unique = dedupe_news_articles(normalized)
        packets = build_news_evidence_packets(
            unique,
            holdings=holdings,
            watchlist=watchlist,
            discovery_candidates=discovery_candidates,
        )
        official_count = sum(1 for p in packets if p.evidence_lane == "official_monitoring")
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "article_count_raw": len(raw_articles),
            "article_count_deduped": len(unique),
            "evidence_packet_count": len(packets),
            "official_monitoring_count": official_count,
            "sandbox_count": len(packets) - official_count,
            "artifacts": {},
            "observe_only": _OBSERVE_ONLY,
            "no_trade": _NO_TRADE,
            "not_recommendation": _NOT_RECOMMENDATION,
            "write_files": False,
        }

    except Exception as exc:
        logger.error("run_fmp_news_intelligence failed: %s", exc, exc_info=True)
        return {
            "error": str(exc),
            "article_count_raw": len(raw_articles) if isinstance(raw_articles, list) else 0,
            "observe_only": _OBSERVE_ONLY,
            "no_trade": _NO_TRADE,
            "not_recommendation": _NOT_RECOMMENDATION,
        }
