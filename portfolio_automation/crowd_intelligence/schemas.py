"""Normalized crowd-intelligence record shapes (observe-only context)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CATEGORIES = ("news", "analyst", "insider", "congress", "attention", "social_sentiment")


@dataclass
class NormalizedEvent:
    provider: str
    endpoint_id: str
    symbol: str
    category: str
    event_time: str | None
    normalized_event_type: str
    raw: dict[str, Any]


@dataclass
class CategoryResult:
    """One category's contribution for one symbol. score is clamped to [-1, 1]."""
    category: str
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    events: list[NormalizedEvent] = field(default_factory=list)
    enabled_endpoints: list[str] = field(default_factory=list)
    disabled_endpoints: list[str] = field(default_factory=list)
    has_data: bool = False
    freshness: float = 0.0  # [0, 1]

    def neutral(self) -> bool:
        return not self.has_data or self.score == 0.0


@dataclass
class CrowdSignal:
    symbol: str
    composite_crowd_score: float
    confidence: float
    category_scores: dict[str, float]
    enabled_sources: list[str]
    disabled_sources: list[str]
    top_reasons: list[str]
    warnings: list[str]
    data_freshness: float
    source_records_count: int
    # Trend vs the most-recent prior daily run (filled from crowd_signal_daily history;
    # "building" until ≥2 days of history exist). Observe-only context.
    composite_trend: float | None = None
    trend_label: str = "building"
