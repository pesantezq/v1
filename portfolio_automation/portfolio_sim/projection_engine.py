"""
Forward Monte-Carlo projection via block bootstrap of historical monthly return
vectors. Sampling whole-month vectors preserves cross-asset correlation + fat
tails (no covariance estimate, no normality). Seeded → reproducible. numpy-backed.

Observe-only illustration, NOT a forecast.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class ProjectionResult:
    tactic_id: str
    horizon_months: int
    metrics: dict[str, Any]
    fan: list[dict[str, Any]] = field(default_factory=list)   # downsampled p5/p50/p95 over time
    degraded: list[str] = field(default_factory=list)


def _weight_vector(tactic_weights: dict[str, float], tickers: list[str]) -> tuple[np.ndarray, list[str]]:
    """Align tactic weights to the panel ticker order; drop+renormalize missing."""
    present = {t: w for t, w in tactic_weights.items() if t in tickers and w > 0}
    dropped = [t for t, w in tactic_weights.items() if w > 0 and t not in tickers]
    tot = sum(present.values())
    vec = np.zeros(len(tickers))
    if tot > 0:
        idx = {t: i for i, t in enumerate(tickers)}
        for t, w in present.items():
            vec[idx[t]] = w / tot
    return vec, dropped


def project(
    tactic_weights: dict[str, float],
    monthly_matrix: list[list[float]],
    panel_tickers: list[str],
    *,
    horizon_months: int,
    n_paths: int = 5000,
    start_value: float = 10000.0,
    monthly_contribution: float = 1000.0,
    seed: int = 12345,
    block: int = 1,
    target_cagr: float = 0.09,
    tactic_id: str = "tactic",
) -> ProjectionResult:
    """
    Block-bootstrap projection. `monthly_matrix[i]` = per-ticker return vector for
    historical month i (aligned to `panel_tickers`). Returns terminal-balance
    percentiles, prob-reach-target, prob-loss, drawdown distribution, and a fan.
    """
    R = np.asarray(monthly_matrix, dtype=float)
    if R.ndim != 2 or R.shape[0] < max(2, block) or horizon_months < 1:
        return ProjectionResult(tactic_id, horizon_months, {"status": "insufficient_data"}, [], [])

    w, dropped = _weight_vector(tactic_weights, panel_tickers)
    if w.sum() <= 0:
        return ProjectionResult(tactic_id, horizon_months, {"status": "insufficient_data"}, [], dropped)

    # Portfolio monthly return for each historical month.
    port_monthly = R @ w                       # shape (n_months,)
    n_months = port_monthly.shape[0]
    rng = np.random.default_rng(seed)
    n_blocks = (horizon_months + block - 1) // block
    max_start = n_months - block               # inclusive upper bound for a block start

    # Build path returns: (n_paths, horizon_months)
    starts = rng.integers(0, max_start + 1, size=(n_paths, n_blocks))
    # gather contiguous blocks
    offs = np.arange(block)
    idx = (starts[:, :, None] + offs[None, None, :]).reshape(n_paths, n_blocks * block)
    idx = idx[:, :horizon_months]
    path_returns = port_monthly[idx]            # (n_paths, horizon_months)

    growth = np.cumprod(1.0 + path_returns, axis=1)        # growth-of-$1 per month
    growth = np.concatenate([np.ones((n_paths, 1)), growth], axis=1)  # include t0

    # DCA dollar paths: start_value at t0 + monthly_contribution each month.
    # value_t = start_value*growth_t + sum_{k<=t} contrib * growth_t/growth_k
    final_growth = growth[:, -1]
    # contributions injected at months 1..horizon (end of each month)
    contrib_steps = np.arange(1, horizon_months + 1)
    g_at_contrib = growth[:, contrib_steps]                # (n_paths, horizon)
    dca_terminal = start_value * final_growth + \
        (monthly_contribution * (final_growth[:, None] / g_at_contrib)).sum(axis=1)
    total_contributed = start_value + monthly_contribution * horizon_months

    # Per-path max drawdown on the growth path.
    running_max = np.maximum.accumulate(growth, axis=1)
    drawdowns = (growth / running_max - 1.0).min(axis=1)   # ≤ 0

    years = horizon_months / 12.0
    cagr_paths = final_growth ** (1.0 / years) - 1.0
    target_balance = total_contributed * ((1 + target_cagr) ** years)

    def pct(a, q):
        return float(np.percentile(a, q))

    metrics = {
        "status": "ok",
        "n_paths": n_paths,
        "horizon_months": horizon_months,
        "seed": seed,
        "block_months": block,
        "p5_balance": round(pct(dca_terminal, 5), 2),
        "p25_balance": round(pct(dca_terminal, 25), 2),
        "p50_balance": round(pct(dca_terminal, 50), 2),
        "p75_balance": round(pct(dca_terminal, 75), 2),
        "p95_balance": round(pct(dca_terminal, 95), 2),
        "total_contributed": round(total_contributed, 2),
        "prob_reach_target": round(float((dca_terminal >= target_balance).mean()), 4),
        "prob_loss": round(float((dca_terminal < total_contributed).mean()), 4),
        "cagr_p5": round(pct(cagr_paths, 5), 6),
        "cagr_p50": round(pct(cagr_paths, 50), 6),
        "cagr_p95": round(pct(cagr_paths, 95), 6),
        "max_drawdown_p50": round(pct(drawdowns, 50), 6),
        "max_drawdown_p95": round(pct(drawdowns, 5), 6),  # 5th pct of (negative) DD = worst tail
        "growth_of_unit_p50": round(float(np.percentile(final_growth, 50)), 6),
    }
    # Fan: p5/p50/p95 of growth over time, downsampled to ≤60 points.
    step = max(1, (horizon_months + 1) // 60)
    fan = []
    for t in range(0, horizon_months + 1, step):
        col = growth[:, t]
        fan.append({"month": t, "p5": round(pct(col, 5), 4),
                    "p50": round(pct(col, 50), 4), "p95": round(pct(col, 95), 4)})
    return ProjectionResult(tactic_id, horizon_months, metrics, fan, dropped)
