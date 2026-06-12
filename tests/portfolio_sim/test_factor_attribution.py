"""Tests for factor data loader + attribution regression."""
from __future__ import annotations

import csv
from pathlib import Path

from portfolio_automation.portfolio_sim.factor_attribution import attribute, build_factor_report
from portfolio_automation.portfolio_sim.factor_data import available_factors, load_factors


def _write_factors(root, months):
    d = root / "data" / "factors"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "ff_monthly.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["month", "Mkt-RF", "SMB", "HML", "RMW", "CMA", "MOM", "RF"])
        for m, mkt in months:
            w.writerow([m, mkt * 100, 0.0, 0.0, 0.0, 0.0, 0.0, 0.1])  # percent form


def test_load_factors_absent_is_empty(tmp_path):
    assert load_factors(tmp_path) == {}


def test_load_and_normalize(tmp_path):
    _write_factors(tmp_path, [("2025-01", 0.02), ("2025-02", -0.01)])
    f = load_factors(tmp_path)
    assert abs(f["2025-01"]["Mkt-RF"] - 0.02) < 1e-9   # percent → decimal
    assert "Mkt-RF" in available_factors(f)


def test_attribute_market_beta_one(tmp_path):
    # tactic return ≈ Mkt-RF + RF each month → beta ~1, alpha ~0
    months = [(f"2024-{m:02d}", 0.01 * ((-1) ** m)) for m in range(1, 13)] + \
             [(f"2025-{m:02d}", 0.015 * ((-1) ** m)) for m in range(1, 13)]
    _write_factors(tmp_path, months)
    factors = load_factors(tmp_path)
    tactic = {m: factors[m]["Mkt-RF"] + factors[m]["RF"] for m in factors}
    res = attribute(tactic, factors)
    assert res["status"] == "ok"
    assert abs(res["betas"]["Mkt-RF"] - 1.0) < 0.05
    assert abs(res["alpha_monthly"]) < 0.005


def test_attribute_degrades_without_factors():
    assert attribute({"2025-01": 0.01}, {})["status"] == "factor_data_unavailable"


def test_build_report_degraded_flag():
    rep = build_factor_report({}, run_id="r", run_mode="discovery", factors_available=False)
    assert rep["factor_data_available"] is False
    assert "factor_data_unavailable" in rep["warnings"]
