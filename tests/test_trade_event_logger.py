"""
Tests for trade_event_logger.
Covers: serialization, append, filtering, reader helpers, dry-run, non-fatal IO.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from trade_event_logger import (
    DEFAULT_PATH,
    LOGGABLE_ACTIONS,
    TradeEvent,
    append_trade_events,
    iter_trade_events,
    load_trade_events,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _buy_action(**overrides) -> dict:
    base = {
        "action": "BUY",
        "symbol": "NVDA",
        "strategy_type": "momentum",
        "score": 82.0,
        "confidence": 0.80,
        "suggested_allocation_pct": 0.08,
        "suggested_allocation_amount": 8000.0,
        "rationale": ["strong breakout", "volume confirmation"],
        "related_symbol": None,
        "exit_plan": None,
    }
    base.update(overrides)
    return base


def _common_ctx(**overrides) -> dict:
    base = dict(
        run_id="2026-04-16_daily",
        run_mode="daily",
        portfolio_value=100_000.0,
        cash_available=10_000.0,
        drawdown_regime="normal",
        degraded_mode=False,
        degraded_reason=None,
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Serialisation — individual action types
# ---------------------------------------------------------------------------

def test_buy_event_fields(tmp_path):
    out = tmp_path / "trade_events.jsonl"
    n = append_trade_events([_buy_action()], **_common_ctx(), history_path=out)
    assert n == 1
    records = load_trade_events(out)
    assert len(records) == 1
    r = records[0]
    assert r["action"] == "BUY"
    assert r["symbol"] == "NVDA"
    assert r["strategy_type"] == "momentum"
    assert r["score"] == 82.0
    assert r["confidence"] == 0.80
    assert r["suggested_allocation_pct"] == 0.08
    assert r["suggested_allocation_amount"] == 8000.0
    assert r["rationale"] == ["strong breakout", "volume confirmation"]
    assert r["run_id"] == "2026-04-16_daily"
    assert r["run_mode"] == "daily"
    assert r["portfolio_value"] == 100_000.0
    assert r["drawdown_regime"] == "normal"
    assert r["degraded_mode"] is False


def test_sell_event(tmp_path):
    out = tmp_path / "trade_events.jsonl"
    action = {"action": "SELL", "symbol": "AAPL", "rationale": ["thesis weakened"]}
    n = append_trade_events([action], **_common_ctx(), history_path=out)
    assert n == 1
    r = load_trade_events(out)[0]
    assert r["action"] == "SELL"
    assert r["symbol"] == "AAPL"


def test_trim_event(tmp_path):
    out = tmp_path / "trade_events.jsonl"
    action = {"action": "TRIM", "symbol": "MSFT", "score": 65.0}
    n = append_trade_events([action], **_common_ctx(), history_path=out)
    assert n == 1
    r = load_trade_events(out)[0]
    assert r["action"] == "TRIM"
    assert r["symbol"] == "MSFT"


def test_promote_to_portfolio_event(tmp_path):
    out = tmp_path / "trade_events.jsonl"
    action = {"action": "PROMOTE_TO_PORTFOLIO", "symbol": "GOOG", "score": 75.0}
    n = append_trade_events([action], **_common_ctx(), history_path=out)
    assert n == 1
    r = load_trade_events(out)[0]
    assert r["action"] == "PROMOTE_TO_PORTFOLIO"


# ---------------------------------------------------------------------------
# Informational actions are skipped by default
# ---------------------------------------------------------------------------

def test_hold_not_logged(tmp_path):
    out = tmp_path / "trade_events.jsonl"
    action = {"action": "HOLD", "symbol": "TSLA"}
    n = append_trade_events([action], **_common_ctx(), history_path=out)
    assert n == 0
    assert not out.exists()


def test_watchlist_not_logged(tmp_path):
    out = tmp_path / "trade_events.jsonl"
    action = {"action": "ADD_TO_WATCHLIST", "symbol": "AMD"}
    n = append_trade_events([action], **_common_ctx(), history_path=out)
    assert n == 0
    assert not out.exists()


def test_mixed_actions_only_loggable_written(tmp_path):
    out = tmp_path / "trade_events.jsonl"
    actions = [
        {"action": "BUY", "symbol": "NVDA"},
        {"action": "HOLD", "symbol": "TSLA"},
        {"action": "SELL", "symbol": "AAPL"},
        {"action": "ADD_TO_WATCHLIST", "symbol": "AMD"},
        {"action": "TRIM", "symbol": "MSFT"},
    ]
    n = append_trade_events(actions, **_common_ctx(), history_path=out)
    assert n == 3
    records = load_trade_events(out)
    logged_actions = {r["action"] for r in records}
    assert logged_actions == {"BUY", "SELL", "TRIM"}


# ---------------------------------------------------------------------------
# Missing optional fields
# ---------------------------------------------------------------------------

def test_missing_optional_fields(tmp_path):
    out = tmp_path / "trade_events.jsonl"
    action = {"action": "BUY", "symbol": "XYZ"}  # no score, confidence, rationale etc.
    n = append_trade_events([action], **_common_ctx(), history_path=out)
    assert n == 1
    r = load_trade_events(out)[0]
    assert r["score"] is None
    assert r["confidence"] is None
    assert r["strategy_type"] is None
    assert r["suggested_allocation_pct"] is None
    assert r["suggested_allocation_amount"] is None
    assert r["rationale"] == []
    assert r["related_symbol"] is None
    assert r["exit_plan"] is None


def test_null_portfolio_context(tmp_path):
    out = tmp_path / "trade_events.jsonl"
    ctx = _common_ctx(portfolio_value=None, cash_available=None, degraded_reason="fmp_error")
    n = append_trade_events([_buy_action()], **ctx, history_path=out)
    assert n == 1
    r = load_trade_events(out)[0]
    assert r["portfolio_value"] is None
    assert r["cash_available"] is None
    assert r["degraded_reason"] == "fmp_error"


# ---------------------------------------------------------------------------
# Append across multiple runs
# ---------------------------------------------------------------------------

def test_append_multiple_runs(tmp_path):
    out = tmp_path / "trade_events.jsonl"
    append_trade_events([_buy_action(symbol="NVDA")], **_common_ctx(run_id="run1"), history_path=out)
    append_trade_events(
        [_buy_action(symbol="AAPL"), {"action": "SELL", "symbol": "MSFT"}],
        **_common_ctx(run_id="run2"),
        history_path=out,
    )
    records = load_trade_events(out)
    assert len(records) == 3
    assert records[0]["run_id"] == "run1"
    assert records[1]["run_id"] == "run2"
    assert records[2]["run_id"] == "run2"


def test_each_record_is_valid_json_line(tmp_path):
    out = tmp_path / "trade_events.jsonl"
    actions = [_buy_action(symbol="A"), {"action": "SELL", "symbol": "B"}]
    append_trade_events(actions, **_common_ctx(), history_path=out)
    lines = [l.strip() for l in out.read_text().splitlines() if l.strip()]
    assert len(lines) == 2
    for line in lines:
        obj = json.loads(line)
        assert "action" in obj
        assert "symbol" in obj
        assert "run_id" in obj


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------

def test_dry_run_no_file_created(tmp_path):
    out = tmp_path / "trade_events.jsonl"
    n = append_trade_events([_buy_action()], **_common_ctx(), history_path=out, dry_run=True)
    assert n == 1          # count returned
    assert not out.exists()  # file not created


def test_dry_run_with_empty_actions(tmp_path):
    out = tmp_path / "trade_events.jsonl"
    n = append_trade_events([], **_common_ctx(), history_path=out, dry_run=True)
    assert n == 0
    assert not out.exists()


# ---------------------------------------------------------------------------
# Non-fatal on bad path / IO error
# ---------------------------------------------------------------------------

def test_bad_path_nonfatal(tmp_path):
    # Place a regular file where the parent directory should be —
    # mkdir will fail, write should not raise, should return 0.
    blocker = tmp_path / "notadir"
    blocker.write_text("block")
    bad_path = blocker / "trade_events.jsonl"   # parent is a file, not a dir
    n = append_trade_events([_buy_action()], **_common_ctx(), history_path=bad_path)
    assert n == 0


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------

def test_empty_action_list(tmp_path):
    out = tmp_path / "trade_events.jsonl"
    n = append_trade_events([], **_common_ctx(), history_path=out)
    assert n == 0
    assert not out.exists()


def test_all_actions_filtered(tmp_path):
    out = tmp_path / "trade_events.jsonl"
    n = append_trade_events(
        [{"action": "HOLD", "symbol": "X"}, {"action": "ADD_TO_WATCHLIST", "symbol": "Y"}],
        **_common_ctx(),
        history_path=out,
    )
    assert n == 0
    assert not out.exists()


# ---------------------------------------------------------------------------
# Reader helpers
# ---------------------------------------------------------------------------

def test_load_missing_file():
    records = load_trade_events(Path("nonexistent/path/trade_events.jsonl"))
    assert records == []


def test_iter_missing_file():
    result = list(iter_trade_events(Path("nonexistent/path/trade_events.jsonl")))
    assert result == []


def test_action_filter(tmp_path):
    out = tmp_path / "trade_events.jsonl"
    actions = [
        _buy_action(symbol="NVDA"),
        {"action": "SELL", "symbol": "AAPL"},
        {"action": "TRIM", "symbol": "MSFT"},
    ]
    append_trade_events(actions, **_common_ctx(), history_path=out)

    buys = load_trade_events(out, action_filter={"BUY"})
    assert len(buys) == 1
    assert buys[0]["symbol"] == "NVDA"

    sells_trims = load_trade_events(out, action_filter={"SELL", "TRIM"})
    assert len(sells_trims) == 2


def test_iter_malformed_lines(tmp_path):
    out = tmp_path / "trade_events.jsonl"
    # Write one good and one malformed line
    out.write_text(
        '{"action":"BUY","symbol":"NVDA","run_id":"r1"}\nNOT_VALID_JSON\n',
        encoding="utf-8",
    )
    records = load_trade_events(out)
    assert len(records) == 1
    assert records[0]["symbol"] == "NVDA"


# ---------------------------------------------------------------------------
# Custom loggable_actions override
# ---------------------------------------------------------------------------

def test_custom_loggable_actions(tmp_path):
    out = tmp_path / "trade_events.jsonl"
    actions = [
        {"action": "HOLD", "symbol": "X"},
        {"action": "ADD_TO_WATCHLIST", "symbol": "Y"},
        {"action": "BUY", "symbol": "Z"},
    ]
    # Override to also log HOLD
    n = append_trade_events(
        actions, **_common_ctx(), history_path=out,
        loggable_actions={"HOLD", "BUY"},
    )
    assert n == 2
    records = load_trade_events(out)
    logged = {r["action"] for r in records}
    assert logged == {"HOLD", "BUY"}


# ---------------------------------------------------------------------------
# TradeEvent.to_dict round-trip
# ---------------------------------------------------------------------------

def test_trade_event_to_dict_round_trip():
    evt = TradeEvent(
        run_id="2026-04-16_daily",
        timestamp="2026-04-16T09:00:00",
        run_mode="daily",
        portfolio_value=100_000.0,
        cash_available=5_000.0,
        drawdown_regime="normal",
        degraded_mode=False,
        degraded_reason=None,
        symbol="NVDA",
        action="BUY",
        strategy_type="momentum",
        score=82.0,
        confidence=0.80,
        suggested_allocation_pct=0.08,
        suggested_allocation_amount=8000.0,
        rationale=["breakout"],
        related_symbol=None,
        exit_plan={"stop_loss": 0.08},
    )
    d = evt.to_dict()
    assert d["symbol"] == "NVDA"
    assert d["exit_plan"] == {"stop_loss": 0.08}
    # JSON round-trip
    restored = json.loads(json.dumps(d))
    assert restored["action"] == "BUY"
    assert restored["score"] == 82.0
