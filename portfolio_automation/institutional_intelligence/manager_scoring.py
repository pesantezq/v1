"""
Manager-symbol scoring — pure, deterministic, no LLM.

Produces a signed per-(manager, symbol) score in [-1, 1] as the product of a
signed ``direction_score`` and eight unit-interval magnitude components, then
applies bounded penalties. EVERY component and penalty is persisted (the score
is never a single opaque number).

    manager_symbol_score = direction * (conviction * manager_quality *
        cloneability * freshness * strategy_fit * persistence *
        options_interpretability * data_quality) * Π(penalty_factors)

Directional signal comes ONLY from ordinary-share events; option events carry
direction_score 0 (options never auto-directional — see options_interpretation).
All thresholds are named constants with rationale.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import position_changes as pcm
from .options_interpretation import OptionInterpretation

# --- direction defaults (named; rationale in comments) -------------------
# Ordinary-share event -> base directional signal in [-1, 1]. A brand-new
# position is the strongest positive signal; a full exit the strongest negative.
DIRECTION_NEW = 1.00
DIRECTION_INCREASE_LARGE = 0.75      # >= +25% shares
DIRECTION_INCREASE_SMALL = 0.40      # +5%..+25%
DIRECTION_UNCHANGED = 0.00
DIRECTION_REDUCE_SMALL = -0.40       # -5%..-25%
DIRECTION_REDUCE_LARGE = -0.75       # <= -25%
DIRECTION_EXIT = -1.00
_INCREASE_LARGE_PCT = 0.25
_INCREASE_SMALL_PCT = 0.05
_REDUCE_SMALL_PCT = -0.05
_REDUCE_LARGE_PCT = -0.25

# Freshness: 13F is quarterly, so decay is far slower than daily sentiment.
# Full strength for the first FRESHNESS_FULL_DAYS after the PUBLIC FILING date,
# then linear decay to a floor by FRESHNESS_ZERO_DAYS. A months-old filing is
# weak but not zero until well past a quarter.
FRESHNESS_FULL_DAYS = 21
FRESHNESS_ZERO_DAYS = 160
FRESHNESS_FLOOR = 0.05

# A position below TINY_WEIGHT of the manager's ordinary book is barely a
# conviction signal regardless of its % change.
TINY_WEIGHT = 0.005          # 0.5% of the manager's disclosed equity book
# Penalty factors (multiplicative dampeners in [floor, 1]).
CROWDING_PENALTY_FLOOR = 0.5
PRICE_MOVE_PENALTY_THRESHOLD = 0.20   # >20% move since filing → dampen
PRICE_MOVE_PENALTY_FLOOR = 0.6
TURNOVER_PENALTY_THRESHOLD = 0.6      # manager turnover >60% → dampen
TURNOVER_PENALTY_FLOOR = 0.7
AMENDMENT_PENALTY = 0.9
STRATEGY_MISMATCH_PENALTY = 0.8
TINY_POSITION_PENALTY = 0.4


@dataclass(frozen=True)
class ManagerSymbolScore:
    symbol: str | None
    direction_score: float
    conviction_score: float
    manager_quality_score: float
    cloneability_score: float
    freshness_score: float
    strategy_fit_score: float
    persistence_score: float
    options_interpretability_score: float
    data_quality_score: float
    base_magnitude: float
    penalties: dict = field(default_factory=dict)
    final_score: float = 0.0
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _clamp_signed(x: float) -> float:
    return max(-1.0, min(1.0, x))


def direction_score(event: str, shares_pct_change: float | None) -> float:
    """Signed directional signal from an ordinary-share event. Options -> 0."""
    if event in (pcm.EV_NEW,):
        return DIRECTION_NEW
    if event in (pcm.EV_EXITED,):
        return DIRECTION_EXIT
    if event == pcm.EV_INCREASED:
        if shares_pct_change is not None and shares_pct_change >= _INCREASE_LARGE_PCT:
            return DIRECTION_INCREASE_LARGE
        return DIRECTION_INCREASE_SMALL
    if event == pcm.EV_REDUCED:
        if shares_pct_change is not None and shares_pct_change <= _REDUCE_LARGE_PCT:
            return DIRECTION_REDUCE_LARGE
        return DIRECTION_REDUCE_SMALL
    if event == pcm.EV_UNCHANGED:
        return DIRECTION_UNCHANGED
    # Options events (new_call, new_put, ...), identity_unresolved,
    # comparison_unavailable → NO directional contribution.
    return 0.0


def conviction_score(*, curr_weight: float | None, weight_delta: float | None,
                     rank: int | None, top10_entry: bool, persistence_quarters: int,
                     concentration: float | None) -> float:
    """Blend of portfolio-weight signals so tiny positions are not "convicted".

    Uses portfolio WEIGHT and RANK — not just % share change — so a 100% jump in
    a 0.1%-weight position does not read as high conviction.
    """
    w = curr_weight or 0.0
    # Weight itself (a 5%+ position is a strong conviction anchor -> ~1.0).
    weight_component = _clamp01(w / 0.05)
    rank_component = 0.0
    if rank is not None:
        rank_component = _clamp01((21 - min(rank, 20)) / 20.0)  # rank 1 ->1.0, 20 ->~0.05
    delta_component = _clamp01(0.5 + (weight_delta or 0.0) * 10.0)  # adds when weight grew
    top10_bonus = 0.15 if top10_entry else 0.0
    persist_component = _clamp01(persistence_quarters / 4.0)
    blended = (0.4 * weight_component + 0.25 * rank_component
               + 0.2 * delta_component + 0.15 * persist_component + top10_bonus)
    return _clamp01(blended)


def freshness_score(filing_age_days: int | None) -> float:
    if filing_age_days is None:
        return FRESHNESS_FLOOR
    if filing_age_days <= FRESHNESS_FULL_DAYS:
        return 1.0
    if filing_age_days >= FRESHNESS_ZERO_DAYS:
        return FRESHNESS_FLOOR
    span = FRESHNESS_ZERO_DAYS - FRESHNESS_FULL_DAYS
    decayed = 1.0 - (filing_age_days - FRESHNESS_FULL_DAYS) / span
    return _clamp01(max(decayed, FRESHNESS_FLOOR))


def strategy_fit_score(security_tags, manager_specialization) -> float:
    """Overlap between a security's sector/theme tags and the manager's
    documented specialization. Neutral 0.5 when either side is unknown."""
    sec = {t.lower() for t in (security_tags or [])}
    spec = {t.lower() for t in (manager_specialization or [])}
    if not sec or not spec:
        return 0.5
    overlap = len(sec & spec)
    return _clamp01(0.4 + 0.6 * min(overlap, 3) / 3.0)


def persistence_score(quarters_held: int) -> float:
    return _clamp01(quarters_held / 4.0) if quarters_held > 0 else 0.25


def data_quality_score(*, identity_resolved: bool, is_amendment: bool,
                       parse_warnings: tuple[str, ...] = ()) -> float:
    score = 1.0
    if not identity_resolved:
        score *= 0.3
    if is_amendment:
        score *= 0.95
    if any("malformed" in w or "value_units_ambiguous" in w for w in parse_warnings):
        score *= 0.9
    return _clamp01(score)


def score_manager_symbol(
    change: pcm.PositionChange,
    *,
    manager_quality_prior: float,
    cloneability: float,
    option_ctx: OptionInterpretation,
    security_tags=None,
    manager_specialization=None,
    persistence_quarters: int = 1,
    is_amendment: bool = False,
    parse_warnings: tuple[str, ...] = (),
    crowding_score: float = 0.0,
    price_move_since_filing: float | None = None,
    manager_turnover: float | None = None,
    manager_options_dominated: bool = False,
    crowding_penalty_enabled: bool = True,
) -> ManagerSymbolScore:
    """Pure scoring of one position change. Returns every component + penalty."""
    warnings: list[str] = []

    d = direction_score(change.event, change.shares_pct_change)
    conv = conviction_score(
        curr_weight=change.curr_weight, weight_delta=change.weight_delta,
        rank=change.curr_rank, top10_entry=change.top10_entry,
        persistence_quarters=persistence_quarters, concentration=change.curr_weight)
    mq = _clamp01(manager_quality_prior)
    clone = _clamp01(cloneability)
    fresh = freshness_score(change.filing_age_days)
    fit = strategy_fit_score(security_tags, manager_specialization)
    persist = persistence_score(persistence_quarters)
    opt_interp = _clamp01(1.0 - option_ctx.interpretability_penalty)
    dq = data_quality_score(identity_resolved=change.identity_resolved,
                            is_amendment=is_amendment, parse_warnings=parse_warnings)

    base_magnitude = conv * mq * clone * fresh * fit * persist * opt_interp * dq

    # --- bounded penalties (multiplicative dampeners) -------------------
    penalties: dict[str, float] = {}
    if crowding_penalty_enabled and crowding_score > 0:
        penalties["crowding"] = max(CROWDING_PENALTY_FLOOR, 1.0 - crowding_score)
    if price_move_since_filing is not None and abs(price_move_since_filing) > PRICE_MOVE_PENALTY_THRESHOLD:
        penalties["price_move"] = PRICE_MOVE_PENALTY_FLOOR
        warnings.append("large_price_move_since_filing")
    if manager_turnover is not None and manager_turnover > TURNOVER_PENALTY_THRESHOLD:
        penalties["turnover"] = TURNOVER_PENALTY_FLOOR
    if is_amendment:
        penalties["amendment"] = AMENDMENT_PENALTY
    if (change.curr_weight is not None and 0 < change.curr_weight < TINY_WEIGHT):
        penalties["tiny_position"] = TINY_POSITION_PENALTY
        warnings.append("tiny_portfolio_position")
    if security_tags and manager_specialization and fit < 0.45:
        penalties["strategy_mismatch"] = STRATEGY_MISMATCH_PENALTY
    if manager_options_dominated:
        penalties["options_dominated"] = 0.7
        warnings.append("options_dominated_exposure")

    penalty_product = 1.0
    for v in penalties.values():
        penalty_product *= v

    final = _clamp_signed(d * base_magnitude * penalty_product)

    return ManagerSymbolScore(
        symbol=change.symbol, direction_score=d, conviction_score=conv,
        manager_quality_score=mq, cloneability_score=clone, freshness_score=fresh,
        strategy_fit_score=fit, persistence_score=persist,
        options_interpretability_score=opt_interp, data_quality_score=dq,
        base_magnitude=round(base_magnitude, 6), penalties=penalties,
        final_score=round(final, 6), warnings=tuple(warnings))
