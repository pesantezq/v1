"""VS-002 frozen evidence contracts.

WHAT THIS PACKAGE IS FOR.

VS-002 cannot execute in the research plane because the evidence it needs lives
in production: recorded watchlist signals in the runtime database, and daily
price history in the backfill archive. The research plane must never hold a
vendor credential, so it cannot fetch either. This package freezes exactly the
evidence VS-002 needs into an immutable, hash-verified package that crosses the
existing agent-export boundary and is validated again on arrival.

WHY RETURNS AND NOT PRICE LEVELS.

The production archive is split-adjusted to whatever vintage it was last
retrieved at, and it is overwritten in place. Its price LEVELS are therefore not
vintage-correct evidence. Its daily RETURN RATIOS are, because a later
whole-series adjustment multiplies every observation in a window by the same
factor and cancels:

    (c * P_t) / (c * P_{t-1}) - 1  ==  P_t / P_{t-1} - 1

That identity is the entire reason this export is honest, so it is a test
(``test_scaling_invariance_*``) and not a comment. The package therefore ships
DERIVED RETURNS as the consumer contract and retains enough source provenance
(archive digest, stored_at) to reproduce them.

``experimental_noncanonical``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from portfolio_automation.northstar.canonical import (
    canonical_dumps, content_hash, deterministic_id)

SCHEMA_VERSION = "engineering.vs002_evidence.v0"
SCHEMA_KIND = "experimental_noncanonical"

#: The frozen VS-002 universe, derived from the durable VS-001 artifacts.
#: 21 non-SPY symbols plus the benchmark = 22 total. Recorded here as data so a
#: reader never has to trust prose: protected state currently says "22 VS-002
#: symbols plus SPY", which over-counts by one and is journaled for correction.
FROZEN_UNIVERSE: tuple[str, ...] = (
    "AAPL", "AMD", "AMZN", "AVGO", "CHAT", "COIN", "GOOGL", "IWM", "MARA",
    "META", "MSFT", "NASA", "NVDA", "PLTR", "QQQ", "RIOT", "SMCI", "TSLA",
    "XLE", "XLF", "XLK",
)
BENCHMARK = "SPY"

#: Universal eligibility rule. Applied to every symbol identically, using only
#: information available before the signal, and frozen before execution. It is
#: NOT a ticker exception: NASA fails it because it has ~106 sessions, and any
#: future short-history symbol would fail it the same way.
MIN_PRIOR_SESSIONS = 252

#: Minimum joint stock/benchmark observations for a beta to be estimated.
MIN_JOINT_OBSERVATIONS = 60

#: Minimum independent evaluation units. VS-001's defect was treating 20
#: overlapping scans as 20 observations; the cohort is the independent unit.
MIN_COHORTS = 10

HORIZON_DAYS = 7

#: The PIT claim this package makes, stated narrowly on purpose.
PIT_CERTIFICATION = "PRICE_HISTORY_PIT_SAFE_FOR_VS002"
PIT_CERTIFICATION_SCOPE = (
    "Certifies use of the archive to derive the bounded pre-signal DAILY RETURN "
    "series for beta estimation. It does NOT certify archive price LEVELS as "
    "vintage-correct. A later whole-series multiplicative adjustment cancels in "
    "a return ratio; it does not cancel in a level."
)

#: Dividend treatment is undocumented by the provider and was not established.
#: The beta is therefore named for its input, not for a return convention it has
#: not been shown to satisfy.
BETA_CONVENTION = "archive-close market beta"
BETA_FORMULA = (
    "r_i(t) = close_i(t)/close_i(t-1) - 1; "
    "r_spy(t) = close_spy(t)/close_spy(t-1) - 1; "
    "beta_i = Cov(r_i, r_spy) / Var(r_spy)"
)
DIVIDEND_LIMITATION = (
    "UNRESOLVED. Archive close is empirically split-adjusted (NVDA 2024-06-10 "
    "and AVGO 2024-07-15 10:1 events show no raw discontinuity). Whether it is "
    "also dividend-adjusted was NOT established: a live-vs-archive comparison "
    "across 76 overlapping dates showed no yield-ordered drift, but that test "
    "resolves to roughly +/-0.5% and cannot exclude a small adjustment. The "
    "result must not be called a total-return or a pure price-return beta."
)


@dataclass(frozen=True)
class SignalRow:
    """One recorded, matured watchlist signal. Genuine history, never reconstructed."""

    ticker: str
    signal_time: str
    signal_score: Optional[float]
    price_at_signal: float
    outcome_return_7d: float
    outcome_price_7d: Optional[float]
    evaluated_at_7d: str
    prediction_intent: str
    data_mode: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker, "signal_time": self.signal_time,
            "signal_score": self.signal_score,
            "price_at_signal": self.price_at_signal,
            "outcome_return_7d": self.outcome_return_7d,
            "outcome_price_7d": self.outcome_price_7d,
            "evaluated_at_7d": self.evaluated_at_7d,
            "prediction_intent": self.prediction_intent,
            "data_mode": self.data_mode,
        }


@dataclass(frozen=True)
class ReturnRow:
    """One derived daily return. The consumer contract is the return, not the level.

    ``close`` and ``prev_close`` are retained so the derivation is reproducible
    and auditable, not because the consumer should compute on levels."""

    symbol: str
    session_date: str
    prev_session_date: str
    close: float
    prev_close: float
    daily_return: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "session_date": self.session_date,
            "prev_session_date": self.prev_session_date,
            "close": self.close, "prev_close": self.prev_close,
            "daily_return": self.daily_return,
        }


def derive_return(close: float, prev_close: float) -> float:
    """The one arithmetic this package performs. Fails closed on a bad divisor."""
    if prev_close is None or prev_close <= 0:
        raise ValueError("prev_close must be positive to derive a return")
    return close / prev_close - 1.0


def artifact_digest(payload: Any) -> str:
    """Canonical content hash, reusing the Northstar serializer rather than a
    second one -- a package with its own hashing rules is a package whose hashes
    mean something different from every other hash in the system."""
    return content_hash(canonical_dumps(payload))


def package_id(manifest_core: Mapping[str, Any]) -> str:
    return deterministic_id("vs002evd", manifest_core)
