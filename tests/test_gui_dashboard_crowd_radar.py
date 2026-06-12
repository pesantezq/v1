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
