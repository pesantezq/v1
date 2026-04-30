"""
Proxy decision simulator for historical replay v1.

Uses a deterministic momentum rule:
  - 5-day return > +3% and close > SMA20  → BUY
  - 5-day return < -3% and symbol is a current holding → SELL
  - 5-day return < -3% and symbol is not a holding     → WAIT
  - otherwise                                           → WAIT

This is NOT the live decision engine. It is an approximation designed to
generate a meaningful sample of historical outcomes for calibration purposes.
All rows are tagged source="historical_replay".
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger("stockbot.portfolio_automation.historical_replay.simulator")

SOURCE = "historical_replay"
STRATEGY_NAME = "historical_momentum_proxy"
BAND_NAME = "replay"

BUY_RETURN_THRESHOLD = 0.03    # +3 %
SELL_RETURN_THRESHOLD = -0.03  # -3 %
SMA_PERIOD = 20
RETURN_PERIOD = 5

# Minimum rows needed before generating a decision (SMA warmup)
_MIN_ROWS_NEEDED = SMA_PERIOD
# Rows reserved at the tail for forward outcome resolution
_FORWARD_RESERVE = 8


def _compute_features(closes: list[float], i: int) -> dict[str, Any] | None:
    """
    Compute lookback features at index i (inclusive).
    Returns None if there is insufficient history.
    """
    if i < RETURN_PERIOD or i < SMA_PERIOD - 1:
        return None

    current = closes[i]
    prior = closes[i - RETURN_PERIOD]
    return_5d = (current - prior) / prior if prior > 0 else 0.0

    window = closes[i - SMA_PERIOD + 1: i + 1]
    sma20 = sum(window) / len(window)
    above_sma20 = current > sma20

    return {
        "return_5d": round(return_5d, 6),
        "sma20": round(sma20, 4),
        "above_sma20": above_sma20,
    }


def _make_decision(
    features: dict[str, Any],
    is_holding: bool,
) -> tuple[str, float, str]:
    """Return (decision, confidence, reason)."""
    r5 = features["return_5d"]
    above = features["above_sma20"]

    if r5 > BUY_RETURN_THRESHOLD and above:
        confidence = round(min(0.75, 0.5 + r5 * 2), 4)
        reason = f"5d return {r5:+.1%} above SMA20 — momentum entry signal"
        return "BUY", confidence, reason

    if r5 < SELL_RETURN_THRESHOLD:
        confidence = round(min(0.75, 0.5 + abs(r5) * 2), 4)
        if is_holding:
            return "SELL", confidence, f"5d return {r5:+.1%} — negative momentum, current holding"
        return "WAIT", 0.4, f"5d return {r5:+.1%} — negative momentum, not a holding"

    return "WAIT", 0.3, f"5d return {r5:+.1%} — no strong signal"


def simulate_decisions(
    symbol: str,
    price_rows: list[dict[str, Any]],
    *,
    holding_symbols: frozenset[str] | None = None,
    days: int = 90,
    generated_at: str | None = None,
) -> list[dict[str, Any]]:
    """
    Generate replay decision rows for a single symbol.

    Works over the trailing `days` trading days, reserving the last
    _FORWARD_RESERVE rows for outcome resolution. Returns one decision
    dict per eligible date.
    """
    if not price_rows:
        return []

    is_holding = (symbol.upper() in (holding_symbols or frozenset()))
    ts = generated_at or datetime.now().isoformat()

    closes = [r["close"] for r in price_rows]
    dates = [r["date"] for r in price_rows]
    total = len(closes)

    # Last index eligible for a decision (reserve tail for forward resolution)
    max_i = total - _FORWARD_RESERVE - 1
    if max_i < SMA_PERIOD - 1:
        return []

    # First index eligible (enough lookback for SMA20, limited to `days` window)
    min_i = max(SMA_PERIOD - 1, max_i - days + 1)

    result: list[dict[str, Any]] = []
    for i in range(min_i, max_i + 1):
        features = _compute_features(closes, i)
        if features is None:
            continue

        decision, confidence, reason = _make_decision(features, is_holding)
        date_str = dates[i]

        result.append({
            "source": SOURCE,
            "run_id": f"historical_{date_str}",
            "date": date_str,
            "symbol": symbol.upper(),
            "decision": decision,
            "strategy": STRATEGY_NAME,
            "band": BAND_NAME,
            "confidence": confidence,
            "price_at_decision": round(closes[i], 4),
            "priority": 0.0,
            "validation_status": "historical_replay",
            "reason": reason,
            "lookback_features": features,
            "generated_at": ts,
            # Resolution fields — populated by replay_outcome_resolver
            "resolved": False,
            "resolved_at": None,
            "days_elapsed": None,
            "price_at_resolution": None,
            "return_pct": None,
            "direction_correct": None,
            "window_days": None,
            "outcome_price": None,
        })

    return result


def simulate_all_decisions(
    price_data: dict[str, list[dict[str, Any]]],
    *,
    holding_symbols: frozenset[str] | None = None,
    days: int = 90,
) -> list[dict[str, Any]]:
    """Simulate decisions for all symbols and return the combined list."""
    ts = datetime.now().isoformat()
    all_rows: list[dict[str, Any]] = []
    for symbol, price_rows in price_data.items():
        rows = simulate_decisions(
            symbol, price_rows,
            holding_symbols=holding_symbols,
            days=days,
            generated_at=ts,
        )
        all_rows.extend(rows)
        logger.debug("simulator: %s — %d decisions", symbol, len(rows))
    logger.info(
        "simulator: %d decisions across %d symbols",
        len(all_rows), len(price_data),
    )
    return all_rows
