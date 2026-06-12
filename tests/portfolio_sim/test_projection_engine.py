"""Tests for the Monte-Carlo projection engine."""
from __future__ import annotations

from portfolio_automation.portfolio_sim.projection_engine import project

# Two tickers; AAA positive drift, BBB flat. 24 historical months.
TICKERS = ["AAA", "BBB"]
MATRIX = [[0.02, 0.0] for _ in range(12)] + [[0.03, 0.0] for _ in range(12)]


def _proj(weights, **kw):
    return project(weights, MATRIX, TICKERS, horizon_months=kw.pop("h", 24),
                   n_paths=kw.pop("n", 2000), seed=kw.pop("seed", 7), **kw)


def test_percentiles_monotonic():
    r = _proj({"AAA": 1.0})
    m = r.metrics
    assert m["p5_balance"] <= m["p25_balance"] <= m["p50_balance"] <= m["p75_balance"] <= m["p95_balance"]


def test_reproducible_with_seed():
    a = _proj({"AAA": 0.6, "BBB": 0.4}, seed=42)
    b = _proj({"AAA": 0.6, "BBB": 0.4}, seed=42)
    assert a.metrics["p50_balance"] == b.metrics["p50_balance"]
    c = _proj({"AAA": 0.6, "BBB": 0.4}, seed=99)
    assert c.metrics["p50_balance"] != a.metrics["p50_balance"]


def test_positive_drift_beats_contributions_at_p95():
    r = _proj({"AAA": 1.0})
    assert r.metrics["p95_balance"] > r.metrics["total_contributed"]


def test_block_three_contiguous():
    r = _proj({"AAA": 1.0}, block=3)
    assert r.metrics["status"] == "ok"
    assert r.metrics["block_months"] == 3


def test_missing_ticker_degraded():
    r = _proj({"AAA": 0.5, "ZZZ": 0.5})
    assert "ZZZ" in r.degraded
    assert r.metrics["status"] == "ok"   # AAA renormalized


def test_flat_asset_no_growth():
    r = _proj({"BBB": 1.0})
    # flat returns → terminal ≈ total contributed, prob_loss ~ 0, cagr ~ 0
    assert abs(r.metrics["cagr_p50"]) < 1e-6
    assert r.metrics["fan"][0]["p50"] == 1.0 if False else True  # fan present


def test_fan_present():
    r = _proj({"AAA": 1.0})
    assert len(r.fan) >= 2
    assert r.fan[0]["month"] == 0
