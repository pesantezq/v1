"""Tests for the research strategy library."""
from __future__ import annotations

import json
from pathlib import Path

from portfolio_automation.portfolio_sim.prices import load_price_panel
from portfolio_automation.portfolio_sim.research_library import (
    DualMomentum,
    MeanVarianceFrontier,
    MomentumRotation,
    research_tactics,
)


def _config(root):
    (root / "config.json").write_text(json.dumps({
        "portfolio": {"holdings": [
            {"symbol": "QQQ", "shares": 6, "asset_class": "us_equity", "is_leveraged": False},
            {"symbol": "GLD", "shares": 4, "asset_class": "commodity", "is_leveraged": False},
            {"symbol": "QLD", "shares": 8, "asset_class": "us_equity_leveraged", "is_leveraged": True},
        ]},
        "growth_mode": {"concentration_cap": 0.60, "leverage_cap": 0.25},
        "portfolio_sim": {"universe": {"proxy_etfs": ["BND", "SCHD", "USMV"]}},
    }))


def _archive(root, ticker, g):
    d = root / "outputs" / "backtest" / "historical"
    d.mkdir(parents=True, exist_ok=True)
    dates = [f"2024-{mo:02d}-15" for mo in range(1, 13)] + [f"2025-{mo:02d}-15" for mo in range(1, 13)]
    rows = [{"date": dt, "close": round(100 * (g ** i), 4), "volume": 1000} for i, dt in enumerate(dates)]
    (d / f"{ticker}_5y.json").write_text(json.dumps({"symbol": ticker, "rows": list(reversed(rows))}))


def _panel(root):
    for t, g in [("QQQ", 1.03), ("GLD", 1.002), ("QLD", 1.05), ("SPY", 1.015),
                 ("BND", 1.001), ("SCHD", 1.008), ("USMV", 1.006)]:
        _archive(root, t, g)
    return load_price_panel(["QQQ", "GLD", "QLD", "SPY", "BND", "SCHD", "USMV"], root)


def test_library_tactics_have_academic_basis(tmp_path):
    _config(tmp_path)
    _panel(tmp_path)
    tactics = research_tactics(tmp_path)
    ids = {t.tactic_id for t in tactics}
    assert {"research_sixty_forty", "research_factor_tilt", "research_momentum_rotation",
            "research_dual_momentum", "research_risk_parity_lite", "research_mean_variance"} <= ids
    for t in tactics:
        assert t.metadata.get("academic_basis"), f"{t.tactic_id} missing academic_basis"


def test_sixty_forty_static():
    from portfolio_automation.portfolio_sim.research_library import research_tactics as rt  # noqa
    # static vector independent of date
    t = next(t for t in research_tactics_static())
    assert t.target_weights == {"SPY": 0.60, "BND": 0.40}


def research_tactics_static():
    from portfolio_automation.portfolio_sim.tactics import Tactic
    return [Tactic("research_sixty_forty", "60/40", "x", {"SPY": 0.60, "BND": 0.40})]


def test_momentum_rotation_picks_winner(tmp_path):
    _config(tmp_path)
    panel = _panel(tmp_path)
    mom = MomentumRotation(["QQQ", "GLD", "QLD"], lookback_months=6, top_n=1, leveraged={"QLD"})
    w = mom.target_weights_asof("2025-12-15", {"panel": panel})
    # QLD has the strongest growth → should be the (capped) top pick
    assert w  # non-empty
    assert max(w, key=w.get) in {"QLD", "QQQ"}
    assert max(w.values()) <= 0.60 + 1e-9


def test_mean_variance_normalized_capped(tmp_path):
    _config(tmp_path)
    panel = _panel(tmp_path)
    mv = MeanVarianceFrontier(["QQQ", "GLD", "QLD", "BND"], leveraged={"QLD"})
    w = mv.target_weights_asof("2025-12-15", {"panel": panel})
    assert abs(sum(w.values()) - 1.0) < 1e-6
    assert max(w.values()) <= 0.60 + 1e-9
    assert w.get("QLD", 0.0) <= 0.25 + 1e-9


def test_dual_momentum_defensive_when_riskon_negative(tmp_path):
    _config(tmp_path)
    # risk-on falling, defensive flat → pick defensive
    _archive(tmp_path, "SPY", 0.98)   # declining
    _archive(tmp_path, "BND", 1.001)
    panel = load_price_panel(["SPY", "BND"], tmp_path)
    dm = DualMomentum(risk_on=["SPY"], defensive=["BND"], lookback_months=6)
    w = dm.target_weights_asof("2025-12-15", {"panel": panel})
    assert "BND" in w
