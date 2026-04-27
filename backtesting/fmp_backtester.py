"""
FMP Backtester

Evaluates historical signal performance using FMP price data.

Usage:
    from backtesting.fmp_backtester import FMPBacktester
    from fmp_client import FMPClient

    fmp = FMPClient()
    bt = FMPBacktester(fmp)
    history = bt.get_historical_prices("AAPL", years=3)
    report  = bt.simulate_signal_performance(signals, forward_days=10)

Decision-support only.  No trading execution.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger("backtesting.fmp_backtester")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_date(raw: Any) -> Optional[date]:
    """Parse a date string to a date object; return None on failure."""
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, str):
        s = raw.strip()
        # All FMP and AV date strings start with YYYY-MM-DD (10 chars)
        if len(s) >= 10:
            try:
                return datetime.strptime(s[:10], "%Y-%m-%d").date()
            except ValueError:
                pass
    return None


def _safe_float(val: Any) -> Optional[float]:
    try:
        v = float(val)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _build_price_map(historical: list[dict]) -> dict[date, float]:
    """
    Build {date → close_price} mapping from FMP historical list.

    FMP returns data newest-first; we normalise to a dict keyed by date.
    Uses 'adjClose' when present, falls back to 'close'.
    """
    price_map: dict[date, float] = {}
    for row in historical:
        d = _parse_date(row.get("date"))
        if d is None:
            continue
        close = _safe_float(row.get("adjClose")) or _safe_float(row.get("close"))
        if close is not None and close > 0:
            price_map[d] = close
    return price_map


def _nearest_trading_date(
    target: date, price_map: dict[date, float], max_offset: int = 5
) -> Optional[date]:
    """
    Return the nearest available trading date at or after target.
    Looks up to max_offset days forward before giving up.
    """
    for offset in range(max_offset + 1):
        candidate = target + timedelta(days=offset)
        if candidate in price_map:
            return candidate
    return None


def _forward_return(
    entry_price: float,
    price_map: dict[date, float],
    entry_date: date,
    forward_days: int,
) -> Optional[float]:
    """
    Compute percentage return from entry_date + forward_days.
    Returns None when data is unavailable.
    """
    exit_date = _nearest_trading_date(
        entry_date + timedelta(days=forward_days), price_map
    )
    if exit_date is None:
        return None
    exit_price = price_map.get(exit_date)
    if not exit_price:
        return None
    return round((exit_price - entry_price) / entry_price * 100, 4)


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class FMPBacktester:
    """
    Evaluates historical signal performance using FMP price data.

    Args:
        fmp_client:     An initialised FMPClient instance.
        years_default:  Default look-back window for get_historical_prices().
    """

    def __init__(self, fmp_client: Any, years_default: int = 5) -> None:
        self._fmp = fmp_client
        self._years_default = years_default
        # Cache fetched price histories within a session to avoid duplicate calls
        self._price_cache: dict[str, dict[date, float]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_historical_prices(
        self,
        symbol: str,
        years: Optional[int] = None,
    ) -> list[dict]:
        """
        Return raw FMP historical price list for symbol.

        Each dict has: date, open, high, low, close, adjClose, volume, …

        Raises nothing — returns [] on any error.
        """
        years = years or self._years_default
        return self._fmp.get_historical_prices(symbol, years=years)

    def simulate_signal_performance(
        self,
        signals: list[dict],
        forward_days: int = 10,
        forward_days_long: int = 30,
    ) -> dict[str, Any]:
        """
        Evaluate forward returns for a list of historical signals.

        Args:
            signals:           List of signal dicts with keys:
                               - ticker / symbol
                               - scan_time / signal_date (ISO date or datetime)
                               - signal_score (optional)
                               - confidence_score (optional)
            forward_days:      Short-term look-forward window in calendar days.
            forward_days_long: Long-term look-forward window.

        Returns:
            {
              "total_signals":      int,
              "evaluated":          int,   # signals with price data
              "hit_rate":           float, # % signals with positive short-term return
              "avg_return":         float, # mean forward return (%)
              "avg_return_long":    float,
              "win_loss_ratio":     float,
              "max_drawdown":       float, # worst single signal outcome
              "results":            list[dict],
            }
        """
        if not signals:
            return self._empty_report()

        results: list[dict] = []
        returns_short: list[float] = []
        returns_long: list[float] = []

        for sig in signals:
            sym = str(sig.get("ticker") or sig.get("symbol") or "").upper()
            if not sym:
                continue

            sig_date_raw = sig.get("scan_time") or sig.get("signal_date")
            sig_date = _parse_date(sig_date_raw)
            if sig_date is None:
                continue

            price_map = self._get_price_map(sym)
            if not price_map:
                continue

            entry_date = _nearest_trading_date(sig_date, price_map)
            if entry_date is None:
                continue
            entry_price = price_map.get(entry_date)
            if not entry_price:
                continue

            ret_short = _forward_return(entry_price, price_map, entry_date, forward_days)
            ret_long  = _forward_return(entry_price, price_map, entry_date, forward_days_long)

            row: dict[str, Any] = {
                "ticker":          sym,
                "signal_date":     sig_date.isoformat(),
                "entry_date":      entry_date.isoformat(),
                "entry_price":     round(entry_price, 4),
                "signal_score":    _safe_float(sig.get("signal_score")),
                "confidence_score": _safe_float(sig.get("confidence_score")),
                f"return_{forward_days}d": ret_short,
                f"return_{forward_days_long}d": ret_long,
                "outcome": (
                    "win" if (ret_short or 0) > 0
                    else "loss" if (ret_short is not None and ret_short <= 0)
                    else "unknown"
                ),
            }
            results.append(row)

            if ret_short is not None:
                returns_short.append(ret_short)
            if ret_long is not None:
                returns_long.append(ret_long)

        evaluated = len(results)
        if not returns_short:
            return {
                **self._empty_report(),
                "total_signals": len(signals),
                "evaluated": evaluated,
                "results": results,
            }

        wins = [r for r in returns_short if r > 0]
        losses = [r for r in returns_short if r <= 0]
        hit_rate = round(len(wins) / len(returns_short) * 100, 2)
        avg_ret = round(sum(returns_short) / len(returns_short), 4)
        avg_ret_long = round(sum(returns_long) / len(returns_long), 4) if returns_long else 0.0
        win_loss = (
            round(abs(sum(wins) / len(wins)) / abs(sum(losses) / len(losses)), 4)
            if wins and losses and sum(losses) != 0
            else float("inf") if wins else 0.0
        )
        max_dd = round(min(returns_short), 4)

        logger.info(
            "Backtest: %d signals evaluated, hit_rate=%.1f%%, avg_ret=%.2f%%, win_loss=%.2f",
            evaluated, hit_rate, avg_ret, win_loss if win_loss != float("inf") else 99.9,
        )

        return {
            "total_signals":   len(signals),
            "evaluated":       evaluated,
            "hit_rate":        hit_rate,
            "avg_return":      avg_ret,
            "avg_return_long": avg_ret_long,
            "win_loss_ratio":  win_loss if win_loss != float("inf") else 99.0,
            "max_drawdown":    max_dd,
            "results":         results,
        }

    def evaluate_confidence_calibration(
        self,
        signals: list[dict],
        forward_days: int = 10,
        n_buckets: int = 5,
    ) -> dict[str, Any]:
        """
        Compare predicted confidence score vs actual forward return.

        Splits signals into confidence buckets and computes hit-rate and
        average return per bucket.  Well-calibrated signals show monotonic
        improvement from low → high confidence.

        Returns:
            {
              "buckets": [
                  {"label": "0-20", "count": int, "hit_rate": float, "avg_return": float},
                  ...
              ],
              "calibration_slope":  float,   # positive = higher conf → better outcomes
              "well_calibrated":    bool,
            }
        """
        perf = self.simulate_signal_performance(signals, forward_days=forward_days)
        evaluated = [r for r in perf.get("results", []) if r.get("confidence_score") is not None]

        if not evaluated:
            return {"buckets": [], "calibration_slope": 0.0, "well_calibrated": False}

        bucket_size = 100.0 / n_buckets
        buckets: list[dict] = []
        for i in range(n_buckets):
            lo = i * bucket_size
            hi = (i + 1) * bucket_size
            bucket_rows = [
                r for r in evaluated
                if lo <= (float(r["confidence_score"] or 0) * 100) < hi
            ]
            returns_in_bucket = [
                r[f"return_{forward_days}d"]
                for r in bucket_rows
                if r.get(f"return_{forward_days}d") is not None
            ]
            if returns_in_bucket:
                wins = [x for x in returns_in_bucket if x > 0]
                hit_rate = round(len(wins) / len(returns_in_bucket) * 100, 2)
                avg_ret = round(sum(returns_in_bucket) / len(returns_in_bucket), 4)
            else:
                hit_rate = 0.0
                avg_ret = 0.0
            buckets.append({
                "label":      f"{lo:.0f}-{hi:.0f}",
                "count":      len(bucket_rows),
                "hit_rate":   hit_rate,
                "avg_return": avg_ret,
            })

        # Calibration slope: do higher-confidence buckets outperform?
        hit_rates = [b["hit_rate"] for b in buckets if b["count"] > 0]
        if len(hit_rates) >= 2:
            n = len(hit_rates)
            x_mean = (n - 1) / 2.0
            y_mean = sum(hit_rates) / n
            num = sum((i - x_mean) * (hit_rates[i] - y_mean) for i in range(n))
            den = sum((i - x_mean) ** 2 for i in range(n))
            slope = round(num / den, 4) if den else 0.0
        else:
            slope = 0.0

        return {
            "buckets":            buckets,
            "calibration_slope":  slope,
            "well_calibrated":    slope > 0,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_price_map(self, symbol: str) -> dict[date, float]:
        """Return (cached) price map for symbol; fetches via FMP on miss."""
        if symbol not in self._price_cache:
            raw = self._fmp.get_historical_prices(symbol, years=self._years_default)
            self._price_cache[symbol] = _build_price_map(raw)
        return self._price_cache[symbol]

    @staticmethod
    def _empty_report() -> dict[str, Any]:
        return {
            "total_signals":   0,
            "evaluated":       0,
            "hit_rate":        0.0,
            "avg_return":      0.0,
            "avg_return_long": 0.0,
            "win_loss_ratio":  0.0,
            "max_drawdown":    0.0,
            "results":         [],
        }
