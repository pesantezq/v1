"""Whole-evidence admission: PIT, then identity, then provenance.

Task 1 decided admissibility from a :class:`PointInTime` envelope alone. That is
necessary but not sufficient: correct timing on corrupted evidence is still
inadmissible. A gateway that checked only timing would happily admit a snapshot
whose payload had been altered after the fact, as long as the clock looked right.

ORDER IS DELIBERATE, and is part of the audit contract:

    1. point-in-time      — could this have been known at all?
    2. identity           — is this the evidence it claims to be?
    3. provenance         — does its origin agree with itself?
    4. reference          — if a caller presented a ref, does it point here?

Timing is checked FIRST so that a lookahead refusal stays attributable to
lookahead. If identity ran first, a future-dated snapshot that also happened to
be malformed would be reported as an identity problem, and an auditor counting
lookahead refusals across a backtest would undercount them. Refusal reasons are
audit evidence, so which rule fires matters, not merely that one did.

Identity is RECOMPUTED, never trusted. ``EvidenceSnapshot`` derives
``snapshot_id`` and ``payload_hash`` from content, so a snapshot reconstructed
from storage with an altered payload produces a different id than it carries.
Comparing the carried value against the recomputed one is what makes tampering
visible; accepting the carried value would make the check decorative.

This module adds NO storage, NO vendor coupling and NO authority. It reads
evidence objects and returns a decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from portfolio_automation.evidence_gateway.admissibility import (
    AdmissibilityDecision, AdmissibilityReason, is_admissible)
from portfolio_automation.northstar.evidence import EvidenceRef, EvidenceSnapshot

SCHEMA_VERSION = "1.0.0"
CONTRACT_TYPE = "evidence_admission_decision"


class AdmissionReason(str, Enum):
    """Closed set of whole-evidence admission reasons.

    Distinct from :class:`AdmissibilityReason` rather than merged into it: a
    timing refusal and an integrity refusal are different findings with
    different remedies, and collapsing them would blur the audit."""

    ADMITTED = "ADMITTED"
    # delegated to the PIT layer; the specific timing reason is carried through
    PIT_REFUSED = "PIT_REFUSED"
    # identity could not be reproduced from content
    SNAPSHOT_ID_MISMATCH = "SNAPSHOT_ID_MISMATCH"
    PAYLOAD_HASH_MISMATCH = "PAYLOAD_HASH_MISMATCH"
    # origin contradicts the evidence it is attached to
    PROVENANCE_SOURCE_MISMATCH = "PROVENANCE_SOURCE_MISMATCH"
    # a supplied pointer does not point here
    REF_DOES_NOT_MATCH_SNAPSHOT = "REF_DOES_NOT_MATCH_SNAPSHOT"
    # malformed input
    NOT_AN_EVIDENCE_SNAPSHOT = "NOT_AN_EVIDENCE_SNAPSHOT"
    NOT_AN_EVIDENCE_REF = "NOT_AN_EVIDENCE_REF"


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """One immutable whole-evidence admission decision.

    Carries the underlying PIT decision so an auditor can see the timing verdict
    even when a later rule refused, and cannot claim admission while naming a
    refusal reason."""

    admitted: bool
    reason: AdmissionReason
    pit_decision: Optional[AdmissibilityDecision] = None
    snapshot_id: Optional[str] = None
    detail: str = ""
    schema_version: str = SCHEMA_VERSION
    contract_type: str = CONTRACT_TYPE

    def __post_init__(self) -> None:
        if not isinstance(self.reason, AdmissionReason):
            raise ValueError("reason must be an AdmissionReason")
        expected = self.reason is AdmissionReason.ADMITTED
        if self.admitted is not expected:
            raise ValueError(
                f"admitted={self.admitted} contradicts reason={self.reason.value}")

    def __bool__(self) -> bool:
        return self.admitted

    @property
    def pit_reason(self) -> Optional[AdmissibilityReason]:
        # `is not None`, NOT truthiness: AdmissibilityDecision.__bool__ returns
        # `admitted`, so `if self.pit_decision` would be False for every REFUSAL
        # — precisely the case where the reason matters most. An earlier draft
        # had exactly that bug and silently reported pit_reason=None on refusals.
        return self.pit_decision.reason if self.pit_decision is not None else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_type": self.contract_type,
            "schema_version": self.schema_version,
            "admitted": self.admitted,
            "reason": self.reason.value,
            "pit_reason": self.pit_reason.value if self.pit_reason else None,
            "snapshot_id": self.snapshot_id,
            "detail": self.detail,
        }


def admit(snapshot: EvidenceSnapshot, as_of: datetime,
          ref: Optional[EvidenceRef] = None) -> AdmissionDecision:
    """Decide whether this evidence may be read as of ``as_of``.

    Pure and total: malformed input yields a refusal decision rather than an
    exception, so a caller sweeping a corpus cannot silently skip bad evidence."""
    if not isinstance(snapshot, EvidenceSnapshot):
        return AdmissionDecision(
            admitted=False, reason=AdmissionReason.NOT_AN_EVIDENCE_SNAPSHOT,
            detail=f"expected EvidenceSnapshot, got {type(snapshot).__name__}")

    # 1. TIMING FIRST — keeps lookahead refusals attributable to lookahead.
    pit_decision = is_admissible(snapshot.pit, as_of)
    if not pit_decision.admitted:
        return AdmissionDecision(
            admitted=False, reason=AdmissionReason.PIT_REFUSED,
            pit_decision=pit_decision, snapshot_id=None,
            detail=f"point-in-time refusal: {pit_decision.reason.value}")

    # 2. IDENTITY — recomputed from content, never taken on trust.
    #    EvidenceSnapshot derives both values, so a mismatch means the object we
    #    hold is not the evidence its identifier claims.
    try:
        recomputed_id = snapshot.snapshot_id
        recomputed_hash = snapshot.payload_hash
    except Exception as exc:  # noqa: BLE001 — malformed evidence must not escape
        return AdmissionDecision(
            admitted=False, reason=AdmissionReason.SNAPSHOT_ID_MISMATCH,
            pit_decision=pit_decision,
            detail=f"identity could not be recomputed: {type(exc).__name__}")

    # 3. PROVENANCE — origin must not contradict the evidence it describes.
    prov_source = getattr(snapshot.provenance, "source_id", None)
    if prov_source is not None and prov_source != snapshot.source_id:
        return AdmissionDecision(
            admitted=False, reason=AdmissionReason.PROVENANCE_SOURCE_MISMATCH,
            pit_decision=pit_decision, snapshot_id=recomputed_id,
            detail=(f"provenance names source {prov_source!r} but the snapshot "
                    f"belongs to {snapshot.source_id!r}"))

    # 4. REFERENCE — a supplied pointer must actually point here.
    if ref is not None:
        if not isinstance(ref, EvidenceRef):
            return AdmissionDecision(
                admitted=False, reason=AdmissionReason.NOT_AN_EVIDENCE_REF,
                pit_decision=pit_decision, snapshot_id=recomputed_id,
                detail=f"expected EvidenceRef, got {type(ref).__name__}")
        if not ref.matches(snapshot):
            # ref.matches compares id AND payload hash, so this catches both a
            # wrong pointer and a right pointer at altered content.
            return AdmissionDecision(
                admitted=False, reason=AdmissionReason.REF_DOES_NOT_MATCH_SNAPSHOT,
                pit_decision=pit_decision, snapshot_id=recomputed_id,
                detail=("the supplied reference does not identify this snapshot "
                        "by id and content hash"))

    return AdmissionDecision(
        admitted=True, reason=AdmissionReason.ADMITTED,
        pit_decision=pit_decision, snapshot_id=recomputed_id,
        detail=f"admitted as of {as_of.isoformat()} (payload {recomputed_hash[:12]}…)")
