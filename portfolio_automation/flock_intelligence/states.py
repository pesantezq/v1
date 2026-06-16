"""Flock state enum, group/ticker dataclasses, and the transparent classifier.

The classifier maps a group's metrics (+ optional prior-run state) onto one of
the six flock states. Rules are simple, ordered, and explainable — every state
carries a natural-language ``explanation`` and a numeric ``confidence`` driven
by data sufficiency and signal strength.

``insufficient_data`` covers BOTH a true data gap (too few tickers / no history /
missing artifacts) and "data is present but no flock structure is detectable" —
the two are distinguished by the explanation and confidence, never by inventing
an enum value outside the spec's six states.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class FlockState(str, Enum):
    FLOCK_FORMING = "flock_forming"
    FLOCK_CONFIRMED = "flock_confirmed"
    FLOCK_EXHAUSTION = "flock_exhaustion"
    FLOCK_DISPERSING = "flock_dispersing"
    FLOCK_BROKEN = "flock_broken"
    INSUFFICIENT_DATA = "insufficient_data"


# States that represent an *active* flock (used to detect "a prior flock existed"
# when classifying dispersion / broken on the next run).
FLOCK_PRESENT_STATES = frozenset({
    FlockState.FLOCK_FORMING, FlockState.FLOCK_CONFIRMED, FlockState.FLOCK_EXHAUSTION,
})


@dataclass
class Thresholds:
    """Transparent, tunable classification thresholds."""
    min_group_size: int = 2
    min_history_points: int = 3
    velocity_elevated: float = 1.0      # mention-velocity z-score
    velocity_high: float = 2.0
    breadth_broad: float = 0.6          # fraction of group participating
    corr_high: float = 0.5              # avg pairwise correlation
    corr_rising_delta: float = 0.05     # avg_corr - prior_avg_corr to count as rising
    corr_falling_delta: float = 0.10    # prior - current to count as falling
    corr_break_delta: float = 0.30      # material correlation collapse
    corr_broken_floor: float = 0.20     # absolute corr below which flock is broken
    concentration_high: float = 0.50    # HHI
    dispersion_trigger: float = 0.50
    exhaustion_trigger: float = 0.55


@dataclass
class GroupMetrics:
    """All raw metrics + composite scores for one theme/sector group."""
    group: str
    group_kind: str                     # "theme" | "sector" | "ticker_only"
    tickers: list[str]
    n_tickers: int
    n_with_returns: int
    history_points: int
    has_crowd_data: bool
    crowd_velocity: float
    crowd_breadth: float
    source_breadth: float
    mention_concentration: float
    avg_correlation: float | None
    prior_avg_correlation: float | None
    return_spread: float
    group_momentum: float
    volatility_change: float
    flock_score: float
    dispersion_score: float
    exhaustion_score: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GroupFlock:
    """Classified flock for one group."""
    group: str
    group_kind: str
    flock_state: str
    flock_score: float
    dispersion_score: float
    crowd_velocity: float
    crowd_breadth: float
    mention_concentration: float
    price_correlation_to_group: float | None
    confidence: float
    explanation: str
    risk_flags: list[str] = field(default_factory=list)
    tickers: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TickerFlock:
    """Per-ticker flock context (the ticker's relationship to its group's flock)."""
    ticker: str
    group: str
    group_kind: str
    flock_state: str
    flock_score: float
    dispersion_score: float
    crowd_velocity: float
    crowd_breadth: float
    mention_concentration: float
    price_correlation_to_group: float | None
    relative_strength_vs_group: float | None
    volatility_change: float
    confidence: float
    explanation: str
    evidence_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Confidence + classification
# ---------------------------------------------------------------------------

def _confidence(m: GroupMetrics, th: Thresholds) -> float:
    """Data-sufficiency-driven confidence in 0..1 (transparent blend)."""
    size_factor = min(1.0, m.n_tickers / 4.0)
    returns_factor = min(1.0, m.n_with_returns / max(1, m.n_tickers))
    history_factor = min(1.0, m.history_points / max(1, th.min_history_points * 2))
    crowd_factor = 1.0 if m.has_crowd_data else 0.6
    return round(0.30 * size_factor + 0.25 * returns_factor
                 + 0.25 * history_factor + 0.20 * crowd_factor, 4)


def _data_sufficient(m: GroupMetrics, th: Thresholds) -> bool:
    if m.n_tickers < th.min_group_size:
        return False
    if not m.has_crowd_data and m.n_with_returns < 2:
        return False
    if m.avg_correlation is None and not m.has_crowd_data:
        return False
    return True


def classify_group(m: GroupMetrics, prior_state: str | None = None,
                   th: Thresholds | None = None) -> GroupFlock:
    """Classify a group's flock state. Ordered, transparent rules.

    ``prior_state`` is the previous run's flock_state for this group (or None on
    a first run) — required to assert dispersion / broken ("a prior flock existed").
    """
    th = th or Thresholds()
    conf = _confidence(m, th)
    prior_flock = prior_state in {s.value for s in FLOCK_PRESENT_STATES}

    corr = m.avg_correlation
    corr_rising = (corr is not None and m.prior_avg_correlation is not None
                   and (corr - m.prior_avg_correlation) >= th.corr_rising_delta)
    corr_falling = (corr is not None and m.prior_avg_correlation is not None
                    and (m.prior_avg_correlation - corr) >= th.corr_falling_delta)
    corr_collapse = (corr is not None and m.prior_avg_correlation is not None
                     and (m.prior_avg_correlation - corr) >= th.corr_break_delta)

    def out(state: FlockState, why: str, flags: list[str] | None = None,
            conf_override: float | None = None) -> GroupFlock:
        return GroupFlock(
            group=m.group, group_kind=m.group_kind, flock_state=state.value,
            flock_score=round(m.flock_score, 4), dispersion_score=round(m.dispersion_score, 4),
            crowd_velocity=round(m.crowd_velocity, 4), crowd_breadth=round(m.crowd_breadth, 4),
            mention_concentration=round(m.mention_concentration, 4),
            price_correlation_to_group=(round(corr, 4) if corr is not None else None),
            confidence=conf if conf_override is None else conf_override,
            explanation=why, risk_flags=flags or [], tickers=list(m.tickers),
        )

    # 1. Data gaps first.
    if not _data_sufficient(m, th):
        return out(FlockState.INSUFFICIENT_DATA,
                   f"Insufficient data: {m.n_tickers} ticker(s), "
                   f"{m.n_with_returns} with returns, {m.history_points} history points.",
                   conf_override=min(conf, 0.3))

    # 2. Flock broken (needs a prior flock + material correlation collapse).
    if prior_flock and ((corr_collapse) or (corr is not None and corr < th.corr_broken_floor)) \
            and m.crowd_velocity < th.velocity_elevated:
        return out(FlockState.FLOCK_BROKEN,
                   f"Prior flock broke down: correlation {corr:.2f} "
                   f"(was {m.prior_avg_correlation}), crowd velocity fading "
                   f"({m.crowd_velocity:.2f}); leaders/laggards split "
                   f"(spread {m.return_spread:.2f}pp).",
                   flags=["flock_broken"])

    # 3. Flock dispersing (prior flock + falling correlation / rising dispersion).
    if prior_flock and (corr_falling or m.dispersion_score >= th.dispersion_trigger):
        return out(FlockState.FLOCK_DISPERSING,
                   f"Flock dispersing: dispersion score {m.dispersion_score:.2f}, "
                   f"correlation {('%.2f' % corr) if corr is not None else 'n/a'} "
                   f"falling, return spread {m.return_spread:.2f}pp widening, "
                   f"breadth {m.crowd_breadth:.2f}.",
                   flags=["dispersion_risk"])

    # 4. Flock exhaustion (hot crowd, weakening confirmation).
    if m.exhaustion_score >= th.exhaustion_trigger \
            and m.crowd_velocity >= th.velocity_elevated \
            and m.mention_concentration >= th.concentration_high:
        return out(FlockState.FLOCK_EXHAUSTION,
                   f"Exhaustion risk: velocity {m.crowd_velocity:.2f} high but "
                   f"attention concentrated (HHI {m.mention_concentration:.2f}), "
                   f"breadth {m.crowd_breadth:.2f}, momentum {m.group_momentum:.2f}pp.",
                   flags=["crowded_trade", "exhaustion_risk"])

    # 5. Flock confirmed (broad + correlated + price confirmation).
    if m.crowd_velocity >= th.velocity_elevated and m.crowd_breadth >= th.breadth_broad \
            and corr is not None and corr >= th.corr_high and m.group_momentum > 0:
        return out(FlockState.FLOCK_CONFIRMED,
                   f"Flock confirmed: broad crowd/price alignment "
                   f"(velocity {m.crowd_velocity:.2f}, breadth {m.crowd_breadth:.2f}, "
                   f"correlation {corr:.2f}, momentum {m.group_momentum:.2f}pp).",
                   flags=["flock_confirmed"])

    # 6. Flock forming (cohesion building).
    if m.crowd_velocity > 0 and (corr_rising or (corr is not None and corr >= 0.30)) \
            and m.crowd_breadth > 0:
        return out(FlockState.FLOCK_FORMING,
                   f"Flock forming: rising velocity ({m.crowd_velocity:.2f}), "
                   f"{'rising ' if corr_rising else ''}correlation "
                   f"{('%.2f' % corr) if corr is not None else 'n/a'}, "
                   f"breadth {m.crowd_breadth:.2f} improving.",
                   flags=["flock_forming"])

    # 7. Data present but no detectable flock structure.
    return out(FlockState.INSUFFICIENT_DATA,
               "No detectable flock structure: crowd/price signals too weak to "
               "classify a flock this run.",
               conf_override=min(conf, 0.4))
