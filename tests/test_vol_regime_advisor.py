"""Tests for portfolio_automation/vol_regime_advisor.py."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portfolio_automation.vol_regime_advisor import (
    _MIN_OBSERVATIONS,
    _REALIZED_VOL_WINDOW_DAYS,
    build_plan,
    classify_regime,
    realized_vol_annualised,
    run_vol_regime_advisor,
)


# ---------------------------------------------------------------------------
# realized_vol_annualised
# ---------------------------------------------------------------------------


def test_vol_zero_when_returns_constant():
    rets = [0.001] * 30
    sigma = realized_vol_annualised(rets)
    assert sigma == 0.0


def test_vol_returns_none_with_too_few_observations():
    rets = [0.01, -0.01]
    assert realized_vol_annualised(rets) is None


def test_vol_annualises_with_sqrt_252():
    # Returns alternate +1%/-1% — daily stdev = 0.01
    rets = [0.01 if i % 2 == 0 else -0.01 for i in range(30)]
    sigma = realized_vol_annualised(rets)
    expected = 0.01 * math.sqrt(252)
    assert sigma == pytest.approx(expected, rel=1e-3)


# ---------------------------------------------------------------------------
# classify_regime
# ---------------------------------------------------------------------------


def test_classify_calm():
    r = classify_regime(0.08)
    assert r["regime"] == "calm"
    assert r["sizing_multiplier"] == 1.10


def test_classify_normal():
    r = classify_regime(0.15)
    assert r["regime"] == "normal"
    assert r["sizing_multiplier"] == 1.00


def test_classify_elevated():
    r = classify_regime(0.22)
    assert r["regime"] == "elevated"
    assert r["sizing_multiplier"] == 0.75


def test_classify_risk_off():
    r = classify_regime(0.35)
    assert r["regime"] == "risk_off"
    assert r["sizing_multiplier"] == 0.50


def test_classify_crisis():
    r = classify_regime(0.60)
    assert r["regime"] == "crisis"
    assert r["sizing_multiplier"] == 0.25


def test_classify_unknown_when_none():
    r = classify_regime(None)
    assert r["regime"] == "unknown"
    assert r["sizing_multiplier"] == 1.00


# ---------------------------------------------------------------------------
# build_plan
# ---------------------------------------------------------------------------


def test_plan_envelope_observe_only():
    plan = build_plan(
        benchmark="SPY", sigma_annual=0.15, observations=20,
        status="ok", notes=[],
    )
    assert plan["observe_only"] is True
    assert plan["schema_version"] == "1"
    assert plan["regime"] == "normal"
    assert plan["sizing_multiplier_suggested"] == 1.00
    assert "advisory" in plan["advisory_disclaimer"].lower()


def test_plan_insufficient_data_shape():
    plan = build_plan(
        benchmark="SPY", sigma_annual=None, observations=0,
        status="insufficient_data", notes=["test reason"],
    )
    assert plan["regime"] == "unknown"
    assert plan["sigma_annual"] is None
    assert plan["status"] == "insufficient_data"


# ---------------------------------------------------------------------------
# run_vol_regime_advisor — integration
# ---------------------------------------------------------------------------


def test_run_without_fmp_reports_insufficient_data(tmp_path):
    plan = run_vol_regime_advisor(
        tmp_path, fmp_client=None, base_dir=tmp_path / "outputs",
    )
    assert plan["status"] == "insufficient_data"
    out_json = tmp_path / "outputs" / "latest" / "vol_regime_advisor.json"
    assert out_json.exists()
    assert json.loads(out_json.read_text("utf-8"))["observe_only"] is True


def test_run_with_stub_fmp_low_vol(tmp_path):
    # Build flat series → near-zero daily returns → calm regime
    series = [
        {"date": f"2026-01-{i:02d}", "close": 100.0 + 0.0001 * i,
         "adjClose": 100.0 + 0.0001 * i}
        for i in range(1, 30)
    ]
    series.reverse()  # newest-first

    class StubFMP:
        def get_historical_prices(self, symbol, *, years=1, ttl_days=1):
            return series

    plan = run_vol_regime_advisor(
        tmp_path, fmp_client=StubFMP(), base_dir=tmp_path / "outputs",
    )
    assert plan["status"] == "ok"
    assert plan["regime"] == "calm"


def test_run_with_stub_fmp_high_vol(tmp_path):
    # Alternating ±3% moves → annualised σ ~47%
    closes = []
    p = 100.0
    for i in range(1, 30):
        p *= 1.03 if i % 2 == 0 else 0.97
        closes.append({"date": f"2026-01-{i:02d}", "close": p, "adjClose": p})
    closes.reverse()

    class StubFMP:
        def get_historical_prices(self, symbol, *, years=1, ttl_days=1):
            return closes

    plan = run_vol_regime_advisor(
        tmp_path, fmp_client=StubFMP(), base_dir=tmp_path / "outputs",
    )
    assert plan["status"] == "ok"
    # ~47% annualised vol → crisis regime
    assert plan["regime"] == "crisis"
    assert plan["sizing_multiplier_suggested"] == 0.25


def test_fmp_failure_is_non_fatal(tmp_path):
    class BrokenFMP:
        def get_historical_prices(self, symbol, *, years=1, ttl_days=1):
            raise RuntimeError("simulated")

    plan = run_vol_regime_advisor(
        tmp_path, fmp_client=BrokenFMP(), base_dir=tmp_path / "outputs",
    )
    assert plan["status"] == "insufficient_data"
    assert plan["observe_only"] is True


def test_observe_only_hardcoded(tmp_path):
    run_vol_regime_advisor(tmp_path, fmp_client=None,
                           base_dir=tmp_path / "outputs")
    payload = json.loads(
        (tmp_path / "outputs" / "latest" / "vol_regime_advisor.json")
        .read_text("utf-8")
    )
    assert payload["observe_only"] is True
