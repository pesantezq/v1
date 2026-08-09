"""Point-in-time semantics for Northstar evidence (Phase 0B kernel).

The five timestamps are NOT interchangeable:

observed_at
    When the underlying event/value occurred or was observed
    (e.g. the market-price timestamp).
published_at
    When the provider/source released the information
    (e.g. an earnings filing's publication moment).
known_at
    The earliest DEFENSIBLE moment StockBot could have known and used the
    information. This is the future anti-lookahead authority: Phase 0C will
    enforce ``evidence.known_at <= experiment.as_of``.
retrieved_at
    When StockBot acquired THIS copy of the information.
effective period
    The business/economic period the information refers to (e.g. fiscal
    2026-Q2 ended 2026-06-30 but published 2026-08-05 — a backtest as of
    July 15 must NOT see it merely because the period ended in June).

NO FABRICATED TIME. Historical data often lacks defensible publication/known
timestamps; that is represented EXPLICITLY (field absent + basis "unknown"),
never silently backfilled. In particular ``known_at = retrieved_at`` is never
set implicitly — the one sanctioned conservative derivation is the explicit
constructor :meth:`PointInTime.with_conservative_known_at`, which records
``known_at_basis="derived_conservative"``. (Conservative because information
cannot have been usable before we possessed it; the true known_at may be
earlier, so this rule can only under-claim knowledge, never lookahead.)
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Optional

from portfolio_automation.northstar.canonical import (
    CanonicalizationError,
    encode_datetime,
)

# How a known_at value came to be known (kept deliberately minimal).
KNOWN_AT_SOURCE_REPORTED = "source_reported"      # provider supplied a defensible timestamp
KNOWN_AT_SYSTEM_OBSERVED = "system_observed"      # StockBot itself observed it live
KNOWN_AT_DERIVED_CONSERVATIVE = "derived_conservative"  # explicit known_at := retrieved_at rule
KNOWN_AT_UNKNOWN = "unknown"                      # no defensible timing — represented, not invented

TIMING_BASES = frozenset({
    KNOWN_AT_SOURCE_REPORTED,
    KNOWN_AT_SYSTEM_OBSERVED,
    KNOWN_AT_DERIVED_CONSERVATIVE,
    KNOWN_AT_UNKNOWN,
})


def _require_aware(name: str, value: Optional[datetime]) -> None:
    if value is None:
        return
    # encode_datetime is the single arbiter of tz-awareness.
    try:
        encode_datetime(value)
    except CanonicalizationError as exc:
        raise ValueError(f"{name}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class PointInTime:
    """The PIT envelope carried by every EvidenceSnapshot.

    All datetime fields are optional (absence = explicitly unknown) but must
    be timezone-aware when present. The effective period uses dates because it
    names a business period, not an instant; ``effective_period_label`` is a
    human label such as ``"2026-Q2"`` and carries no identity weight beyond
    its text.
    """

    observed_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    known_at: Optional[datetime] = None
    known_at_basis: str = KNOWN_AT_UNKNOWN
    retrieved_at: Optional[datetime] = None
    effective_period_start: Optional[date] = None
    effective_period_end: Optional[date] = None
    effective_period_label: Optional[str] = None

    def __post_init__(self) -> None:
        for name in ("observed_at", "published_at", "known_at", "retrieved_at"):
            _require_aware(name, getattr(self, name))
        if self.known_at_basis not in TIMING_BASES:
            raise ValueError(
                f"known_at_basis must be one of {sorted(TIMING_BASES)}, "
                f"got {self.known_at_basis!r}"
            )
        # No fabricated time, in both directions:
        if self.known_at is not None and self.known_at_basis == KNOWN_AT_UNKNOWN:
            raise ValueError(
                "known_at is set but known_at_basis is 'unknown' — a known_at "
                "value requires a documented basis"
            )
        if self.known_at is None and self.known_at_basis != KNOWN_AT_UNKNOWN:
            raise ValueError(
                f"known_at_basis={self.known_at_basis!r} without a known_at value"
            )
        if (
            self.effective_period_start is not None
            and self.effective_period_end is not None
            and self.effective_period_end < self.effective_period_start
        ):
            raise ValueError("effective_period_end precedes effective_period_start")

    def with_conservative_known_at(self) -> "PointInTime":
        """The ONLY sanctioned known_at derivation: known_at := retrieved_at.

        Explicit, recorded as ``derived_conservative``, and only available
        when retrieved_at exists and known_at is genuinely unknown.
        """
        if self.known_at is not None:
            raise ValueError("known_at already set — refusing to overwrite")
        if self.retrieved_at is None:
            raise ValueError("cannot derive known_at without retrieved_at")
        return replace(
            self,
            known_at=self.retrieved_at,
            known_at_basis=KNOWN_AT_DERIVED_CONSERVATIVE,
        )

    def to_canonical_dict(self) -> dict:
        """Canonical mapping (datetimes encoded by the kernel serializer)."""
        return {
            "observed_at": self.observed_at,
            "published_at": self.published_at,
            "known_at": self.known_at,
            "known_at_basis": self.known_at_basis,
            "retrieved_at": self.retrieved_at,
            "effective_period_start": self.effective_period_start,
            "effective_period_end": self.effective_period_end,
            "effective_period_label": self.effective_period_label,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PointInTime":
        from portfolio_automation.northstar.serde import parse_optional_date, parse_optional_datetime

        return cls(
            observed_at=parse_optional_datetime(data.get("observed_at")),
            published_at=parse_optional_datetime(data.get("published_at")),
            known_at=parse_optional_datetime(data.get("known_at")),
            known_at_basis=data.get("known_at_basis", KNOWN_AT_UNKNOWN),
            retrieved_at=parse_optional_datetime(data.get("retrieved_at")),
            effective_period_start=parse_optional_date(data.get("effective_period_start")),
            effective_period_end=parse_optional_date(data.get("effective_period_end")),
            effective_period_label=data.get("effective_period_label"),
        )
