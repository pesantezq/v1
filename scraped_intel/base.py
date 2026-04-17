"""
Scraped Intelligence — abstract base classes.

Adding a new source adapter
---------------------------
1. Subclass SourceAdapter.
2. Set class-level source_type and domain.
3. Implement fetch(symbol, lookback_days) → list[ScrapedRecord].
4. Register in pipeline.py's _build_adapters().

Adapters must not raise on individual failures — return an empty list and
log a warning.  The pipeline treats each adapter as best-effort.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

from scraped_intel.models import ScrapedRecord

logger = logging.getLogger("scraped_intel.base")


class SourceAdapter(ABC):
    """
    Abstract base for all scraped-intelligence source adapters.

    Implementations are responsible for:
    - fetching raw content (HTTP, RSS, local cache)
    - normalising it into ScrapedRecord objects
    - gracefully returning [] on any per-adapter failure
    """

    #: Identifies the kind of content this adapter produces.
    #: Used in ScrapedRecord.source_type and provenance weighting.
    source_type: str = "unknown"

    #: Primary domain / authority name for this source.
    domain: str = "unknown"

    #: Source quality weight in [0, 1].
    #: 1.0 = primary regulatory source (SEC);
    #: 0.8 = established financial press;
    #: 0.5 = aggregated news / RSS;
    #: 0.3 = community / low-quality source.
    source_weight: float = 0.5

    def __init__(self, cache_dir: str = "data/scraped_cache") -> None:
        self.cache_dir = cache_dir

    @abstractmethod
    def fetch(self, symbol: str, lookback_days: int = 30) -> list[ScrapedRecord]:
        """
        Fetch and normalize records for `symbol` covering the past `lookback_days`.

        Must be idempotent: re-fetching the same window should produce the
        same record_ids (dedup is handled by the store).

        Returns an empty list — never raises — on failure.
        """
        ...

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def recency_hours(published_at: Optional[str]) -> Optional[float]:
        """Return age in hours from published_at ISO string, or None."""
        if not published_at:
            return None
        try:
            pub = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            delta = now - pub
            return max(0.0, delta.total_seconds() / 3600)
        except (ValueError, TypeError):
            return None
