"""
Walk-forward out-of-sample validation — the anti-overfitting check.

For a parameterized tactic: choose params on a train window, evaluate on the next
test window, roll forward. Aggregate OOS results and the in-sample minus
out-of-sample excess-vs-SPY gap → an `overfit` score that the master strategy
score penalizes. Params are chosen ONLY from train-window data (look-ahead safe).
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable

from portfolio_automation.portfolio_sim.backtest_engine import benchmark_total_return, run_backtest
from portfolio_automation.portfolio_sim.rebalance import make_policy
from portfolio_automation.portfolio_sim.windows import Window


def _win(start: str, end: str) -> Window:
    yrs = max((date.fromisoformat(end) - date.fromisoformat(start)).days / 365.25, 1e-9)
    return Window("wf", "wf", start, end, yrs)


def walk_forward(
    build_fn: Callable[[dict], Any],
    param_grid: list[dict],
    panel,
    *,
    benchmark: str = "SPY",
    train_months: int = 24,
    test_months: int = 3,
) -> dict[str, Any]:
    """
    Roll train→test across the panel calendar. `build_fn(params)` → a Tactic.
    Returns OOS aggregates + the IS−OOS gap. `status: no_params` if grid is empty.
    """
    if not param_grid:
        return {"status": "no_params"}
    mdates = panel.month_end_dates()
    if len(mdates) < train_months + test_months + 1:
        return {"status": "insufficient_data", "months_available": len(mdates)}

    pol = make_policy("periodic")
    is_excess, oos_excess = [], []
    splits = 0
    i = train_months
    while i + test_months <= len(mdates):
        tr = _win(mdates[i - train_months], mdates[i - 1])
        te = _win(mdates[i], mdates[min(i + test_months, len(mdates) - 1)])
        bench_tr = {benchmark: benchmark_total_return(panel, benchmark, tr)}
        bench_te = {benchmark: benchmark_total_return(panel, benchmark, te)}

        # choose params on the train window by excess-vs-SPY
        best_params, best_train = None, -1e18
        for params in param_grid:
            r = run_backtest(build_fn(params), pol, panel, tr, benchmark_returns=bench_tr)
            if r.metrics.get("status") == "ok" and r.metrics["excess_vs_spy"] > best_train:
                best_train, best_params = r.metrics["excess_vs_spy"], params
        if best_params is None:
            i += test_months
            continue

        # evaluate the chosen params out-of-sample on the test window
        rt = run_backtest(build_fn(best_params), pol, panel, te, benchmark_returns=bench_te)
        if rt.metrics.get("status") == "ok":
            is_excess.append(best_train)
            oos_excess.append(rt.metrics["excess_vs_spy"])
            splits += 1
        i += test_months

    if splits == 0:
        return {"status": "insufficient_data", "splits": 0}

    is_mean = sum(is_excess) / len(is_excess)
    oos_mean = sum(oos_excess) / len(oos_excess)
    oos_hit = sum(1 for e in oos_excess if e > 0) / len(oos_excess)
    gap = is_mean - oos_mean   # positive = degrades out-of-sample (overfit)
    return {
        "status": "ok",
        "train_months": train_months, "test_months": test_months, "splits": splits,
        "is_mean_excess": round(is_mean, 6), "oos_mean_excess": round(oos_mean, 6),
        "oos_hit_rate": round(oos_hit, 4), "is_oos_gap": round(gap, 6),
        "overfit": round(max(0.0, gap), 6),
        "still_works_oos": bool(oos_mean > 0 and oos_hit >= 0.5),
    }
