"""End-to-end + governance tests for the Crowd Radar orchestrator.

These are the acceptance-criteria gatekeepers:
  - sandbox-only / observe-only artifacts
  - cannot mutate official portfolio outputs
  - disabled / no-credentials degrade gracefully (no crash)
  - run-mode governance keeps writes sandbox-only
  - artifacts carry required metadata
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from portfolio_automation.social_intelligence.base import RawPost, SourceStatus
from portfolio_automation.social_intelligence.reddit_connector import FetchResult
from portfolio_automation.social_intelligence.public_knowledge_velocity import (
    run_public_knowledge_velocity,
)

ARTIFACTS = [
    "social_source_compliance.json",
    "public_knowledge_velocity.json",
    "crowd_knowledge_state.json",
    "social_signal_backtest.json",
    "crowd_radar_summary.md",
]


def _write_config(root: Path, enabled: bool):
    cfg = {
        "watchlist_scanner": {"watchlist": ["NVDA", "GME", "TSLA", "PLTR"]},
        "crowd_radar": {"enabled": enabled, "subreddits": ["stocks"],
                        "min_mentions_for_state": 3, "min_backtest_sample": 20},
    }
    (root / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


def _sample_posts():
    posts = []
    for i in range(6):
        posts.append(RawPost(post_id=f"d{i}", source="reddit", community="stocks",
                             created_utc=0.0, title="$NVDA valuation thesis",
                             body="DCF earnings guidance margin catalyst fundamentals",
                             author_hash=f"a{i}"))
    for i in range(8):
        posts.append(RawPost(post_id=f"h{i}", source="reddit", community="wallstreetbets",
                             created_utc=0.0, title="$GME to the moon rocket yolo",
                             body="diamond hands tendies squeeze", author_hash=f"b{i}"))
    return posts


def test_disabled_writes_degraded_artifact_no_crash(tmp_path):
    _write_config(tmp_path, enabled=False)
    r = run_public_knowledge_velocity(root=tmp_path, run_mode="discovery")
    assert r["status"] == SourceStatus.DISABLED.value
    assert r["wrote_files"] is True
    disc = tmp_path / "outputs" / "sandbox" / "discovery"
    for name in ARTIFACTS:
        assert (disc / name).exists(), f"missing {name}"


def test_no_credentials_degrades_gracefully(tmp_path, monkeypatch):
    _write_config(tmp_path, enabled=True)
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("REDDIT_USER_AGENT", raising=False)
    r = run_public_knowledge_velocity(root=tmp_path, run_mode="discovery")
    assert r["status"] == SourceStatus.NO_CREDENTIALS.value
    assert r["state_count"] == 0


def test_end_to_end_with_injected_posts(tmp_path):
    _write_config(tmp_path, enabled=True)
    r = run_public_knowledge_velocity(
        root=tmp_path, run_mode="discovery", posts_override=_sample_posts(),
    )
    assert r["status"] == SourceStatus.OK.value
    assert r["state_count"] >= 2
    disc = tmp_path / "outputs" / "sandbox" / "discovery"
    state = json.loads((disc / "crowd_knowledge_state.json").read_text())
    # Required envelope metadata.
    for key in ("run_id", "run_mode", "created_at", "schema_version",
                "source_status", "data_quality_status", "observe_only",
                "no_trade", "not_recommendation", "sandbox_only", "warnings", "records"):
        assert key in state, f"missing envelope key {key}"
    assert state["observe_only"] is True
    assert state["no_trade"] is True
    assert state["sandbox_only"] is True
    # Every record must carry a research-only next step.
    forbidden = {"buy", "sell", "hold", "rebalance", "trim", "scale", "promote"}
    for rec in state["records"]:
        assert rec["recommended_next_step"] not in forbidden


def test_does_not_mutate_official_outputs(tmp_path):
    _write_config(tmp_path, enabled=True)
    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    plan = latest / "decision_plan.json"
    plan.write_text(json.dumps({"decisions": ["UNTOUCHED"]}), encoding="utf-8")
    before = plan.read_text()
    run_public_knowledge_velocity(root=tmp_path, run_mode="discovery",
                                  posts_override=_sample_posts())
    assert plan.read_text() == before


def test_daily_run_mode_cannot_write_sandbox(tmp_path):
    _write_config(tmp_path, enabled=True)
    r = run_public_knowledge_velocity(root=tmp_path, run_mode="daily",
                                      posts_override=_sample_posts())
    # DAILY may not write the sandbox namespace; orchestrator catches the
    # RunModeViolation and degrades rather than crashing.
    assert r["wrote_files"] is False
    assert any("write_skipped" in w for w in r["warnings"])
    disc = tmp_path / "outputs" / "sandbox" / "discovery"
    assert not (disc / "crowd_knowledge_state.json").exists()


def test_fetch_fn_injection(tmp_path):
    _write_config(tmp_path, enabled=True)

    def fake_fetch(subreddits, limit_per_sub=200, **kw):
        return FetchResult(SourceStatus.OK, _sample_posts(), [])

    r = run_public_knowledge_velocity(root=tmp_path, run_mode="discovery",
                                      fetch_fn=fake_fetch)
    assert r["status"] == SourceStatus.OK.value
    assert r["post_count"] == 14


def test_summary_md_has_sandbox_disclaimer(tmp_path):
    _write_config(tmp_path, enabled=True)
    run_public_knowledge_velocity(root=tmp_path, run_mode="discovery",
                                  posts_override=_sample_posts())
    md = (tmp_path / "outputs" / "sandbox" / "discovery" / "crowd_radar_summary.md").read_text()
    assert "Not a trade recommendation" in md
    assert "cannot trigger any trade" in md


def test_source_compliance_artifact_shape(tmp_path):
    _write_config(tmp_path, enabled=True)
    run_public_knowledge_velocity(root=tmp_path, run_mode="discovery",
                                  posts_override=_sample_posts())
    comp = json.loads((tmp_path / "outputs" / "sandbox" / "discovery"
                       / "social_source_compliance.json").read_text())
    assert comp["records"], "compliance must list governed sources"
    reddit = next(r for r in comp["records"] if r["source_name"] == "reddit")
    for field in ("collection_method", "allowed_fields", "rate_limit",
                  "raw_text_storage_allowed", "ai_processing_allowed",
                  "terms_review_date", "compliance_status"):
        assert field in reddit
    # Conservative defaults: no raw-text storage.
    assert reddit["raw_text_storage_allowed"] is False
