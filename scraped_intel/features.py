"""
Scraped Intelligence — soft signal feature engineering.

All functions here are pure (no I/O, no side effects).  They accept a list
of ScrapedRecords and return a SoftSignals instance.

Soft signal definitions
-----------------------
headline_count_7d   : distinct records within the past 7 days
headline_count_30d  : distinct records within the past 30 days
source_count        : number of distinct source domains
avg_sentiment       : mean sentiment across records that have a score
theme_alignment_score: fraction of records mentioning ≥1 known watchlist theme
mention_acceleration: (7d_rate / 30d_rate) − 1, capped to [−1, +1]
                      > 0 → accelerating coverage; < 0 → fading
recency_score       : sum of exp(−hours / HALF_LIFE_H) for each record,
                      normalised to [0, 1] by MAX_RECORDS
scraped_confidence  : delegated to provenance.compute_scraped_confidence()

Contamination guard
-------------------
None of these features modify or are mixed into signal_score, confidence_score,
price, fundamentals, technicals, or news.  They live exclusively in
SoftSignals and are exported to training data under "soft_" prefixed columns.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Optional

from scraped_intel.models import ScrapedRecord, SoftSignals

# Recency decay: e^(-hours/HALF_LIFE_H)
# 72 h ≈ 3 days → weight 0.5 at 3 days, ~0.04 at 30 days
_HALF_LIFE_H: float = 72.0

# Normalisation ceiling for recency_score (prevents a single burst from
# dominating; anything ≥ MAX_RECORDS records gets score capped at 1.0)
_MAX_RECORDS_NORMALISE: int = 10


def compute_soft_signals(
    symbol: str,
    records: list[ScrapedRecord],
    as_of_date: Optional[str] = None,
    known_themes: Optional[list[str]] = None,
    scraped_confidence: float = 0.0,
) -> SoftSignals:
    """
    Compute soft signal features from a list of ScrapedRecords for one symbol.

    Args:
        symbol:             Ticker symbol.
        records:            All scraped records for this symbol (any lookback).
        as_of_date:         YYYY-MM-DD date string; defaults to today.
        known_themes:       Watchlist-level theme names for alignment scoring.
        scraped_confidence: Pre-computed provenance confidence (from provenance.py).

    Returns:
        A SoftSignals instance.  Returns a zero-state SoftSignals if records
        is empty.
    """
    as_of = as_of_date or date.today().isoformat()
    themes_lower = {t.lower() for t in (known_themes or [])}

    if not records:
        return SoftSignals(
            symbol=symbol,
            as_of_date=as_of,
            scraped_confidence=scraped_confidence,
        )

    # ── Recency buckets ────────────────────────────────────────────────────
    count_7d  = 0
    count_30d = 0
    for r in records:
        h = r.recency_hours
        if h is None:
            continue
        if h <= 168.0:    # 7 × 24
            count_7d += 1
        if h <= 720.0:    # 30 × 24
            count_30d += 1

    # ── Source diversity ──────────────────────────────────────────────────
    source_count = len({r.domain for r in records if r.domain})

    # ── Sentiment ─────────────────────────────────────────────────────────
    sentiments = [r.sentiment for r in records if r.sentiment is not None]
    avg_sentiment: Optional[float] = (
        round(sum(sentiments) / len(sentiments), 4) if sentiments else None
    )

    # ── Theme alignment ───────────────────────────────────────────────────
    if themes_lower:
        aligned = sum(
            1 for r in records
            if any(t.lower() in themes_lower for t in (r.themes or []))
        )
        theme_alignment_score = round(aligned / len(records), 4)
    else:
        theme_alignment_score = 0.0

    # ── Mention acceleration ──────────────────────────────────────────────
    # Compare 7-day rate vs. implied 7-day rate from 30-day window.
    # acceleration > 0 → growing coverage, < 0 → fading.
    if count_30d > 0:
        baseline_7d = count_30d / 4.0    # expected 7d count at steady state
        raw_accel = (count_7d - baseline_7d) / max(baseline_7d, 1.0)
        mention_acceleration = round(max(-1.0, min(1.0, raw_accel)), 4)
    else:
        mention_acceleration = 0.0

    # ── Recency-weighted score ────────────────────────────────────────────
    # Exponential decay: each record contributes e^(-hours/HALF_LIFE_H)
    # Records without a published_at get a neutral weight of 0.25.
    decay_sum = 0.0
    for r in records:
        h = r.recency_hours
        if h is not None:
            decay_sum += math.exp(-h / _HALF_LIFE_H)
        else:
            decay_sum += 0.25
    recency_score = round(
        min(1.0, decay_sum / _MAX_RECORDS_NORMALISE), 4
    )

    # ── Evidence provenance ───────────────────────────────────────────────
    evidence_items = [r.record_id for r in records]

    return SoftSignals(
        symbol=symbol,
        as_of_date=as_of,
        headline_count_7d=count_7d,
        headline_count_30d=count_30d,
        source_count=source_count,
        avg_sentiment=avg_sentiment,
        theme_alignment_score=theme_alignment_score,
        mention_acceleration=mention_acceleration,
        recency_score=recency_score,
        scraped_confidence=scraped_confidence,
        evidence_items=evidence_items,
    )
