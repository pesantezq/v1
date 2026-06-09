"""Phase 7 — sandbox shadow tracking + shadow portfolios (sandbox-only, no real positions)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import portfolio_automation.sandbox.shadow_tracker as sh
from portfolio_automation.next_stage.contracts import BOOM_BUCKET_TOTAL_CAP


def _now():
    return datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)


def _seed(tmp_path: Path):
    L = tmp_path / "outputs" / "latest"; L.mkdir(parents=True)
    sb = tmp_path / "outputs" / "sandbox"; sb.mkdir(parents=True)
    (tmp_path / "config.json").write_text(json.dumps({"portfolio": {"holdings": [
        {"symbol": "QQQ", "shares": 10, "target_weight": 0.5},
        {"symbol": "GLD", "shares": 5, "target_weight": 0.3},
        {"symbol": "VXUS", "shares": 5, "target_weight": 0.2}]}}))
    sb.joinpath("opportunity_radar.json").write_text(json.dumps({"opportunities": [
        {"candidate": "AMD", "candidate_type": "public_ticker", "theme": "AI Infrastructure",
         "final_status": "QUALIFIED", "opportunity_score": 0.62, "boom_score": 0.55,
         "risk_score": 0.3, "investability_score": 0.8, "evidence_score": 0.6,
         "catalyst_strength": 0.7, "portfolio_fit_score": 0.5},
        {"candidate": "SpaceX", "candidate_type": "private_ipo", "final_status": "PRIVATE_WATCH_ONLY",
         "opportunity_score": 0.5, "boom_score": 0.7, "risk_score": 0.4, "investability_score": 0.2}]}))


def test_shadow_portfolios_built_with_six_models(tmp_path):
    _seed(tmp_path)
    sh.write_shadow_artifacts(tmp_path, _now())
    sp = json.loads((tmp_path / "outputs" / "sandbox" / "shadow_portfolios.json").read_text())
    assert set(sp["portfolios"]) == {"actual_baseline", "target_allocation_baseline",
                                     "engine_followed", "lower_risk",
                                     "discovery_enhanced", "boom_bucket"}
    assert sp["observe_only"] is True


def test_boom_bucket_respects_speculative_cap(tmp_path):
    _seed(tmp_path)
    sh.write_shadow_artifacts(tmp_path, _now())
    sp = json.loads((tmp_path / "outputs" / "sandbox" / "shadow_portfolios.json").read_text())
    boom = sp["portfolios"]["boom_bucket"]["metrics"]
    assert boom["speculative_exposure"] <= BOOM_BUCKET_TOTAL_CAP + 1e-6
    assert boom["within_boom_cap"] is True


def test_private_candidate_excluded_from_boom_and_discovery_sleeves(tmp_path):
    _seed(tmp_path)
    sh.write_shadow_artifacts(tmp_path, _now())
    sp = json.loads((tmp_path / "outputs" / "sandbox" / "shadow_portfolios.json").read_text())
    for model in ("boom_bucket", "discovery_enhanced"):
        assert "SpaceX" not in sp["portfolios"][model]["weights"]


def test_promotion_review_only_qualified_and_action_gated(tmp_path):
    _seed(tmp_path)
    sh.write_shadow_artifacts(tmp_path, _now())
    rev = json.loads((tmp_path / "outputs" / "sandbox" / "candidate_promotion_review.json").read_text())
    assert rev["candidate_count"] == 1  # only AMD (QUALIFIED); private watch-only excluded
    c = rev["candidates"][0]
    assert "approve_to_watchlist_review" in c["allowed_actions"]
    assert "place_trade" in c["blocked_actions"]


def test_writes_sandbox_only(tmp_path):
    _seed(tmp_path)
    sh.write_shadow_artifacts(tmp_path, _now())
    assert not (tmp_path / "outputs" / "latest" / "decision_plan.json").exists()
    sb = tmp_path / "outputs" / "sandbox"
    for fn in ("shadow_opportunity_tracking.json", "shadow_portfolios.json",
               "candidate_promotion_review.json"):
        assert (sb / fn).exists()
    # shadow_tracker must NOT own strategy_comparison.json (§23.13)
    assert not (sb / "strategy_comparison.json").exists()


def test_degrades_without_radar(tmp_path):
    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    (tmp_path / "outputs" / "sandbox").mkdir(parents=True)
    (tmp_path / "config.json").write_text(json.dumps({"portfolio": {"holdings": []}}))
    res = sh.write_shadow_artifacts(tmp_path, _now())
    assert res["degraded"] is False  # empty radar is valid, not an error
    tr = json.loads((tmp_path / "outputs" / "sandbox" / "shadow_opportunity_tracking.json").read_text())
    assert tr["record_count"] == 0
