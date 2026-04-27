"""
Data models for the theme_discovery pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Article:
    """A single news article collected from an RSS feed."""
    title: str
    summary: str
    link: str
    published: str        # ISO-8601 string from RSSCollector
    source_domain: str    # e.g. "marketwatch.com"
    item_hash: str


@dataclass
class ArticleSignal:
    """An article tagged with its relevance score and any tickers found."""
    article: Article
    theme_score: float                          # normalized keyword score [0, 1]
    tickers_found: list[str] = field(default_factory=list)


@dataclass
class ExtractResult:
    """Container for the two extraction paths."""
    classified: dict[str, list[ArticleSignal]]   # theme_name → signals
    emerging: dict[str, list[ArticleSignal]]     # phrase → signals


@dataclass
class ThemeOpportunity:
    """A scored market theme — either a classified keyword theme or an emerging phrase."""
    name: str
    theme_type: str                # "classified" | "emerging"
    score: float                   # 0-1 composite
    confidence: float              # 0-1
    mention_count: int
    unique_ticker_count: int
    tickers: list[str]
    evidence: list[str]            # up to 5 representative headlines
    source_count: int
    persistence_score: float       # fraction of recent runs this theme appeared in
    acceleration_score: float      # sigmoid-normalized recent vs prior mention ratio
    recency_score: float
    diversity_score: float
    history_runs_seen: int         # total runs in history where this theme appeared
    first_seen: str | None         # ISO timestamp from history
    last_seen: str | None          # ISO timestamp from history

    def to_dict(self) -> dict:
        return {
            # primary keys
            "name": self.name,
            "theme_type": self.theme_type,
            "score": round(self.score, 4),
            "confidence": round(self.confidence, 4),
            "mention_count": self.mention_count,
            "unique_ticker_count": self.unique_ticker_count,
            "tickers": self.tickers,
            "evidence": self.evidence,
            "source_count": self.source_count,
            "persistence_score": round(self.persistence_score, 4),
            "acceleration_score": round(self.acceleration_score, 4),
            "recency_score": round(self.recency_score, 4),
            "diversity_score": round(self.diversity_score, 4),
            "history_runs_seen": self.history_runs_seen,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            # backward-compat alias consumed by earlier GUI work
            "theme": self.name,
        }
