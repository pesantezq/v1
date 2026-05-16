"""Tests for portfolio_automation/exit_advisor.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portfolio_automation.exit_advisor import (
    _DECISION_EXIT_FULL,
    _DECISION_EXIT_HALF,
    _DECISION_HOLD,
    _DECISION_TIGHTEN,
    build_plan,
    classify_strategy,
    evaluate_position,
    run_exit_advisor,
)


# ---------------------------------------------------------------------------
# classify_strategy
# ---------------------------------------------------------------------------


def test_classify_strategy_leveraged_is_momentum():
    assert classify_strategy({"symbol": "QLD", "is_leveraged": True}) == "momentum"


def test_classify_strategy_leveraged_asset_class_is_momentum():
    assert (
        classify_strategy({"symbol": "TQQQ", "asset_class": "us_equity_leveraged"})
        == "momentum"
    )


def test_classify_strategy_explicit_override():
    assert (
        classify_strategy({"symbol": "X", "strategy_type": "momentum"})
        == "momentum"
    )


def test_classify_strategy_default_is_compounder():
    assert classify_strategy({"symbol": "QQQ"}) == "compounder"


# ---------------------------------------------------------------------------
# evaluate_position — happy path & ladders
# ---------------------------------------------------------------------------


def test_evaluate_no_price_returns_insufficient_data():
    row = evaluate_position(
        symbol="X", strategy="compounder",
        current_price=None, peak_price=None,
    )
    assert row["status"] == "insufficient_data"
    assert row["decision"] == _DECISION_HOLD
    assert row["drawdown_from_peak"] is None


def test_evaluate_inside_envelope_holds():
    # 3% drawdown on a compounder — below 10% soft threshold
    row = evaluate_position(
        symbol="QQQ", strategy="compounder",
        current_price=97.0, peak_price=100.0,
    )
    assert row["decision"] == _DECISION_HOLD
    assert row["status"] == "ok"
    assert row["triggers"] == []


def test_evaluate_compounder_soft_dd_tightens():
    # 12% drawdown
    row = evaluate_position(
        symbol="QQQ", strategy="compounder",
        current_price=88.0, peak_price=100.0,
    )
    assert row["decision"] == _DECISION_TIGHTEN
    assert "drawdown_soft" in row["triggers"]


def test_evaluate_compounder_hard_dd_exits_half():
    row = evaluate_position(
        symbol="QQQ", strategy="compounder",
        current_price=80.0, peak_price=100.0,
    )
    assert row["decision"] == _DECISION_EXIT_HALF
    assert "drawdown_hard" in row["triggers"]


def test_evaluate_compounder_full_dd_exits_full():
    row = evaluate_position(
        symbol="QQQ", strategy="compounder",
        current_price=70.0, peak_price=100.0,
    )
    assert row["decision"] == _DECISION_EXIT_FULL
    assert "drawdown_full" in row["triggers"]


def test_evaluate_momentum_tighter_thresholds():
    # 6% drawdown — would be HOLD on compounder, TIGHTEN on momentum
    row = evaluate_position(
        symbol="QLD", strategy="momentum",
        current_price=94.0, peak_price=100.0,
    )
    assert row["decision"] == _DECISION_TIGHTEN


def test_evaluate_profit_protect_triggers_tighten():
    # No drawdown but big gain → TIGHTEN_STOP
    row = evaluate_position(
        symbol="X", strategy="compounder",
        current_price=130.0, peak_price=130.0,
        entry_price=100.0,
    )
    assert row["decision"] == _DECISION_TIGHTEN
    assert "profit_protect" in row["triggers"]
    assert row["gain_from_entry"] == 0.30


def test_evaluate_time_stop_momentum():
    row = evaluate_position(
        symbol="X", strategy="momentum",
        current_price=100.0, peak_price=100.0,
        days_held=200,
    )
    assert row["decision"] == _DECISION_TIGHTEN
    assert "time_stop" in row["triggers"]


def test_evaluate_time_stop_does_not_apply_to_compounder():
    row = evaluate_position(
        symbol="X", strategy="compounder",
        current_price=100.0, peak_price=100.0,
        days_held=2000,
    )
    assert row["decision"] == _DECISION_HOLD
    assert "time_stop" not in row["triggers"]


def test_evaluate_signal_decay_escalates_tighten_to_exit_half():
    # In soft drawdown (12%) AND signal score dropped 0.30 from entry
    row = evaluate_position(
        symbol="X", strategy="compounder",
        current_price=88.0, peak_price=100.0,
        entry_signal_score=0.80, current_signal_score=0.50,
    )
    assert row["decision"] == _DECISION_EXIT_HALF
    assert "signal_decay" in row["triggers"]


def test_evaluate_signal_decay_alone_does_not_trigger():
    # No drawdown — signal decay alone should not exit
    row = evaluate_position(
        symbol="X", strategy="compounder",
        current_price=100.0, peak_price=100.0,
        entry_signal_score=0.80, current_signal_score=0.40,
    )
    assert row["decision"] == _DECISION_HOLD
    assert "signal_decay" not in row["triggers"]


# ---------------------------------------------------------------------------
# build_plan
# ---------------------------------------------------------------------------


def test_build_plan_counts_and_summary():
    rows = [
        evaluate_position(symbol="A", strategy="compounder",
                          current_price=100.0, peak_price=100.0),
        evaluate_position(symbol="B", strategy="compounder",
                          current_price=88.0, peak_price=100.0),
        evaluate_position(symbol="C", strategy="momentum",
                          current_price=70.0, peak_price=100.0),
    ]
    plan = build_plan(rows)
    assert plan["observe_only"] is True
    assert plan["counts"]["HOLD"] == 1
    assert plan["counts"]["TIGHTEN_STOP"] == 1
    assert plan["counts"]["EXIT_FULL"] == 1
    assert plan["schema_version"] == "1"
    assert "tighten-stop" in plan["summary_line"]


# ---------------------------------------------------------------------------
# run_exit_advisor — pipeline entry point
# ---------------------------------------------------------------------------


def _write_config(path: Path, holdings: list[dict]) -> None:
    path.write_text(
        json.dumps({"portfolio": {"holdings": holdings}}, indent=2),
        encoding="utf-8",
    )


def test_run_exit_advisor_without_fmp_writes_insufficient_data(tmp_path):
    repo = tmp_path
    _write_config(
        repo / "config.json",
        [
            {"symbol": "QQQ", "shares": 6, "is_leveraged": False},
            {"symbol": "QLD", "shares": 8, "is_leveraged": True},
        ],
    )
    plan = run_exit_advisor(repo, fmp_client=None, base_dir=repo / "outputs")
    assert plan["observe_only"] is True
    assert len(plan["positions"]) == 2
    assert all(p["status"] == "insufficient_data" for p in plan["positions"])
    # Artifacts written to LATEST namespace
    out_json = repo / "outputs" / "latest" / "exit_advisor.json"
    out_md = repo / "outputs" / "latest" / "exit_advisor.md"
    assert out_json.exists()
    assert out_md.exists()
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["observe_only"] is True


def test_run_exit_advisor_with_stub_fmp(tmp_path):
    repo = tmp_path
    _write_config(
        repo / "config.json",
        [{"symbol": "QQQ", "shares": 6, "is_leveraged": False}],
    )

    class StubFMP:
        def get_historical_prices(self, symbol, *, years=1, ttl_days=1):
            # FMP-style newest-first list. Peak=120, current=90 → 25% DD
            return [
                {"date": "2026-05-15", "close": 90.0, "adjClose": 90.0},
                {"date": "2026-04-01", "close": 120.0, "adjClose": 120.0},
                {"date": "2026-03-01", "close": 100.0, "adjClose": 100.0},
            ]

    plan = run_exit_advisor(
        repo, fmp_client=StubFMP(), base_dir=repo / "outputs"
    )
    assert len(plan["positions"]) == 1
    row = plan["positions"][0]
    assert row["symbol"] == "QQQ"
    assert row["status"] == "ok"
    assert row["drawdown_from_peak"] == 0.25
    # 25% drawdown is above compounder hard threshold (18%) but below full (28%)
    assert row["decision"] == _DECISION_EXIT_HALF


def test_run_exit_advisor_handles_missing_config(tmp_path):
    plan = run_exit_advisor(tmp_path, fmp_client=None, base_dir=tmp_path / "outputs")
    assert plan["observe_only"] is True
    assert plan["positions"] == []


def test_run_exit_advisor_skips_zero_share_holdings(tmp_path):
    _write_config(
        tmp_path / "config.json",
        [
            {"symbol": "QQQ", "shares": 6, "is_leveraged": False},
            {"symbol": "VXUS", "shares": 0, "is_leveraged": False},
            {"symbol": "VFH", "shares": 0, "is_leveraged": False},
        ],
    )
    plan = run_exit_advisor(tmp_path, fmp_client=None, base_dir=tmp_path / "outputs")
    syms = [r["symbol"] for r in plan["positions"]]
    assert syms == ["QQQ"]


def test_run_exit_advisor_fmp_failure_is_non_fatal(tmp_path):
    _write_config(
        tmp_path / "config.json",
        [{"symbol": "QQQ", "shares": 6, "is_leveraged": False}],
    )

    class BrokenFMP:
        def get_historical_prices(self, symbol, *, years=1, ttl_days=1):
            raise RuntimeError("simulated FMP failure")

    plan = run_exit_advisor(
        tmp_path, fmp_client=BrokenFMP(), base_dir=tmp_path / "outputs"
    )
    assert plan["observe_only"] is True
    assert len(plan["positions"]) == 1
    assert plan["positions"][0]["status"] == "insufficient_data"


def test_artifact_observe_only_field_is_hardcoded(tmp_path):
    _write_config(
        tmp_path / "config.json",
        [{"symbol": "QQQ", "shares": 1, "is_leveraged": False}],
    )
    run_exit_advisor(tmp_path, fmp_client=None, base_dir=tmp_path / "outputs")
    payload = json.loads(
        (tmp_path / "outputs" / "latest" / "exit_advisor.json").read_text("utf-8")
    )
    assert payload["observe_only"] is True
