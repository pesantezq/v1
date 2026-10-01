"""Session 3.1 signal-state evaluator.

Signals are preregistered predictions only. There are no orders, fills, costs,
positions, P&L, or outcome calculations in this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Sequence

from portfolio_automation.intraday_lab import calendar as CAL
from portfolio_automation.intraday_lab import irregular_sessions as IR
from portfolio_automation.intraday_lab.models import IntradayBar, ensure_utc
from portfolio_automation.intraday_lab.strategy_definitions import (
    EARLY_TO_LATE_INTRADAY_MOMENTUM_V1,
    OPENING_RANGE_BREAKOUT_CONTINUATION_V1,
    SHORT_HORIZON_MEAN_REVERSION_V1,
    StrategyDefinition,
    generation1_strategy_by_id,
)

NOT_ENOUGH_HISTORY = "NOT_ENOUGH_HISTORY"
FEATURE_UNAVAILABLE = "FEATURE_UNAVAILABLE"
INELIGIBLE_SESSION = "INELIGIBLE_SESSION"
HALT_ACTIVE = "HALT_ACTIVE"
NO_SIGNAL = "NO_SIGNAL"
SIGNAL_ELIGIBLE_LONG = "SIGNAL_ELIGIBLE_LONG"
SIGNAL_ELIGIBLE_SHORT = "SIGNAL_ELIGIBLE_SHORT"

SIGNAL_STATES = (
    NOT_ENOUGH_HISTORY, FEATURE_UNAVAILABLE, INELIGIBLE_SESSION, HALT_ACTIVE,
    NO_SIGNAL, SIGNAL_ELIGIBLE_LONG, SIGNAL_ELIGIBLE_SHORT,
)


@dataclass(frozen=True)
class SessionView:
    symbol: str
    market_date: date
    classification: str
    bars: tuple[IntradayBar, ...]
    timeframe: str = "5min"

    def bars_known_by(self, as_of: datetime) -> tuple[IntradayBar, ...]:
        as_of = ensure_utc(as_of)
        return tuple(sorted(
            (b for b in self.bars
             if b.symbol == self.symbol
             and b.timeframe == self.timeframe
             and b.is_known_at(as_of)),
            key=lambda b: b.bar_start_at,
        ))


@dataclass(frozen=True)
class SignalObservation:
    strategy_id: str
    strategy_fingerprint: str
    state: str
    as_of: datetime
    signal_known_at: datetime | None
    reason: str
    consumed_bar_starts: tuple[str, ...]
    prediction_value: float | None = None

    def to_dict(self) -> dict:
        return {
            "strategy_id": self.strategy_id,
            "strategy_fingerprint": self.strategy_fingerprint,
            "state": self.state,
            "as_of": self.as_of.isoformat(),
            "signal_known_at": self.signal_known_at.isoformat() if self.signal_known_at else None,
            "reason": self.reason,
            "consumed_bar_starts": list(self.consumed_bar_starts),
            "prediction_value": self.prediction_value,
        }


def _result(strategy: StrategyDefinition, state: str, as_of: datetime,
            reason: str, bars: Sequence[IntradayBar] = (),
            prediction_value: float | None = None) -> SignalObservation:
    known = max((b.known_at for b in bars), default=None)
    return SignalObservation(
        strategy_id=strategy.strategy_id,
        strategy_fingerprint=strategy.fingerprint,
        state=state,
        as_of=ensure_utc(as_of),
        signal_known_at=known,
        reason=reason,
        consumed_bar_starts=tuple(b.bar_start_at.isoformat() for b in bars),
        prediction_value=prediction_value,
    )


def _eligible(classification: str) -> bool:
    return classification in (
        IR.VALID_CONTINUOUS_SESSION,
        IR.VALID_MARKET_WIDE_HALT_SESSION,
    )


def _halt_active(market_date: date, as_of: datetime) -> bool:
    event = IR.mwcb_event_for(market_date)
    if event is None:
        return False
    start, end = event.window_utc()
    instant = ensure_utc(as_of)
    return start <= instant < end


def _opening_count(strategy: StrategyDefinition) -> int | None:
    if strategy.observation_window.anchor != "CERTIFIED_SESSION_OPEN":
        return None
    return strategy.observation_window.bars


def _opening_ready_at(strategy: StrategyDefinition, market_date: date) -> datetime | None:
    count = _opening_count(strategy)
    if count is None:
        return None
    session = CAL.resolve_session(market_date)
    if len(session.expected_bar_starts) < count:
        return None
    from portfolio_automation.intraday_lab.models import DEFAULT_PUBLICATION_DELAY
    return session.expected_bar_starts[count - 1] + timedelta(minutes=5) + DEFAULT_PUBLICATION_DELAY


def opening_window_interrupted(strategy: StrategyDefinition, market_date: date) -> bool:
    """Whether an authoritative halt intersects this strategy's opening window."""
    count = _opening_count(strategy)
    if count is None:
        return False
    session = CAL.resolve_session(market_date)
    starts = session.expected_bar_starts[:count]
    if len(starts) != count:
        return True
    event = IR.mwcb_event_for(market_date)
    if event is None:
        return False
    halt_start, reopen_start = event.window_utc()
    window_start = starts[0]
    window_end = starts[-1] + timedelta(minutes=5)
    return halt_start < window_end and reopen_start > window_start


