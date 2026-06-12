"""
Price panel loader for the simulation suite.

Reads the 5y daily OHLCV archive (`outputs/backtest/historical/<T>_5y.json`),
aligns tickers onto a shared trading calendar (forward-fill ≤ max_gap days), and
exposes daily closes + a monthly-return matrix. Optional free FMP fallback for
tickers missing an archive. Reads HISTORICAL, never writes.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from portfolio_automation.historical_replay.replay_data_loader import normalize_prices

logger = logging.getLogger("stockbot.portfolio_sim.prices")

_ARCHIVE_REL = ("outputs", "backtest", "historical")
_MAX_FFILL_DAYS = 5


class PricePanel:
    """Calendar-aligned daily closes for a set of tickers."""

    def __init__(self, closes: dict[str, dict[str, float]], volumes: dict[str, dict[str, float]],
                 dates: list[str], missing: list[str]):
        self._closes = closes          # ticker -> {date: close} (forward-filled)
        self._volumes = volumes        # ticker -> {date: volume}
        self.dates = dates             # sorted union calendar (oldest-first)
        self.tickers = sorted(closes.keys())
        self.missing = missing

    def close(self, ticker: str, date: str) -> float | None:
        return self._closes.get(ticker.upper(), {}).get(date)

    def volume(self, ticker: str, date: str) -> float | None:
        return self._volumes.get(ticker.upper(), {}).get(date)

    def series(self, ticker: str) -> list[tuple[str, float]]:
        c = self._closes.get(ticker.upper(), {})
        return [(d, c[d]) for d in self.dates if d in c]

    def month_end_dates(self) -> list[str]:
        """Last available calendar date per (year, month), oldest-first."""
        by_month: dict[str, str] = {}
        for d in self.dates:
            by_month[d[:7]] = d  # dates sorted asc → last wins
        return [by_month[k] for k in sorted(by_month)]

    def monthly_returns(self, tickers: list[str] | None = None):
        """
        Return (months, matrix) where matrix[i] is the per-ticker simple return
        for month i (i≥1) vs the prior month-end close. Tickers lacking a close
        at a month-end contribute 0.0 for that month.
        """
        tickers = tickers or self.tickers
        mdates = self.month_end_dates()
        months: list[str] = []
        matrix: list[list[float]] = []
        for i in range(1, len(mdates)):
            prev, cur = mdates[i - 1], mdates[i]
            row: list[float] = []
            for t in tickers:
                p0 = self.close(t, prev)
                p1 = self.close(t, cur)
                row.append((p1 / p0 - 1.0) if (p0 and p1 and p0 > 0) else 0.0)
            months.append(cur)
            matrix.append(row)
        return months, matrix


def _load_archive_rows(root: Path, ticker: str) -> list[dict[str, Any]]:
    path = root.joinpath(*_ARCHIVE_REL, f"{ticker.upper()}_5y.json")
    try:
        doc = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []
    rows = doc.get("rows") if isinstance(doc, dict) else doc
    return normalize_prices(rows or [])  # oldest-first {date,open,high,low,close,volume}


def load_price_panel(
    tickers: list[str],
    root: str | Path,
    *,
    fmp_client: Any = None,
    max_ffill_days: int = _MAX_FFILL_DAYS,
) -> PricePanel:
    """Build a calendar-aligned panel for *tickers*. Never raises."""
    root = Path(root)
    raw: dict[str, list[dict[str, Any]]] = {}
    missing: list[str] = []

    for t in {x.upper() for x in tickers}:
        rows = _load_archive_rows(root, t)
        if not rows and fmp_client is not None:
            try:
                rows = normalize_prices(fmp_client.get_historical_prices(t, years=5) or [])
            except Exception as exc:  # pragma: no cover - network path
                logger.debug("portfolio_sim prices: FMP fallback failed for %s (%s)", t, exc)
                rows = []
        if rows:
            raw[t] = rows
        else:
            missing.append(t)

    # Union calendar.
    all_dates = sorted({r["date"] for rows in raw.values() for r in rows})
    closes: dict[str, dict[str, float]] = {}
    volumes: dict[str, dict[str, float]] = {}
    for t, rows in raw.items():
        by_date = {r["date"]: r for r in rows}
        c: dict[str, float] = {}
        v: dict[str, float] = {}
        last_close: float | None = None
        gap = 0
        for d in all_dates:
            if d in by_date:
                last_close = float(by_date[d]["close"])
                c[d] = last_close
                v[d] = float(by_date[d].get("volume", 0) or 0)
                gap = 0
            elif last_close is not None and gap < max_ffill_days:
                c[d] = last_close
                gap += 1
            else:
                gap += 1
        closes[t] = c
        volumes[t] = v

    return PricePanel(closes, volumes, all_dates, sorted(missing))
