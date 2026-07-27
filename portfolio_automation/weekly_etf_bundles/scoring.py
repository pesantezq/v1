"""
Transparent, deterministic 0-100 watch score + bundle-level analysis.

The score is fully explainable: every component is a bounded, absolute transform
of one point-in-time metric (no cross-sectional dependence, so a single ETF's
score is stable regardless of the rest of the universe — global/relative
normalization is a Strat Lab challenger variant, not the v1 baseline). Weights
and thresholds are parameterized so Strat Lab can tune them without touching this
math. Missing components are dropped and the remaining weights renormalized —
a missing metric is NEVER scored as zero.

Labels are INFORMATIONAL only (no action language):
  80-100 leading | 65-79 strengthening | 45-64 mixed | 30-44 weakening | 0-29 lagging
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

# v1 baseline component weights (must sum to 1.0).
DEFAULT_WEIGHTS: dict[str, float] = {
    "relative_strength_12w": 0.30,
    "momentum_4w": 0.20,
    "trend_structure": 0.20,
    "distance_from_52w_high": 0.10,
    "volatility_adjusted_return": 0.10,
    "drawdown_resilience": 0.10,
}

# Minimum fraction of total component weight that must be computable for a watch
# score to be emitted at all.
_MIN_AVAILABLE_WEIGHT = 0.5

_LABELS = (
    (80, "leading"),
    (65, "strengthening"),
    (45, "mixed"),
    (30, "weakening"),
    (0, "lagging"),
)


@dataclass(frozen=True)
class ScoringParams:
    """Tunable knobs. Strat Lab challengers vary these; v1 uses the defaults."""
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    # transform scales (exposed for challengers; v1 values chosen so ~+/-10-20%
    # metric excursions span the 0-100 range)
    rel_strength_scale: float = 400.0
    momentum_scale: float = 500.0
    dist_high_scale: float = 500.0
    vol_adj_scale: float = 25.0
    drawdown_scale: float = 200.0


def _clamp100(x: float) -> float:
    return max(0.0, min(100.0, x))


def label_for_score(score: float | None) -> str:
    if score is None:
        return "insufficient_data"
    for threshold, name in _LABELS:
        if score >= threshold:
            return name
    return "lagging"


def compute_components(metrics: dict[str, Any], params: ScoringParams) -> dict[str, float | None]:
    """Map point-in-time metrics → 0-100 components (or None if uncomputable)."""
    p = params
    rel12 = metrics.get("excess_return_12w")
    mom4 = metrics.get("return_4w")
    dist = metrics.get("distance_from_52w_high")
    vadj = metrics.get("volatility_adjusted_return")
    mdd = metrics.get("max_drawdown")
    a20, a50, a200 = metrics.get("above_sma20"), metrics.get("above_sma50"), metrics.get("above_sma200")

    # Trend structure: 25 pts per MA the price is above + 25 for bullish ordering.
    if None in (a20, a50, a200):
        trend = None
    else:
        trend = 25.0 * (int(bool(a20)) + int(bool(a50)) + int(bool(a200)))
        if metrics.get("ma_ordering") == "bullish":
            trend += 25.0
        trend = _clamp100(trend)

    return {
        "relative_strength_12w": _clamp100(50.0 + p.rel_strength_scale * rel12) if rel12 is not None else None,
        "momentum_4w": _clamp100(50.0 + p.momentum_scale * mom4) if mom4 is not None else None,
        "trend_structure": trend,
        "distance_from_52w_high": _clamp100(100.0 + p.dist_high_scale * dist) if dist is not None else None,
        "volatility_adjusted_return": _clamp100(50.0 + p.vol_adj_scale * vadj) if vadj is not None else None,
        "drawdown_resilience": _clamp100(100.0 + p.drawdown_scale * mdd) if mdd is not None else None,
    }


def score_etf(metrics: dict[str, Any], params: ScoringParams | None = None) -> dict[str, Any]:
    """Compute the watch score + components for one ETF's metrics."""
    params = params or ScoringParams()
    if not metrics.get("available"):
        return {
            "symbol": metrics.get("symbol"),
            "watch_score": None, "label": "insufficient_data",
            "components": {}, "available_weight": 0.0,
            "reason": metrics.get("reason", "unavailable"),
        }
    components = compute_components(metrics, params)
    num = 0.0
    denom = 0.0
    for name, weight in params.weights.items():
        comp = components.get(name)
        if comp is not None:
            num += weight * comp
            denom += weight
    total_weight = sum(params.weights.values()) or 1.0
    available_weight = denom / total_weight
    if available_weight < _MIN_AVAILABLE_WEIGHT or denom <= 0:
        return {
            "symbol": metrics.get("symbol"),
            "watch_score": None, "label": "insufficient_data",
            "components": components, "available_weight": round(available_weight, 4),
            "reason": "insufficient_components",
        }
    score = round(num / denom)
    return {
        "symbol": metrics.get("symbol"),
        "watch_score": int(score),
        "label": label_for_score(score),
        "components": {k: (round(v, 2) if v is not None else None) for k, v in components.items()},
        "available_weight": round(available_weight, 4),
    }


# ---------------------------------------------------------------------------
# Bundle-level analysis
# ---------------------------------------------------------------------------

def _pct(values: list[bool | None]) -> float | None:
    present = [v for v in values if v is not None]
    if not present:
        return None
    return round(sum(1 for v in present if v) / len(present), 4)


