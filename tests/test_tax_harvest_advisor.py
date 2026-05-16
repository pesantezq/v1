"""Tests for portfolio_automation/tax_harvest_advisor.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portfolio_automation.tax_harvest_advisor import (
    _MATERIAL_LOSS_PCT,
    _MIN_LOSS_DOLLARS,
    build_plan,
    evaluate_position,
    run_tax_harvest_advisor,
)


# ---------------------------------------------------------------------------
# evaluate_position
# ---------------------------------------------------------------------------


def test_evaluate_no_position():
    r = evaluate_position(symbol="X", shares=0, cost_basis=100,
                          current_price=80)
    assert r["status"] == "no_position"
    assert r["harvest_recommended"] is False


def test_evaluate_missing_cost_basis():
    r = evaluate_position(symbol="X", shares=10, cost_basis=None,
                          current_price=80)
    assert r["status"] == "missing_cost_basis"
    assert r["harvest_recommended"] is False


def test_evaluate_missing_price():
    r = evaluate_position(symbol="X", shares=10, cost_basis=100,
                          current_price=None)
    assert r["status"] == "missing_price"
    assert r["harvest_recommended"] is False


def test_evaluate_no_loss_when_at_or_above_basis():
    r = evaluate_position(symbol="X", shares=10, cost_basis=100,
                          current_price=110)
    assert r["status"] == "no_loss"
    assert r["harvest_recommended"] is False


def test_evaluate_sub_minimum_loss_not_recommended():
    # $1 loss × 10 shares = $10 — below $25 threshold
    r = evaluate_position(symbol="X", shares=10, cost_basis=100,
                          current_price=99)
    assert r["status"] == "sub_minimum"
    assert r["harvest_recommended"] is False


def test_evaluate_material_loss_flagged():
    # 10% loss × 10 shares × $100 = $100 loss
    r = evaluate_position(symbol="X", shares=10, cost_basis=100,
                          current_price=90)
    assert r["status"] == "ok"
    assert r["harvest_recommended"] is True
    assert r["loss_dollars"] == 100.0
    assert r["loss_pct"] == 0.10
    assert any("material loss" in n for n in r["notes"])
    assert any("wash-sale" in n for n in r["notes"])


def test_evaluate_with_replacement_candidate():
    r = evaluate_position(
        symbol="QQQ", shares=10, cost_basis=400, current_price=360,
        replacement_map={"QQQ": ["VGT", "XLK"]},
    )
    assert r["harvest_recommended"] is True
    assert r["replacement_candidates"] == ["VGT", "XLK"]


def test_evaluate_small_loss_above_threshold_no_material_flag():
    # 4% loss × 25 shares × $100 = $100 loss (≥ $25 threshold, but < 5%)
    r = evaluate_position(symbol="X", shares=25, cost_basis=100,
                          current_price=96)
    assert r["harvest_recommended"] is True
    assert not any("material loss" in n for n in r["notes"])


# ---------------------------------------------------------------------------
# build_plan
# ---------------------------------------------------------------------------


def test_plan_non_taxable_skips():
    plan = build_plan(is_taxable=False, rows=[], notes=[])
    assert plan["observe_only"] is True
    assert plan["is_taxable_account"] is False
    assert "skipped" in plan["summary_line"]
    assert plan["harvestable_count"] == 0


def test_plan_taxable_with_harvestable():
    rows = [
        {"symbol": "A", "harvest_recommended": True, "loss_dollars": 100.0},
        {"symbol": "B", "harvest_recommended": False, "loss_dollars": 0.0},
        {"symbol": "C", "harvest_recommended": True, "loss_dollars": 250.0},
    ]
    plan = build_plan(is_taxable=True, rows=rows, notes=[])
    assert plan["harvestable_count"] == 2
    assert plan["total_harvestable_loss_dollars"] == 350.0
    assert "advisory" in plan["advisory_disclaimer"].lower()


# ---------------------------------------------------------------------------
# run_tax_harvest_advisor — integration
# ---------------------------------------------------------------------------


def _write_config(path: Path, holdings: list[dict], is_taxable: bool = True) -> None:
    path.write_text(
        json.dumps({
            "portfolio": {
                "holdings": holdings,
                "is_taxable_account": is_taxable,
            }
        }, indent=2),
        encoding="utf-8",
    )


def test_run_skips_when_not_taxable(tmp_path):
    _write_config(
        tmp_path / "config.json",
        [{"symbol": "QQQ", "shares": 10, "cost_basis": 400}],
        is_taxable=False,
    )
    plan = run_tax_harvest_advisor(
        tmp_path, fmp_client=None, base_dir=tmp_path / "outputs",
    )
    assert plan["is_taxable_account"] is False
    assert plan["positions"] == []
    assert (tmp_path / "outputs" / "latest" / "tax_harvest_advisor.json").exists()


def test_run_with_price_overrides(tmp_path):
    _write_config(
        tmp_path / "config.json",
        [
            {"symbol": "QQQ", "shares": 10, "cost_basis": 400},
            {"symbol": "GLD", "shares": 5, "cost_basis": 200},
        ],
    )
    plan = run_tax_harvest_advisor(
        tmp_path,
        fmp_client=None,
        price_overrides={"QQQ": 360.0, "GLD": 210.0},
        replacement_map={"QQQ": ["VGT"]},
        base_dir=tmp_path / "outputs",
    )
    qqq = [r for r in plan["positions"] if r["symbol"] == "QQQ"][0]
    gld = [r for r in plan["positions"] if r["symbol"] == "GLD"][0]
    assert qqq["harvest_recommended"] is True
    assert qqq["replacement_candidates"] == ["VGT"]
    assert gld["status"] == "no_loss"
    assert plan["harvestable_count"] == 1


def test_run_with_stub_fmp(tmp_path):
    _write_config(
        tmp_path / "config.json",
        [{"symbol": "QQQ", "shares": 10, "cost_basis": 400}],
    )

    class StubFMP:
        def get_historical_prices(self, symbol, *, years=1, ttl_days=1):
            return [{"date": "2026-05-15", "close": 360.0, "adjClose": 360.0}]

    plan = run_tax_harvest_advisor(
        tmp_path, fmp_client=StubFMP(), base_dir=tmp_path / "outputs",
    )
    assert plan["positions"][0]["harvest_recommended"] is True
    assert plan["positions"][0]["current_price"] == 360.0


def test_run_missing_cost_basis_safe(tmp_path):
    _write_config(
        tmp_path / "config.json",
        [{"symbol": "QQQ", "shares": 10}],  # no cost_basis
    )
    plan = run_tax_harvest_advisor(
        tmp_path, fmp_client=None, price_overrides={"QQQ": 360.0},
        base_dir=tmp_path / "outputs",
    )
    assert plan["positions"][0]["status"] == "missing_cost_basis"


def test_observe_only_hardcoded(tmp_path):
    _write_config(tmp_path / "config.json",
                  [{"symbol": "X", "shares": 1, "cost_basis": 100}])
    run_tax_harvest_advisor(tmp_path, fmp_client=None,
                            base_dir=tmp_path / "outputs")
    payload = json.loads(
        (tmp_path / "outputs" / "latest" / "tax_harvest_advisor.json")
        .read_text("utf-8")
    )
    assert payload["observe_only"] is True
