"""
Scraped Intelligence — core data models.

Strict separation contract
--------------------------
ScrapedRecord  — raw evidence from a scraping/fetching operation.
SoftSignals    — derived features computed from a set of ScrapedRecords.
IntelBundle    — per-symbol container grouping records + signals.

These types NEVER overwrite trusted hard-data fields (price, signal_score,
confidence_score, fundamentals, technicals).  They are always attached to a
scan result under the separate "scraped_intel" key.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# ScrapedRecord — one item of raw scraped evidence
# ---------------------------------------------------------------------------

@dataclass
class ScrapedRecord:
    """
    Normalized representation of a single scraped item.

    Fields are deliberately narrow — only what is needed for feature
    engineering and provenance tracking.  Adapter-specific metadata
    lives in `extra` and is never used in signal computation.
    """
    symbol: str                         # ticker this record is associated with
    source_type: str                    # "sec_filing" | "rss_article" | "company_ir"
    domain: str                         # e.g. "sec.gov", "reuters.com"
    url: Optional[str]                  # source URL (may be None for private feeds)
    published_at: Optional[str]         # ISO-8601 string; None if unknown
    collected_at: str                   # ISO-8601 string — when we fetched it
    title: str                          # headline or filing title
    excerpt: str                        # first ~500 chars of body text (empty if not available)
    extraction_status: str              # "ok" | "partial" | "failed"
    parse_quality: float                # 0.0–1.0; 1.0 = high-quality, well-structured source
    themes: list[str]                   # theme labels matched in title/excerpt
    sentiment: Optional[float]          # –1.0 to +1.0 (None = not extracted)
    recency_hours: Optional[float]      # age in hours at collection time (None if no published_at)
    record_id: str                      # stable SHA-256 dedup key
    extra: dict[str, Any]              # adapter-specific metadata (not used in features)

    @staticmethod
    def make_record_id(url: Optional[str], title: str, published_date: str = "") -> str:
        """Stable dedup key from URL + title + date prefix."""
        raw = f"{url or ''}|{title}|{published_date[:10]}"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]


# ---------------------------------------------------------------------------
# SoftSignals — derived features from a set of ScrapedRecords
# ---------------------------------------------------------------------------

@dataclass
class SoftSignals:
    """
    Soft (scraped-derived) feature vector for a single symbol on a given date.

    All fields here are *derived* from ScrapedRecords.  They are kept
    entirely separate from hard market-data features and are only combined
    at the export/training stage where column prefixes make origin explicit.
    """
    symbol: str
    as_of_date: str                     # YYYY-MM-DD

    # Rolling headline / mention counts
    headline_count_7d: int = 0          # distinct records in the past 7 days
    headline_count_30d: int = 0         # distinct records in the past 30 days

    # Source diversity
    source_count: int = 0              # number of distinct domains contributing records

    # Sentiment
    avg_sentiment: Optional[float] = None   # mean sentiment across records with scores

    # Theme alignment
    theme_alignment_score: float = 0.0  # fraction of records mentioning a known watchlist theme

    # Narrative momentum — acceleration vs. 30-day baseline
    # > 0 → accelerating coverage; < 0 → fading; 0 → steady state
    mention_acceleration: float = 0.0

    # Recency-weighted evidence score (exponential decay, half-life ~3 days)
    recency_score: float = 0.0

    # Overall confidence in the soft signals (0–1)
    scraped_confidence: float = 0.0

    # Provenance — which records contributed
    evidence_items: list[str] = field(default_factory=list)   # record_ids


# ---------------------------------------------------------------------------
# IntelBundle — per-symbol container
# ---------------------------------------------------------------------------

@dataclass
class IntelBundle:
    """
    Full scraped intelligence package for one symbol.

    Attached to a scan result row as result["scraped_intel"] after being
    converted to a plain dict via `to_dict()`.
    """
    symbol: str
    as_of_date: str
    records: list[ScrapedRecord] = field(default_factory=list)
    signals: Optional[SoftSignals] = None

    def to_dict(self) -> dict[str, Any]:
        """
        Produce the dict that is embedded in the scan result row.

        Only signals + provenance summary are included; raw record text is
        kept in the store and not embedded in the scan result to avoid bloat.
        """
        signals_dict: Optional[dict] = None
        if self.signals:
            s = self.signals
            signals_dict = {
                "headline_count_7d":     s.headline_count_7d,
                "headline_count_30d":    s.headline_count_30d,
                "source_count":          s.source_count,
                "avg_sentiment":         s.avg_sentiment,
                "theme_alignment_score": s.theme_alignment_score,
                "mention_acceleration":  s.mention_acceleration,
                "recency_score":         round(s.recency_score, 4),
                "scraped_confidence":    round(s.scraped_confidence, 4),
                "evidence_items":        s.evidence_items[:10],  # cap to avoid bloat
            }
        return {
            "soft_signals":    signals_dict,
            "records_count":   len(self.records),
            "scraped_confidence": self.signals.scraped_confidence if self.signals else 0.0,
        }
