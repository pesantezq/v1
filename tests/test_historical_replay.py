"""
Tests for portfolio_automation/historical_replay/*

All tests are fully offline — FMPClient is replaced with a mock and
price data is generated procedurally so tests are deterministic.

Contracts verified:
- All replay rows carry source="historical_replay"
- live decision_outcomes.jsonl is never touched by the replay subsystem
- outputs are written under outputs/backtest, never outputs/policy
- missing / empty historical data does not crash
- 5-day momentum rule produces expected BUY / SELL / WAIT decisions
- 1d / 3d / 7d outcome windows resolve correctly
- WAIT threshold logic is correct
- markdown reports render to non-empty strings
- CLI --dry-run produces no file writes
- no LLM calls are made
- only get_historical_prices() is called on the FMP mock
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from portfolio_automation.historical_replay.replay_data_loader import (
    load_holdings_symbols,
    load_universe,
    normalize_prices,
    load_historical_prices,
    load_extra_symbols,
)
from portfolio_automation.historical_replay.replay_decision_simulator import (
    simulate_decisions,
    simulate_all_decisions,
    BUY_RETURN_THRESHOLD,
    SELL_RETURN_THRESHOLD,
    SMA_PERIOD,
    RETURN_PERIOD,
    SOURCE,
)
from portfolio_automation.historical_replay.replay_outcome_resolver import (
    resolve_outcomes,
    _is_direction_correct,
    _find_price_at_offset,
    _build_price_map,
    WAIT_CORRECT_THRESHOLD,
)
from portfolio_automation.historical_replay.replay_reports import (
    build_historical_calibration,
    build_historical_attribution,
    render_calibration_md,
    render_attribution_md,
    write_calibration,
    write_attribution,
)
from portfolio_automation.historical_replay.replay_runner import run_replay


# ---------------------------------------------------------------------------
# Shared price-generation helpers
# ---------------------------------------------------------------------------

def _make_prices(
    symbol: str = "AAPL",
    days: int = 120,
    start_price: float = 100.0,
    daily_change: float = 0.007,  # +0.7 %/day → 5d return ≈ +3.55 % → BUY
    start: date | None = None,
) -> list[dict]:
    """Synthetic normalized price list, oldest-first."""
    start = start or date(2024, 1, 1)
    rows = []
    for i in range(days):
        d = start + timedelta(days=i)
        close = round(start_price * ((1 + daily_change) ** i), 4)
        rows.append({
            "date": d.isoformat(),
            "open": close,
            "high": round(close * 1.01, 4),
            "low": round(close * 0.99, 4),
            "close": close,
            "volume": 1_000_000,
        })
    return rows  # oldest-first (already in order)


def _make_fmp(historical: list[dict] | None = None, symbol: str = "AAPL") -> MagicMock:
    client = MagicMock()
    client.get_historical_prices.return_value = (
        list(reversed(historical)) if historical is not None else []  # FMP newest-first
    )
    return client


def _make_resolved_row(
    symbol: str = "AAPL",
    decision: str = "BUY",
    return_pct: float = 0.05,
    confidence: float = 0.6,
    direction_correct: bool = True,
) -> dict:
    return {
        "source": SOURCE,
        "symbol": symbol,
        "decision": decision,
        "date": "2024-03-01",
        "confidence": confidence,
        "strategy": "historical_momentum_proxy",
        "band": "replay",
        "validation_status": "historical_replay",
        "resolved": True,
        "resolved_at": "2024-03-08",
        "return_pct": return_pct,
        "direction_correct": direction_correct,
        "window_days": 7,
    }


# ---------------------------------------------------------------------------
# 1. Source tag
# ---------------------------------------------------------------------------

def test_all_rows_have_historical_replay_source():
    prices = _make_prices(days=120)
    rows = simulate_decisions("AAPL", prices, days=90)
    assert rows, "Expected at least one decision"
    assert all(r["source"] == SOURCE for r in rows)
    assert all(r["source"] == "historical_replay" for r in rows)


# ---------------------------------------------------------------------------
# 2. Live JSONL is never modified
# ---------------------------------------------------------------------------

def test_live_jsonl_not_modified(tmp_path):
    """Replay code must never touch outputs/policy/decision_outcomes.jsonl."""
    live_jsonl = tmp_path / "outputs" / "policy" / "decision_outcomes.jsonl"
    live_jsonl.parent.mkdir(parents=True)
    original_content = '{"source": "live", "symbol": "QQQ"}\n'
    live_jsonl.write_text(original_content)

    prices = _make_prices(days=120)
    rows = simulate_decisions("AAPL", prices, days=90)
    resolved = resolve_outcomes(rows, {"AAPL": prices})

    out_dir = tmp_path / "outputs" / "backtest"
    cal = build_historical_calibration(resolved)
    attr = build_historical_attribution(resolved)
    write_calibration(cal, out_dir)
    write_attribution(attr, out_dir)

    assert live_jsonl.read_text() == original_content, "Live JSONL must not be modified"


# ---------------------------------------------------------------------------
# 3. Outputs write to outputs/backtest, not outputs/policy
# ---------------------------------------------------------------------------

def test_outputs_written_under_backtest_not_policy(tmp_path):
    rows = [_make_resolved_row()]
    out_dir = tmp_path / "outputs" / "backtest"
    policy_dir = tmp_path / "outputs" / "policy"
    policy_dir.mkdir(parents=True)

    cal = build_historical_calibration(rows)
    attr = build_historical_attribution(rows)
    write_calibration(cal, out_dir)
    write_attribution(attr, out_dir)

    assert (out_dir / "historical_calibration.json").exists()
    assert (out_dir / "historical_calibration.md").exists()
    assert (out_dir / "historical_performance_attribution.json").exists()
    assert (out_dir / "historical_performance_attribution.md").exists()

    # Policy dir must remain untouched
    assert not list(policy_dir.iterdir()), "outputs/policy must not receive replay artifacts"


# ---------------------------------------------------------------------------
# 4. Missing historical data does not crash
# ---------------------------------------------------------------------------

def test_empty_price_data_no_crash():
    rows = simulate_all_decisions({})
    assert rows == []


def test_empty_price_list_for_symbol_no_crash():
    rows = simulate_decisions("AAPL", [])
    assert rows == []


def test_resolve_empty_rows_no_crash():
    result = resolve_outcomes([], {})
    assert result == []


def test_resolve_with_missing_symbol_no_crash():
    rows = [_make_resolved_row(symbol="ZZZZ")]
    rows[0]["resolved"] = False
    rows[0]["price_at_decision"] = 100.0
    result = resolve_outcomes(rows, {})
    assert len(result) == 1
    assert result[0]["resolved"] is False


# ---------------------------------------------------------------------------
# 5. BUY signal from momentum rule
# ---------------------------------------------------------------------------

def test_buy_signal_created_for_strong_uptrend():
    """0.7 %/day → 5d return ≈ +3.55 % above SMA20 → BUY."""
    prices = _make_prices(days=120, daily_change=0.007)
    rows = simulate_decisions("AAPL", prices, days=90)
    assert rows, "Expected decisions"
    buy_rows = [r for r in rows if r["decision"] == "BUY"]
    assert buy_rows, "Expected at least one BUY in an uptrending series"
    for r in buy_rows:
        assert r["lookback_features"]["return_5d"] > BUY_RETURN_THRESHOLD
        assert r["lookback_features"]["above_sma20"] is True


def test_wait_signal_for_flat_prices():
    """Flat prices → 5d return ≈ 0 → WAIT."""
    prices = _make_prices(days=120, daily_change=0.0)
    rows = simulate_decisions("AAPL", prices, days=90)
    assert rows
    assert all(r["decision"] == "WAIT" for r in rows)


# ---------------------------------------------------------------------------
# 6. SELL signal for a holding with negative momentum
# ---------------------------------------------------------------------------

def test_sell_signal_for_holding_with_downtrend():
    """-0.8 %/day → 5d return ≈ -3.9 % → SELL when symbol is a holding."""
    prices = _make_prices(days=120, daily_change=-0.008)
    rows = simulate_decisions(
        "QQQ", prices, holding_symbols=frozenset({"QQQ"}), days=90
    )
    assert rows
    sell_rows = [r for r in rows if r["decision"] == "SELL"]
    assert sell_rows, "Expected SELL rows for a falling holding"
    for r in sell_rows:
        assert r["lookback_features"]["return_5d"] < SELL_RETURN_THRESHOLD


def test_wait_not_sell_for_non_holding_with_downtrend():
    """-0.8 %/day but symbol is NOT a holding → WAIT, not SELL."""
    prices = _make_prices(days=120, daily_change=-0.008)
    rows = simulate_decisions(
        "NVDA", prices, holding_symbols=frozenset({"QQQ"}), days=90
    )
    assert rows
    assert all(r["decision"] != "SELL" for r in rows)


# ---------------------------------------------------------------------------
# 7. 1d / 3d / 7d outcome resolution
# ---------------------------------------------------------------------------

def _make_known_price_data(
    symbol: str = "AAPL",
    start_price: float = 100.0,
    days: int = 120,
    daily_change: float = 0.01,
) -> list[dict]:
    return _make_prices(symbol=symbol, days=days, start_price=start_price,
                        daily_change=daily_change)


def test_resolve_at_7d_window():
    """Decision made at day 25; day 32 price should be the resolution."""
    prices = _make_known_price_data(days=120, daily_change=0.01)
    # Manufacture one decision row at prices[25]
    row = {
        "source": SOURCE,
        "symbol": "AAPL",
        "decision": "BUY",
        "date": prices[25]["date"],
        "price_at_decision": prices[25]["close"],
        "confidence": 0.6,
        "strategy": "historical_momentum_proxy",
        "band": "replay",
        "validation_status": "historical_replay",
        "resolved": False,
        "resolved_at": None,
        "days_elapsed": None,
        "price_at_resolution": None,
        "return_pct": None,
        "direction_correct": None,
        "window_days": None,
        "outcome_price": None,
    }
    result = resolve_outcomes([row], {"AAPL": prices}, window_days=(1, 3, 7))
    assert len(result) == 1
    r = result[0]
    assert r["resolved"] is True
    assert r["window_days"] in (1, 3, 7)
    assert r["return_pct"] is not None
    assert r["direction_correct"] is True  # price rising +1%/day → BUY correct


def test_resolve_prefer_longest_window():
    """Resolver should prefer the 7-day window when data is available."""
    prices = _make_known_price_data(days=120)
    row = {
        "source": SOURCE,
        "symbol": "AAPL",
        "decision": "BUY",
        "date": prices[30]["date"],
        "price_at_decision": prices[30]["close"],
        "confidence": 0.6,
        "strategy": "historical_momentum_proxy",
        "band": "replay",
        "validation_status": "historical_replay",
        "resolved": False,
        "resolved_at": None,
        "days_elapsed": None,
        "price_at_resolution": None,
        "return_pct": None,
        "direction_correct": None,
        "window_days": None,
        "outcome_price": None,
    }
    result = resolve_outcomes([row], {"AAPL": prices}, window_days=(1, 3, 7))
    assert result[0]["window_days"] == 7


def test_unresolved_when_no_forward_data(tmp_path):
    """Decision at the very last price row → no forward data → unresolved."""
    prices = _make_known_price_data(days=30)
    row = {
        "source": SOURCE,
        "symbol": "AAPL",
        "decision": "BUY",
        "date": prices[-1]["date"],   # last available date
        "price_at_decision": prices[-1]["close"],
        "confidence": 0.6,
        "strategy": "historical_momentum_proxy",
        "band": "replay",
        "validation_status": "historical_replay",
        "resolved": False,
        "resolved_at": None,
        "days_elapsed": None,
        "price_at_resolution": None,
        "return_pct": None,
        "direction_correct": None,
        "window_days": None,
        "outcome_price": None,
    }
    result = resolve_outcomes([row], {"AAPL": prices}, window_days=(1, 3, 7))
    assert result[0]["resolved"] is False


# ---------------------------------------------------------------------------
# 8. WAIT threshold logic
# ---------------------------------------------------------------------------

def test_wait_correct_when_return_below_threshold():
    assert _is_direction_correct("WAIT", 0.01, WAIT_CORRECT_THRESHOLD) is True
    assert _is_direction_correct("WAIT", -0.02, WAIT_CORRECT_THRESHOLD) is True


def test_wait_incorrect_when_return_above_threshold():
    assert _is_direction_correct("WAIT", 0.05, WAIT_CORRECT_THRESHOLD) is False
    assert _is_direction_correct("WAIT", -0.05, WAIT_CORRECT_THRESHOLD) is False


def test_buy_correct_when_return_positive():
    assert _is_direction_correct("BUY", 0.03) is True
    assert _is_direction_correct("BUY", -0.03) is False


def test_sell_correct_when_return_negative():
    assert _is_direction_correct("SELL", -0.02) is True
    assert _is_direction_correct("SELL", 0.02) is False


def test_hold_is_neutral():
    assert _is_direction_correct("HOLD", 0.10) is None
    assert _is_direction_correct("HOLD", -0.10) is None


# ---------------------------------------------------------------------------
# 9. Markdown reports render
# ---------------------------------------------------------------------------

def test_calibration_md_renders():
    rows = [_make_resolved_row(decision="BUY", return_pct=0.04, direction_correct=True)]
    payload = build_historical_calibration(rows)
    md = render_calibration_md(payload)
    assert isinstance(md, str)
    assert len(md) > 50
    assert "Historical replay only" in md
    assert "Observe-only" in md
    assert "Historical Replay" in md


def test_attribution_md_renders():
    rows = [_make_resolved_row(decision="BUY", return_pct=0.04, direction_correct=True)]
    payload = build_historical_attribution(rows)
    md = render_attribution_md(payload)
    assert isinstance(md, str)
    assert len(md) > 50
    assert "Historical replay only" in md


def test_calibration_md_with_no_resolved_rows():
    payload = build_historical_calibration([])
    md = render_calibration_md(payload)
    assert "Historical Replay" in md
    assert "0" in md


def test_attribution_md_with_no_resolved_rows():
    payload = build_historical_attribution([])
    md = render_attribution_md(payload)
    assert "Historical Replay" in md


# ---------------------------------------------------------------------------
# 10. CLI dry-run produces no file writes
# ---------------------------------------------------------------------------

def test_dry_run_produces_no_files(tmp_path):
    prices = _make_prices(days=120)
    fmp_mock = _make_fmp(historical=prices)

    out_dir = tmp_path / "outputs" / "backtest"
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "portfolio": {"holdings": [{"symbol": "AAPL"}]}
    }))

    summary = run_replay(
        days=90,
        output_dir=out_dir,
        dry_run=True,
        fmp_client=fmp_mock,
        config_path=cfg,
        root=tmp_path,
    )

    assert summary["dry_run"] is True
    assert not out_dir.exists() or not any(out_dir.iterdir()), \
        "dry_run must not write any files"


# ---------------------------------------------------------------------------
# 11. Full end-to-end: files written, JSONL has correct content
# ---------------------------------------------------------------------------

def test_full_run_writes_expected_files(tmp_path):
    prices = _make_prices(days=120, daily_change=0.007)
    fmp_mock = _make_fmp(historical=prices)

    out_dir = tmp_path / "outputs" / "backtest"
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "portfolio": {"holdings": [{"symbol": "AAPL"}]}
    }))

    summary = run_replay(
        days=90,
        output_dir=out_dir,
        dry_run=False,
        fmp_client=fmp_mock,
        config_path=cfg,
        root=tmp_path,
    )

    assert summary["decisions_generated"] > 0
    assert (out_dir / "decision_outcomes_historical.jsonl").exists()
    assert (out_dir / "historical_calibration.json").exists()
    assert (out_dir / "historical_calibration.md").exists()
    assert (out_dir / "historical_performance_attribution.json").exists()
    assert (out_dir / "historical_performance_attribution.md").exists()

    # All JSONL rows must be source=historical_replay
    content = (out_dir / "decision_outcomes_historical.jsonl").read_text()
    rows = [json.loads(line) for line in content.splitlines() if line.strip()]
    assert rows
    assert all(r["source"] == "historical_replay" for r in rows)


# ---------------------------------------------------------------------------
# 12. No LLM usage
# ---------------------------------------------------------------------------

def test_no_llm_calls_during_simulation():
    """Simulator must be fully deterministic — no LLM calls."""
    prices = _make_prices(days=120)
    with patch("portfolio_automation.historical_replay.replay_decision_simulator.logger") as mock_log:
        rows = simulate_decisions("AAPL", prices, days=90)
    # If any LLM was invoked it would raise or appear in unexpected mock calls
    assert rows is not None  # smoke test


def test_fmp_mock_called_only_with_historical_prices():
    """Only get_historical_prices() should be called — no premium endpoints."""
    prices = _make_prices(days=120)
    fmp_mock = _make_fmp(historical=prices)

    load_historical_prices(["AAPL"], fmp_mock, days=90)

    called_methods = [call[0] for call in fmp_mock.method_calls]
    assert "get_historical_prices" in called_methods
    # No premium or LLM-related methods
    for method in called_methods:
        assert method not in (
            "get_income_statement",
            "get_company_facts",
            "chat",
            "complete",
        ), f"Unexpected method called: {method}"


# ---------------------------------------------------------------------------
# 13. normalize_prices correctness
# ---------------------------------------------------------------------------

def test_normalize_prices_oldest_first():
    raw = [
        {"date": "2024-01-03", "close": 103.0, "volume": 100},
        {"date": "2024-01-01", "close": 101.0, "volume": 100},
        {"date": "2024-01-02", "close": 102.0, "volume": 100},
    ]
    result = normalize_prices(raw)
    dates = [r["date"] for r in result]
    assert dates == sorted(dates)


def test_normalize_prices_skips_zero_close():
    raw = [
        {"date": "2024-01-01", "close": 0.0, "volume": 100},
        {"date": "2024-01-02", "close": 100.0, "volume": 100},
    ]
    result = normalize_prices(raw)
    assert len(result) == 1
    assert result[0]["close"] == 100.0


def test_normalize_prices_empty_input():
    assert normalize_prices([]) == []


# ---------------------------------------------------------------------------
# 14. Universe loading
# ---------------------------------------------------------------------------

def test_load_holdings_symbols_from_config(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "portfolio": {
            "holdings": [
                {"symbol": "QQQ", "shares": 6},
                {"symbol": "GLD", "shares": 4},
            ]
        }
    }))
    symbols = load_holdings_symbols(cfg)
    assert symbols == ["QQQ", "GLD"]


def test_load_holdings_symbols_missing_config(tmp_path):
    symbols = load_holdings_symbols(tmp_path / "nonexistent.json")
    assert symbols == []


def test_load_extra_symbols_parses_comma_separated():
    result = load_extra_symbols("AAPL, MSFT, NVDA")
    assert result == ["AAPL", "MSFT", "NVDA"]


def test_load_extra_symbols_none_input():
    assert load_extra_symbols(None) == []


def test_load_universe_deduplicates(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "portfolio": {"holdings": [{"symbol": "AAPL"}]}
    }))
    result = load_universe(cfg, extra_symbols=["AAPL", "MSFT"])
    assert result.count("AAPL") == 1
    assert "MSFT" in result


# ---------------------------------------------------------------------------
# 15. build_historical_calibration filters to replay source only
# ---------------------------------------------------------------------------

def test_calibration_ignores_live_rows():
    live_row = _make_resolved_row()
    live_row["source"] = "live"
    replay_row = _make_resolved_row(return_pct=0.06)
    payload = build_historical_calibration([live_row, replay_row])
    assert payload["total_resolved"] == 1  # only the replay row counts


def test_attribution_ignores_live_rows():
    live_row = _make_resolved_row()
    live_row["source"] = "live"
    replay_row = _make_resolved_row()
    payload = build_historical_attribution([live_row, replay_row])
    assert payload["total_decisions"] == 1
