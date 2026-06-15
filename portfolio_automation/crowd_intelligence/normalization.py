"""Pure scoring helpers: clamp, winsorize, composite, confidence.

All scores live in [-1, 1]; confidence in [0, 1]. Context-only — these numbers
never create or modify a BUY/SELL/HOLD decision.
"""
from __future__ import annotations

from statistics import pstdev

# Composite weights. social_sentiment is 0.0 because direct social is PLAN_LOCKED
# on the current FMP plan (Phase 1 probe). Weights sum to 1.0.
WEIGHTS: dict[str, float] = {
    "news": 0.25,
    "analyst": 0.25,
    "insider": 0.15,
    "congress": 0.10,
    "attention": 0.25,
    "social_sentiment": 0.00,
}


def clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 0.0
    if x != x:  # NaN
        return 0.0
    return max(lo, min(hi, x))


def clamp01(x: float) -> float:
    return clamp(x, 0.0, 1.0)


def winsorize(values: list[float], p: float = 0.90) -> list[float]:
    """Cap magnitudes at the p-th percentile so one extreme can't dominate."""
    nums = [float(v) for v in values if isinstance(v, (int, float))]
    if not nums:
        return []
    mags = sorted(abs(v) for v in nums)
    idx = min(len(mags) - 1, int(p * (len(mags) - 1)))
    cap = mags[idx] or (mags[-1] if mags else 0.0)
    out = []
    for v in nums:
        if cap and abs(v) > cap:
            out.append(cap if v > 0 else -cap)
        else:
            out.append(v)
    return out


def composite(category_scores: dict[str, float]) -> float:
    """Weighted sum over WEIGHTS; missing/None categories contribute neutral 0.0."""
    total = 0.0
    for cat, w in WEIGHTS.items():
        total += w * clamp(category_scores.get(cat, 0.0) or 0.0)
    return clamp(total)


def agreement(scores: list[float]) -> float:
    """1.0 when non-zero category scores agree; lower as they diverge. [0,1]."""
    nz = [s for s in scores if s]
    if len(nz) < 2:
        return 1.0 if nz else 0.0
    # population stdev of values in [-1,1] maxes near 1.0; invert + clamp.
    return clamp01(1.0 - pstdev(nz))


def confidence(*, coverage: float, freshness: float,
               agree: float, completeness: float) -> float:
    """Mean of the four signals, each in [0,1]."""
    parts = [clamp01(coverage), clamp01(freshness), clamp01(agree), clamp01(completeness)]
    return round(clamp01(sum(parts) / len(parts)), 4)
