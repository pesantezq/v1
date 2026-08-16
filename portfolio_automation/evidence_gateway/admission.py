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

WHAT INTEGRITY THIS ACTUALLY PROVES — stated precisely, because an earlier
version of this module overstated it and a senior review caught the false claim.

The three guarantees are NOT the same strength, and are separated deliberately:

A. Guaranteed by ``EvidenceSnapshot`` CONSTRUCTION.
   ``payload_canonical`` and ``payload_hash`` are both derived from the payload
   at construction and cannot disagree for a normally-built object. The gateway
   cannot add anything here.

B. INDEPENDENTLY RECHECKED HERE.
   The payload's canonical form is re-parsed and re-hashed, and the result is
   compared against the stored ``payload_hash``. This is a real recomputation
   from stored content — not a read-back of the stored hash, which is what an
   earlier draft did and which proved nothing. It catches evidence that was
   reconstructed outside the constructor and then altered: a mutated
   ``payload_canonical`` no longer hashes to its recorded ``payload_hash``, and
   a mutated ``payload_hash`` no longer matches its content.

C. Available ONLY with an external anchor.
   ``snapshot_id`` is a DERIVED PROPERTY with no stored counterpart, so the
   gateway has nothing to compare it against and cannot detect an "id mismatch"
   on a bare snapshot — there is no second value to disagree with. Identity is
   anchored only when the caller supplies an ``EvidenceRef``, whose
   ``snapshot_id`` and ``payload_hash`` were recorded elsewhere, or when the
   snapshot came through ``EvidenceSnapshot.from_dict``, which already rejects a
   serialized id that does not reproduce.

There is deliberately no SNAPSHOT_ID_MISMATCH reason code. A branch that can
never fire would make the API look stronger than it is, which is exactly the
kind of claim this repair exists to remove.

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
from portfolio_automation.northstar.canonical import content_hash
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
    # payload content does not hash to its recorded payload_hash
    PAYLOAD_HASH_MISMATCH = "PAYLOAD_HASH_MISMATCH"
    # payload canonical form is unusable (cannot be parsed or re-hashed)
    PAYLOAD_NOT_CANONICAL = "PAYLOAD_NOT_CANONICAL"
    # NOTE: there is deliberately NO SNAPSHOT_ID_MISMATCH. snapshot_id is a
    # derived property with no stored counterpart, so on a bare snapshot there
    # is no second value to disagree with and such a branch could never fire.
    # Identity anchoring comes from an EvidenceRef (below) or from
    # EvidenceSnapshot.from_dict, which already rejects a non-reproducing id.
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

    # 2. PAYLOAD INTEGRITY — a genuine recomputation from stored content.
    #    Re-parse the canonical payload and re-hash it, then compare against the
    #    recorded payload_hash. Reading snapshot.payload_hash back and calling it
    #    "recomputed" (as an earlier draft did) proves nothing: both values would
    #    come from the same stored field. Hashing the stored CONTENT is what
    #    makes a post-construction alteration visible.
    try:
        recomputed_hash = content_hash(snapshot.payload_copy())
    except Exception as exc:  # noqa: BLE001 — malformed evidence must not escape
        return AdmissionDecision(
            admitted=False, reason=AdmissionReason.PAYLOAD_NOT_CANONICAL,
            pit_decision=pit_decision,
            detail=f"payload could not be parsed or re-hashed: {type(exc).__name__}")

    if recomputed_hash != snapshot.payload_hash:
        return AdmissionDecision(
            admitted=False, reason=AdmissionReason.PAYLOAD_HASH_MISMATCH,
            pit_decision=pit_decision,
            detail=("payload content does not hash to its recorded payload_hash; "
                    "the evidence was altered after construction"))

    # snapshot_id is derived, so it is computed here for reporting only — NOT
    # as an integrity check. See the module docstring, guarantee (C).
    recomputed_id = snapshot.snapshot_id

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
