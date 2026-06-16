"""Flock Intelligence metrics — pure, transparent, I/O-free.

"Flocking" = tickers in a theme/sector moving together (herding, cascades,
shared narratives). "Dispersion" = that shared movement breaking down.

Every score here is a documented weighted blend of normalized 0..1 components,
so the downstream classification is fully explainable. No file I/O, no network,
no external data deps beyond stdlib + numpy (already a project dependency, used
only for a vectorized correlation). All functions are defensive: missing/short
inputs degrade to neutral values, never raise.

Inputs are plain dicts keyed by ticker so the math is decoupled from where the
data comes from (the producer maps real artifacts onto these). Conventions:
  * returns_by_ticker:   {ticker: [r0, r1, ...]}  time-ordered recent returns
  * velocity_by_ticker:  {ticker: float}          mention-velocity z-score-ish
  * breadth_by_ticker:   {ticker: int}            distinct sources/authors
  * mention_by_ticker:   {ticker: float}          current mention volume
"""
from __future__ import annotations

from statistics import pstdev
from typing import Mapping, Sequence

# ---------------------------------------------------------------------------
# Normalization thresholds (documented so scores are explainable)
# ---------------------------------------------------------------------------

# A mention-velocity z-score of this magnitude maps to a normalized 1.0.
VELOCITY_FULL_Z = 2.0
# A cross-ticker return spread (stdev, in pct points) of this maps to 1.0.
RETURN_SPREAD_FULL_PP = 4.0
# A volatility change ratio of +this (e.g. +100%) maps to 1.0.
VOL_CHANGE_FULL = 1.0
# Minimum aligned points needed for a meaningful correlation.
MIN_CORR_POINTS = 3


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return lo


def _norm(value: float, full: float) -> float:
    """Linear-clamp |value|/full into 0..1 (transparent normalizer)."""
    if full <= 0:
        return 0.0
    return _clamp(abs(float(value)) / full)


def pairwise_correlation(a: Sequence[float], b: Sequence[float]) -> float | None:
    """Pearson correlation of two aligned series. None if too short / zero variance."""
    n = min(len(a), len(b))
    if n < MIN_CORR_POINTS:
        return None
    xa, xb = list(a[-n:]), list(b[-n:])
    ma, mb = sum(xa) / n, sum(xb) / n
    cov = sum((xa[i] - ma) * (xb[i] - mb) for i in range(n))
    va = sum((xa[i] - ma) ** 2 for i in range(n))
    vb = sum((xb[i] - mb) ** 2 for i in range(n))
    if va <= 0 or vb <= 0:
        return None
    return _clamp(cov / (va ** 0.5 * vb ** 0.5), -1.0, 1.0)


def average_pairwise_correlation(returns_by_ticker: Mapping[str, Sequence[float]]) -> float | None:
    """Mean Pearson correlation across all ticker pairs in the group.

    Returns None when fewer than 2 tickers have usable series. Higher = the
    group is moving together (cohesive flock).
    """
    series = {t: list(r) for t, r in (returns_by_ticker or {}).items()
              if isinstance(r, (list, tuple)) and len(r) >= MIN_CORR_POINTS}
    syms = sorted(series)
    if len(syms) < 2:
        return None
    corrs: list[float] = []
    for i in range(len(syms)):
        for j in range(i + 1, len(syms)):
            c = pairwise_correlation(series[syms[i]], series[syms[j]])
            if c is not None:
                corrs.append(c)
    if not corrs:
        return None
    return sum(corrs) / len(corrs)


def crowd_velocity(velocity_by_ticker: Mapping[str, float]) -> float:
    """Mean mention-velocity across the group (signed; can be negative)."""
    vals = [float(v) for v in (velocity_by_ticker or {}).values()
            if isinstance(v, (int, float))]
    return sum(vals) / len(vals) if vals else 0.0


def crowd_breadth(velocity_by_ticker: Mapping[str, float], group_size: int) -> float:
    """Fraction of the group actively participating (velocity > 0). 0..1.

    Broad participation (many names rising) => high breadth; attention in one
    name => low breadth.
    """
    if group_size <= 0:
        return 0.0
    participating = sum(1 for v in (velocity_by_ticker or {}).values()
                        if isinstance(v, (int, float)) and v > 0)
    return _clamp(participating / group_size)


def source_breadth(breadth_by_ticker: Mapping[str, int]) -> float:
    """Mean distinct-source/author count across the group (raw count, >=0)."""
    vals = [float(v) for v in (breadth_by_ticker or {}).values()
            if isinstance(v, (int, float))]
    return sum(vals) / len(vals) if vals else 0.0


