"""
Extract classified theme signals and emerging phrases from collected articles.

Two extraction paths:
  classified — reuses watchlist_scanner keyword dictionary (10 named themes)
  emerging   — bigram/trigram frequency detection from headline text

Both paths share the same ticker extraction against the Nasdaq-100 + sector-ETF
universe. No external NLP libraries required.
"""
from __future__ import annotations

import re
import logging
from collections import Counter, defaultdict

from market_universe import NASDAQ_100_SYMBOLS, SECTOR_ETF_SYMBOLS
from watchlist_scanner.theme_engine import classify_headlines
from theme_discovery.models import Article, ArticleSignal, ExtractResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ticker extraction
# ---------------------------------------------------------------------------

_KNOWN_TICKERS: frozenset[str] = frozenset(NASDAQ_100_SYMBOLS + SECTOR_ETF_SYMBOLS)
_TICKER_RE = re.compile(r"\b([A-Z]{1,5})\b")


def _extract_tickers(text: str) -> list[str]:
    """Return known tickers from text, ordered by appearance, deduplicated."""
    seen: set[str] = set()
    result: list[str] = []
    for t in _TICKER_RE.findall(text):
        if t in _KNOWN_TICKERS and t not in seen:
            seen.add(t)
            result.append(t)
    return result


# ---------------------------------------------------------------------------
# Phrase normalization and filtering
# ---------------------------------------------------------------------------

_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "as", "by", "from", "up", "about", "into", "through",
    "is", "are", "was", "were", "be", "been", "being", "have", "has", "had",
    "do", "does", "did", "will", "would", "could", "should", "may", "might",
    "not", "no", "nor", "so", "yet", "both", "either", "neither",
    "it", "its", "this", "that", "these", "those", "i", "we", "you", "he",
    "she", "they", "them", "their", "our", "your",
    "new", "says", "said", "amid", "after", "before", "over", "under",
    "more", "than", "less", "well", "just", "now", "also", "still",
    "some", "all", "any", "such", "how", "what", "when", "where", "who",
    "which", "while", "if", "then", "per", "its", "can", "vs", "get",
})

_BLOCKED_PHRASES: frozenset[str] = frozenset({
    "stock market", "share price", "breaking news", "top news",
    "latest news", "market update", "market news", "the market",
    "stocks rise", "stocks fall", "wall street", "press release",
    "full year", "first quarter", "second quarter", "third quarter",
    "fourth quarter", "fiscal year", "year ago", "last year",
    "this week", "this year", "next year", "last week",
    "market cap", "trading day", "trading session", "closing bell",
})

_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = text.lower()
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def _tokenize(text: str) -> list[str]:
    return _normalize(text).split()


def _keep_phrase(tokens: list[str]) -> bool:
    """Return True if this token sequence is a useful phrase."""
    # Discard single-char tokens (noise)
    if any(len(t) < 2 for t in tokens):
        return False
    # Discard all-stopword phrases
    if all(t in _STOPWORDS for t in tokens):
        return False
    # Discard phrases where every non-stopword token is purely numeric
    non_stop = [t for t in tokens if t not in _STOPWORDS]
    if non_stop and all(t.isdigit() for t in non_stop):
        return False
    phrase = " ".join(tokens)
    if phrase in _BLOCKED_PHRASES:
        return False
    return True


def _extract_phrase_frequencies(
    titles: list[str],
    min_freq: int = 2,
) -> dict[str, int]:
    """
    Count bigram/trigram frequencies across all (normalized) titles.
    Returns only phrases that meet min_freq and pass quality filters.
    """
    counter: Counter[str] = Counter()
    for title in titles:
        tokens = _tokenize(title)
        for n in (2, 3):
            for i in range(len(tokens) - n + 1):
                chunk = tokens[i:i + n]
                if _keep_phrase(chunk):
                    counter[" ".join(chunk)] += 1
    return {p: c for p, c in counter.items() if c >= min_freq}


def _phrase_in_normalized(phrase: str, normalized_title: str) -> bool:
    """Word-boundary-safe containment check."""
    return (" " + phrase + " ") in (" " + normalized_title + " ")


# ---------------------------------------------------------------------------
# Main extraction entry point
# ---------------------------------------------------------------------------

def extract(
    articles: list[Article],
    min_phrase_freq: int = 2,
) -> ExtractResult:
    """
    Extract classified themes and emerging phrases from collected articles.

    Args:
        articles:        Output of collector.collect_articles().
        min_phrase_freq: Minimum occurrences for a phrase to be included.

    Returns:
        ExtractResult with two dicts:
          classified: theme_name → [ArticleSignal]
          emerging:   phrase    → [ArticleSignal]
        Both dicts are empty on empty input.
    """
    if not articles:
        return ExtractResult(classified={}, emerging={})

    # Single pass: build per-article data used by both paths
    article_cache: list[tuple[Article, str, list[str], str]] = []
    for article in articles:
        full_text = article.title + " " + article.summary
        tickers = _extract_tickers(full_text)
        norm_title = _normalize(article.title)
        article_cache.append((article, full_text, tickers, norm_title))

    # --- Path 1: classified themes ---
    classified: dict[str, list[ArticleSignal]] = defaultdict(list)
    for article, full_text, tickers, _ in article_cache:
        theme_scores = classify_headlines([full_text], threshold=0.0)
        for theme, ts in theme_scores.items():
            if ts > 0.0:
                classified[theme].append(ArticleSignal(
                    article=article,
                    theme_score=ts,
                    tickers_found=tickers,
                ))

    # --- Path 2: emerging phrases ---
    titles = [art.title for art in articles]
    phrase_freq = _extract_phrase_frequencies(titles, min_freq=min_phrase_freq)

    emerging: dict[str, list[ArticleSignal]] = defaultdict(list)
    for phrase in phrase_freq:
        for article, _, tickers, norm_title in article_cache:
            if _phrase_in_normalized(phrase, norm_title):
                emerging[phrase].append(ArticleSignal(
                    article=article,
                    theme_score=1.0,
                    tickers_found=tickers,
                ))

    result = ExtractResult(
        classified=dict(classified),
        emerging=dict(emerging),
    )
    logger.debug(
        "theme_discovery.extractor: %d articles → %d classified themes, %d emerging phrases",
        len(articles), len(result.classified), len(result.emerging),
    )
    return result
