"""
Crowding score.

Crowding measures how many (effectively independent) managers are piled into the
same name. It is DUAL-NATURED and must never be treated as simply "more bullish":

  * It is evidence of consensus (many managers agree), AND
  * It is a reversal / liquidity / crowded-expectations RISK.

The score in [0,1] rises with the raw supporting-manager count but is tempered by
how *independent* that support is — many correlated managers (same parent /
strategy cluster / overlapping books) are a shallower, riskier crowd than a few
genuinely independent ones. Downstream, a crowded accumulation gets a distinct
state label and a caution warning — not a higher directional score.
"""

from __future__ import annotations

# A raw supporting count at/above this is "fully crowded" on the count axis.
CROWD_FULL_COUNT = 8.0
# At/above this score a signal is flagged crowded (distinct state + caution).
CROWDED_THRESHOLD = 0.60
# How much correlated (non-independent) support inflates crowding risk.
_CORRELATION_WEIGHT = 0.4


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def crowding_score(*, supporting_count: int, effective_independent: float) -> float:
    """Crowding in [0,1].

    Base rises with the raw supporting count. A gap between raw count and
    effective-independent count (i.e. the crowd is correlated) INCREASES crowding
    risk — a correlated pile-in is more fragile than independent breadth.
    """
    if supporting_count <= 0:
        return 0.0
    base = _clamp01(supporting_count / CROWD_FULL_COUNT)
    correlation_gap = _clamp01(
        (supporting_count - max(effective_independent, 0.0)) / supporting_count)
    return _clamp01(base * (1.0 - _CORRELATION_WEIGHT)
                    + base * correlation_gap * _CORRELATION_WEIGHT
                    + correlation_gap * 0.1)


def is_crowded(score: float) -> bool:
    return score >= CROWDED_THRESHOLD