def mention_concentration(mention_by_ticker: Mapping[str, float]) -> float:
    """Herfindahl-Hirschman index of mention shares across the group.

    Range (1/n .. 1]. ~1/n = attention spread evenly; ~1 = concentrated in a
    single name. High concentration alongside high velocity is an exhaustion tell.
    """
    counts = [max(0.0, float(v)) for v in (mention_by_ticker or {}).values()
              if isinstance(v, (int, float))]
    total = sum(counts)
    if total <= 0 or not counts:
        return 0.0
    return _clamp(sum((c / total) ** 2 for c in counts))


def return_spread(latest_return_by_ticker: Mapping[str, float]) -> float:
    """Population stdev of the latest return across the group (pct points).

    Rising spread = leaders separating from laggards = dispersion.
    """
    vals = [float(v) for v in (latest_return_by_ticker or {}).values()
            if isinstance(v, (int, float))]
    if len(vals) < 2:
        return 0.0
    return pstdev(vals)


def group_momentum(latest_return_by_ticker: Mapping[str, float]) -> float:
    """Mean latest return across the group (signed pct points). Price confirmation."""
    vals = [float(v) for v in (latest_return_by_ticker or {}).values()
            if isinstance(v, (int, float))]
    return sum(vals) / len(vals) if vals else 0.0


def group_volatility(returns_by_ticker: Mapping[str, Sequence[float]]) -> float:
    """Mean per-ticker return volatility (stdev of each ticker's series)."""
    vols: list[float] = []
    for r in (returns_by_ticker or {}).values():
        if isinstance(r, (list, tuple)) and len(r) >= 2:
            vols.append(pstdev([float(x) for x in r]))
    return sum(vols) / len(vols) if vols else 0.0


# ---------------------------------------------------------------------------
# Composite scores (each 0..1, transparent weighted blend)
# ---------------------------------------------------------------------------

def flock_score(*, velocity: float, breadth: float, avg_corr: float | None,
                momentum: float) -> float:
    """How strong/cohesive the flock is. 0..1.

    Higher when: crowd velocity rising, breadth broad, correlation high, and
    price momentum confirms. Correlation contributes only its positive half.
    """
    corr_pos = _clamp(avg_corr) if avg_corr is not None else 0.0
    return _clamp(
        0.30 * _norm(max(0.0, velocity), VELOCITY_FULL_Z)
        + 0.30 * _clamp(breadth)
        + 0.25 * corr_pos
        + 0.15 * _norm(max(0.0, momentum), RETURN_SPREAD_FULL_PP)
    )


def dispersion_score(*, avg_corr: float | None, prior_avg_corr: float | None,
                     ret_spread: float, breadth: float, concentration: float,
                     vol_change: float) -> float:
    """How much a previously-correlated group is now breaking apart. 0..1.

    Higher when: pairwise correlation falls, return spread across the group
    widens, breadth weakens while attention concentrates, and volatility rises.
    """
    # Falling correlation: use the drop from prior if available, else (1 - corr).
    if avg_corr is None:
        corr_break = 0.5  # unknown structure -> mid signal
    elif prior_avg_corr is not None and prior_avg_corr > avg_corr:
        corr_break = _clamp(prior_avg_corr - avg_corr)
    else:
        corr_break = _clamp(1.0 - _clamp(avg_corr, -1.0, 1.0))
    breadth_weak = _clamp(1.0 - _clamp(breadth))
    return _clamp(
        0.35 * corr_break
        + 0.25 * _norm(ret_spread, RETURN_SPREAD_FULL_PP)
        + 0.20 * (breadth_weak * _clamp(concentration))
        + 0.20 * _norm(max(0.0, vol_change), VOL_CHANGE_FULL)
    )


def exhaustion_score(*, velocity: float, concentration: float, breadth: float,
                     prior_breadth: float | None, vol_change: float,
                     momentum: float) -> float:
    """Crowd hot but confirmation weakening. 0..1.

    Higher when: mention velocity high, attention concentrated in a few leaders,
    breadth weakening, volatility rising, and price momentum fading/negative.
    """
    vel = _norm(max(0.0, velocity), VELOCITY_FULL_Z)
    breadth_decline = 0.0
    if prior_breadth is not None and prior_breadth > breadth:
        breadth_decline = _clamp(prior_breadth - breadth)
    momentum_fade = _clamp(-momentum / RETURN_SPREAD_FULL_PP)  # only negative momentum
    return _clamp(
        0.30 * vel
        + 0.25 * _clamp(concentration)
        + 0.20 * breadth_decline
        + 0.15 * _norm(max(0.0, vol_change), VOL_CHANGE_FULL)
        + 0.10 * momentum_fade
    )
