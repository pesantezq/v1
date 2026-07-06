"""Tests: unified crowd feeds the simulation lane + the AI review packet.

Governance invariants verified here:
- The sim lane (active) MAY consume unified crowd to change simulation outputs.
- The AI review packet includes unified crowd evidence with NO second AI call.
- Production gating preserved: the reviewer may RECOMMEND production-readiness;
  human approval is required for any production change (it cannot self-approve).
"""
from __future__ import annotations

import json
from pathlib import Path

from portfolio_automation.sim_governance import simulation_lane as SL
from portfolio_automation.sim_governance import ai_review_packet as AP
from portfolio_automation.sim_governance import daily_simulation_bundle as DB


def _write_unified(root: Path):
    (root / "outputs/latest").mkdir(parents=True, exist_ok=True)
    (root / "outputs/latest/unified_crowd_intelligence.json").write_text(json.dumps({
        "records": [
            {"ticker": "AAPL", "crowd_state": "confirmed_attention",
             "cross_source_confirmation_score": 0.8, "cross_source_divergence_score": 0.1,
             "crowd_confidence": 0.7, "retail_attention_score": 0.9, "fmp_attention_score": 0.85},
            {"ticker": "GME", "crowd_state": "retail_only_attention",
             "cross_source_confirmation_score": 0.1, "cross_source_divergence_score": 0.6,
             "crowd_confidence": 0.3, "retail_attention_score": 0.8, "fmp_attention_score": None},
        ],
        "generated_at": "2026-06-16T12:00:00+00:00",
    }), encoding="utf-8")


def test_sim_crowd_context_maps_unified(tmp_path):
    _write_unified(tmp_path)
    ctx = SL._load_unified_crowd_context(tmp_path)
    assert "AAPL" in ctx and "GME" in ctx
    # confirmed -> velocity = confirmation*2 = 1.6 (>=1.5 ready), confirmed True
    assert ctx["AAPL"]["velocity"] == 1.6
    assert ctx["AAPL"]["confirmed"] is True
    assert ctx["AAPL"]["state"] == "confirmed_attention"
    # retail-only -> low confirmation velocity, not confirmed
    assert ctx["GME"]["confirmed"] is False
    assert ctx["GME"]["velocity"] < 1.0


def test_sim_crowd_context_empty_without_unified(tmp_path):
    assert SL._load_unified_crowd_context(tmp_path) == {}


def test_advisory_crowd_experiment_fires_with_unified():
    baseline = {
        "advisory": [{"symbol": "AAPL"}],
        "crowd": {"AAPL": {"state": "confirmed_attention", "confidence": 0.7, "confirmed": True}},
    }
    cands = SL.experiment_advisory_crowd_context(baseline)
    assert len(cands) == 1
    assert cands[0].symbol == "AAPL"
    # crowd_context is an observe-only, self-refreshing annotation — it never
    # enters the human-gated promotion queue, so it is never marked ready even
    # when the crowd state is confirmed.
    assert cands[0].ready_for_production_review is False
    # provenance points at the real unified crowd bus, not the absent legacy path
    assert cands[0].source_evidence == ["outputs/latest/unified_crowd_intelligence.json"]


def test_watchlist_rerank_uses_confirmation_velocity():
    baseline = {
        "watchlist_ranked": [{"symbol": "AAPL", "rank": 5}],
        "crowd": {"AAPL": {"velocity": 1.6}},  # confirmation-derived, >=1.5
    }
    cands = SL.experiment_watchlist_rerank(baseline)
    assert len(cands) == 1
    assert cands[0].ready_for_production_review is True


def test_summarize_unified_crowd_compact(tmp_path):
    (tmp_path / "outputs/latest").mkdir(parents=True, exist_ok=True)
    (tmp_path / "outputs/latest/unified_crowd_intelligence_status.json").write_text(json.dumps({
        "overall_status": "ok", "total_tickers": 3, "lane_a_tickers": 2, "lane_b_tickers": 2,
        "overlap_tickers": 1, "social_sentiment_status": "PLAN_LOCKED", "crowd_confidence_avg": 0.4,
        "state_counts": {"confirmed_attention": 1},
        "top_confirmed_attention": [{"ticker": "AAPL"}, {"ticker": "MSFT"}],
        "top_divergent_attention": [{"ticker": "GME"}],
    }), encoding="utf-8")
    s = DB._summarize_unified_crowd(str(tmp_path / "outputs"))
    assert s["available"] is True
    assert s["social_sentiment_status"] == "PLAN_LOCKED"
    assert s["top_confirmed_attention"] == ["AAPL", "MSFT"]
    assert s["candidates_ready_for_production_review"] == ["AAPL", "MSFT"]


def test_summarize_unified_crowd_missing(tmp_path):
    s = DB._summarize_unified_crowd(str(tmp_path / "outputs"))
    assert s == {"available": False}


def test_review_packet_includes_unified_crowd_no_extra_call():
    bundle = {
        "advisory_experiment_results": [],
        "watchlist_experiment_results": [],
        "unified_crowd_summary": {"available": True, "overall_status": "ok",
                                  "top_confirmed_attention": ["AAPL"]},
    }
    packet = AP.build_review_packet(bundle, "2026-06-16T12:00:00+00:00")
    assert "unified_crowd_summary" in packet
    assert packet["unified_crowd_summary"]["top_confirmed_attention"] == ["AAPL"]
    # the packet is still ONE review (no second workflow/call added)
    assert packet["covers_workflows"] == ["advisory", "watchlist"]
    # production-gate language preserved: reviewer recommends, human approves production
    assert "cannot approve production" in packet["instruction"]
