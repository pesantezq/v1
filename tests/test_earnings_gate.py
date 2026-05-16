"""Tests for portfolio_automation/earnings_gate.py."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portfolio_automation.earnings_gate import (
    _GATE_APPROACHING,
    _GATE_HOLD,
    _GATE_POST,
    _GATE_REVIEW,
    build_plan,
    classify_window,
    evaluate_position,
    run_earnings_gate,
)


# ---------------------------------------------------------------------------
# classify_window
# ---------------------------------------------------------------------------


def test_classify_far_future_holds():
    assert classify_window(25) == _GATE_HOLD


def test_classify_inside_warning_window():
    assert classify_window(12) == _GATE_APPROACHING


def test_classify_inside_critical_window():
    assert classify_window(3) == _GATE_REVIEW


def test_classify_day_of_earnings():
    assert classify_window(0) == _GATE_REVIEW


def test_classify_recent_past():
    assert classify_window(-2) == _GATE_POST


def test_classify_distant_past_holds():
    assert classify_window(-30) == _GATE_HOLD


# ---------------------------------------------------------------------------
# evaluate_position
# ---------------------------------------------------------------------------


def test_evaluate_no_source_returns_no_earnings_source():
    row = evaluate_position(symbol="X", earnings_data=None)
    assert row["status"] == "no_earnings_source"
    assert row["gate"] == _GATE_HOLD
    assert row["days_until"] is None


def test_evaluate_bad_date_returns_unparseable():
    row = evaluate_position(
        symbol="X", earnings_data={"earnings_date": "not-a-date"},
    )
    assert row["status"] == "unparseable_date"
    assert row["gate"] == _GATE_HOLD


def test_evaluate_approaching_window():
    today = date(2026, 5, 15)
    row = evaluate_position(
        symbol="NVDA",
        earnings_data={"earnings_date": "2026-05-25"},
        today=today,
    )
    assert row["gate"] == _GATE_APPROACHING
    assert row["days_until"] == 10


def test_evaluate_review_window():
    today = date(2026, 5, 15)
    row = evaluate_position(
        symbol="AAPL",
        earnings_data={"earnings_date": "2026-05-17", "time": "amc"},
        today=today,
    )
    assert row["gate"] == _GATE_REVIEW
    assert row["days_until"] == 2
    assert row["earnings_time"] == "amc"


def test_evaluate_post_earnings_window():
    today = date(2026, 5, 15)
    row = evaluate_position(
        symbol="MSFT",
        earnings_data={"earnings_date": "2026-05-13"},
        today=today,
    )
    assert row["gate"] == _GATE_POST
    assert row["days_until"] == -2


# ---------------------------------------------------------------------------
# build_plan
# ---------------------------------------------------------------------------


def test_build_plan_envelope():
    today = date(2026, 5, 15)
    rows = [
        evaluate_position(symbol="A", earnings_data=None, today=today),
        evaluate_position(symbol="B", earnings_data={"earnings_date": "2026-05-17"},
                          today=today),
    ]
    plan = build_plan(rows)
    assert plan["observe_only"] is True
    assert plan["schema_version"] == "1"
    assert plan["counts"][_GATE_HOLD] == 1
    assert plan["counts"][_GATE_REVIEW] == 1
    assert "review" in plan["summary_line"]


# ---------------------------------------------------------------------------
# run_earnings_gate — integration
# ---------------------------------------------------------------------------


def _write_config(path: Path, holdings: list[dict]) -> None:
    path.write_text(
        json.dumps({"portfolio": {"holdings": holdings}}, indent=2),
        encoding="utf-8",
    )


def test_run_without_lookup_writes_no_earnings_source(tmp_path):
    _write_config(
        tmp_path / "config.json",
        [{"symbol": "QQQ", "shares": 6}, {"symbol": "GLD", "shares": 4}],
    )
    plan = run_earnings_gate(tmp_path, earnings_lookup=None,
                             base_dir=tmp_path / "outputs")
    assert plan["observe_only"] is True
    assert all(r["status"] == "no_earnings_source" for r in plan["positions"])
    assert (tmp_path / "outputs" / "latest" / "earnings_gate.json").exists()


def test_run_with_stub_lookup(tmp_path):
    _write_config(
        tmp_path / "config.json",
        [{"symbol": "AAPL", "shares": 10}, {"symbol": "MSFT", "shares": 5}],
    )

    def lookup(sym):
        return {
            # 2 days out — REVIEW window
            "AAPL": {"earnings_date": "2026-05-17"},
            # 12 days out — APPROACHING window (within 15-day warning)
            "MSFT": {"earnings_date": "2026-05-27"},
        }.get(sym)

    plan = run_earnings_gate(
        tmp_path, earnings_lookup=lookup, today=date(2026, 5, 15),
        base_dir=tmp_path / "outputs",
    )
    syms_to_gate = {r["symbol"]: r["gate"] for r in plan["positions"]}
    assert syms_to_gate["AAPL"] == _GATE_REVIEW
    assert syms_to_gate["MSFT"] == _GATE_APPROACHING


def test_run_skips_zero_share_holdings(tmp_path):
    _write_config(
        tmp_path / "config.json",
        [
            {"symbol": "QQQ", "shares": 6},
            {"symbol": "VXUS", "shares": 0},
        ],
    )
    plan = run_earnings_gate(tmp_path, earnings_lookup=None,
                             base_dir=tmp_path / "outputs")
    assert [r["symbol"] for r in plan["positions"]] == ["QQQ"]


def test_lookup_exception_is_non_fatal(tmp_path):
    _write_config(
        tmp_path / "config.json",
        [{"symbol": "QQQ", "shares": 6}],
    )

    def broken(sym):
        raise RuntimeError("simulated")

    plan = run_earnings_gate(
        tmp_path, earnings_lookup=broken, base_dir=tmp_path / "outputs",
    )
    assert plan["positions"][0]["status"] == "no_earnings_source"


def test_observe_only_hardcoded(tmp_path):
    _write_config(tmp_path / "config.json", [{"symbol": "X", "shares": 1}])
    run_earnings_gate(tmp_path, earnings_lookup=None, base_dir=tmp_path / "outputs")
    payload = json.loads(
        (tmp_path / "outputs" / "latest" / "earnings_gate.json").read_text("utf-8")
    )
    assert payload["observe_only"] is True
