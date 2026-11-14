"""FMP4-R2 independent QA: all current-advice surfaces refuse paid-derived reads."""
from datetime import datetime, timezone
from pathlib import Path
import pytest

from portfolio_automation.data_budget import fmp_cutover as fc

POST = datetime(2026, 11, 27, 1, tzinfo=timezone.utc)


@pytest.fixture
def held(monkeypatch):
    monkeypatch.setattr(fc, "_system_clock", lambda: POST)


def _no_old_read(path):
    raise AssertionError(f"Unlicensed outputs/latest read during HOLD: {path}")


def test_status_does_not_open_previously_paid_plan_or_other_latest(held, monkeypatch, tmp_path):
    from tools import status
    monkeypatch.setattr(status, "_read_json", _no_old_read)
    report = status.collect_status(tmp_path)
    assert report.overall_severity == "FAIL"
    assert len(report.checks) == 1
    assert report.checks[0].name == "fmp_cutover_hold"
    assert "DATA_UNAVAILABLE" in report.checks[0].message


def test_today_does_not_open_stale_artifacts(held, monkeypatch, tmp_path):
    from gui_v2.data import dash_today
    monkeypatch.setattr(dash_today, "_read_json", _no_old_read)
    result = dash_today.collect_today_view(tmp_path)
    assert result["fmp_cutover_hold"]["is_current"] is False
    assert result["cards"][0]["label"] == "FMP_CUTOVER_HOLD"


def test_portfolio_does_not_open_stale_decision_plan(held, monkeypatch, tmp_path):
    from gui_v2.data import dash_portfolio
    monkeypatch.setattr(dash_portfolio, "_read_json", _no_old_read)
    result = dash_portfolio.collect_portfolio_view(tmp_path)
    assert result["decisions"] == []
    assert result["fmp_cutover_hold"]["is_current"] is False
    assert result["cards"][0]["label"] == "FMP_CUTOVER_HOLD"


def test_quant_does_not_open_stale_research(held, monkeypatch, tmp_path):
    from gui_v2.data import dash_quant
    monkeypatch.setattr(dash_quant, "_read_json", _no_old_read)
    result = dash_quant.collect_quant_view(tmp_path)
    assert result["efficacy_rows"] == []
    assert result["fmp_cutover_hold"]["is_current"] is False
    assert result["cards"][0]["label"] == "FMP_CUTOVER_HOLD"


@pytest.mark.parametrize("route", ["/dashboard/today", "/dashboard/portfolio", "/dashboard/quant"])
def test_dashboard_routes_show_hold_not_stale_plan(held, monkeypatch, tmp_path, route):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from gui_v2 import app as app_mod
    monkeypatch.setattr(app_mod, "REPO_ROOT", tmp_path)
    response = TestClient(app_mod.app).get(route)
    assert response.status_code == 200
    assert "FMP_CUTOVER_HOLD" in response.text or "Fmp Cutover Hold" in response.text
