"""Backend truth states and capability readiness for the Worker Control Center.

WHY THIS EXISTS.

The dashboard previously had exactly one way to say "we do not have this":
``PENDING_BACKEND``. That single value was carrying at least three different
meanings — nobody has built the producer, the producer exists but cannot answer
right now, and we hold a value but cannot tell whether it is still true. An
operator reading the dashboard could not distinguish engineering incompleteness
from an operational outage, which is precisely the distinction that decides
whether to wait, to investigate, or to intervene.

THE FIVE STATES ARE NOT SYNONYMS.

  LIVE             an authoritative value exists and is within its freshness
                   threshold
  STALE            an authoritative value exists, carries a VALID timestamp, and
                   that timestamp is older than the threshold
  PENDING_BACKEND  the interface expects this capability but no producer has
                   been built — engineering incompleteness
  UNAVAILABLE      the producer exists and cannot currently return a usable
                   value — an operational condition
  UNKNOWN          the truth state cannot be determined from available evidence

MISSING TIMESTAMP IS ``UNKNOWN``, NEVER ``STALE``.

Calling an untimestamped value stale would be a guess dressed as a measurement:
it asserts the value is old when the honest statement is that its age is
unknowable. That guess is attractive because it looks conservative, and it is
the specific mistake this module refuses to make.

READINESS IS ABOUT CAPABILITY, NOT ARITHMETIC.

A percentage of LIVE fields is the wrong summary: a dashboard can carry dozens
of live cosmetic fields while the operator still cannot see what the worker is
doing. Readiness is therefore decided by whether whole CAPABILITY GROUPS are
usable, with the groups needed for oversight treated as required. Raw counts are
still emitted, but only as diagnostics.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Sequence

from portfolio_automation.engineer_worker import EXPERIMENTAL_MARKER

SCHEMA_KIND = EXPERIMENTAL_MARKER
TRUTH_SCHEMA_VERSION = "engineering.control_center_truth.v0"


class TruthState(str, Enum):
    LIVE = "LIVE"
    STALE = "STALE"
    PENDING_BACKEND = "PENDING_BACKEND"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


#: Named thresholds, in seconds. A buried literal would make the freshness rule
#: invisible at the call site and impossible to review. Distinct classes are
#: kept because different capabilities genuinely age at different rates -- a
#: heartbeat is stale in minutes, an authority grant is not.
FRESHNESS_SECONDS: dict[str, int] = {
    "heartbeat": 300,          # 5 min — liveness signal
    "supervisor": 900,         # 15 min — verification activity
    "verification": 86_400,    # 24 h — recorded verdicts age slowly
    "default": 3_600,          # 1 h
}


def _parse(ts: Any) -> Optional[_dt.datetime]:
    """Parse an ISO-8601 instant, or None if it cannot be trusted.

    Returns None rather than raising: an unparseable timestamp is evidence we
    cannot decide freshness, which is UNKNOWN -- not an error to swallow and not
    grounds to call something stale."""
    if not isinstance(ts, str) or not ts.strip():
        return None
    try:
        parsed = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # A naive timestamp cannot be compared to an aware reference time
        # without inventing a zone, and inventing one shifts the age silently.
        return None
    return parsed


def classify(*, producer_exists: bool, value: Any, recorded_at: Any = None,
             now: Any = None, threshold: str = "default",
             requires_freshness: bool = True) -> TruthState:
    """Decide one capability's truth state from evidence alone.

    ``producer_exists`` is the engineering fact: has anything been built that
    can supply this? It is checked FIRST, because a missing producer is not an
    outage and must never be reported as one.

    ``requires_freshness=False`` is for values whose truth does not decay --
    an authority level read from a protected config file is as true now as when
    it was written, and demanding a timestamp for it would manufacture UNKNOWNs.
    """
    if not producer_exists:
        return TruthState.PENDING_BACKEND
    if value is None or value == "" or value == TruthState.PENDING_BACKEND.value:
        # The producer exists and did not yield a usable value: operational,
        # not structural.
        return TruthState.UNAVAILABLE
    if not requires_freshness:
        return TruthState.LIVE

    stamped = _parse(recorded_at)
    reference = _parse(now)
    if stamped is None or reference is None:
        # We hold a value but cannot say whether it is still true. Saying STALE
        # here would assert an age we do not know.
        return TruthState.UNKNOWN
    age = (reference - stamped).total_seconds()
    limit = FRESHNESS_SECONDS.get(threshold, FRESHNESS_SECONDS["default"])
    if age < 0:
        # A value stamped in the future relative to the evaluation time is not
        # fresh evidence; it is unexplained.
        return TruthState.UNKNOWN
    return TruthState.LIVE if age <= limit else TruthState.STALE


class Readiness(str, Enum):
    READY = "READY"
    MOSTLY_LIVE = "MOSTLY_LIVE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class Capability:
    """One named oversight capability and how its state was decided."""

    name: str
    state: TruthState
    #: Required capabilities are the ones an operator needs in order to trust
    #: what the dashboard says about the worker. Secondary capabilities are
    #: useful but their absence does not blind the operator.
    required: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"capability": self.name, "state": self.state.value,
                "required": self.required, "detail": self.detail}


@dataclass(frozen=True)
class ReadinessAssessment:
    readiness: Readiness
    capabilities: tuple[Capability, ...]
    reasons: tuple[str, ...] = ()

    @property
    def counts(self) -> dict[str, int]:
        """Diagnostics only. Never the basis of the readiness decision."""
        out = {s.value: 0 for s in TruthState}
        for cap in self.capabilities:
            out[cap.state.value] += 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": TRUTH_SCHEMA_VERSION, "schema_kind": SCHEMA_KIND,
                "readiness": self.readiness.value,
                "capabilities": [c.to_dict() for c in self.capabilities],
                "state_counts": self.counts,
                "reasons": list(self.reasons)}


#: Capabilities whose loss makes the dashboard untrustworthy as an oversight
#: surface. Without these an operator cannot answer "what is it doing, and what
#: is it allowed to do?"
_OVERSIGHT_FLOOR = ("controller_state", "worker_authority")


def assess_readiness(capabilities: Sequence[Capability]) -> ReadinessAssessment:
    """Derive readiness from capability groups, never from a LIVE percentage.

    The ordering matters. If the oversight floor itself is not established the
    dashboard cannot be trusted at all, and that outranks any number of healthy
    secondary fields."""
    caps = tuple(capabilities)
    by_name = {c.name: c for c in caps}
    usable = {TruthState.LIVE}

    floor_broken = [n for n in _OVERSIGHT_FLOOR
                    if n not in by_name or by_name[n].state not in usable]
    if floor_broken:
        return ReadinessAssessment(
            Readiness.UNAVAILABLE, caps,
            tuple(f"oversight floor not established: {n}" for n in floor_broken))

    required_gaps = [c for c in caps if c.required and c.state not in usable]
    if required_gaps:
        return ReadinessAssessment(
            Readiness.PARTIAL, caps,
            tuple(f"required capability {c.name} is {c.state.value}"
                  for c in required_gaps))

    secondary_gaps = [c for c in caps if not c.required and c.state not in usable]
    if secondary_gaps:
        return ReadinessAssessment(
            Readiness.MOSTLY_LIVE, caps,
            tuple(f"secondary capability {c.name} is {c.state.value}"
                  for c in secondary_gaps))
    return ReadinessAssessment(Readiness.READY, caps, ())