def halt_boundary_compatibility(strategy: StrategyDefinition, market_date: date) -> dict:
    """Mechanical primitive compatibility for a required opening window.

    The authoritative policy is consumed as data. A blocked primitive only
    makes a strategy unavailable when a relevant partial boundary bar actually
    occurs inside the strategy's required opening window.
    """
    count = _opening_count(strategy)
    if count is None:
        return {"compatible": True, "reason": None, "blocked_primitives": []}
    session = CAL.resolve_session(market_date)
    starts = session.expected_bar_starts[:count]
    boundary = IR.halt_boundary_bars(market_date, starts, timeframe=strategy.timeframe)
    interrupted = opening_window_interrupted(strategy, market_date)
    if not interrupted:
        return {"compatible": True, "reason": None, "blocked_primitives": []}

    blocked = []
    policy = IR.halt_boundary_policy()["features"]
    for requirement in strategy.required_primitives:
        key = requirement.halt_boundary_policy_key
        if key and key in policy and policy[key]["status"] == IR.BLOCKED:
            blocked.append(requirement.primitive_id)
    if strategy.strategy_id == OPENING_RANGE_BREAKOUT_CONTINUATION_V1:
        entry = policy["opening_range_construction"]
        if entry["status"] != IR.BLOCKED:
            raise AssertionError("foundation no longer blocks halt-interrupted opening ranges")
        if "opening_range_construction" not in blocked:
            blocked.append("opening_range_construction")
    return {
        "compatible": False,
        "reason": (
            "authoritative halt intersects the required opening window; "
            "FEATURE_UNAVAILABLE under " + IR.HALT_BOUNDARY_POLICY_VERSION
        ),
        "blocked_primitives": sorted(blocked),
        "partial_boundary_bars": sorted(boundary),
    }


def _opening_bars(strategy: StrategyDefinition, view: SessionView,
                  known: Sequence[IntradayBar]) -> tuple[IntradayBar, ...]:
    count = _opening_count(strategy)
    if count is None:
        return ()
    expected = CAL.resolve_session(view.market_date).expected_bar_starts[:count]
    by_start = {b.bar_start_at: b for b in known}
    return tuple(by_start[t] for t in expected if t in by_start)


def _evaluate_early_late(strategy: StrategyDefinition, view: SessionView,
                         known: tuple[IntradayBar, ...], as_of: datetime) -> SignalObservation:
    required = int(next(p.value for p in strategy.parameters if p.name == "observation_bars"))
    opening = _opening_bars(strategy, view, known)
    if len(opening) < required:
        return _result(strategy, NOT_ENOUGH_HISTORY, as_of,
                       f"need {required} knowable opening bars, have {len(opening)}", opening)
    compat = halt_boundary_compatibility(strategy, view.market_date)
    if not compat["compatible"]:
        return _result(strategy, FEATURE_UNAVAILABLE, as_of, compat["reason"], opening)
    early_return = opening[-1].close / opening[0].open - 1.0
    threshold = float(next(p.value for p in strategy.parameters if p.name == "direction_threshold"))
    if early_return > threshold:
        return _result(strategy, SIGNAL_ELIGIBLE_LONG, as_of,
                       "positive preregistered early-session direction", opening, early_return)
    if early_return < -threshold:
        return _result(strategy, SIGNAL_ELIGIBLE_SHORT, as_of,
                       "negative preregistered early-session direction", opening, early_return)
    return _result(strategy, NO_SIGNAL, as_of,
                   "early-session direction equals the preregistered zero threshold", opening, early_return)


