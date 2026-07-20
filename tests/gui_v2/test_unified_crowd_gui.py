"""Unified Crowd Intelligence — GUI integration (observe-only, additive).

Asserts:
  * collect_crowd_radar_view always returns a degraded-safe `unified_crowd` key.
  * collect_crowd_radar_view shapes the unified status artifact for display.
  * crowd_context_for attaches a `unified` sub-dict per ticker when the
    unified_crowd_intelligence.json artifact exists.
  * build_advisory_picks surfaces unified display fields without touching
    action / confidence / scoring fields.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from gui_v2.data.dash_crowd_radar import collect_crowd_radar_view
from gui_v2.data.dash_crowd_context import crowd_context_for
from gui_v2.data.portfolio_presenter import build_advisory_picks


def _write_unified_status(root: Path, **over) -> None:
    latest = root / "outputs" / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    doc = {
        "source": "unified_crowd_intelligence_status",
        "simulation_active": True,
        "production_gated": True,
        "generated_at": "2026-06-16T19:14:39+00:00",
        "total_tickers": 126,
        "lane_a_tickers": 100,
        "lane_b_tickers": 46,
        "overlap_tickers": 20,
        "source_breadth_max": 4,
        "enabled_categories": ["analyst", "attention", "congress", "insider", "news"],
        "disabled_categories": ["social_sentiment"],
        "social_sentiment_status": "PLAN_LOCKED",
        "crowd_confidence_avg": 0.3165,
        "state_counts": {"confirmed_attention": 4},
        "top_confirmed_attention": [
            {"ticker": "TSLA", "crowd_confidence": 0.95, "retail_attention_score": 1.0,
             "fmp_attention_score": 0.99, "cross_source_confirmation_score": 0.99,
             "cross_source_divergence_score": 0.01,
             "explanation": "Confirmed attention: retail velocity aligns with FMP context."},
        ],
        "top_retail_only_attention": [
            {"ticker": "GME", "crowd_confidence": 0.4, "retail_attention_score": 0.9,
             "fmp_attention_score": None, "cross_source_confirmation_score": 0.1,
             "cross_source_divergence_score": 0.8, "explanation": "Retail-only."},
        ],
        "top_divergent_attention": [
            {"ticker": "MRVL", "crowd_confidence": 0.6, "retail_attention_score": 0.6,
             "fmp_attention_score": 0.99, "cross_source_confirmation_score": 0.6,
             "cross_source_divergence_score": 0.39, "explanation": "Divergent."},
        ],
        "top_institutional_context_only": [
            {"ticker": "JPM", "crowd_confidence": 0.5, "retail_attention_score": None,
             "fmp_attention_score": 0.8, "cross_source_confirmation_score": 0.0,
             "cross_source_divergence_score": 0.0, "explanation": "Institutional context only."},
        ],
    }
    doc.update(over)
    (latest / "unified_crowd_intelligence_status.json").write_text(json.dumps(doc))


def _write_unified_records(root: Path) -> None:
    latest = root / "outputs" / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    (latest / "unified_crowd_intelligence.json").write_text(json.dumps({
        "generated_at": "2026-06-16T19:14:39+00:00",
        "records": [
            {"ticker": "NVDA", "crowd_state": "confirmed_attention",
             "retail_attention_score": 0.9, "fmp_attention_score": 0.95,
             "cross_source_confirmation_score": 0.92, "cross_source_divergence_score": 0.05,
             "explanation": "Confirmed: retail aligns with FMP context."},
        ],
    }))


# --- Task 1: crowd radar view ---------------------------------------------

def test_unified_crowd_key_present_and_degraded_safe(tmp_path):
    v = collect_crowd_radar_view(tmp_path)
    assert "unified_crowd" in v
    assert v["unified_crowd"] == {"has_data": False}


def test_unified_crowd_shaped_for_display(tmp_path):
    _write_unified_status(tmp_path)
    v = collect_crowd_radar_view(tmp_path)
    uc = v["unified_crowd"]
    assert uc["has_data"] is True
    assert uc["total_tickers"] == 126
    assert uc["lane_a_tickers"] == 100
    assert uc["lane_b_tickers"] == 46
    assert uc["overlap_tickers"] == 20
    assert uc["source_breadth_max"] == 4
    assert "social_sentiment" in uc["disabled_categories"]
    assert uc["social_sentiment_status"] == "PLAN_LOCKED"
    # Four top-lists, each shaped with display keys.
    for key in ("top_confirmed", "top_retail_only", "top_divergent", "top_market_context"):
        assert uc[key], f"{key} should be populated"
        row = uc[key][0]
        for col in ("ticker", "retail_attention_score", "fmp_attention_score",
                    "confirmation", "divergence", "crowd_confidence", "explanation"):
            assert col in row
    assert uc["top_confirmed"][0]["ticker"] == "TSLA"


def test_unified_crowd_handles_malformed_artifact(tmp_path):
    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    (latest / "unified_crowd_intelligence_status.json").write_text("{not valid json")
    v = collect_crowd_radar_view(tmp_path)
    assert v["unified_crowd"] == {"has_data": False}


# --- Task 2: per-symbol crowd context attaches unified sub-dict -----------

def test_crowd_context_attaches_unified_when_artifact_exists(tmp_path):
    _write_unified_records(tmp_path)
    out = crowd_context_for(tmp_path, ["NVDA", "AAPL"])
    nvda = out["by_symbol"]["NVDA"]
    assert "unified" in nvda
    assert nvda["unified"]["crowd_state"] == "confirmed_attention"
    assert nvda["unified"]["retail_attention_score"] == 0.9
    assert nvda["unified"]["fmp_attention_score"] == 0.95
    assert nvda["unified"]["cross_source_confirmation_score"] == 0.92
    assert nvda["unified"]["cross_source_divergence_score"] == 0.05
    assert nvda["unified"]["explanation"]
    # A ticker with no unified row must not carry an empty/invalid unified block.
    assert "unified" not in out["by_symbol"]["AAPL"]


def test_crowd_context_no_unified_when_absent(tmp_path):
    out = crowd_context_for(tmp_path, ["NVDA"])
    # Existing keys preserved; honest degraded state with no unified attachment.
    nvda = out["by_symbol"]["NVDA"]
    assert nvda["present"] is False
    assert "unified" not in nvda


# --- Task 2b: advisory picks surface unified display fields ----------------

def test_advisory_picks_surface_unified_fields():
    decisions = [{"ticker": "NVDA", "action": "BUY", "confidence": 0.7,
                  "rationale": "thesis"}]
    crowd_by_symbol = {"NVDA": {"present": True, "label": "Crowd Validation",
                                "severity": "green", "top_reasons": ["x"],
                                "warnings": [], "unified": {
                                    "crowd_state": "confirmed_attention",
                                    "retail_attention_score": 0.9,
                                    "fmp_attention_score": 0.95,
                                    "cross_source_confirmation_score": 0.92,
                                    "cross_source_divergence_score": 0.05,
                                    "explanation": "Confirmed."}}}
    picks = build_advisory_picks(decisions, crowd_by_symbol, {})
    p = picks[0]
    assert p["unified_crowd_state"] == "confirmed_attention"
    assert p["unified_retail_attention"] == 0.9
    assert p["unified_fmp_context"] == 0.95
    assert p["unified_confirmation"] == 0.92
    assert p["unified_divergence"] == 0.05
    assert p["unified_explanation"] == "Confirmed."
    # Scoring/action fields untouched.
    assert p["action"] == "BUY"
    assert p["confidence_pct"] == 70


def test_advisory_picks_degrade_without_unified():
    decisions = [{"ticker": "NVDA", "action": "BUY", "confidence": 0.7}]
    crowd_by_symbol = {"NVDA": {"present": True, "label": "Crowd Validation",
                                "severity": "green", "top_reasons": [], "warnings": []}}
    picks = build_advisory_picks(decisions, crowd_by_symbol, {})
    p = picks[0]
    assert "unified_crowd_state" not in p
    assert p["action"] == "BUY"
