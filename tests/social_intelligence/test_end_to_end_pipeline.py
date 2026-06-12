"""Full-pipeline integration test for the Crowd Radar / Public Knowledge Velocity Layer.

Proves the *populated* path (not just disabled): multi-day runs build the mention
-history velocity baseline, the market-context join from existing artifacts unlocks
news/price-aware states, and the generated records flow through the downstream
consumers (daily memo + GUI loader). This is the "test the full pipeline generates
records before production" gate.
"""
from __future__ import annotations

import json
from pathlib import Path

from gui_v2.data.dash_crowd_radar import collect_crowd_radar_view
from portfolio_automation.social_intelligence.base import RawPost
from portfolio_automation.social_intelligence.public_knowledge_velocity import (
    run_public_knowledge_velocity,
)
from watchlist_scanner.daily_memo import _crowd_radar_section_lines


def _post(pid, ticker, kind, author):
    bodies = {
        "dd": (f"${ticker} valuation thesis", "DCF earnings guidance margin catalyst fundamentals"),
        "meme": (f"${ticker} to the moon rocket yolo", "diamond hands tendies squeeze moon"),
        "bear": (f"${ticker} overvalued puts", "crash short bear downside red"),
    }
    t, b = bodies[kind]
    return RawPost(post_id=pid, source="reddit", community="wsb", created_utc=0.0,
                   title=t, body=b, author_hash=author)


def _setup(root: Path):
    (root / "outputs" / "latest").mkdir(parents=True)
    (root / "config.json").write_text(json.dumps({
        "watchlist_scanner": {"watchlist": ["NVDA", "GME", "AMD", "TSLA", "PLTR", "BB"]},
        "crowd_radar": {"enabled": True, "min_mentions_for_state": 3, "mention_history_window": 20},
    }))
    (root / "outputs" / "latest" / "news_intelligence.json").write_text(json.dumps({
        "evidence_packets": [
            {"entity_key": "AMD", "related_tickers": ["AMD"]},
            {"entity_key": "TSLA", "related_tickers": ["TSLA"]},
            {"entity_key": "PLTR", "related_tickers": ["PLTR"]},
            {"entity_key": "BB", "related_tickers": ["BB"]},
        ]}))
    (root / "outputs" / "latest" / "watchlist_signals.json").write_text(json.dumps({
        "results": [{"ticker": "TSLA", "price_change_pct": 6.4, "volume_spike": True}]}))


def _run_baseline_then_spike(root: Path):
    """Three quiet days (with variance) then a busy day; returns the day-4 records."""
    counts = {"GME": [1, 3, 2], "NVDA": [1, 2, 3], "BB": [2, 1, 3]}
    for d in range(3):
        base = []
        base += [_post(f"g{d}{i}", "GME", "meme", f"gm{i}") for i in range(counts["GME"][d])]
        base += [_post(f"n{d}{i}", "NVDA", "dd", f"nv{i}") for i in range(counts["NVDA"][d])]
        base += [_post(f"b{d}{i}", "BB", "meme", f"bb{i}") for i in range(counts["BB"][d])]
        run_public_knowledge_velocity(root=root, run_mode="discovery", posts_override=base)

    today = []
    today += [_post(f"G{i}", "GME", "meme", f"gm{i % 4}") for i in range(20)]   # hype_acceleration
    today += [_post(f"N{i}", "NVDA", "dd", f"nvx{i % 2}") for i in range(8)]     # emerging_dd
    today += [_post(f"A{i}", "AMD", "dd", f"amx{i}") for i in range(8)]          # crowd_validation
    today += [_post(f"T{i}", "TSLA", "meme", f"tsx{i}") for i in range(5)]       # known_news_echo
    today += [_post(f"P{i}", "PLTR", "dd", f"plx{i}") for i in range(2)]         # contrarian_neglect
    today += [_post(f"Bm{i}", "BB", "meme", f"bbx{i}") for i in range(5)]        # crowd_exhaustion
    today += [_post(f"Bb{i}", "BB", "bear", f"bby{i}") for i in range(4)]
    run_public_knowledge_velocity(root=root, run_mode="discovery", posts_override=today)
    return json.loads((root / "outputs/sandbox/discovery/crowd_knowledge_state.json").read_text())


def test_full_pipeline_generates_varied_states(tmp_path):
    _setup(tmp_path)
    state = _run_baseline_then_spike(tmp_path)
    states = {r["crowd_state"] for r in state["records"]}
    # The 6 reachable-without-short-interest states should all appear.
    for expected in ("hype_acceleration", "emerging_dd", "crowd_validation",
                     "known_news_echo", "contrarian_neglect", "crowd_exhaustion"):
        assert expected in states, f"{expected} not generated; got {sorted(states)}"
    # Governance still holds on the populated path.
    forbidden = {"buy", "sell", "hold", "rebalance", "trim", "scale", "promote"}
    for r in state["records"]:
        assert r["recommended_next_step"] not in forbidden


def test_velocity_becomes_nonzero_after_history_builds(tmp_path):
    _setup(tmp_path)
    state = _run_baseline_then_spike(tmp_path)
    gme = next(r for r in state["records"] if r["ticker"] == "GME")
    # GME spiked 20 vs a varied ~2 baseline → large positive z (history persisted).
    assert gme["score_components"]["velocity_z"] > 2.0
    assert (tmp_path / "outputs/sandbox/discovery/crowd_mention_history.json").exists()


def test_first_run_has_no_velocity(tmp_path):
    """A brand-new layer (no history) cannot fabricate velocity — honest maturity."""
    _setup(tmp_path)
    posts = [_post(f"g{i}", "GME", "meme", f"a{i}") for i in range(10)]
    run_public_knowledge_velocity(root=tmp_path, run_mode="discovery", posts_override=posts)
    state = json.loads((tmp_path / "outputs/sandbox/discovery/crowd_knowledge_state.json").read_text())
    gme = next(r for r in state["records"] if r["ticker"] == "GME")
    assert gme["score_components"]["velocity_z"] == 0.0  # no baseline yet


def test_populated_records_flow_to_memo_and_gui(tmp_path):
    _setup(tmp_path)
    _run_baseline_then_spike(tmp_path)

    memo_lines = _crowd_radar_section_lines(tmp_path)
    joined = "\n".join(memo_lines)
    assert "Hype Acceleration Warning: GME" in joined
    assert "not a trade recommendation" in joined.lower()

    view = collect_crowd_radar_view(tmp_path)
    assert view["has_data"] is True
    assert view["source_status"] == "ok"
    section_keys = {s["key"] for s in view["sections"]}
    assert "hype_acceleration" in section_keys
    assert "crowd_validation" in section_keys
