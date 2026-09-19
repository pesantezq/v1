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
from portfolio_automation.northstar.sources import DataSourceDescriptor

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


# ===========================================================================
# Dividend-adjusted daily bars — the bounded 0C prerequisite
# (mission northstar_0c_historical_price_evidence_for_vs002)
# ===========================================================================

#: The ONE authorized source. Adjustment semantics are carried by the endpoint
#: identity, never inferred from a field named ``adjClose``. Anything else —
#: /full, the mutable archive, another vendor — is refused, not substituted.
AUTHORIZED_ENDPOINT = "/stable/historical-price-eod/dividend-adjusted"
SOURCE_PROVIDER = "fmp"
SOURCE_DATASET = "historical_price_eod_dividend_adjusted"
EVIDENCE_TYPE_BAR = "market.price_daily_dividend_adjusted"

#: Fields every provider row MUST carry for the semantics this contract claims.
#: The real endpoint's shape is UNVERIFIED (deferred B3); if it cannot supply
#: these fields the production build FAILS CLOSED rather than establishing the
#: claim from weaker data.
REQUIRED_PROVIDER_FIELDS = ("date", "close", "adjClose", "volume")

#: A symbol series may not skip more than this many BENCHMARK sessions between
#: two consecutive bars. From the frozen minimum-evidence contract: "no gap
#: longer than 5 sessions inside a claimed continuous window" — a deterministic
#: bound taken from the experiment's own contract, not invented continuity.
MAX_SESSION_GAP = 5

#: Tolerances for the adjustment-semantics battery. Relative, small, and only
#: as forgiving as provider rounding requires.
RATIO_FINAL_TOLERANCE = 1e-4
RATIO_MONOTONE_TOLERANCE = 1e-6

#: The PIT claim for the bar panel, stated as narrowly as the returns claim.
#: Two DIFFERENT truths, deliberately not conflated (operator ruling B1):
#:
#: 1. POSSESSION PIT. Every bar snapshot's ``known_at`` is its ``retrieved_at``
#:    under the one sanctioned conservative derivation. A backfilled bar is
#:    therefore truthfully REFUSED by the EvidenceGateway for any
#:    ``as_of`` before retrieval — this copy of history was not possessed
#:    then. That refusal is correct behavior, never bypassed.
#: 2. STRUCTURAL WINDOW SAFETY. The beta estimation window admits only bars
#:    whose ``session_date`` is STRICTLY before the signal date. No
#:    contemporaneous or forward session participates, enforced on the dates
#:    the sessions actually carry — no knowledge time is fabricated to make
#:    historical bars "admissible" at historical instants.
ADJUSTED_PIT_CERTIFICATION = "ADJUSTED_BARS_PIT_SAFE_FOR_VS002"
ADJUSTED_PIT_CERTIFICATION_SCOPE = (
    "Certifies (1) possession PIT: bar snapshots carry known_at=retrieved_at "
    "(derived_conservative) and are honestly refused by the gateway before "
    "retrieval; and (2) structural window safety: beta windows admit only "
    "sessions strictly before the signal date. It does NOT certify that any "
    "bar was knowable at a historical instant, and no historical known_at is "
    "fabricated."
)

ADJUSTED_BETA_CONVENTION = "dividend-adjusted close market beta"
ADJUSTED_BETA_FORMULA = (
    "r_i(t) = adj_close_i(t)/adj_close_i(t-1) - 1; "
    "r_spy(t) = adj_close_spy(t)/adj_close_spy(t-1) - 1; "
    "beta_i = Cov(r_i, r_spy) / Var(r_spy)"
)

#: Operator ruling B2 — VS-002 ONLY. The frozen risk-adjustment formula
#: requires rf; no authorized risk-free data source exists in this bounded
#: mission, and adding one (Treasury/FRED/...) would broaden scope. So rf over
#: the 7-day horizon is preregistered as an explicit simplifying ASSUMPTION —
#: named, attributable, and never reportable as an observed market value. It is
#: NOT a reusable global default: future experiments must justify their own.
RISK_FREE_RATE_7D_ASSUMPTION = {
    "value": 0.0,
    "basis": "operator_preregistered_assumption",
    "observed_evidence": False,
    "scope": "VS-002 only",
    "authorized": "operator ruling B2, 2026-09-18",
    "note": (
        "ASSUMPTION, NOT MEASUREMENT. The result must never be described as "
        "having used an observed risk-free rate. Not a reusable default."),
}


def data_source_descriptor() -> DataSourceDescriptor:
    """The canonical descriptor for the ONE authorized bar source.

    ``pit_capability`` is ``reconstructable`` — the provider serves history but
    cannot state when a value became knowable — which is exactly why the bar
    snapshots carry conservative possession timing rather than a fabricated
    historical known_at.
    """
    return DataSourceDescriptor(
        provider=SOURCE_PROVIDER,
        dataset=SOURCE_DATASET,
        source_type="market_data",
        access_class="api",
        rights_class="private_research",
        cost_class="existing_subscription",
        pit_capability="reconstructable",
        historical_capability="deep",
        status="probe_only",
        notes=(
            f"Endpoint {AUTHORIZED_ENDPOINT}. Bounded VS-002 use only; "
            f"entitlement verification deferred to the production build; "
            f"fail closed, no fallback."),
    )


@dataclass(frozen=True)
class BarRow:
    """One dividend-adjusted daily bar, exactly as the contract requires.

    ``close`` is the unadjusted official close (retained so the adjustment is
    auditable); ``adj_close`` is the split-AND-dividend adjusted close the
    beta consumes; ``volume`` exists for liquidity sanity only.
    """

    symbol: str
    session_date: str
    close: float
    adj_close: float
    volume: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "session_date": self.session_date,
            "close": self.close, "adj_close": self.adj_close,
            "volume": self.volume,
        }


def derive_adjusted_return(adj_close: float, prev_adj_close: float) -> float:
    """Daily return from the ADJUSTED close. Same fail-closed arithmetic as
    :func:`derive_return`; a separate name so a call site cannot silently be
    fed the unadjusted series."""
    return derive_return(adj_close, prev_adj_close)
