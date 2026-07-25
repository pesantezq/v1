"""
Scorecard for the weekly ETF bundle subsystem — measures ranking quality, not
just raw directional accuracy.

Reuses nothing from production scoring. Spearman rank-correlation, information
coefficient, precision@K, and top-bottom spread are built here (the repo has no
numeric rank-IC). Every headline is gated behind a sample-status label so a thin
sample is never presented as reliable.
"""
from __future__ import annotations

import statistics
from typing import Any, Callable

from portfolio_automation.weekly_etf_bundles.outcomes import (
    PRIMARY_HORIZON,
    STATUS_MATURED,
    mature_bundle_toprank,
)

# Score buckets (high → low) for calibration + top/bottom spread.
SCORE_BUCKETS = ((80, 100), (65, 79), (45, 64), (30, 44), (0, 29))

# Sample-sufficiency defaults (configurable by caller).
DEFAULT_MIN_SAMPLE = 100
DEFAULT_MIN_WEEKS = 26

STATUS_INSUFFICIENT = "insufficient_sample"
STATUS_PROVISIONAL = "provisional"
STATUS_SUFFICIENT = "sufficient"


# --------------------------------------------------------------------------- #
# rank / correlation primitives (built new — no numeric rank-IC in the repo)
# --------------------------------------------------------------------------- #
def _ranks(values: list[float]) -> list[float]:
    """Average (fractional) ranks, ties averaged."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0  # 1-based average rank
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / (sxx ** 0.5 * syy ** 0.5)


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Rank correlation = Pearson on ranks."""
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    return pearson(_ranks(xs), _ranks(ys))


# --------------------------------------------------------------------------- #
# group statistics
# --------------------------------------------------------------------------- #
def _f(rows: list[dict[str, Any]], key: str) -> list[float]:
    return [r[key] for r in rows if r.get(key) is not None]


def _rate(rows: list[dict[str, Any]], key: str) -> float | None:
    present = [bool(r[key]) for r in rows if r.get(key) is not None]
    if not present:
        return None
    return round(sum(present) / len(present), 4)


def _mean(vals: list[float]) -> float | None:
    return round(sum(vals) / len(vals), 6) if vals else None


def _median(vals: list[float]) -> float | None:
    return round(statistics.median(vals), 6) if vals else None


def group_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    excess = _f(rows, "excess_return")
    return {
        "count": len(rows),
        "directional_hit_rate": _rate(rows, "directional_hit"),
        "relative_hit_rate": _rate(rows, "relative_hit"),
        "strong_hit_rate": _rate(rows, "strong_hit"),
        "avg_excess_return": _mean(excess),
        "median_excess_return": _median(excess),
        "avg_max_adverse_excursion": _mean(_f(rows, "max_adverse_excursion")),
        "avg_max_drawdown": _mean(_f(rows, "max_drawdown_in_window")),
    }


