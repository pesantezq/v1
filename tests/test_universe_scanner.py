"""Phase 5 — broad-market universe scanner (sandbox-only)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import portfolio_automation.universe_scanner as us
from portfolio_automation.next_stage.contracts import CandidateType, OpportunityStatus as S


def _now():
    return datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)


def _seed(tmp_path: Path):
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "universe_lists.yaml").write_text(
        "broad_market_etfs: [SPY, QQQ]\n"
        "sector_etfs: [XLK, XLE]\n"
        "commodity_proxies: [GLD, URA]\n"
        "theme_baskets:\n"
        "  AI Infrastructure: [NVDA, AMD]\n"
        "  Space Economy: [RKLB]\n"
        "private_ipo_watch:\n"
        "  - name: SpaceX\n    theme: Space Economy\n    access_route: proxy\n    proxies: [RKLB]\n"
        "user_themes: []\n")
    (tmp_path / "config.json").write_text(json.dumps(
        {"watchlist_scanner": {"watchlist": ["AAPL", "MSFT"]}}))
    (tmp_path / "outputs" / "latest").mkdir(parents=True, exist_ok=True)


def test_builds_typed_candidates(tmp_path):
    _seed(tmp_path)
    built = us.build_universe_candidates(tmp_path)
    types = {c["candidate_type"] for c in built["public"]}
    assert CandidateType.ETF.value in types
    assert CandidateType.COMMODITY_PROXY.value in types
    assert built["themes"] and built["themes"][0]["candidate_type"] == CandidateType.THEME_BASKET.value
    assert built["private"] and built["private"][0]["candidate_type"] == CandidateType.PRIVATE_IPO.value


def test_private_items_have_no_tradeable_fields(tmp_path):
    _seed(tmp_path)
    us.write_universe_artifacts(tmp_path, _now())
    priv = json.loads((tmp_path / "outputs" / "sandbox" / "private_ipo_watchlist.json").read_text())
    assert priv["items"]
    for it in priv["items"]:
        assert it["candidate_type"] == CandidateType.PRIVATE_IPO.value
        # never a tradeable price/quantity/shares field
        for forbidden in ("price", "quantity", "shares", "market_value"):
            assert forbidden not in it


def test_writes_sandbox_only_never_decision_plan(tmp_path):
    _seed(tmp_path)
    us.write_universe_artifacts(tmp_path, _now())
    sb = tmp_path / "outputs" / "sandbox"
    for fn in ("universe_scan_candidates.json", "opportunity_radar.json",
               "theme_candidates.json", "private_ipo_watchlist.json"):
        assert (sb / fn).exists(), fn
    # the official source of truth must never be written by the scanner
    assert not (tmp_path / "outputs" / "latest" / "decision_plan.json").exists()


def test_radar_scored_and_private_never_qualified(tmp_path):
    _seed(tmp_path)
    us.write_universe_artifacts(tmp_path, _now())
    radar = json.loads((tmp_path / "outputs" / "sandbox" / "opportunity_radar.json").read_text())
    assert radar["opportunity_count"] >= 1
    for opp in radar["opportunities"]:
        if opp["candidate_type"] == CandidateType.PRIVATE_IPO.value:
            assert opp["final_status"] in (S.PRIVATE_WATCH_ONLY.value, S.ACCESS_LIMITED.value)


def test_degrades_with_no_config(tmp_path):
    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    res = us.write_universe_artifacts(tmp_path, _now())
    # no lists → empty but valid artifacts, no crash
    radar = json.loads((tmp_path / "outputs" / "sandbox" / "opportunity_radar.json").read_text())
    assert radar["observe_only"] is True
    assert isinstance(res["candidate_count"], int)
