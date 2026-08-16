"""Deterministic, fail-closed point-in-time admissibility.

THE RULE, and its authority:

    portfolio_automation/northstar/pit.py names ``known_at`` as "the earliest
    DEFENSIBLE moment StockBot could have known and used the information" and
    states outright: "This is the future anti-lookahead authority: Phase 0C will
    enforce evidence.known_at <= experiment.as_of."

This module is that enforcement, and nothing more.

    admissible  <=>  known_at is present  AND  known_at <= as_of

FAIL CLOSED, in both directions the 0B kernel already cares about:

* Absent timing is NOT permission. Evidence whose ``known_at`` is unknown is
  refused, never admitted by default. The kernel's whole no-fabricated-time
  discipline exists so that missing timestamps stay visible; admitting on
  absence would quietly undo it.
* Incoherent timing is refused. If evidence claims it was knowable BEFORE its
  own publication, its timing cannot be trusted to bound lookahead, so it is
  refused rather than interpreted.

DELIBERATELY UNDER-CLAIMING. Every rule here can only refuse evidence that
might have been admissible; none can admit evidence that should have been
refused. That asymmetry is the point: a backtest that sees too little produces
a pessimistic result, while one that sees too much produces a false one.

PURE. No clock is read, no file or socket is opened, and no default "now"
exists. The caller supplies ``as_of`` explicitly, so a decision is reproducible
forever and can be re-audited from stored evidence. A predicate that could read
the wall clock would make historical audit non-deterministic.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from portfolio_automation.northstar.pit import KNOWN_AT_UNKNOWN, PointInTime

SCHEMA_VERSION = "1.0.0"
CONTRACT_TYPE = "evidence_admissibility_decision"


class AdmissibilityError(ValueError):
    """Raised by :func:`require_admissible` when evidence is not admissible."""


class AdmissibilityReason(str, Enum):
    """Closed set of decision reasons.

    A closed enum rather than free text: the reason is audit evidence, and an
    auditor must be able to COUNT refusals by cause. Free-text reasons drift and
    cannot be aggregated across a backtest."""

    ADMITTED = "ADMITTED"
    # as_of is unusable as a boundary
    AS_OF_NOT_TIMEZONE_AWARE = "AS_OF_NOT_TIMEZONE_AWARE"
    AS_OF_NOT_A_DATETIME = "AS_OF_NOT_A_DATETIME"
    # evidence timing cannot support an anti-lookahead decision
    KNOWN_AT_UNKNOWN = "KNOWN_AT_UNKNOWN"
    KNOWN_AT_AFTER_AS_OF = "KNOWN_AT_AFTER_AS_OF"
    KNOWN_AT_BEFORE_PUBLISHED_AT = "KNOWN_AT_BEFORE_PUBLISHED_AT"
    # malformed input
    NOT_A_POINT_IN_TIME = "NOT_A_POINT_IN_TIME"


#: Reasons that represent a refusal (everything except ADMITTED).
REFUSAL_REASONS = frozenset(r for r in AdmissibilityReason
                            if r is not AdmissibilityReason.ADMITTED)


@dataclass(frozen=True, slots=True)
class AdmissibilityDecision:
    """One immutable admission decision, and why.

    ``admitted`` and ``reason`` cannot disagree: the constructor enforces that
    ``admitted is True`` exactly when the reason is ``ADMITTED``. A decision
    object that claimed admission while carrying a refusal reason would be a
    lookahead hole disguised as a record."""

    admitted: bool
    reason: AdmissibilityReason
    as_of: Optional[datetime] = None
    known_at: Optional[datetime] = None
    known_at_basis: Optional[str] = None
    detail: str = ""
    schema_version: str = SCHEMA_VERSION
    contract_type: str = CONTRACT_TYPE

    def __post_init__(self) -> None:
        if not isinstance(self.reason, AdmissibilityReason):
            raise ValueError("reason must be an AdmissibilityReason")
        expected = self.reason is AdmissibilityReason.ADMITTED
        if self.admitted is not expected:
            raise ValueError(
                f"admitted={self.admitted} contradicts reason={self.reason.value}"
            )

    def __bool__(self) -> bool:
        return self.admitted

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["reason"] = self.reason.value
        return d


def _is_aware(value: datetime) -> bool:
    """True iff the datetime carries a usable UTC offset.

    ``tzinfo is not None`` alone is insufficient — a tzinfo whose utcoffset()
    returns None is still naive, and comparing it would raise."""
    return value.tzinfo is not None and value.tzinfo.utcoffset(value) is not None


def is_admissible(pit: PointInTime, as_of: datetime) -> AdmissibilityDecision:
    """Decide whether evidence with this PIT envelope may be read as of ``as_of``.

    Pure and total: every input produces a decision rather than an exception, so
    a caller iterating a corpus cannot accidentally skip malformed evidence by
    catching an error. Malformed input yields an explicit refusal reason."""
    # --- the as-of boundary must itself be usable -------------------------
    if not isinstance(as_of, datetime):
        return AdmissibilityDecision(
            admitted=False, reason=AdmissibilityReason.AS_OF_NOT_A_DATETIME,
            detail=f"as_of must be a datetime, got {type(as_of).__name__}")
    if not _is_aware(as_of):
        # Refused rather than coerced: assuming a timezone for the boundary
        # would silently shift it by hours, which is precisely how lookahead
        # enters a backtest.
        return AdmissibilityDecision(
            admitted=False, reason=AdmissibilityReason.AS_OF_NOT_TIMEZONE_AWARE,
            as_of=as_of,
            detail="as_of must be timezone-aware; it is never assumed to be UTC")

    if not isinstance(pit, PointInTime):
        return AdmissibilityDecision(
            admitted=False, reason=AdmissibilityReason.NOT_A_POINT_IN_TIME,
            as_of=as_of,
            detail=f"pit must be a PointInTime, got {type(pit).__name__}")

    # --- evidence timing must be able to bound lookahead ------------------
    if pit.known_at is None:
        return AdmissibilityDecision(
            admitted=False, reason=AdmissibilityReason.KNOWN_AT_UNKNOWN,
            as_of=as_of, known_at=None, known_at_basis=pit.known_at_basis,
            detail=("known_at is unknown, so the earliest defensible moment this "
                    "evidence could have been used cannot be established; absent "
                    "timing is not permission"))

    # Coherence before comparison: evidence claiming it was knowable before its
    # own publication cannot bound lookahead, whatever the as_of.
    if pit.published_at is not None and pit.known_at < pit.published_at:
        return AdmissibilityDecision(
            admitted=False,
            reason=AdmissibilityReason.KNOWN_AT_BEFORE_PUBLISHED_AT,
            as_of=as_of, known_at=pit.known_at, known_at_basis=pit.known_at_basis,
            detail=("known_at precedes published_at; the evidence claims it was "
                    "knowable before it was released, so its timing cannot be "
                    "trusted to bound lookahead"))

    # --- the anti-lookahead rule named by the 0B kernel -------------------
    if pit.known_at > as_of:
        return AdmissibilityDecision(
            admitted=False, reason=AdmissibilityReason.KNOWN_AT_AFTER_AS_OF,
            as_of=as_of, known_at=pit.known_at, known_at_basis=pit.known_at_basis,
            detail=("known_at is after as_of; this evidence was not knowable at "
                    "the read instant"))

    # known_at == as_of is ADMITTED: the kernel specifies "<= as_of", and
    # known_at is already the EARLIEST defensible moment, so equality means the
    # information was usable exactly then.
    return AdmissibilityDecision(
        admitted=True, reason=AdmissibilityReason.ADMITTED,
        as_of=as_of, known_at=pit.known_at, known_at_basis=pit.known_at_basis,
        detail="known_at <= as_of with coherent timing")


def require_admissible(pit: PointInTime, as_of: datetime) -> AdmissibilityDecision:
    """Admissibility as an assertion, for callers that must not proceed.

    Provided so that a consumer choosing to fail loudly does not hand-roll the
    check and get the boundary subtly wrong."""
    decision = is_admissible(pit, as_of)
    if not decision.admitted:
        raise AdmissibilityError(
            f"evidence not admissible as of {as_of.isoformat() if isinstance(as_of, datetime) else as_of!r}: "
            f"{decision.reason.value} — {decision.detail}")
    return decision