def _evaluate_mean_reversion(strategy: StrategyDefinition, view: SessionView,
                             known: tuple[IntradayBar, ...], as_of: datetime) -> SignalObservation:
    lookback = int(next(p.value for p in strategy.parameters if p.name == "lookback_bars"))
    threshold = float(next(p.value for p in strategy.parameters if p.name == "displacement_threshold"))
    segments = IR.segment_bars(known, timeframe=view.timeframe)
    segment = tuple(segments[-1]) if segments else ()
    required = lookback + 1
    if len(segment) < required:
        return _result(strategy, NOT_ENOUGH_HISTORY, as_of,
                       f"need {required} contiguous close endpoints after any gap/halt reset",
                       segment)
    used = segment[-required:]
    displacement = used[-1].close / used[0].close - 1.0
    if displacement >= threshold:
        return _result(strategy, SIGNAL_ELIGIBLE_SHORT, as_of,
                       "positive displacement meets mean-reversion threshold", used, displacement)
    if displacement <= -threshold:
        return _result(strategy, SIGNAL_ELIGIBLE_LONG, as_of,
                       "negative displacement meets mean-reversion threshold", used, displacement)
    return _result(strategy, NO_SIGNAL, as_of,
                   "absolute displacement is below the preregistered threshold", used, displacement)


def _evaluate_opening_range(strategy: StrategyDefinition, view: SessionView,
                            known: tuple[IntradayBar, ...], as_of: datetime) -> SignalObservation:
    count = int(next(p.value for p in strategy.parameters if p.name == "opening_range_bars"))
    opening = _opening_bars(strategy, view, known)
    if len(opening) < count:
        return _result(strategy, NOT_ENOUGH_HISTORY, as_of,
                       f"need {count} knowable opening-range bars, have {len(opening)}", opening)
    compat = halt_boundary_compatibility(strategy, view.market_date)
    if not compat["compatible"]:
        return _result(strategy, FEATURE_UNAVAILABLE, as_of, compat["reason"], opening)

    opening_starts = {b.bar_start_at for b in opening}
    post = [b for b in known if b.bar_start_at not in opening_starts
            and b.bar_start_at > opening[-1].bar_start_at]
    if not post:
        return _result(strategy, NOT_ENOUGH_HISTORY, as_of,
                       "opening range is known but no post-range close endpoint is knowable yet",
                       opening)
    latest = post[-1]
    used = (*opening, latest)
    high = max(b.high for b in opening)
    low = min(b.low for b in opening)
    threshold = float(next(p.value for p in strategy.parameters if p.name == "break_threshold"))
    if latest.close > high * (1.0 + threshold):
        return _result(strategy, SIGNAL_ELIGIBLE_LONG, as_of,
                       "post-range close is strictly above the preregistered opening range",
                       used, latest.close / high - 1.0)
    if latest.close < low * (1.0 - threshold):
        return _result(strategy, SIGNAL_ELIGIBLE_SHORT, as_of,
                       "post-range close is strictly below the preregistered opening range",
                       used, latest.close / low - 1.0)
    return _result(strategy, NO_SIGNAL, as_of,
                   "post-range close remains inside the preregistered opening range",
                   used, 0.0)


def evaluate_signal(strategy_id: str, view: SessionView, as_of: datetime) -> SignalObservation:
    """Evaluate one preregistered prediction state using only knowable bars."""
    strategy = generation1_strategy_by_id(strategy_id)
    instant = ensure_utc(as_of)
    if not _eligible(view.classification):
        return _result(strategy, INELIGIBLE_SESSION, instant,
                       f"Session 3 population state {view.classification!r} is not eligible")
    if _halt_active(view.market_date, instant):
        return _result(strategy, HALT_ACTIVE, instant,
                       "authoritative market-wide halt is active; no prediction is eligible")
    known = view.bars_known_by(instant)

    ready_at = _opening_ready_at(strategy, view.market_date)
    if ready_at is not None and instant < ready_at:
        return _result(strategy, NOT_ENOUGH_HISTORY, instant,
                       "required opening observation window is not yet fully knowable", known)

    if strategy_id == EARLY_TO_LATE_INTRADAY_MOMENTUM_V1:
        return _evaluate_early_late(strategy, view, known, instant)
    if strategy_id == SHORT_HORIZON_MEAN_REVERSION_V1:
        return _evaluate_mean_reversion(strategy, view, known, instant)
    if strategy_id == OPENING_RANGE_BREAKOUT_CONTINUATION_V1:
        return _evaluate_opening_range(strategy, view, known, instant)
    raise KeyError(strategy_id)
