"""
Per-symbol confidence scoring for watchlist scan results.

confidence_score (0.0–1.0) measures how trustworthy a signal is today,
based on data provenance, completeness, cache age, and enrichment degradation.

It does NOT measure how attractive the signal looks — that is signal_score.

Formula (when OVERVIEW cache age is available):
    confidence = freshness * 0.45 + completeness * 0.30 + cache_age * 0.15 + budget * 0.10

Formula (when cache age is unavailable):
    confidence = freshness * 0.53 + completeness * 0.32 + budget * 0.15

Bands:
    high    >= 0.85
    medium  >= 0.65
    low     <  0.65
"""

from __future__ import annotations

from typing import Any, Optional

# ---------------------------------------------------------------------------
# Score tables (easy to tune in one place)
# ---------------------------------------------------------------------------

# Freshness base score from data_quality provenance label
FRESHNESS_SCORES: dict[str, float] = {
    "fresh":          1.00,
    "partial":        0.85,
    "budget_skipped": 0.72,
    "cached":         0.62,  # raised from 0.55 — cached data still holds meaningful signal
}

# Minimum confidence floor — prevents extreme suppression from thin-data runs.
CONFIDENCE_FLOOR: float = 0.30

# Enrichment budget factor from overview fetch provenance (ov_source)
BUDGET_SCORES: dict[str, float] = {
    "fresh":          1.00,
    "cached":         0.92,
    "budget_skipped": 0.80,
}

# Cache age → score (based on hours since last write)
_AGE_BRACKETS: list[tuple[float, float]] = [
    (24.0,  1.00),   # ≤ 24 h
    (72.0,  0.90),   # ≤ 72 h
    (168.0, 0.75),   # ≤ 7 days
    (float("inf"), 0.55),
]

# Confidence band thresholds
HIGH_THRESHOLD:   float = 0.85
MEDIUM_THRESHOLD: float = 0.65

# Weights — must sum to 1.0
_WEIGHTS_WITH_AGE    = {"freshness": 0.45, "completeness": 0.30, "cache_age": 0.15, "budget": 0.10}
_WEIGHTS_WITHOUT_AGE = {"freshness": 0.53, "completeness": 0.32, "budget": 0.15}


# ---------------------------------------------------------------------------
# Sub-scores
# ---------------------------------------------------------------------------

def _cache_age_score(age_seconds: Optional[float]) -> Optional[float]:
    """Convert OVERVIEW cache age in seconds to a [0, 1] score. None if unknown."""
    if age_seconds is None:
        return None
    hours = age_seconds / 3600
    for threshold_h, score in _AGE_BRACKETS:
        if hours <= threshold_h:
            return score
    return 0.55  # unreachable fallback


def _completeness_score(
    tech: dict[str, Any],
    fundamentals: dict[str, Any],
    articles: list[dict],
) -> float:
    """
    Fraction of the four major signal components that are meaningfully present.

    Components:
        - price data       (tech dict has a non-None price)
        - technicals       (SMA data computed — above_sma20 is set)
        - news / sentiment (at least one article present)
        - fundamentals     (sector populated in overview)
    """
    present = 0

    if tech and tech.get("price") is not None:
        present += 1
    if tech and tech.get("above_sma20") is not None:
        present += 1
    if articles:
        present += 1
    if fundamentals and fundamentals.get("sector"):
        present += 1

    return present / 4


def _build_reasons(
    data_quality: str,
    ov_source: str,
    tech: dict[str, Any],
    fundamentals: dict[str, Any],
    articles: list[dict],
    cache_age_seconds: Optional[float],
) -> list[str]:
    """Build a short, factual list of human-readable confidence reasons."""
    reasons: list[str] = []

    # 1. Provenance / freshness
    if data_quality == "fresh":
        reasons.append("live price and fundamental data")
    elif data_quality == "partial":
        reasons.append("live price; fundamentals from cache")
    elif data_quality == "budget_skipped":
        reasons.append("overview skipped due to API budget")
    elif data_quality == "cached":
        reasons.append("result primarily from cache")

    # 2. Component completeness
    missing: list[str] = []
    if not (tech and tech.get("price") is not None):
        missing.append("price")
    if not (tech and tech.get("above_sma20") is not None):
        missing.append("technicals")
    if not articles:
        missing.append("news")
    if not (fundamentals and fundamentals.get("sector")):
        missing.append("fundamentals")

    if not missing:
        reasons.append("all major components present")
    else:
        if "fundamentals" in missing:
            reasons.append("fundamental context incomplete")
        if "news" in missing:
            reasons.append("no news data available")
        if "technicals" in missing:
            reasons.append("technical data unavailable")

    # 3. Cache age note (only if meaningfully stale)
    if cache_age_seconds is not None:
        hours = cache_age_seconds / 3600
        if hours > 48:
            reasons.append(f"cached data is {int(hours)}h old")

    return reasons


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_confidence(
    data_quality: str,
    ov_source: str,
    tech: dict[str, Any],
    fundamentals: dict[str, Any],
    articles: list[dict],
    cache_age_seconds: Optional[float] = None,
) -> tuple[float, str, list[str]]:
    """
    Compute confidence score, band, and reasons for one watchlist scan result.

    Args:
        data_quality:      Result's data_quality label (fresh/partial/budget_skipped/cached).
        ov_source:         OVERVIEW fetch provenance (fresh/cached/budget_skipped).
        tech:              Technicals dict from _compute_technicals().
        fundamentals:      Fundamentals dict from parse_fmp_fundamentals_bundle().
        articles:          News articles for this symbol.
        cache_age_seconds: Age of the OVERVIEW cache file in seconds; None if unavailable.

    Returns:
        Tuple of (confidence_score, confidence_band, confidence_reasons).
        confidence_score is clamped to [0.0, 1.0].
        confidence_band is "high" | "medium" | "low".
        confidence_reasons is a short list of human-readable strings.
    """
    freshness    = FRESHNESS_SCORES.get(data_quality, 0.55)
    completeness = _completeness_score(tech, fundamentals, articles)
    budget       = BUDGET_SCORES.get(ov_source, 0.80)
    age_score    = _cache_age_score(cache_age_seconds)

    if age_score is not None:
        w = _WEIGHTS_WITH_AGE
        score = (
            freshness    * w["freshness"]
            + completeness * w["completeness"]
            + age_score    * w["cache_age"]
            + budget       * w["budget"]
        )
    else:
        w = _WEIGHTS_WITHOUT_AGE
        score = (
            freshness    * w["freshness"]
            + completeness * w["completeness"]
            + budget       * w["budget"]
        )

    score = round(max(CONFIDENCE_FLOOR, min(1.0, score)), 4)

    if score >= HIGH_THRESHOLD:
        band = "high"
    elif score >= MEDIUM_THRESHOLD:
        band = "medium"
    else:
        band = "low"

    reasons = _build_reasons(
        data_quality, ov_source, tech, fundamentals, articles, cache_age_seconds
    )

    return score, band, reasons