def _group_by(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        buckets.setdefault(str(r.get(key, "unknown")), []).append(r)
    return {k: group_stats(v) for k, v in sorted(buckets.items())}


def _score_bucket_label(score: float | None) -> str:
    if score is None:
        return "unknown"
    for lo, hi in SCORE_BUCKETS:
        if lo <= score <= hi:
            return f"{lo}-{hi}"
    return "unknown"


# --------------------------------------------------------------------------- #
# ranking-quality metrics
# --------------------------------------------------------------------------- #
def precision_at_k(rows: list[dict[str, Any]], k: int,
                   hit: Callable[[dict[str, Any]], bool] | None = None) -> float | None:
    """Mean over periods (market_data_date) of (#hits in the top-k by rank_global
    / k). Only periods with >= k matured predictions count. hit defaults to
    relative_hit (beat benchmark)."""
    hit = hit or (lambda r: bool(r.get("relative_hit")))
    by_date: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_date.setdefault(r["market_data_date"], []).append(r)
    per_period: list[float] = []
    for _, day_rows in by_date.items():
        if len(day_rows) < k:
            continue
        ranked = sorted(day_rows, key=lambda r: (r.get("rank_global") or 1e9))
        top = ranked[:k]
        per_period.append(sum(1 for r in top if hit(r)) / k)
    return round(sum(per_period) / len(per_period), 4) if per_period else None


def information_coefficient(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-period Spearman(watch_score, excess_return), averaged (the IC), plus
    its stability (stdev across periods)."""
    by_date: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if r.get("watch_score") is not None and r.get("excess_return") is not None:
            by_date.setdefault(r["market_data_date"], []).append(r)
    per_period: list[float] = []
    for _, day_rows in by_date.items():
        rho = spearman([float(r["watch_score"]) for r in day_rows],
                       [float(r["excess_return"]) for r in day_rows])
        if rho is not None:
            per_period.append(rho)
    if not per_period:
        return {"information_coefficient": None, "ic_stability": None, "periods": 0}
    ic = sum(per_period) / len(per_period)
    stability = statistics.pstdev(per_period) if len(per_period) > 1 else None
    return {
        "information_coefficient": round(ic, 4),
        "ic_stability": round(stability, 4) if stability is not None else None,
        "periods": len(per_period),
    }


def top_bottom_spread(rows: list[dict[str, Any]]) -> float | None:
    """Mean excess return of the top score bucket minus the bottom score bucket."""
    top = [r for r in rows if _score_bucket_label(r.get("watch_score")) == "80-100"]
    bot = [r for r in rows if _score_bucket_label(r.get("watch_score")) == "0-29"]
    tm = _mean(_f(top, "excess_return"))
    bm = _mean(_f(bot, "excess_return"))
    if tm is None or bm is None:
        return None
    return round(tm - bm, 6)


def _weeks_span(dates: list[str]) -> int:
    if len(dates) < 2:
        return 0
    from datetime import date
    lo, hi = min(dates), max(dates)
    return (date.fromisoformat(hi) - date.fromisoformat(lo)).days // 7


def sample_status(count: int, weeks: int, *,
                  min_sample: int = DEFAULT_MIN_SAMPLE,
                  min_weeks: int = DEFAULT_MIN_WEEKS) -> str:
    if count >= min_sample and weeks >= min_weeks:
        return STATUS_SUFFICIENT
    if count == 0:
        return STATUS_INSUFFICIENT
    if count >= max(20, min_sample // 5) and weeks >= min_weeks // 2:
        return STATUS_PROVISIONAL
    return STATUS_INSUFFICIENT


# --------------------------------------------------------------------------- #
# scorecard
# --------------------------------------------------------------------------- #
def build_scorecard(
    matured_rows: list[dict[str, Any]],
    *,
    primary_horizon: str = PRIMARY_HORIZON,
    min_sample: int = DEFAULT_MIN_SAMPLE,
    min_weeks: int = DEFAULT_MIN_WEEKS,
) -> dict[str, Any]:
    """Build the scorecard from matured outcome rows (all horizons). Headline
    metrics are computed on the primary horizon; per-horizon breakdown covers
    all. Nothing is presented as reliable below the sample thresholds."""
    matured = [r for r in matured_rows if r.get("status") == STATUS_MATURED]
    primary = [r for r in matured if r.get("horizon") == primary_horizon]

    dates = sorted({r["market_data_date"] for r in primary})
    weeks = _weeks_span(dates)
    status = sample_status(len(primary), weeks, min_sample=min_sample, min_weeks=min_weeks)

    ic = information_coefficient(primary)
    toprank = mature_bundle_toprank(primary)
    top_beat = [t["top_beat_median"] for t in toprank if t.get("top_beat_median") is not None]
    top_vs_median_rate = round(sum(1 for x in top_beat if x) / len(top_beat), 4) if top_beat else None

    overall = group_stats(primary)

    return {
        "primary_horizon": primary_horizon,
        "sample_status": status,
        "matured_prediction_count": len(primary),
        "distinct_weeks": len(dates),
        "calendar_weeks_span": weeks,
        "min_sample_required": min_sample,
        "min_weeks_required": min_weeks,
        # headline (interpret only when sample_status == sufficient)
        "directional_hit_rate": overall["directional_hit_rate"],
        "benchmark_relative_hit_rate": overall["relative_hit_rate"],
        "strong_hit_rate": overall["strong_hit_rate"],
        "avg_excess_return": overall["avg_excess_return"],
        "median_excess_return": overall["median_excess_return"],
        "precision_at_1": precision_at_k(primary, 1),
        "precision_at_3": precision_at_k(primary, 3),
        "precision_at_5": precision_at_k(primary, 5),
        "top_ranked_vs_bundle_median_hit_rate": top_vs_median_rate,
        "top_bottom_score_spread": top_bottom_spread(primary),
        "spearman_rank_correlation": spearman(
            [float(r["watch_score"]) for r in primary if r.get("watch_score") is not None
             and r.get("excess_return") is not None],
            [float(r["excess_return"]) for r in primary if r.get("watch_score") is not None
             and r.get("excess_return") is not None],
        ),
        "information_coefficient": ic["information_coefficient"],
        "ic_stability": ic["ic_stability"],
        "avg_max_adverse_excursion": overall["avg_max_adverse_excursion"],
        "avg_max_drawdown": overall["avg_max_drawdown"],
        # breakdowns
        "by_bundle": _group_by(primary, "bundle_id"),
        "by_etf": _group_by(primary, "symbol"),
        "by_score_bucket": {
            b: group_stats([r for r in primary if _score_bucket_label(r.get("watch_score")) == b])
            for b in [f"{lo}-{hi}" for lo, hi in SCORE_BUCKETS]
        },
        "by_label": _group_by(primary, "label"),
        "by_market_regime": _group_by(primary, "market_regime"),
        "by_volatility_regime": _group_by(primary, "volatility_regime"),
        "by_strategy_version": _group_by(primary, "strategy_variant"),
        "by_horizon": _group_by(matured, "horizon"),
    }
