"""
Independent-manager consensus.

Builds a symbol-level accumulation/distribution consensus that adjusts for
manager INDEPENDENCE — raw manager count is never used directly. Correlated
signals are discounted: same parent org, same strategy cluster (archetype),
market makers, low cloneability, options-dominated, and stale/amended filings.

Persists the full evidence trail (counts, effective-independent count, weighted
support/opposition, consensus score/confidence, manager + strategy-cluster
concentration, filing-age distribution, crowding, disagreement, top supporters/
opposers, reasons/warnings). A crowded signal is a DISTINCT state (crowded_*)
carrying reversal risk — not a more-bullish one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import crowding as cr

# State vocabulary.
STATE_STRONG_ACCUM = "strong_accumulation"
STATE_MODERATE_ACCUM = "moderate_accumulation"
STATE_MIXED = "mixed"
STATE_NEUTRAL = "neutral"
STATE_MODERATE_DIST = "moderate_distribution"
STATE_STRONG_DIST = "strong_distribution"
STATE_CROWDED_ACCUM = "crowded_accumulation"
STATE_CROWDED_DIST = "crowded_distribution"
STATE_INSUFFICIENT = "insufficient_data"

# Signed consensus-score thresholds.
_STRONG = 0.50
_MODERATE = 0.20

# Independence discounts (multiplicative).
_MM_DISCOUNT = 0.20            # market maker: 13F is dealer inventory
_OPT_DOMINATED_DISCOUNT = 0.50
_STALE_DISCOUNT = 0.60
_AMEND_DISCOUNT = 0.90
# Within a correlated cluster, only the strongest counts fully; the rest are
# heavily diminished (they are not independent evidence).
_CLUSTER_ECHO_WEIGHT = 0.30

# Gates (overridable via config).
DEFAULT_MIN_EFFECTIVE = 1.5
DEFAULT_MIN_CONFIDENCE = 0.55


@dataclass(frozen=True)
class ManagerConsensusInput:
    internal_id: str
    archetype: str
    cloneability: float
    final_score: float
    filing_age_days: int | None = None
    data_quality: float = 1.0
    market_maker: bool = False
    options_dominated: bool = False
    is_stale: bool = False
    is_amended: bool = False
    parent_org: str | None = None


@dataclass(frozen=True)
class SymbolConsensus:
    symbol: str
    consensus_state: str
    consensus_score: float
    consensus_confidence: float
    supporting_count: int
    opposing_count: int
    effective_independent_managers: float
    weighted_support: float
    weighted_opposition: float
    manager_concentration: float
    strategy_cluster_concentration: float
    crowding_score: float
    disagreement_score: float
    filing_age_min: int | None
    filing_age_max: int | None
    top_supporting: tuple[str, ...]
    top_opposing: tuple[str, ...]
    reasons: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _clamp_signed(x: float) -> float:
    return max(-1.0, min(1.0, x))


def _independence_weight(m: ManagerConsensusInput) -> float:
    w = _clamp01(m.cloneability)
    if m.market_maker:
        w *= _MM_DISCOUNT
    if m.options_dominated:
        w *= _OPT_DOMINATED_DISCOUNT
    if m.is_stale:
        w *= _STALE_DISCOUNT
    if m.is_amended:
        w *= _AMEND_DISCOUNT
    return _clamp01(w)


def _cluster_key(m: ManagerConsensusInput) -> str:
    # Same parent org OR same archetype cluster => correlated evidence.
    return m.parent_org or f"archetype:{m.archetype}"


def _effective_independent(members: list[tuple[ManagerConsensusInput, float]]) -> float:
    """Sum of independence weights, discounting within correlated clusters.

    Within a cluster: the largest weight counts fully; the rest are echoed at
    _CLUSTER_ECHO_WEIGHT (they are not independent).
    """
    clusters: dict[str, list[float]] = {}
    for m, w in members:
        clusters.setdefault(_cluster_key(m), []).append(w)
    total = 0.0
    for weights in clusters.values():
        weights.sort(reverse=True)
        total += weights[0] + _CLUSTER_ECHO_WEIGHT * sum(weights[1:])
    return total


def _herfindahl(weights: list[float]) -> float:
    s = sum(weights)
    if s <= 0:
        return 0.0
    return sum((w / s) ** 2 for w in weights)


def build_symbol_consensus(
    symbol: str,
    managers: list[ManagerConsensusInput],
    *,
    min_effective: float = DEFAULT_MIN_EFFECTIVE,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> SymbolConsensus:
    reasons: list[str] = []
    warnings: list[str] = []

    weights = {m.internal_id: _independence_weight(m) for m in managers}
    supporters = [(m, weights[m.internal_id]) for m in managers if m.final_score > 0]
    opposers = [(m, weights[m.internal_id]) for m in managers if m.final_score < 0]

    weighted_support = sum(w * m.final_score for m, w in supporters)
    weighted_opposition = sum(w * m.final_score for m, w in opposers)  # negative
    total_weight = sum(weights.values())

    eff_independent = _effective_independent(supporters)
    consensus_score = _clamp_signed(
        (weighted_support + weighted_opposition) / total_weight) if total_weight > 0 else 0.0

    crowd = cr.crowding_score(supporting_count=len(supporters),
                              effective_independent=eff_independent)

    support_mag = abs(weighted_support)
    oppose_mag = abs(weighted_opposition)
    disagreement = (min(support_mag, oppose_mag) / (support_mag + oppose_mag)
                    if (support_mag + oppose_mag) > 0 else 0.0)

    avg_dq = (sum(m.data_quality for m in managers) / len(managers)) if managers else 0.0
    confidence = _clamp01(0.5 * min(eff_independent / 3.0, 1.0)
                          + 0.3 * (1.0 - disagreement) + 0.2 * avg_dq)

    mgr_conc = _herfindahl([w for _, w in supporters])
    cluster_weights: dict[str, float] = {}
    for m, w in supporters:
        cluster_weights[_cluster_key(m)] = cluster_weights.get(_cluster_key(m), 0.0) + w
    cluster_conc = _herfindahl(list(cluster_weights.values()))

    ages = [m.filing_age_days for m in managers if m.filing_age_days is not None]
    age_min = min(ages) if ages else None
    age_max = max(ages) if ages else None

    top_sup = tuple(m.internal_id for m, _ in sorted(
        supporters, key=lambda p: p[1] * p[0].final_score, reverse=True)[:5])
    top_opp = tuple(m.internal_id for m, _ in sorted(
        opposers, key=lambda p: p[1] * p[0].final_score)[:5])

    # --- state ---------------------------------------------------------
    if not managers or eff_independent < min_effective or confidence < min_confidence:
        state = STATE_INSUFFICIENT
        reasons.append(
            f"insufficient independent evidence (eff={eff_independent:.2f} < "
            f"{min_effective} or confidence={confidence:.2f} < {min_confidence})")
    else:
        accumulating = consensus_score > 0
        crowded = cr.is_crowded(crowd)
        if support_mag > 0 and oppose_mag > 0 and disagreement >= 0.35:
            state = STATE_MIXED
            reasons.append("meaningful support AND opposition — mixed")
        elif consensus_score >= _STRONG:
            state = STATE_CROWDED_ACCUM if crowded else STATE_STRONG_ACCUM
        elif consensus_score >= _MODERATE:
            state = STATE_CROWDED_ACCUM if crowded else STATE_MODERATE_ACCUM
        elif consensus_score <= -_STRONG:
            state = STATE_CROWDED_DIST if crowded else STATE_STRONG_DIST
        elif consensus_score <= -_MODERATE:
            state = STATE_CROWDED_DIST if crowded else STATE_MODERATE_DIST
        else:
            state = STATE_NEUTRAL
        if crowded:
            warnings.append("crowded: consensus AND reversal/liquidity/expectations risk")

    if age_max is not None and age_max > 130:
        warnings.append("stale_filings_in_consensus")

    return SymbolConsensus(
        symbol=symbol, consensus_state=state, consensus_score=round(consensus_score, 4),
        consensus_confidence=round(confidence, 4), supporting_count=len(supporters),
        opposing_count=len(opposers), effective_independent_managers=round(eff_independent, 4),
        weighted_support=round(weighted_support, 4),
        weighted_opposition=round(weighted_opposition, 4),
        manager_concentration=round(mgr_conc, 4),
        strategy_cluster_concentration=round(cluster_conc, 4),
        crowding_score=round(crowd, 4), disagreement_score=round(disagreement, 4),
        filing_age_min=age_min, filing_age_max=age_max,
        top_supporting=top_sup, top_opposing=top_opp,
        reasons=tuple(reasons), warnings=tuple(warnings))
