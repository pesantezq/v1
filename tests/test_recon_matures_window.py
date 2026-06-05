"""
F4 integration contract: reconstructing a multi-year archive produces signals
whose date span matures the walk-forward OOS window (folds_possible=true). This
is the whole point of sub-project F — getting real OOS evidence now instead of
waiting for ~315 live calendar days.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from backtesting.historical_signal_recon import reconstruct_universe
from backtesting.signal_sources import load_historical_signal_snapshots
from backtesting.walk_forward import oos_window_status


def _multiyear_archive(dirpath: Path, ticker: str, start: date, days: int):
    dirpath.mkdir(parents=True, exist_ok=True)
    rows, price = [], 100.0
    for i in range(days):
        d = start + timedelta(days=i)
        price *= 1.04 if i % 7 == 0 else 0.999  # periodic +4% → STRONG_MOVE signals
        rows.append({"date": d.isoformat(), "close": round(price, 2), "volume": 1_000_000})
    (dirpath / f"{ticker}_5y.json").write_text(json.dumps({"symbol": ticker, "rows": rows}))


def test_reconstruction_matures_oos_window(tmp_path):
    arch = tmp_path / "historical"
    _multiyear_archive(arch, "AAA", date(2024, 1, 1), 500)  # > 315-day span
    recon = tmp_path / "recon"
    reconstruct_universe(str(arch), str(recon))
    signals = load_historical_signal_snapshots(str(recon))
    assert len(signals) > 0
    ow = oos_window_status(signals, today=date(2026, 6, 5))
    assert ow["folds_possible"] is True
    assert ow["calendar_days_observed"] >= 315
