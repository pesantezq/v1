"""
Common interface + structured result for Crowd Radar source connectors.

The cardinal rule: **no method ever raises into the pipeline.** Every failure
mode maps to a :class:`SourceResult` carrying a :class:`SourceStatus`. This keeps
the multi-source aggregator simple — it can treat zero / one / partial / all-
degraded sources uniformly because each source always hands back a result object.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from portfolio_automation.social_intelligence.base import SourceStatus, utc_now_iso

# Statuses that mean "this source contributed usable records this run".
ACTIVE_OK_STATUSES: frozenset[SourceStatus] = frozenset({SourceStatus.OK, SourceStatus.DEGRADED})

# Statuses that mean "intentionally not collecting — do not treat as a failure".
INERT_STATUSES: frozenset[SourceStatus] = frozenset({
    SourceStatus.DISABLED,
    SourceStatus.NO_CREDENTIALS,
    SourceStatus.NOT_CONFIGURED,
    SourceStatus.NOT_ENTITLED,
    SourceStatus.REQUIRES_MANUAL_REVIEW,
    SourceStatus.BLOCKED_NO_EXTRA_COST,
    SourceStatus.MANUAL_REFERENCE_ONLY,
})


@dataclass
class SourceResult:
    """Uniform return value for every connector method."""

    source_name: str
    status: SourceStatus
    records: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    fetched_at: str = field(default_factory=utc_now_iso)

    @property
    def contributed(self) -> bool:
        """True when this source produced usable records this run."""
        return self.status in ACTIVE_OK_STATUSES and bool(self.records)

    @property
    def is_failure(self) -> bool:
        """True for genuine failures (not inert/intentional-off states)."""
        return self.status in (SourceStatus.ERROR, SourceStatus.RATE_LIMITED,
                               SourceStatus.BUDGET_EXHAUSTED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "status": self.status.value if isinstance(self.status, SourceStatus) else str(self.status),
            "record_count": len(self.records),
            "records": self.records,
            "warnings": list(self.warnings),
            "meta": dict(self.meta),
            "fetched_at": self.fetched_at,
        }

    def health_dict(self) -> dict[str, Any]:
        """Compact health view (no records) for the source-health artifact."""
        d = self.to_dict()
        d.pop("records", None)
        return d


@runtime_checkable
class CrowdSource(Protocol):
    """
    The connector contract. Implementations must never raise; on any failure they
    return a SourceResult with the appropriate SourceStatus.

    - ``is_configured()`` → cheap, no network: is this source switched on + has
      whatever credentials/policy it needs to even attempt collection?
    - ``probe()``        → minimal entitlement check (one tiny request at most).
    - ``fetch()``        → pull raw aggregate records (bounded; rate-limit polite).
    - ``normalize()``    → map raw records to the common crowd-record schema.
    - ``health()``       → status snapshot for the source-health artifact.
    """

    source_name: str

    def is_configured(self) -> bool: ...
    def probe(self) -> SourceResult: ...
    def fetch(self) -> SourceResult: ...
    def normalize(self, raw: SourceResult) -> SourceResult: ...
    def health(self) -> SourceResult: ...
