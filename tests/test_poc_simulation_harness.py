"""
Tests for backtesting/poc_simulation_harness.py

Fully offline and deterministic (synthetic provider; no network, no API keys).
Covers a HEALTHY state (signals present, edge detectable) and a DEGRADED state
(no signals / no price data), per the repo's analysis+health coverage rule.
"""

from __future__ import annotations

import json

from backtesting.poc_simulation_harness import (
    SyntheticPriceProvider,
    generate_signals,
    run_poc,
)


# --------------------------------------------------------------------------
# Healthy state
# --------------------------------------------------------------------------

def test_healthy_payload_shape_and_flags():
    p = run_poc(n_signals=120, seed=42, write=False)
    assert p["observe_only"] is True
    assert p["advisory_only"] is True
    assert p["mode"] == "synthetic_offline"
    for key in ("performance", "calibration", "added_metrics", "params", "disclaimer"):
        assert key in p
    perf = p["performance"]
    assert perf["total_signals"] == 120
    assert perf["evaluated"] == 120          # synthetic data always resolves
    assert 0.0 <= perf["hit_rate"] <= 100.0
    am = p["added_metrics"]
    assert set(am) >= {"sharpe_like", "edge_vs_random_baseline_pct", "per_pattern"}
    assert am["per_pattern"], "expected a non-empty per-pattern breakdown"
    assert isinstance(p["calibration"]["calibration_slope"], float)


def test_embedded_edge_is_detected_vs_noise_control():
    """With edge, confidence should track outcome (positive slope); with the
    pure-noise control it should not. Deterministic for a fixed seed."""
    with_edge = run_poc(n_signals=200, seed=42, edge=0.7, write=False)
    noise = run_poc(n_signals=200, seed=42, edge=0.0, write=False)
    assert with_edge["calibration"]["calibration_slope"] > noise["calibration"]["calibration_slope"]
    assert with_edge["calibration"]["calibration_slope"] > 0


def test_writes_to_historical_namespace(tmp_path):
    run_poc(n_signals=50, seed=1, write=True, base_dir=str(tmp_path))
    out = tmp_path / "backtest" / "poc_simulation_results.json"
    assert out.exists(), "artifact should land in the HISTORICAL (backtest) namespace"
    data = json.loads(out.read_text())
    assert data["observe_only"] is True
    assert (tmp_path / "backtest" / "poc_simulation_results.md").exists()


# --------------------------------------------------------------------------
# Degraded states
# --------------------------------------------------------------------------

def test_degraded_no_signals_is_safe():
    p = run_poc(n_signals=0, seed=42, write=False)
    perf = p["performance"]
    assert perf["total_signals"] == 0
    assert perf["evaluated"] == 0
    assert perf["hit_rate"] == 0.0
    assert perf["results"] == []
    assert p["added_metrics"]["per_pattern"] == []


def test_degraded_no_price_data_is_safe():
    """If the provider returns no prices, nothing resolves but no crash."""
    class EmptyProvider(SyntheticPriceProvider):
        def get_historical_prices(self, symbol, years=5):
            return []

    from backtesting.fmp_backtester import FMPBacktester
    prov = EmptyProvider(seed=42)
    signals = generate_signals(prov, 30, 12, 42, 30)
    bt = FMPBacktester(prov)
    rep = bt.simulate_signal_performance(signals, forward_days=10)
    assert rep["evaluated"] == 0
    assert rep["results"] == []


def test_run_poc_includes_oos_window_when_provided():
    from backtesting.poc_simulation_harness import run_poc
    ow = {"calendar_days_observed": 38, "folds_possible": False}
    payload = run_poc(n_signals=12, n_symbols=4, seed=1, write=False, oos_window=ow)
    assert payload["oos_window"] == ow


def test_run_poc_omits_oos_window_by_default():
    from backtesting.poc_simulation_harness import run_poc
    payload = run_poc(n_signals=12, n_symbols=4, seed=1, write=False)
    assert "oos_window" not in payload
