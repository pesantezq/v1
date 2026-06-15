"""Tests for the Crowd Radar dashboard view (data loader + route).

Acceptance: the GUI shows Crowd Radar without breaking when no social data exists,
and never renders a trade verb.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from gui_v2.data.dash_crowd_radar import collect_crowd_radar_view

_FORBIDDEN = ("buy now", "sell now", "place order", "rebalance now")


def test_empty_view_when_artifacts_absent(tmp_path):
    v = collect_crowd_radar_view(tmp_path)
    assert v["persona"] == "crowd_radar"
    assert v["observe_only"] is True
    assert v["has_data"] is False
    # Summary cards always render (status / backtest / compliance).
    assert len(v["cards"]) == 3
    assert v["sections"] == []


def test_disabled_artifact_renders_unknown(tmp_path):
    disc = tmp_path / "outputs" / "sandbox" / "discovery"
    disc.mkdir(parents=True)
    (disc / "crowd_knowledge_state.json").write_text(json.dumps({
        "source_status": "disabled", "data_quality_status": "disabled",
        "records": [], "warnings": ["crowd_radar.enabled=false"], "created_at": "2026-06-12T00:00:00Z",
    }))
    v = collect_crowd_radar_view(tmp_path)
    assert v["source_status"] == "disabled"
    assert v["has_data"] is False
    assert any("enabled=false" in w for w in v["warnings"])


def test_populated_view_groups_by_state(tmp_path):
    disc = tmp_path / "outputs" / "sandbox" / "discovery"
    disc.mkdir(parents=True)
    (disc / "crowd_knowledge_state.json").write_text(json.dumps({
        "source_status": "ok", "data_quality_status": "ok", "created_at": "2026-06-12T00:00:00Z",
        "records": [
            {"ticker": "NVDA", "crowd_state": "crowd_validation", "confidence": 0.7,
             "crowd_research_priority_score": 8.0, "recommended_next_step": "requires_news_validation",
             "risk_flags": [], "score_components": {"velocity_z": 1.0, "dd_density": 0.5, "evidence_score": 0.7}},
            {"ticker": "GME", "crowd_state": "hype_acceleration", "confidence": 0.75,
             "crowd_research_priority_score": -2.0, "recommended_next_step": "flag_as_hype_risk",
             "risk_flags": ["fast_mention_growth_weak_evidence"],
             "score_components": {"velocity_z": 3.0, "dd_density": 0.0, "evidence_score": 0.0}},
        ],
        "warnings": [],
    }))
    (disc / "public_knowledge_velocity.json").write_text(json.dumps({"post_count": 14}))
    (disc / "social_signal_backtest.json").write_text(json.dumps(
        {"states_matured": [], "total_observations": 0, "min_sample": 20}))
    (disc / "social_source_compliance.json").write_text(json.dumps(
        {"review_needed_count": 0, "active_sources": 1, "total_sources": 1}))

    v = collect_crowd_radar_view(tmp_path)
    assert v["has_data"] is True
    keys = {s["key"] for s in v["sections"]}
    assert "crowd_validation" in keys
    assert "hype_acceleration" in keys


def test_route_renders_200_and_no_trade_verbs():
    from gui_v2.app import app

    client = TestClient(app)
    r = client.get("/dashboard/crowd-radar")
    assert r.status_code == 200
    text = r.text.lower()
    assert "sandbox research intelligence only" in text
    assert "not a trade recommendation" in text
    for verb in _FORBIDDEN:
        assert verb not in text


# --- Redesign (2026-06-15): summary strip, source reason/action, advisory why ---

def _write_state(disc, **over):
    base = {"source_status": "ok", "data_quality_status": "ok",
            "created_at": "2026-06-12T00:00:00Z", "records": [], "warnings": []}
    base.update(over)
    (disc / "crowd_knowledge_state.json").write_text(json.dumps(base))


def test_summary_strip_fields_present_when_empty(tmp_path):
    v = collect_crowd_radar_view(tmp_path)
    # Derived summary fields always present + honest defaults.
    assert v["active_source_count"] == 0
    assert v["total_source_count"] == 0
    assert v["active_source_severity"] == "gray"
    assert v["data_quality_label"] == "Unavailable"
    assert v["ticker_count"] == 0
    assert v["velocity_rows"] == []
    # Honest empty advisory.
    assert v["advisory"]["produced"] is False
    assert v["advisory"]["why"]  # non-empty reason list
    assert v["advisory"]["next_steps"]


def test_source_health_reason_action_mapping(tmp_path):
    disc = tmp_path / "outputs" / "sandbox" / "discovery"
    disc.mkdir(parents=True)
    _write_state(disc, source_status="insufficient_data",
                 data_quality_status="insufficient_data")
    (disc / "crowd_source_health.json").write_text(json.dumps({"records": [
        {"source_name": "apewisdom", "status": "ok", "warnings": []},
        {"source_name": "fmp_social_sentiment", "status": "not_entitled", "warnings": []},
        {"source_name": "finnhub_social", "status": "no_credentials", "warnings": []},
        {"source_name": "quiver_wsb", "status": "blocked_no_extra_cost", "warnings": []},
        {"source_name": "stocktwits", "status": "not_configured", "warnings": []},
    ]}))
    v = collect_crowd_radar_view(tmp_path)
    rows = {r["source"]: r for r in v["source_health_rows"]}
    assert "Active" in rows["apewisdom"]["reason"]
    assert rows["fmp_social_sentiment"]["reason"] == "Plan does not include this endpoint"
    assert rows["fmp_social_sentiment"]["action"] == "Enable FMP entitlement"
    assert rows["finnhub_social"]["action"] == "Add API credentials"
    assert rows["quiver_wsb"]["reason"].startswith("Disabled")
    assert "config or token" in rows["stocktwits"]["reason"]
    # X / Y counts.
    assert v["active_source_count"] == 1
    assert v["total_source_count"] == 5
    assert v["active_source_severity"] == "green"


def test_advisory_why_and_next_steps_derived(tmp_path):
    disc = tmp_path / "outputs" / "sandbox" / "discovery"
    disc.mkdir(parents=True)
    # records present but data quality insufficient -> no advisory.
    _write_state(disc, source_status="insufficient_data",
                 data_quality_status="insufficient_data",
                 records=[{"ticker": "GME", "crowd_state": "hype_acceleration",
                           "confidence": 0.6, "crowd_research_priority_score": 1.0,
                           "recommended_next_step": "flag_as_hype_risk", "risk_flags": [],
                           "score_components": {}}])
    (disc / "crowd_source_health.json").write_text(json.dumps({"records": [
        {"source_name": "apewisdom", "status": "ok", "warnings": []},
        {"source_name": "fmp_social_sentiment", "status": "not_entitled", "warnings": []},
        {"source_name": "finnhub_social", "status": "no_credentials", "warnings": []},
    ]}))
    v = collect_crowd_radar_view(tmp_path)
    adv = v["advisory"]
    assert adv["produced"] is False
    why = " ".join(adv["why"]).lower()
    assert "not entitled" in why                 # fmp entitlement reason
    assert "only apewisdom is active" in why     # single governed source
    assert "below the advisory threshold" in why # confidence reason
    steps = " ".join(adv["next_steps"]).lower()
    assert "fmp" in steps and "finnhub_social" in steps
    # Single active governed source -> low-confidence flag drives the velocity banner.
    assert v["active_source_count"] == 1


def test_velocity_rows_sorted_desc_with_rank(tmp_path):
    disc = tmp_path / "outputs" / "sandbox" / "discovery"
    disc.mkdir(parents=True)
    _write_state(disc)
    (disc / "crowd_multi_source_velocity.json").write_text(json.dumps({"labels": ["x"], "records": [
        {"ticker": "AAA", "mention_velocity": 1.0, "source_breadth": 1,
         "hype_risk_score": 0.1, "confidence": 0.3, "labels": []},
        {"ticker": "BBB", "mention_velocity": 5.0, "source_breadth": 2,
         "hype_risk_score": 0.2, "confidence": 0.5, "labels": ["multi_source"]},
    ]}))
    v = collect_crowd_radar_view(tmp_path)
    assert [r["ticker"] for r in v["velocity_rows"]] == ["BBB", "AAA"]
    assert v["velocity_rows"][0]["rank"] == 1
    assert v["velocity_rows"][0]["signal"] == "multi_source"
