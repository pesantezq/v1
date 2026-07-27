"""
Phase 7 tests — health assessment, run.py orchestrator (e2e with injected panel),
idempotent rerun, CLI, and pipeline wiring. Hermetic.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from portfolio_automation.portfolio_sim.prices import PricePanel
from portfolio_automation.weekly_etf_bundles import health as H
from portfolio_automation.weekly_etf_bundles import run as RUN

REPO_ROOT = Path(__file__).resolve().parents[1]


def _weekdays(start_iso, n):
    d = date.fromisoformat(start_iso); out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _panel(series_by_symbol, dates):
    closes = {s: {dates[i]: float(p) for i, p in enumerate(ps)} for s, ps in series_by_symbol.items()}
    return PricePanel(closes, {s: {d: 1.0 for d in dates} for s in series_by_symbol}, list(dates), [])


def _cfg_file(tmp_path):
    body = """
schema_version: 1
defaults:
  benchmark: SPY
bundles:
  - id: ai
    name: AI
    benchmark: QQQ
    members:
      - symbol: SMH
      - symbol: IGV
"""
    p = tmp_path / "wb.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def _full_panel(dates):
    return _panel({
        "SMH": [50 + 0.5 * i for i in range(len(dates))],
        "IGV": [80 + 0.2 * i for i in range(len(dates))],
        "QQQ": [100 + 0.3 * i for i in range(len(dates))],
        "SPY": [100 + 0.1 * i for i in range(len(dates))],
    }, dates)


# --------------------------------------------------------------------------- #
# health
# --------------------------------------------------------------------------- #
def _ok_payload():
    return {
        "status": "ok", "market_data_date": "2026-07-24", "feeds_decision_engine": False,
        "observe_only": True, "coverage": 1.0, "bundle_count": 1, "etf_count": 2,
        "stale_symbols": [], "failed_symbols": [],
        "bundles": [{"bundle_id": "ai", "pct_above_sma50": 1.0, "pct_above_sma200": 1.0,
                     "members": [{"symbol": "SMH"}]}],
        "ranking_global": [{"symbol": "SMH", "watch_score": 86, "expected_direction": "outperform"}],
    }


def test_health_green_when_clean():
    h = H.build_health(analysis_payload=_ok_payload(), config_valid=True, enabled_bundle_ids=["ai"])
    assert h["status"] == "GREEN"
    assert h["feeds_decision_engine"] is False


def test_health_red_on_feeds_decision_engine():
    p = _ok_payload(); p["feeds_decision_engine"] = True
    h = H.build_health(analysis_payload=p, config_valid=True, enabled_bundle_ids=["ai"])
    assert h["status"] == "RED"
    assert any("feeds_decision_engine" in r for r in h["reasons"])


def test_health_red_on_out_of_bounds_score():
    p = _ok_payload(); p["ranking_global"][0]["watch_score"] = 150
    h = H.build_health(analysis_payload=p, config_valid=True, enabled_bundle_ids=["ai"])
    assert h["status"] == "RED"


def test_health_red_on_disabled_bundle_present():
    h = H.build_health(analysis_payload=_ok_payload(), config_valid=True, enabled_bundle_ids=[])
    assert h["status"] == "RED"   # 'ai' present but not in enabled list


def test_health_red_on_auto_approved_promotion():
    sl = {"pending_promotion_candidates": [
        {"is_human_approved": True, "production_mutation": False,
         "feeds_decision_engine": False, "target_lane": "simulation"}]}
    h = H.build_health(analysis_payload=_ok_payload(), config_valid=True,
                       enabled_bundle_ids=["ai"], strat_lab_comparison=sl)
    assert h["status"] == "RED"


def test_health_amber_on_low_coverage():
    p = _ok_payload(); p["coverage"] = 0.5
    h = H.build_health(analysis_payload=p, config_valid=True, enabled_bundle_ids=["ai"])
    assert h["status"] == "AMBER"


def test_health_red_config_invalid():
    h = H.build_health(analysis_payload=None, config_valid=False)
    assert h["status"] == "RED"


# --------------------------------------------------------------------------- #
# run.py orchestrator
# --------------------------------------------------------------------------- #
def test_run_full_writes_artifacts_and_is_isolated(tmp_path):
    dates = _weekdays("2025-01-01", 300)
    res = RUN.run_weekly_etf_bundles(
        root=tmp_path, as_of=dates[-1], config_path=_cfg_file(tmp_path),
        mode="full", panel=_full_panel(dates), email_dry_run=True)
    assert res["status"] == "ok"
    base = tmp_path / "outputs" / "weekly_etf_bundles"
    for f in ("latest.json", "latest.md", "latest.html", "health.json",
              "scorecard.json", "calibration.json", "attribution.json"):
        assert (base / f).exists(), f
    # champion predictions frozen
    assert list((base / "predictions").glob("*.json"))
    # ISOLATION: never wrote decision_plan or the latest (daily) namespace
    assert not (tmp_path / "outputs" / "latest" / "decision_plan.json").exists()


def test_run_is_idempotent(tmp_path):
    dates = _weekdays("2025-01-01", 300)
    kwargs = dict(root=tmp_path, as_of=dates[-1], config_path=_cfg_file(tmp_path),
                  mode="full", panel=_full_panel(dates), email_dry_run=True)
    RUN.run_weekly_etf_bundles(**kwargs)
    res2 = RUN.run_weekly_etf_bundles(**kwargs)
    assert res2["steps"]["freeze"]["status"] == "idempotent_skip"


def test_run_config_invalid_fails_closed(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("schema_version: 99\nbundles: []\n", encoding="utf-8")
    res = RUN.run_weekly_etf_bundles(root=tmp_path, config_path=bad,
                                     panel=_full_panel(_weekdays("2025-01-01", 10)))
    assert res["status"] == "error"
    assert res["reason"] == "config_invalid"
    assert not (tmp_path / "outputs" / "weekly_etf_bundles" / "latest.json").exists()


def test_run_analysis_only_skips_predictions(tmp_path):
    dates = _weekdays("2025-01-01", 300)
    RUN.run_weekly_etf_bundles(root=tmp_path, as_of=dates[-1], config_path=_cfg_file(tmp_path),
                               mode="analysis-only", panel=_full_panel(dates))
    base = tmp_path / "outputs" / "weekly_etf_bundles"
    assert (base / "latest.json").exists()
    assert not (base / "predictions").exists()   # no freeze in analysis-only


# --------------------------------------------------------------------------- #
# wiring
# --------------------------------------------------------------------------- #
def test_wrapper_and_preflight_wired():
    wrapper = (REPO_ROOT / "scripts" / "run_weekly_etf_bundles.sh").read_text()
    assert "portfolio_automation.weekly_etf_bundles.run" in wrapper
    assert "flock" in wrapper                       # own lock
    preflight = (REPO_ROOT / "scripts" / "preflight.sh").read_text()
    assert "portfolio_automation/weekly_etf_bundles/run.py" in preflight   # py_compile
    assert "portfolio_automation.weekly_etf_bundles.run" in preflight      # smoke import
    assert "run_weekly_etf_bundles.sh" in preflight                        # bash -n
