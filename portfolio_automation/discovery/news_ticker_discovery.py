"""
Deterministic ticker extraction from news/text records.

No network calls. No AI calls. Conservative extraction only.

Supported extraction methods:
1. Source-provided: ``symbols`` or ``tickers`` field in the record dict (most reliable)
2. Cashtag: ``$NVDA``, ``$AAPL`` (reliable)
3. Parenthetical: ``NVIDIA (NVDA)``, ``Apple (AAPL)`` (reliable)

Standalone uppercase words are NOT extracted — too noisy for conservative mode.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Noise words — common uppercase tokens that are not stock tickers
# ---------------------------------------------------------------------------

NOISE_WORDS: frozenset[str] = frozenset({
    # Roles / titles
    "CEO", "CFO", "COO", "CTO", "CMO", "CPO", "EVP", "SVP",
    # Geopolitical / geographic
    "USA", "US", "UK", "EU", "UN", "NATO", "OPEC", "IMF",
    # Macro / economic indicators
    "GDP", "CPI", "PPI", "PCE", "EPS", "PE", "EV",
    # Regulatory / financial agencies
    "FED", "SEC", "IRS", "FDIC", "CFTC", "FINRA",
    # Market cycle shorthand
    "FOMC", "YOY", "QOQ", "MOM", "TTM", "LTM",
    # Asset class / instrument types
    "ETF", "IPO", "NAV", "AUM", "REIT",
    # Well-known ETFs / indices treated as noise by default
    "QQQ", "SPY", "SPX", "DXY", "DOW",
    # Tech / business jargon
    "AI", "API", "ML", "NLP", "AR", "VR",
    "SAAS", "PAAS", "IAAS", "BYOD",
    # Legal entity suffixes
    "LLC", "INC", "LTD", "PLC", "CORP",
    # Time zones / times
    "AM", "PM", "EST", "PST", "UTC", "GMT",
    # Common short words that look like tickers
    "THE", "AND", "FOR", "NOT", "BUT", "NEW", "TOP", "KEY",
    "SET", "OUT", "LOW", "HIGH", "ALL", "ONE",
    # Avoid recommendation language
    "BUY", "SELL", "HOLD",
})

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_CASHTAG_RE = re.compile(r'\$([A-Z]{1,5})\b')
_PAREN_TICKER_RE = re.compile(r'\(([A-Z]{2,5})\)')


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class TickerEvidence:
    """Record-level evidence for a single ticker mention."""
    record_index: int
    source: str
    published_at: str | None
    extraction_method: str  # "source_provided" | "cashtag" | "parenthetical"
    context: str            # short snippet from the record


@dataclass
class DiscoveredTicker:
    """Aggregated discovery state for one ticker across all input records."""
    ticker: str
    mention_count: int
    unique_sources: list[str]
    evidence: list[TickerEvidence]
    discovery_only: bool = True
    corroboration_required: bool = True
    corroboration_met: bool = False
    corroboration_sources: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_text(record: dict) -> str:
    """Combine title and summary into a single searchable string."""
    parts = []
    for key in ("title", "summary"):
        val = record.get(key) or ""
        if isinstance(val, str):
            parts.append(val)
    return " ".join(parts)


def _get_source(record: dict) -> str:
    return str(record.get("source") or "unknown")


def _get_published_at(record: dict) -> str | None:
    val = record.get("published_at")
    return str(val) if val else None


def _is_valid_ticker(ticker: str, noise: frozenset[str]) -> bool:
    """Return True if ticker passes basic sanity checks."""
    if not ticker or len(ticker) < 1 or len(ticker) > 5:
        return False
    if not ticker.isalpha():
        return False
    if ticker in noise:
        return False
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_tickers(
    records: list[dict],
    *,
    known_universe: set[str] | frozenset[str] | None = None,
    min_mentions: int = 1,
    extra_noise_words: set[str] | None = None,
) -> list[DiscoveredTicker]:
    """
    Extract ticker candidates from *records* using conservative deterministic rules.

    Parameters
    ----------
    records:
        List of news/event record dicts. Each may have: title, summary, source,
        published_at, symbols, tickers.
    known_universe:
        Optional allowlist of valid ticker symbols. When provided, only tickers
        present in this set are returned.
    min_mentions:
        Minimum total mention count for a ticker to be included in results.
    extra_noise_words:
        Additional uppercase tokens to treat as noise (merged with built-in set).

    Returns
    -------
    List of :class:`DiscoveredTicker` objects, one per unique ticker, sorted by
    mention count descending. All carry ``discovery_only=True`` and
    ``corroboration_required=True``.
    """
    noise = NOISE_WORDS
    if extra_noise_words:
        noise = noise | {w.upper() for w in extra_noise_words}

    # ticker → list of evidence
    accumulator: dict[str, list[TickerEvidence]] = {}

    for idx, record in enumerate(records):
        source = _get_source(record)
        published_at = _get_published_at(record)
        text = _get_text(record)
        short_text = text[:120]

        # 1. Source-provided symbols/tickers (highest reliability)
        for key in ("symbols", "tickers"):
            provided = record.get(key)
            if isinstance(provided, (list, tuple)):
                for raw in provided:
                    ticker = str(raw).upper().strip()
                    if not _is_valid_ticker(ticker, noise):
                        continue
                    if known_universe is not None and ticker not in known_universe:
                        continue
                    accumulator.setdefault(ticker, []).append(
                        TickerEvidence(
                            record_index=idx,
                            source=source,
                            published_at=published_at,
                            extraction_method="source_provided",
                            context=short_text,
                        )
                    )

        if not text:
            continue

        # 2. Cashtag extraction: $NVDA
        for match in _CASHTAG_RE.finditer(text):
            ticker = match.group(1).upper()
            if not _is_valid_ticker(ticker, noise):
                continue
            if known_universe is not None and ticker not in known_universe:
                continue
            start = max(0, match.start() - 20)
            end = min(len(text), match.end() + 20)
            accumulator.setdefault(ticker, []).append(
                TickerEvidence(
                    record_index=idx,
                    source=source,
                    published_at=published_at,
                    extraction_method="cashtag",
                    context=text[start:end].strip(),
                )
            )

        # 3. Parenthetical extraction: NVIDIA (NVDA)
        for match in _PAREN_TICKER_RE.finditer(text):
            ticker = match.group(1).upper()
            if not _is_valid_ticker(ticker, noise):
                continue
            if known_universe is not None and ticker not in known_universe:
                continue
            start = max(0, match.start() - 30)
            end = min(len(text), match.end() + 10)
            accumulator.setdefault(ticker, []).append(
                TickerEvidence(
                    record_index=idx,
                    source=source,
                    published_at=published_at,
                    extraction_method="parenthetical",
                    context=text[start:end].strip(),
                )
            )

    # Build DiscoveredTicker objects
    results: list[DiscoveredTicker] = []
    for ticker, evidence_list in accumulator.items():
        if len(evidence_list) < min_mentions:
            continue
        unique_sources = sorted({e.source for e in evidence_list})
        results.append(
            DiscoveredTicker(
                ticker=ticker,
                mention_count=len(evidence_list),
                unique_sources=unique_sources,
                evidence=evidence_list,
            )
        )

    results.sort(key=lambda t: t.mention_count, reverse=True)
    return results
