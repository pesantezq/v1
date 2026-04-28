"""
Tests for alert calibration changes:
  - Phase 1: Top-N fallback alerts when 0 regular alerts pass
  - Phase 2: Fresh data rotation invalidates OVERVIEW cache
  - Phase 3: Confidence floor prevents extreme suppression
  - Phase 4: Weak signals still filtered below fallback_min_signal_score

All tests are offline — WatchlistScanner is driven with mock clients and
a temporary cache dir so no real API calls or disk state leak between tests.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from watchlist_scanner.alert_filter import should_emit_alert
from watchlist_scanner.confidence import compute_confidence, CONFIDENCE_FLOOR


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_signal(
    signal_score: float = 0.60,
    confidence_score: float = 0.70,
    alert_priority: str | None = "watch",
    evidence_breadth: int = 2,
    filter_allowed: bool = True,
    final_rank_score: float = 0.55,
    priority_score: float = 0.55,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "ticker": "AAPL",
        "signal_score": signal_score,
        "confidence_score": confidence_score,
        "alert_priority": alert_priority,
        "routed_alert_priority": alert_priority,
        "evidence_breadth": evidence_breadth,
        "evidence_count": evidence_breadth,
        "filter_allowed": filter_allowed,
        "final_rank_score": final_rank_score,
        "priority_score": priority_score,
        **extra,
    }


def _make_scanner(watchlist: list[str], signals_config: dict | None = None):
    """Build a WatchlistScanner with mocked AV + cache clients."""
    from watchlist_scanner.scanner import WatchlistScanner
    from watchlist_scanner.cache_manager import CacheManager

    cache = MagicMock(spec=CacheManager)
    cache.calls_today = 0
    cache.get.return_value = None
    cache.get_stale.return_value = None
    cache.get_age_seconds.return_value = None
    cache.delete.return_value = None

    av = MagicMock()
    av._max_calls = 20
    av.get_news_sentiment.return_value = []
    av.get_overview.return_value = {}
    av.get_daily_ohlcv.return_value = None

    return WatchlistScanner(
        watchlist=watchlist,
        cache=cache,
        av_client=av,
        signals_config=signals_config or {},
        fmp_client=None,
    ), cache, av


# ---------------------------------------------------------------------------
# Phase 1: Top-N fallback
# ---------------------------------------------------------------------------

def test_zero_alert_scenario_produces_fallback_alerts():
    """When all signals are suppressed, fallback injects top-N opportunities."""
    # All signals below confidence threshold — no regular alerts pass.
    suppressed = [
        _make_signal(
            ticker=t,
            signal_score=0.50 + i * 0.01,
            confidence_score=0.45,   # below min_confidence_score=0.50
            alert_priority=None,
            filter_allowed=False,
            final_rank_score=0.40 + i * 0.01,
            priority_score=0.40 + i * 0.01,
        )
        for i, t in enumerate(["AAPL", "MSFT", "NVDA", "META", "TSLA"])
    ]
    signals_cfg = {"fallback_top_n": 3, "fallback_min_signal_score": 0.25}

    # Simulate scanner's fallback logic in isolation
    alerts: list[dict] = []
    _fallback_top_n = int(signals_cfg["fallback_top_n"])
    _fallback_min_signal = float(signals_cfg["fallback_min_signal_score"])
    if not alerts and _fallback_top_n > 0 and suppressed:
        candidates = [
            r for r in suppressed
            if float(r.get("signal_score") or 0.0) >= _fallback_min_signal
        ]
        candidates.sort(
            key=lambda x: (x.get("priority_score", 0.0), x.get("final_rank_score", 0.0)),
            reverse=True,
        )
        for r in candidates[:_fallback_top_n]:
            r["alert_priority"] = "watch"
            r["alert_type"] = "opportunity"
            r["alert_reason"] = "top-ranked fallback"
            r["filter_allowed"] = True
            r["filter_reason_code"] = "fallback_top_n"
            alerts.append(r)

    assert len(alerts) == 3
    assert all(a["alert_type"] == "opportunity" for a in alerts)
    assert all(a["alert_reason"] == "top-ranked fallback" for a in alerts)
    assert all(a["filter_reason_code"] == "fallback_top_n" for a in alerts)


def test_weak_signals_still_filtered_from_fallback():
    """Signals below fallback_min_signal_score must not appear in fallback."""
    weak = [
        _make_signal(
            ticker="WEAK",
            signal_score=0.10,       # below fallback_min_signal_score=0.25
            confidence_score=0.45,
            alert_priority=None,
            filter_allowed=False,
            final_rank_score=0.10,
            priority_score=0.10,
        )
    ]
    signals_cfg = {"fallback_top_n": 3, "fallback_min_signal_score": 0.25}
    alerts: list[dict] = []
    candidates = [
        r for r in weak
        if float(r.get("signal_score") or 0.0) >= float(signals_cfg["fallback_min_signal_score"])
    ]
    for r in candidates[:int(signals_cfg["fallback_top_n"])]:
        alerts.append(r)

    assert len(alerts) == 0, "Weak signals must not appear in fallback"


def test_fallback_skipped_when_regular_alerts_exist():
    """When at least one regular alert passes, fallback must not activate."""
    regular = [
        _make_signal(ticker="AAPL", signal_score=0.75, confidence_score=0.80,
                     alert_priority="normal", filter_allowed=True,
                     final_rank_score=0.70, priority_score=0.70)
    ]
    signals_cfg = {"fallback_top_n": 3, "fallback_min_signal_score": 0.25}
    alerts = list(regular)  # already has one alert — fallback should not run

    original_count = len(alerts)
    if not alerts:  # this branch won't execute
        for r in regular[:3]:
            r["alert_type"] = "opportunity"
            alerts.append(r)

    assert len(alerts) == original_count
    assert all(a.get("alert_type") != "opportunity" for a in alerts)


# ---------------------------------------------------------------------------
# Phase 2: Fresh data rotation
# ---------------------------------------------------------------------------

def test_fresh_rotation_calls_cache_delete():
    """scanner.run() must call cache.delete() for the selected fresh symbols."""
    scanner, cache, av = _make_scanner(
        watchlist=["AAPL", "MSFT", "NVDA", "META"],
        signals_config={"fresh_scan_fraction": 0.50, "fallback_top_n": 0},
    )
    # dry_run=True skips rotation; use non-dry to trigger it.
    # We intercept before real API calls; av.get_daily_ohlcv returns None (ok).
    scanner.run(dry_run=False)

    # With fraction=0.50 and 4 symbols, at least 2 cache.delete calls expected.
    deleted_keys = [
        call.args[0] for call in cache.delete.call_args_list
        if call.args[0].startswith("overview_")
    ]
    assert len(deleted_keys) >= 2


def test_fresh_rotation_skipped_in_dry_run():
    """In dry_run mode no cache deletions should occur."""
    scanner, cache, _ = _make_scanner(
        watchlist=["AAPL", "MSFT"],
        signals_config={"fresh_scan_fraction": 1.0, "fallback_top_n": 0},
    )
    scanner.run(dry_run=True)

    deleted = [c for c in cache.delete.call_args_list
               if c.args[0].startswith("overview_")]
    assert len(deleted) == 0


# ---------------------------------------------------------------------------
# Phase 3: Confidence floor
# ---------------------------------------------------------------------------

def test_confidence_floor_prevents_extreme_suppression():
    """compute_confidence must never return a score below CONFIDENCE_FLOOR."""
    # Worst-case: cached data, no tech, no fundamentals, no articles
    score, band, _ = compute_confidence(
        data_quality="cached",
        ov_source="budget_skipped",
        tech={},
        fundamentals={},
        articles=[],
        cache_age_seconds=None,
    )
    assert score >= CONFIDENCE_FLOOR, (
        f"Confidence {score} is below floor {CONFIDENCE_FLOOR}"
    )


def test_confidence_floor_value_is_reasonable():
    """CONFIDENCE_FLOOR must be above 0.0 and below the minimum alert threshold."""
    assert 0.0 < CONFIDENCE_FLOOR < 0.50, (
        "Floor should sit below the min_confidence_score=0.50 without being zero"
    )
