"""
Score-calibration report for the weekly ETF bundle subsystem.

For each score bucket, reports matured count, relative + strong hit rates, mean +
median excess return, average drawdown, and a Wilson score interval (a free,
standard binomial uncertainty measure — no new dependency). Assigns a
calibration status and raises a warning when higher-score buckets fail to
outperform lower-score buckets over a sufficient sample.
"""
from __future__ import annotations

import statistics
from typing import Any

from portfolio_automation.weekly_etf_bundles.evaluation import (
    SCORE_BUCKETS,
    _score_bucket_label,
    STATUS_INSUFFICIENT as _EVAL_INSUFFICIENT,
)
from portfolio_automation.weekly_etf_bundles.outcomes import PRIMARY_HORIZON, STATUS_MATURED

WELL_CALIBRATED = "well_calibrated"
OVERCONFIDENT = "overconfident"
UNDERCONFIDENT = "underconfident"
NON_MONOTONIC = "non_monotonic"
INSUFFICIENT_SAMPLE = "insufficient_sample"

_DEFAULT_MIN_BUCKET_N = 20
_MONO_TOL = 0.05          # tolerance before an inversion counts as non-monotonic


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """Wilson score interval for a binomial proportion."""
    if n <= 0:
        return None
    phat = successes / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    margin = (z * ((phat * (1 - phat) + z * z / (4 * n)) / n) ** 0.5) / denom
    return (round(max(0.0, center - margin), 4), round(min(1.0, center + margin), 4))


def _bucket_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    rel = [bool(r["relative_hit"]) for r in rows if r.get("relative_hit") is not None]
    strong = [bool(r["strong_hit"]) for r in rows if r.get("strong_hit") is not None]
    excess = [r["excess_return"] for r in rows if r.get("excess_return") is not None]
    mdd = [r["max_drawdown_in_window"] for r in rows if r.get("max_drawdown_in_window") is not None]
    rel_hits = sum(rel)
    return {
        "count": n,
        "relative_hit_rate": round(rel_hits / len(rel), 4) if rel else None,
        "relative_hit_ci": wilson_interval(rel_hits, len(rel)) if rel else None,
        "strong_hit_rate": round(sum(strong) / len(strong), 4) if strong else None,
        "avg_excess_return": round(sum(excess) / len(excess), 6) if excess else None,
        "median_excess_return": round(statistics.median(excess), 6) if excess else None,
        "avg_max_drawdown": round(sum(mdd) / len(mdd), 6) if mdd else None,
    }


def build_calibration(
    matured_rows: list[dict[str, Any]],
    *,
    horizon: str = PRIMARY_HORIZON,
    min_bucket_n: int = _DEFAULT_MIN_BUCKET_N,
) -> dict[str, Any]:
    rows = [r for r in matured_rows
            if r.get("status") == STATUS_MATURED and r.get("horizon") == horizon]
    labels = [f"{lo}-{hi}" for lo, hi in SCORE_BUCKETS]  # high → low
    buckets = {
        lbl: _bucket_report([r for r in rows if _score_bucket_label(r.get("watch_score")) == lbl])
        for lbl in labels
    }

    # Qualifying buckets (enough sample) ordered high → low.
    qualifying = [(lbl, buckets[lbl]) for lbl in labels
                  if buckets[lbl]["count"] >= min_bucket_n
                  and buckets[lbl]["relative_hit_rate"] is not None]

    status, warning = _classify(qualifying)

    return {
        "horizon": horizon,
        "min_bucket_n": min_bucket_n,
        "total_matured": len(rows),
        "qualifying_bucket_count": len(qualifying),
        "buckets": buckets,
        "calibration_status": status,
        "higher_buckets_underperform_warning": warning,
    }


def _classify(qualifying: list[tuple[str, dict[str, Any]]]) -> tuple[str, bool]:
    """Return (status, higher_buckets_underperform_warning)."""
    if len(qualifying) < 2:
        return INSUFFICIENT_SAMPLE, False

    rates = [rep["relative_hit_rate"] for _, rep in qualifying]  # high-score → low-score
    top_rate, bottom_rate = rates[0], rates[-1]

    # Monotonic non-increasing (within tolerance) from high-score to low-score.
    inversion = any(rates[i] + _MONO_TOL < rates[i + 1] for i in range(len(rates) - 1))
    underperform = (top_rate - bottom_rate) <= 0

    if inversion:
        return NON_MONOTONIC, underperform
    # Monotonic, but does the top actually beat benchmark more than half the time?
    if top_rate < 0.50 and top_rate <= bottom_rate + _MONO_TOL:
        return OVERCONFIDENT, underperform
    if bottom_rate > 0.55 and bottom_rate >= top_rate - _MONO_TOL:
        return UNDERCONFIDENT, underperform
    return WELL_CALIBRATED, underperform
