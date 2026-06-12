"""Tests for cross-run mention-history persistence + market-context join."""
from __future__ import annotations

import json
from pathlib import Path

from portfolio_automation.social_intelligence.context_join import (
    build_market_context,
    load_mention_history,
    update_mention_history,
)


def test_load_mention_history_missing_is_empty(tmp_path):
    assert load_mention_history(tmp_path) == {}


def test_update_history_appends_and_trims():
    prior = {"NVDA": [1, 2, 3], "GME": [5]}
    updated = update_mention_history(prior, {"NVDA": 4, "AMC": 7}, window=3)
    assert updated["NVDA"] == [2, 3, 4]          # trimmed to window
    assert updated["GME"] == [5, 0]              # quiet ticker decays with a 0
    assert updated["AMC"] == [7]                 # new ticker


def test_history_roundtrip(tmp_path):
    from portfolio_automation.social_intelligence.context_join import (
        MENTION_HISTORY_REL, build_history_payload,
    )
    disc = tmp_path / "outputs" / "sandbox" / "discovery"
    disc.mkdir(parents=True)
    payload = build_history_payload({"NVDA": [1, 2, 3]}, window=20, created_at="t")
    (tmp_path / "outputs" / "sandbox" / MENTION_HISTORY_REL).write_text(json.dumps(payload))
    assert load_mention_history(tmp_path)["NVDA"] == [1, 2, 3]


def test_market_context_from_news_and_signals(tmp_path):
    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    (latest / "news_intelligence.json").write_text(json.dumps({
        "evidence_packets": [
            {"entity_key": "AMD", "related_tickers": ["AMD", "NVDA"]},
        ]}))
    (latest / "watchlist_signals.json").write_text(json.dumps({
        "results": [
            {"ticker": "TSLA", "price_change_pct": 6.4, "volume_spike": True},
        ]}))
    ctx = build_market_context(tmp_path)
    assert ctx["AMD"]["external_news_match"] is True
    assert ctx["NVDA"]["external_news_match"] is True   # related ticker
    assert ctx["TSLA"]["price_move_before_social_spike"] == 6.4
    assert ctx["TSLA"]["volume_confirmation"] is True
    # No free artifact carries short interest → always None.
    assert ctx["TSLA"]["options_or_short_interest_context"] is None


def test_market_context_missing_artifacts_is_empty(tmp_path):
    assert build_market_context(tmp_path) == {}
