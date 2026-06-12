"""
Return / risk metrics for the simulation suite. Pure functions, numpy-backed.

Time-weighted return uses the contribution-neutral value path; the dollar (DCA)
terminal balance is computed separately by the engine. The operator objective is
excess return vs SPY, so `excess_return` is a first-class metric here.
"""
from __future__ import annotations

import math

import numpy as np

TRADING_DAYS = 252


def total_return(values: list[float]) -> float:
    if len(values) < 2 or values[0] <= 0:
        return 0.0
    return values[-1] / values[0] - 1.0


def cagr(values: list[float], years: float) -> float:
    if len(values) < 2 or values[0] <= 0 or years <= 0:
        return 0.0
    return (values[-1] / values[0]) ** (1.0 / years) - 1.0


def daily_returns(values: list[float]) -> list[float]:
    out: list[float] = []
    for i in range(1, len(values)):
        if values[i - 1] > 0:
            out.append(values[i] / values[i - 1] - 1.0)
    return out


def annual_vol(values: list[float], periods_per_year: int = TRADING_DAYS) -> float:
    r = daily_returns(values)
    if len(r) < 2:
        return 0.0
    return float(np.std(r, ddof=1) * math.sqrt(periods_per_year))


def max_drawdown(values: list[float]) -> float:
    """Most negative peak-to-trough drawdown (≤ 0)."""
    peak = -math.inf
    mdd = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    return mdd


def sharpe(values: list[float], rf_annual: float = 0.0, periods_per_year: int = TRADING_DAYS) -> float:
    r = daily_returns(values)
    if len(r) < 2:
        return 0.0
    rf_per = rf_annual / periods_per_year
    excess = np.array(r) - rf_per
    sd = np.std(excess, ddof=1)
    if sd == 0:
        return 0.0
    return float(np.mean(excess) / sd * math.sqrt(periods_per_year))


def sortino(values: list[float], rf_annual: float = 0.0, periods_per_year: int = TRADING_DAYS) -> float:
    r = daily_returns(values)
    if len(r) < 2:
        return 0.0
    rf_per = rf_annual / periods_per_year
    excess = np.array(r) - rf_per
    downside = excess[excess < 0]
    dd = np.std(downside, ddof=1) if len(downside) >= 2 else 0.0
    if dd == 0:
        return 0.0
    return float(np.mean(excess) / dd * math.sqrt(periods_per_year))


def excess_return(tactic_total_return: float, benchmark_total_return: float) -> float:
    """Excess of the tactic over a benchmark (the operator's beat-SPY objective)."""
    return tactic_total_return - benchmark_total_return


def dca_terminal(growth_of_unit: list[float], contributions: list[tuple[int, float]]) -> tuple[float, float]:
    """
    Terminal balance of a dollar-cost-averaging schedule.

    `growth_of_unit[i]` = value at step i of $1 invested at step 0 (the
    contribution-neutral path). `contributions` = list of (step_index, amount)
    injected at that step. Each contribution grows by the ratio
    growth[-1]/growth[step]. Returns (terminal_balance, total_contributed).
    """
    if not growth_of_unit:
        return 0.0, 0.0
    final = growth_of_unit[-1]
    total = 0.0
    balance = 0.0
    for step, amt in contributions:
        step = max(0, min(step, len(growth_of_unit) - 1))
        g = growth_of_unit[step]
        if g > 0:
            balance += amt * (final / g)
        total += amt
    return balance, total
