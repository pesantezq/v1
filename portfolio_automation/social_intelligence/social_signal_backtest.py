"""
Social-signal backtest / efficacy evaluation.

Given historical (crowd_state, ticker, signal_date) observations and their forward
returns, evaluate each crowd state's forward performance against benchmarks
(SPY / QQQ / sector ETF / same-ticker baseline). The point is **gating**: no
crowd state may be treated as reliable below a minimum sample size; under-sampled
states are labeled ``insufficient_data`` and may influence only research priority.

Pure, deterministic, no network. Forward returns are supplied by the caller (the
orchestrator joins them from the price layer); this module only does the stats.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from portfolio_automation.social_intelligence.base import base_envelope

# Forward windows we evaluate.
HORIZONS = ("1D", "5D", "20D", "60D")


@dataclass
class SignalObservation:
    """One resolved historical signal with realized forward returns."""

    ticker: str
    crowd_state: str
    signal_date: str
    # Excess returns vs each benchmark, per horizon, e.g.
    # {"1D": {"vs_spy": 0.4, "vs_qqq": 0.2, "vs_sector": 0.1, "vs_self_baseline": 0.3}}
    returns: dict[str, dict[str, float]] = field(default_factory=dict)
    raw_returns: dict[str, float] = field(default_factory=dict)   # absolute, per horizon
    max_drawdown: float | None = None
    volatility: float | None = None


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _stdev(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def _confidence_bucket(n: int, min_sample: int) -> str:
    if n < min_sample:
        return "insufficient_data"
    if n < min_sample * 3:
        return "low"
    if n < min_sample * 6:
        return "medium"
    return "high"


def evaluate_state(
    observations: list[SignalObservation],
    *,
    min_sample: int = 20,
) -> dict[str, Any]:
    """Aggregate one crowd state's observations into a stats block."""
    n = len(observations)
    bucket = _confidence_bucket(n, min_sample)
    reliable = bucket not in ("insufficient_data",)

    per_horizon: dict[str, Any] = {}
    for h in HORIZONS:
        raw = [o.raw_returns[h] for o in observations if h in o.raw_returns]
        excess_spy = [o.returns[h]["vs_spy"] for o in observations
                      if h in o.returns and "vs_spy" in o.returns[h]]
        excess_qqq = [o.returns[h]["vs_qqq"] for o in observations
                      if h in o.returns and "vs_qqq" in o.returns[h]]
        excess_sector = [o.returns[h]["vs_sector"] for o in observations
                         if h in o.returns and "vs_sector" in o.returns[h]]
        wins = sum(1 for r in raw if r > 0)
        per_horizon[h] = {
            "mean_return": round(_mean(raw), 4),
            "mean_excess_vs_spy": round(_mean(excess_spy), 4) if excess_spy else None,
            "mean_excess_vs_qqq": round(_mean(excess_qqq), 4) if excess_qqq else None,
            "mean_excess_vs_sector": round(_mean(excess_sector), 4) if excess_sector else None,
            "hit_rate": round(wins / len(raw), 4) if raw else None,
            "volatility": round(_stdev(raw), 4) if raw else None,
            "sample_size": len(raw),
        }

    drawdowns = [o.max_drawdown for o in observations if o.max_drawdown is not None]
    # A "false positive" = a signal whose 5D raw return was negative (didn't pan out).
    fp_basis = [o.raw_returns.get("5D") for o in observations if "5D" in o.raw_returns]
    false_positives = sum(1 for r in fp_basis if r is not None and r <= 0)
    false_positive_rate = round(false_positives / len(fp_basis), 4) if fp_basis else None

    # Risk-adjusted score on the 20D horizon (mean/vol), only when reliable.
    h20 = per_horizon.get("20D", {})
    risk_adjusted = None
    if reliable and h20.get("volatility"):
        risk_adjusted = round((h20.get("mean_return") or 0.0) / h20["volatility"], 4)

    return {
        "sample_size": n,
        "confidence_bucket": bucket,
        "reliable": reliable,
        "by_horizon": per_horizon,
        "max_drawdown": round(min(drawdowns), 4) if drawdowns else None,
        "false_positive_rate": false_positive_rate,
        "risk_adjusted_score": risk_adjusted,
        "impact": "research_priority_only",   # never "confidence" until matured
    }


def build_social_signal_backtest(
    observations: list[SignalObservation],
    *,
    run_id: str,
    run_mode: str,
    min_sample: int = 20,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Build the ``social_signal_backtest.json`` payload, grouped by crowd state."""
    by_state: dict[str, list[SignalObservation]] = {}
    for o in observations:
        by_state.setdefault(o.crowd_state, []).append(o)

    states_block = {state: evaluate_state(obs, min_sample=min_sample)
                    for state, obs in by_state.items()}

    matured = [s for s, b in states_block.items() if b["reliable"]]
    data_quality = "ok" if matured else "insufficient_data"

    env = base_envelope(
        run_id=run_id,
        run_mode=run_mode,
        source_status="ok",
        data_quality_status=data_quality,
        warnings=warnings,
    )
    env.update({
        "min_sample": min_sample,
        "total_observations": len(observations),
        "states_evaluated": len(states_block),
        "states_matured": matured,
        "benchmarks": ["SPY", "QQQ", "sector_etf", "same_ticker_baseline"],
        "records": states_block,
    })
    return env