def _stdev(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    return round(statistics.pstdev(vals), 4)


def score_bundle(
    bundle: Any,
    member_metrics: dict[str, dict[str, Any]],
    member_scores: dict[str, dict[str, Any]],
    *,
    minimum_bundle_coverage: float = 0.80,
    prior_bundle_score: float | None = None,
) -> dict[str, Any]:
    """Aggregate member metrics/scores into a bundle summary. Pure."""
    weights = bundle.resolved_weights()
    scored = {s: member_scores[s] for s in bundle.symbols if member_scores.get(s, {}).get("watch_score") is not None}
    n_members = len(bundle.symbols)
    coverage = round(len(scored) / n_members, 4) if n_members else 0.0

    # Weighted bundle score over members that have a score (weights renormalized).
    if scored:
        w_sum = sum(weights.get(s, 0.0) for s in scored) or 0.0
        if w_sum > 0:
            weighted_score = round(
                sum(weights.get(s, 0.0) * scored[s]["watch_score"] for s in scored) / w_sum, 2
            )
        else:
            weighted_score = round(sum(scored[s]["watch_score"] for s in scored) / len(scored), 2)
    else:
        weighted_score = None

    # Basket returns (equal/custom-weighted) per window over members with data.
    def basket_return(window_key: str) -> float | None:
        contribs = [
            (weights.get(s, 0.0), member_metrics[s].get(window_key))
            for s in bundle.symbols
            if member_metrics.get(s, {}).get(window_key) is not None
        ]
        contribs = [(w, r) for w, r in contribs if w > 0]
        if not contribs:
            return None
        tot_w = sum(w for w, _ in contribs)
        return round(sum(w * r for w, r in contribs) / tot_w, 6) if tot_w > 0 else None

    ret_1w = basket_return("return_1w")
    ret_4w = basket_return("return_4w")
    ret_12w = basket_return("return_12w")
    bm_12w = basket_return("benchmark_return_12w")
    excess_12w = round(ret_12w - bm_12w, 6) if (ret_12w is not None and bm_12w is not None) else None

    pct_above_50 = _pct([member_metrics.get(s, {}).get("above_sma50") for s in bundle.symbols])
    pct_above_200 = _pct([member_metrics.get(s, {}).get("above_sma200") for s in bundle.symbols])
    pct_pos_mom_4w = _pct([
        (member_metrics.get(s, {}).get("return_4w") or 0) > 0
        if member_metrics.get(s, {}).get("return_4w") is not None else None
        for s in bundle.symbols
    ])

    score_list = [scored[s]["watch_score"] for s in scored]
    ret_list = [member_metrics[s]["return_4w"] for s in bundle.symbols
                if member_metrics.get(s, {}).get("return_4w") is not None]
    score_dispersion = _stdev([float(x) for x in score_list])
    return_dispersion = _stdev([float(x) for x in ret_list])

    strongest = max(scored, key=lambda s: scored[s]["watch_score"]) if scored else None
    weakest = min(scored, key=lambda s: scored[s]["watch_score"]) if scored else None

    # Leadership concentration: how much the top score exceeds the median.
    if len(score_list) >= 2:
        med = statistics.median(score_list)
        concentration = round(max(0.0, (max(score_list) - med)) / 100.0, 4)
    else:
        concentration = None

    weekly_score_change = (
        round(weighted_score - prior_bundle_score, 2)
        if (weighted_score is not None and prior_bundle_score is not None) else None
    )

    state = _bundle_state(
        coverage=coverage, minimum_coverage=minimum_bundle_coverage,
        pct_above_50=pct_above_50, pct_pos_mom_4w=pct_pos_mom_4w,
        concentration=concentration, weekly_score_change=weekly_score_change,
    )

    return {
        "bundle_id": bundle.id,
        "name": bundle.name,
        "benchmark": bundle.benchmark,
        "bundle_score": weighted_score,
        "state": state,
        "return_1w": ret_1w,
        "return_4w": ret_4w,
        "return_12w": ret_12w,
        "excess_return_12w": excess_12w,
        "pct_above_sma50": pct_above_50,
        "pct_above_sma200": pct_above_200,
        "pct_positive_momentum_4w": pct_pos_mom_4w,
        "strongest": strongest,
        "weakest": weakest,
        "score_dispersion": score_dispersion,
        "return_dispersion": return_dispersion,
        "data_coverage": coverage,
        "leadership_concentration": concentration,
        "weekly_score_change": weekly_score_change,
        "member_count": n_members,
        "scored_member_count": len(scored),
    }


def _bundle_state(
    *, coverage: float, minimum_coverage: float,
    pct_above_50: float | None, pct_pos_mom_4w: float | None,
    concentration: float | None, weekly_score_change: float | None,
) -> str:
    """Descriptive participation state. A broad-strength label REQUIRES broad
    participation AND low concentration — one strong member can never earn it."""
    if coverage < minimum_coverage or pct_above_50 is None or pct_pos_mom_4w is None:
        return "Insufficient data"
    broad = pct_above_50 >= 0.70 and pct_pos_mom_4w >= 0.60
    narrow_only = (concentration is not None and concentration >= 0.25)
    # "Broad leadership" REQUIRES a real dispersion measure (>= 2 scored members).
    # concentration is None only for a single-member bundle — which can never be
    # "broad", so it must not slip through the `not narrow_only` branch.
    if broad and concentration is not None and not narrow_only:
        return "Broad leadership"
    if (pct_above_50 >= 0.50) and narrow_only:
        return "Narrow leadership"
    if weekly_score_change is not None and weekly_score_change >= 2.0:
        return "Improving participation"
    if weekly_score_change is not None and weekly_score_change <= -2.0:
        return "Deteriorating participation"
    return "Mixed participation"
