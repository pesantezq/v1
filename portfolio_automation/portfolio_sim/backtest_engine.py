"""
Historical backtest engine.

Runs a tactic under a rebalance policy over a window, tracking two value paths:
- **neutral** (start $1, no contributions) → time-weighted return + risk metrics,
- **DCA** (start at `start_value`, monthly contributions) → realistic dollar path.

Look-ahead safe: only reads closes for dates within the window, walked forward.
Missing tickers are dropped + renormalized and recorded in `degraded`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from portfolio_automation.portfolio_sim import metrics as M
from portfolio_automation.portfolio_sim.rebalance import RebalancePolicy
from portfolio_automation.portfolio_sim.tactics import Tactic
from portfolio_automation.portfolio_sim.windows import Window


@dataclass
class BacktestResult:
    tactic_id: str
    policy: str
    window_key: str
    metrics: dict[str, Any]
    value_series: list[dict[str, Any]] = field(default_factory=list)  # downsampled neutral path
    degraded: list[str] = field(default_factory=list)


def _avail_weights(weights: dict[str, float], panel, date: str) -> tuple[dict[str, float], list[str]]:
    """Keep only tickers with a price at `date`; renormalize; report dropped."""
    keep, dropped = {}, []
    for t, w in weights.items():
        if panel.close(t, date) is not None:
            keep[t] = w
        elif w > 0:
            dropped.append(t)
    tot = sum(keep.values())
    if tot > 0:
        keep = {k: v / tot for k, v in keep.items()}
    return keep, dropped


def _inject_cash(holdings: dict[str, float], target: dict[str, float], cash: float) -> dict[str, float]:
    new = dict(holdings)
    tw = {k: v for k, v in target.items() if v > 0}
    tot = sum(tw.values()) or 1.0
    for k, w in tw.items():
        new[k] = new.get(k, 0.0) + cash * (w / tot)
    return new


def run_backtest(
    tactic: Tactic,
    policy: RebalancePolicy,
    panel,
    window: Window,
    *,
    start_value: float = 10000.0,
    monthly_contribution: float = 0.0,
    benchmark_returns: dict[str, float] | None = None,
) -> BacktestResult:
    """Backtest `tactic` under `policy` over `window`. Never raises on data gaps."""
    benchmark_returns = benchmark_returns or {}
    dates = [d for d in panel.dates if window.start <= d <= window.end]
    if len(dates) < 2:
        return BacktestResult(tactic.tactic_id, policy.name, window.key,
                              {"status": "insufficient_data"}, [], [])

    ctx = {"panel": panel}
    t0 = dates[0]
    init_w, degraded = _avail_weights(tactic.target_weights_asof(t0, ctx), panel, t0)
    if not init_w:
        return BacktestResult(tactic.tactic_id, policy.name, window.key,
                              {"status": "insufficient_data"}, [], degraded)

    # Initial allocation for both paths.
    neutral = {t: 1.0 * w for t, w in init_w.items()}
    dca = {t: start_value * w for t, w in init_w.items()}
    last_rebal = t0
    contributed = start_value
    neutral_series, dca_series = [], []

    def record(date):
        nv = sum(neutral.values())
        dv = sum(dca.values())
        neutral_series.append((date, nv))
        dca_series.append((date, dv))

    record(t0)
    for i in range(1, len(dates)):
        prev, day = dates[i - 1], dates[i]
        # grow both paths by each ticker's daily return
        for book in (neutral, dca):
            for t in list(book.keys()):
                p0, p1 = panel.close(t, prev), panel.close(t, day)
                if p0 and p1 and p0 > 0:
                    book[t] *= p1 / p0
        month_boundary = day[:7] != prev[:7]
        cash_in = monthly_contribution if month_boundary else 0.0
        target, dropped = _avail_weights(tactic.target_weights_asof(day, ctx), panel, day)
        for d in dropped:
            if d not in degraded:
                degraded.append(d)
        if policy.due(day, last_rebal):
            neutral = policy.apply(neutral, target, day, 0.0)
            dca = policy.apply(dca, target, day, cash_in)
            last_rebal = day
        elif cash_in > 0:
            dca = _inject_cash(dca, target, cash_in)
        if cash_in > 0:
            contributed += cash_in
        record(day)

    neutral_vals = [v for _, v in neutral_series]
    dca_final = sum(dca.values())
    tw_ret = M.total_return(neutral_vals)
    metrics = {
        "status": "ok",
        "time_weighted_return": round(tw_ret, 6),
        "cagr": round(M.cagr(neutral_vals, window.years), 6),
        "annual_vol": round(M.annual_vol(neutral_vals), 6),
        "max_drawdown": round(M.max_drawdown(neutral_vals), 6),
        "sharpe": round(M.sharpe(neutral_vals), 4),
        "sortino": round(M.sortino(neutral_vals), 4),
        "final_balance_dca": round(dca_final, 2),
        "total_contributed": round(contributed, 2),
        "net_gain_dca": round(dca_final - contributed, 2),
        "excess_vs_spy": round(M.excess_return(tw_ret, benchmark_returns.get("SPY", 0.0)), 6),
        "excess_vs_qqq": round(M.excess_return(tw_ret, benchmark_returns.get("QQQ", 0.0)), 6),
        "window_label": window.label,
        "window_years": round(window.years, 3),
    }
    # downsample neutral series to ≤120 points for charting
    step = max(1, len(neutral_series) // 120)
    series = [{"date": d, "value": round(v, 6)} for d, v in neutral_series[::step]]
    return BacktestResult(tactic.tactic_id, policy.name, window.key, metrics, series, degraded)


def benchmark_total_return(panel, ticker: str, window: Window) -> float:
    """Window total return of a single benchmark ticker (for excess-vs-SPY)."""
    dates = [d for d in panel.dates if window.start <= d <= window.end]
    if len(dates) < 2:
        return 0.0
    p0, p1 = panel.close(ticker, dates[0]), panel.close(ticker, dates[-1])
    return (p1 / p0 - 1.0) if (p0 and p1 and p0 > 0) else 0.0
