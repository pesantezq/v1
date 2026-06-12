"""Tests for the price panel loader."""
from __future__ import annotations

import json
from pathlib import Path

from portfolio_automation.portfolio_sim.prices import load_price_panel


def _archive(root: Path, ticker: str, rows: list[dict]):
    d = root / "outputs" / "backtest" / "historical"
    d.mkdir(parents=True, exist_ok=True)
    # archive stores newest-first under "rows"
    (d / f"{ticker}_5y.json").write_text(json.dumps({"symbol": ticker, "rows": list(reversed(rows))}))


def test_panel_aligns_and_returns_closes(tmp_path):
    _archive(tmp_path, "AAA", [
        {"date": "2026-01-02", "close": 100, "volume": 10},
        {"date": "2026-01-05", "close": 110, "volume": 12},
    ])
    _archive(tmp_path, "BBB", [
        {"date": "2026-01-02", "close": 50, "volume": 5},
        {"date": "2026-01-05", "close": 55, "volume": 6},
    ])
    panel = load_price_panel(["AAA", "BBB"], tmp_path)
    assert panel.tickers == ["AAA", "BBB"]
    assert panel.close("AAA", "2026-01-05") == 110
    assert panel.missing == []


def test_forward_fill_within_gap(tmp_path):
    _archive(tmp_path, "AAA", [
        {"date": "2026-01-02", "close": 100, "volume": 10},
        {"date": "2026-01-06", "close": 120, "volume": 12},
    ])
    _archive(tmp_path, "BBB", [
        {"date": "2026-01-02", "close": 50, "volume": 5},
        {"date": "2026-01-03", "close": 51, "volume": 5},
        {"date": "2026-01-06", "close": 60, "volume": 6},
    ])
    panel = load_price_panel(["AAA", "BBB"], tmp_path, max_ffill_days=5)
    # AAA has no 2026-01-03 row → forward-filled from 2026-01-02
    assert panel.close("AAA", "2026-01-03") == 100


def test_missing_ticker_recorded(tmp_path):
    _archive(tmp_path, "AAA", [{"date": "2026-01-02", "close": 100, "volume": 1}])
    panel = load_price_panel(["AAA", "ZZZ"], tmp_path)
    assert "ZZZ" in panel.missing
    assert "AAA" in panel.tickers


def test_monthly_returns(tmp_path):
    _archive(tmp_path, "AAA", [
        {"date": "2026-01-30", "close": 100, "volume": 1},
        {"date": "2026-02-27", "close": 110, "volume": 1},
        {"date": "2026-03-31", "close": 99, "volume": 1},
    ])
    panel = load_price_panel(["AAA"], tmp_path)
    months, matrix = panel.monthly_returns(["AAA"])
    assert months == ["2026-02-27", "2026-03-31"]
    assert abs(matrix[0][0] - 0.10) < 1e-9      # 100 → 110
    assert abs(matrix[1][0] - (99 / 110 - 1)) < 1e-9
