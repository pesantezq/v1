"""Tests for the unified loader fallback chain + writer artifact assembly."""
from __future__ import annotations

import json
from pathlib import Path

from portfolio_automation.crowd_intelligence import unified_loader as ul
from portfolio_automation.crowd_intelligence import unified_writer as uw


def _write(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _social_artifact(tickers):
    return {
        "schema_version": "1",
        "source": "crowd_multi_source_velocity",
        "created_at": "2026-06-16T12:00:00+00:00",
        "source_status": "ok",
        "records": [
            {"ticker": t, "mention_velocity": v, "source_breadth": 1, "confidence": 0.35,
             "active_sources": ["apewisdom"]}
            for t, v in tickers
        ],
    }


def _fmp_artifact(symbols):
    return {
        "observe_only": True,
        "source": "crowd_intelligence",
        "generated_at": "2026-06-16T12:00:00+00:00",
        "symbols": [
            {"symbol": s, "composite_crowd_score": 0.0, "confidence": 0.6,
             "category_scores": {"news": 0.0, "analyst": 0.0, "insider": 0.0,
                                 "congress": 0.0, "attention": 0.0, "social_sentiment": 0.0},
             "data_freshness": 0.95, "source_records_count": 20,
             "enabled_sources": [], "disabled_sources": []}
            for s in symbols
        ],
    }


def _fmp_status():
    return {
        "observe_only": True, "overall_status": "ok", "symbols_count": 1,
        "enabled_categories": ["analyst", "attention", "congress", "insider", "news"],
        "disabled_categories": ["social_sentiment"],
    }


# --- writer --------------------------------------------------------------------

def test_writer_joins_and_writes_artifacts(tmp_path):
    _write(tmp_path / "outputs/sandbox/discovery/crowd_multi_source_velocity.json",
           _social_artifact([("AAPL", 5.0), ("GME", 8.0)]))
    _write(tmp_path / "outputs/latest/crowd_intelligence.json", _fmp_artifact(["AAPL", "XOM"]))
    _write(tmp_path / "outputs/latest/crowd_intelligence_status.json", _fmp_status())

    status = uw.run(tmp_path)
    assert status["overall_status"] == "ok"
    assert status["total_tickers"] == 3  # AAPL, GME, XOM
    assert status["overlap_tickers"] == 1  # AAPL
    assert status["social_sentiment_status"] == "PLAN_LOCKED"

    art = json.loads((tmp_path / "outputs/latest/unified_crowd_intelligence.json").read_text())
    assert art["record_count"] == 3
    # simulation-active (NOT observe-only) — sim lane may consume it; production-gated.
    assert art["simulation_active"] is True
    assert art["production_gated"] is True
    assert art["feeds_decision_engine"] is False
    assert (tmp_path / "outputs/latest/unified_crowd_intelligence_status.json").exists()
    assert (tmp_path / "outputs/latest/unified_crowd_intelligence.md").exists()


def test_writer_degrades_when_one_lane_empty(tmp_path):
    # Only FMP present.
    _write(tmp_path / "outputs/latest/crowd_intelligence.json", _fmp_artifact(["XOM"]))
    _write(tmp_path / "outputs/latest/crowd_intelligence_status.json", _fmp_status())
    status = uw.run(tmp_path)
    assert status["overall_status"] == "degraded"
    assert status["lane_b_tickers"] == 1
    assert status["lane_a_tickers"] == 0
    assert "social_lane_unavailable" in status["warnings"]


def test_writer_no_lanes_does_not_crash(tmp_path):
    status = uw.run(tmp_path)
    assert status["overall_status"] in ("degraded", "failed")
    assert status["total_tickers"] == 0


# --- loader fallback chain -----------------------------------------------------

def test_fallback_prefers_unified(tmp_path):
    _write(tmp_path / "outputs/latest/unified_crowd_intelligence.json",
           {"records": [{"ticker": "AAPL", "crowd_state": "confirmed_attention"}],
            "generated_at": "now"})
    out = ul.read_unified_crowd(tmp_path)
    assert out["source"] == "unified"
    assert out["fallback_level"] == 1
    assert "AAPL" in out["by_ticker"]


def test_fallback_to_fmp_when_no_unified(tmp_path):
    _write(tmp_path / "outputs/latest/crowd_intelligence.json", _fmp_artifact(["XOM"]))
    _write(tmp_path / "outputs/latest/crowd_intelligence_status.json", _fmp_status())
    out = ul.read_unified_crowd(tmp_path)
    assert out["source"] == "crowd_intelligence"
    assert out["fallback_level"] == 2
    assert "XOM" in out["by_ticker"]


def test_fallback_to_social_when_only_social(tmp_path):
    _write(tmp_path / "outputs/sandbox/discovery/crowd_multi_source_velocity.json",
           _social_artifact([("GME", 8.0)]))
    out = ul.read_unified_crowd(tmp_path)
    assert out["source"] == "social_intelligence"
    assert out["fallback_level"] == 3
    assert "GME" in out["by_ticker"]


def test_fallback_honest_empty(tmp_path):
    out = ul.read_unified_crowd(tmp_path)
    assert out["available"] is False
    assert out["source"] == "none"
    assert out["fallback_level"] == 4
    assert out["by_ticker"] == {}


# --- staleness detection -------------------------------------------------------

def test_social_lane_stale_detection(tmp_path):
    old = _social_artifact([("AAPL", 5.0)])
    old["created_at"] = "2020-01-01T00:00:00+00:00"  # ancient
    _write(tmp_path / "outputs/sandbox/discovery/crowd_multi_source_velocity.json", old)
    lane = ul.load_social_lane(tmp_path)
    assert lane["available"] is True
    assert lane["stale"] is True
