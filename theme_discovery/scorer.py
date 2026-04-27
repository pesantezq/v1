"""
Score classified themes and emerging phrases into ranked ThemeOpportunity objects.

Composite score weights (sum to 1.0):
    mention_score      0.25   min(mention_count / 10, 1.0)
    recency_score      0.20   mean exp(-age_h * ln2 / 48)   48-h half-life
    diversity_score    0.15   unique_source_domains / mention_count
    persistence_score  0.20   fraction of recent N runs where theme appeared
    acceleration_score 0.20   sigmoid ratio of recent vs prior avg mentions

Confidence:
    raw  = diversity * min(mention_count / 5, 1.0)
    lift = 0.70 + 0.30 * persistence   (floor 0.70 even for new themes)
    base = raw * lift
    + emerging, single-source:    × 0.70
    + emerging, < 3 mentions:     × 0.80
    → clamped to [0, 1]

Recency half-life is 48 hours; articles older than ~6 days score near zero.
Unparseable published dates are treated as 72 h old (conservative penalty).
"""
from __future__ import annotations

import logging
import math
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from theme_discovery.history import compute_history_metrics
from theme_discovery.models import ArticleSignal, ExtractResult, ThemeOpportunity

logger = logging.getLogger(__name__)

_HALF_LIFE_HOURS: float = 48.0
_STALE_AGE_HOURS: float = 72.0
_MAX_EVIDENCE: int = 5
_MAX_TICKERS: int = 10

# Score weights — must sum to 1.0
_W_MENTION: float = 0.25
_W_RECENCY: float = 0.20
_W_DIVERSITY: float = 0.15
_W_PERSISTENCE: float = 0.20
_W_ACCELERATION: float = 0.20


def score(
    extract_result: ExtractResult,
    history: dict,
    top_n: int = 10,
) -> list[ThemeOpportunity]:
    """
    Score and rank themes from both extraction paths.

    Args:
        extract_result: Output of extractor.extract().
        history:        Loaded history dict from history.load_theme_history().
        top_n:          Maximum opportunities to return.

    Returns:
        List of ThemeOpportunity sorted by composite score descending.
        Returns [] when both classified and emerging are empty.
    """
    if not extract_result.classified and not extract_result.emerging:
        return []

    opportunities: list[ThemeOpportunity] = []

    for theme_name, signals in extract_result.classified.items():
        if signals:
            opportunities.append(
                _score_group(theme_name, "classified", signals, history)
            )

    for phrase, signals in extract_result.emerging.items():
        if signals:
            opportunities.append(
                _score_group(phrase, "emerging", signals, history)
            )

    opportunities.sort(key=lambda o: o.score, reverse=True)
    return opportunities[:top_n]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _score_group(
    name: str,
    theme_type: str,
    signals: list[ArticleSignal],
    history: dict,
) -> ThemeOpportunity:
    mention_count = len(signals)
    mention_score = min(mention_count / 10.0, 1.0)

    ages = [_age_hours(sig.article.published) for sig in signals]
    recency_score = sum(_recency_weight(a) for a in ages) / len(ages)

    domains = {sig.article.source_domain for sig in signals}
    source_count = len(domains)
    diversity_score = source_count / mention_count

    metrics = compute_history_metrics(name, theme_type, history)
    persistence_score = metrics["persistence_score"]
    acceleration_score = metrics["acceleration_score"]

    composite = (
        _W_MENTION      * mention_score
        + _W_RECENCY    * recency_score
        + _W_DIVERSITY  * diversity_score
        + _W_PERSISTENCE * persistence_score
        + _W_ACCELERATION * acceleration_score
    )

    # Confidence — trust-weighted, not identical to score
    raw_conf = diversity_score * min(mention_count / 5.0, 1.0)
    persistence_lift = 0.70 + 0.30 * persistence_score
    confidence = raw_conf * persistence_lift
    if theme_type == "emerging" and source_count <= 1:
        confidence *= 0.70
    if theme_type == "emerging" and mention_count < 3:
        confidence *= 0.80
    confidence = min(confidence, 1.0)

    # Tickers — frequency-ranked, sorted for determinism within equal frequency
    ticker_counter: Counter[str] = Counter()
    for sig in signals:
        ticker_counter.update(sig.tickers_found)
    # stable sort: alphabetical within same count
    top_tickers = sorted(
        (t for t, _ in ticker_counter.most_common(_MAX_TICKERS)),
        key=lambda t: (-ticker_counter[t], t),
    )
    unique_ticker_count = len(ticker_counter)

    # Evidence — top-scored, deduplicated, capped
    seen_titles: set[str] = set()
    evidence: list[str] = []
    for sig in sorted(signals, key=lambda s: s.theme_score, reverse=True):
        title = sig.article.title
        if title not in seen_titles:
            seen_titles.add(title)
            evidence.append(title)
        if len(evidence) >= _MAX_EVIDENCE:
            break

    return ThemeOpportunity(
        name=name,
        theme_type=theme_type,
        score=round(min(composite, 1.0), 4),
        confidence=round(confidence, 4),
        mention_count=mention_count,
        unique_ticker_count=unique_ticker_count,
        tickers=top_tickers,
        evidence=evidence,
        source_count=source_count,
        persistence_score=round(persistence_score, 4),
        acceleration_score=round(acceleration_score, 4),
        recency_score=round(recency_score, 4),
        diversity_score=round(diversity_score, 4),
        history_runs_seen=metrics["history_runs_seen"],
        first_seen=metrics["first_seen"],
        last_seen=metrics["last_seen"],
    )


def _recency_weight(age_hours: float) -> float:
    return math.exp(-age_hours * math.log(2) / _HALF_LIFE_HOURS)


def _age_hours(published: str) -> float:
    if not published:
        return _STALE_AGE_HOURS
    now = datetime.now(timezone.utc)
    dt = _parse_iso(published) or _parse_rfc2822(published)
    if dt is None:
        return _STALE_AGE_HOURS
    return max(0.0, (now - dt).total_seconds() / 3600.0)


def _parse_iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _parse_rfc2822(s: str) -> datetime | None:
    try:
        return parsedate_to_datetime(s)
    except Exception:
        return None
