"""
Phase 11: Bounded daily history for social sentiment per source and per ticker.

History is an append-only JSONL ledger capped at MAX_HISTORY_DAYS days.
Each entry is one day's aggregate per (ticker, source). The tracker also
computes per-ticker trend states from the rolling window.

Trend states:
  - "building_history"    — fewer than MIN_HISTORY_DAYS data points
  - "positive_rising"     — sentiment trending up, currently positive
  - "positive_stable"     — positive, slope near zero
  - "neutral"             — near-zero sentiment, low slope
  - "negative_falling"    — sentiment trending down, currently negative
  - "negative_stable"     — negative, slope near zero
  - "mixed"               — high variance, no clear direction

This module is sandbox-only and observe-only.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("stockbot.social_sentiment.history")

MAX_HISTORY_DAYS = 30
MIN_HISTORY_DAYS = 5
_NEUTRAL_BAND = 0.1   # |sentiment| < NEUTRAL_BAND → "neutral"
_SLOPE_THRESHOLD = 0.02  # slope magnitude to count as "rising" or "falling"


def _utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _slope(values: list[float]) -> float:
    """Simple linear regression slope over index values."""
    n = len(values)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values))
    den = sum((x - mean_x) ** 2 for x in xs)
    return num / den if den else 0.0


class SentimentHistoryTracker:
    """
    Append-only daily sentiment history tracker.

    The ledger is stored as a JSONL file at ``ledger_path``. Each line is one
    daily entry for one (ticker, source) pair.

    Call ``record_daily(...)`` once per pipeline run to append today's aggregates.
    Call ``get_ticker_history(ticker)`` to read all history for a ticker.
    Call ``compute_trend_state(ticker)`` to get the current trend label.
    """

    def __init__(self, ledger_path: str | Path) -> None:
        self._path = Path(ledger_path)

    def record_daily(
        self,
        ticker: str,
        source: str,
        sentiment_score: float,
        confidence: float,
        sample_size: int,
        date: str | None = None,
    ) -> None:
        """Append one daily entry. Idempotent for same (ticker, source, date)."""
        entry_date = date or _utc_date()
        entry: dict[str, Any] = {
            "date": entry_date,
            "ticker": ticker.upper(),
            "source": source,
            "sentiment_score": round(float(sentiment_score), 4),
            "confidence": round(float(confidence), 4),
            "sample_size": int(sample_size),
        }
        # Check for duplicate (same ticker+source+date) — skip if present.
        existing = self._load_raw()
        for row in existing:
            if (row.get("date") == entry_date
                    and row.get("ticker") == ticker.upper()
                    and row.get("source") == source):
                return  # already recorded today

        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

        self._prune()

    def _prune(self) -> None:
        """Keep only the last MAX_HISTORY_DAYS distinct dates per ticker+source."""
        rows = self._load_raw()
        if not rows:
            return
        # Group by (ticker, source), keep last MAX_HISTORY_DAYS dates.
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in rows:
            key = (row.get("ticker", ""), row.get("source", ""))
            groups.setdefault(key, []).append(row)
        pruned: list[dict[str, Any]] = []
        for key, group in groups.items():
            sorted_group = sorted(group, key=lambda r: r.get("date", ""))
            pruned.extend(sorted_group[-MAX_HISTORY_DAYS:])
        if len(pruned) < len(rows):
            with self._path.open("w", encoding="utf-8") as fh:
                for row in pruned:
                    fh.write(json.dumps(row) + "\n")

    def _load_raw(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        rows: list[dict[str, Any]] = []
        try:
            with self._path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            rows.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except OSError:
            pass
        return rows

    def get_ticker_history(self, ticker: str) -> list[dict[str, Any]]:
        """All history rows for a ticker, sorted by date ascending."""
        ticker = ticker.upper()
        rows = [r for r in self._load_raw() if r.get("ticker") == ticker]
        return sorted(rows, key=lambda r: r.get("date", ""))

    def compute_trend_state(self, ticker: str) -> str:
        """
        Compute current trend state label for a ticker.

        Aggregates across sources by date (mean sentiment per day), then
        classifies the rolling window.
        """
        history = self.get_ticker_history(ticker)
        if not history:
            return "building_history"

        # Aggregate per date (mean across sources)
        by_date: dict[str, list[float]] = {}
        for row in history:
            date = row.get("date", "")
            s = float(row.get("sentiment_score") or 0.0)
            by_date.setdefault(date, []).append(s)

        dates = sorted(by_date.keys())
        daily_scores = [sum(by_date[d]) / len(by_date[d]) for d in dates]

        if len(daily_scores) < MIN_HISTORY_DAYS:
            return "building_history"

        recent = daily_scores[-MIN_HISTORY_DAYS:]
        mean = sum(recent) / len(recent)
        slope = _slope(recent)
        variance = sum((x - mean) ** 2 for x in recent) / len(recent)

        if variance > 0.1:
            return "mixed"
        if abs(mean) < _NEUTRAL_BAND and abs(slope) < _SLOPE_THRESHOLD:
            return "neutral"
        if mean > _NEUTRAL_BAND:
            if slope > _SLOPE_THRESHOLD:
                return "positive_rising"
            return "positive_stable"
        if mean < -_NEUTRAL_BAND:
            if slope < -_SLOPE_THRESHOLD:
                return "negative_falling"
            return "negative_stable"
        return "neutral"

    def get_summary(self) -> dict[str, Any]:
        """Summary for health/status reporting."""
        rows = self._load_raw()
        tickers = {r.get("ticker", "") for r in rows if r.get("ticker")}
        sources = {r.get("source", "") for r in rows if r.get("source")}
        dates = {r.get("date", "") for r in rows if r.get("date")}
        return {
            "ledger_path": str(self._path),
            "total_rows": len(rows),
            "unique_tickers": len(tickers),
            "unique_sources": len(sources),
            "date_range": {
                "earliest": min(dates) if dates else None,
                "latest": max(dates) if dates else None,
            },
        }
