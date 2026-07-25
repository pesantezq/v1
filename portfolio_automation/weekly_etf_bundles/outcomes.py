"""
Forward outcome maturation for frozen weekly ETF predictions.

Matures each prediction at 1, 4, 12, and 26 weeks (4-week is the primary
horizon). For every matured prediction it computes the ETF/benchmark forward
returns, excess return, directional/relative/strong/neutral/miss classification,
max favorable/adverse excursion, realized volatility, and max drawdown over the
evaluation window.

Two invariants:
  * NO FUTURE LEAK — the entry is the frozen close at market_data_date; the exit
    is the close on-or-before (market_data_date + horizon). Only prices strictly
    after the prediction date are used for the outcome.
  * MISSING IS NOT A MISS — if the horizon has not elapsed yet the outcome is
    "pending"; if endpoint prices are unavailable it is "unresolvable". Neither
    is ever scored as a miss.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

from portfolio_automation.weekly_etf_bundles.analysis import last_on_or_before

# Horizon → (weeks, strong-hit excess threshold, neutral band). Configurable.
# 4w is the primary evaluation horizon.
HORIZON_SPECS: dict[str, dict[str, Any]] = {
    "1w":  {"weeks": 1,  "strong": 0.010, "neutral_band": 0.010},
    "4w":  {"weeks": 4,  "strong": 0.020, "neutral_band": 0.020},
    "12w": {"weeks": 12, "strong": 0.040, "neutral_band": 0.020},
    "26w": {"weeks": 26, "strong": 0.060, "neutral_band": 0.020},
}
PRIMARY_HORIZON = "4w"

# outcome status vocabulary
STATUS_MATURED = "matured"
STATUS_PENDING = "pending"          # horizon not elapsed yet
STATUS_UNRESOLVABLE = "unresolvable"  # data missing/invalid — NOT a miss


def horizon_end_target(market_data_date: str, weeks: int) -> str:
    return (date.fromisoformat(market_data_date[:10]) + timedelta(weeks=weeks)).isoformat()


def _forward_metrics(
    panel: Any, symbol: str, entry_date: str, entry_price: float, end_date: str,
) -> dict[str, Any] | None:
    """Closes strictly after entry_date through end_date (inclusive). None if the
    window has no post-entry data or the entry price is invalid."""
    if not entry_price or entry_price <= 0:
        return None
    window = [(d, c) for (d, c) in panel.series(symbol) if entry_date < d <= end_date]
    if not window:
        return None
    exit_price = window[-1][1]
    fwd_return = exit_price / entry_price - 1.0
    rel = [c / entry_price - 1.0 for _, c in window]
    mfe = max(rel)
    mae = min(rel)
    # realized vol of daily returns across the window (entry price seeds day 0)
    closes = [entry_price] + [c for _, c in window]
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes)) if closes[i - 1] > 0]
    if len(rets) >= 2:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        realized_vol = math.sqrt(var) * math.sqrt(252)
    else:
        realized_vol = None
    # max drawdown across the window
    peak = entry_price
    mdd = 0.0
    for c in [entry_price] + [c for _, c in window]:
        if c > peak:
            peak = c
        if peak > 0:
            mdd = min(mdd, c / peak - 1.0)
    return {
        "exit_price": round(exit_price, 4),
        "exit_date": window[-1][0],
        "forward_return": fwd_return,
        "max_favorable_excursion": mfe,
        "max_adverse_excursion": mae,
        "realized_volatility": realized_vol,
        "max_drawdown": mdd,
        "observations": len(window),
    }


def classify_excess(excess: float, spec: dict[str, Any]) -> str:
    if excess > spec["strong"]:
        return "strong_hit"
    if excess < -spec["neutral_band"]:
        return "miss"
    return "neutral"


def mature_prediction(
    prediction: dict[str, Any],
    panel: Any,
    horizon_key: str,
    *,
    now_date: str,
    spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate one prediction at one horizon. Always returns a record with a
    `status`; only status==matured carries the metric fields. Never a silent
    miss for missing data."""
    spec = spec or HORIZON_SPECS[horizon_key]
    mdd = prediction["market_data_date"]
    symbol = prediction["symbol"]
    benchmark = prediction.get("benchmark") or symbol
    entry_price = prediction.get("price_at_prediction")

    base = {
        "prediction_id": prediction["prediction_id"],
        "horizon": horizon_key,
        "market_data_date": mdd,
        "bundle_id": prediction.get("bundle_id"),
        "symbol": symbol,
        "benchmark": benchmark,
        "watch_score": prediction.get("watch_score"),
        "label": prediction.get("label"),
        "rank_in_bundle": prediction.get("rank_in_bundle"),
        "rank_global": prediction.get("rank_global"),
        "strategy_variant": prediction.get("strategy_variant"),
        "config_version": prediction.get("config_version"),
        "observe_only": True,
    }

    target = horizon_end_target(mdd, spec["weeks"])
    last_avail = panel.dates[-1] if getattr(panel, "dates", None) else None
    # Horizon not elapsed in the data yet → pending (NOT a miss).
    if last_avail is None or last_avail < target:
        return {**base, "status": STATUS_PENDING, "horizon_end_target": target,
                "last_available_date": last_avail}

    end_date = last_on_or_before(panel.dates, target)
    if end_date is None or end_date <= mdd or entry_price is None:
        return {**base, "status": STATUS_UNRESOLVABLE, "horizon_end_target": target,
                "reason": "no_post_entry_price" if entry_price is not None else "no_entry_price"}

    etf_fwd = _forward_metrics(panel, symbol, mdd, entry_price, end_date)
    if etf_fwd is None:
        return {**base, "status": STATUS_UNRESOLVABLE, "reason": "etf_window_empty"}

    # Benchmark forward return over the same window (entry = its close at mdd).
    bm_entry = None
    bm_series = [(d, c) for (d, c) in panel.series(benchmark) if d <= mdd]
    if bm_series:
        bm_entry = bm_series[-1][1]
    bm_fwd = _forward_metrics(panel, benchmark, mdd, bm_entry, end_date) if bm_entry else None
    if bm_fwd is None:
        return {**base, "status": STATUS_UNRESOLVABLE, "reason": "no_benchmark_forward"}

    fwd = etf_fwd["forward_return"]
    bm_ret = bm_fwd["forward_return"]
    excess = fwd - bm_ret
    result = classify_excess(excess, spec)
    # data validity across the window: continuous coverage from entry to exit
    data_valid = etf_fwd["observations"] >= max(1, spec["weeks"])

    return {
        **base,
        "status": STATUS_MATURED,
        "horizon_end_target": target,
        "evaluation_end_date": end_date,
        "entry_price": round(entry_price, 4),
        "exit_price": etf_fwd["exit_price"],
        "forward_return": fwd,
        "benchmark_forward_return": bm_ret,
        "excess_return": excess,
        "directional_hit": fwd > 0,
        "relative_hit": fwd > bm_ret,
        "strong_hit": result == "strong_hit",
        "neutral": result == "neutral",
        "miss": result == "miss",
        "result_class": result,
        "max_favorable_excursion": etf_fwd["max_favorable_excursion"],
        "max_adverse_excursion": etf_fwd["max_adverse_excursion"],
        "realized_volatility": etf_fwd["realized_volatility"],
        "max_drawdown_in_window": etf_fwd["max_drawdown"],
        "data_valid_throughout": data_valid,
    }


def mature_bundle_toprank(
    matured: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Per (bundle, horizon): did the top-ranked ETF (rank_in_bundle==1) beat the
    bundle median forward return? Only over matured rows. Missing → excluded."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in matured:
        if r.get("status") != STATUS_MATURED:
            continue
        groups.setdefault((r["bundle_id"], r["horizon"]), []).append(r)
    out: list[dict[str, Any]] = []
    for (bundle_id, horizon), rows in groups.items():
        returns = sorted(x["forward_return"] for x in rows)
        n = len(returns)
        median = returns[n // 2] if n % 2 else (returns[n // 2 - 1] + returns[n // 2]) / 2
        top = next((x for x in rows if x.get("rank_in_bundle") == 1), None)
        out.append({
            "bundle_id": bundle_id,
            "horizon": horizon,
            "n": n,
            "bundle_median_forward_return": median,
            "top_symbol": top["symbol"] if top else None,
            "top_forward_return": top["forward_return"] if top else None,
            "top_beat_median": (top["forward_return"] > median) if top else None,
        })
    return out
