"""Trusted review-packet builder with a fail-closed completeness gate.

WHY THIS EXISTS.

An independent reviewer returned REPAIR on a Northstar 0C repair because the
packet it received did not contain the artifacts proving the criteria it was
asked to judge: it got implementation source and test NAMES and COUNTS, but not
the test source or the read-model diff. It could not verify, so it refused.

That refusal was the correct outcome, but it depended on the reviewer being
careful. A less careful reviewer would have PASSed an under-evidenced packet,
and the controller would have collected a green verdict for a claim nobody
actually checked. The failure mode is therefore not "the reviewer was strict" —
it is "the controller was allowed to ask a question it had not earned".

So completeness moves OUT of the controller's discipline and INTO deterministic
verification:

    criterion  ->  declared required evidence  ->  present?  ->  dispatch
                                              ->  absent?   ->  REFUSE, never call

The reviewer must never be the mechanism that discovers missing evidence.

WHAT THIS IS NOT. It does not judge whether evidence is CONVINCING — that is the
reviewer's job and cannot be automated. It only proves that what the controller
declared necessary is actually present. A packet can be complete and still earn
REPAIR on the merits; that is a healthy outcome, not a gate failure.

``experimental_noncanonical``.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Optional

from portfolio_automation.engineer_worker import EXPERIMENTAL_MARKER, supervisor_screen

SCHEMA_KIND = EXPERIMENTAL_MARKER
PACKET_SCHEMA_VERSION = "engineering.review_packet.v0"

#: Hard ceiling on packet size. Exceeding it FAILS CLOSED rather than truncating:
#: silently dropping authority-critical evidence is the exact defect this module
#: exists to prevent, and a truncated packet is an incomplete packet.
MAX_PACKET_BYTES = 220 * 1024


class EvidenceKind(str, Enum):
    """What KIND of artifact proves a claim.

    The distinction is load-bearing. A criterion whose semantics require reading
    code cannot be satisfied by a test COUNT, and declaring the kind is what lets
    the gate refuse that substitution deterministically instead of relying on the
    controller to notice."""

    SOURCE = "SOURCE"                 # actual file contents
    DIFF = "DIFF"                     # actual before/after change
    TEST_SOURCE = "TEST_SOURCE"       # the test body that constructs the case
    TEST_RESULT = "TEST_RESULT"       # pass/fail outcome
    TEST_COUNT = "TEST_COUNT"         # aggregate only — never sufficient alone
    LEDGER_RECORD = "LEDGER_RECORD"   # recorded controller evidence
    PROSE = "PROSE"                   # explanation — never sufficient alone

#: Kinds that can never, on their own, prove a semantic criterion. Aggregate
#: counts and prose are SUPPORTING evidence: they describe the proof rather than
#: being it. This encodes the specific mistake that caused the REPAIR.
INSUFFICIENT_ALONE = frozenset({EvidenceKind.TEST_COUNT, EvidenceKind.PROSE})


class PacketError(ValueError):
    """Deterministic, fail-closed packet error."""


@dataclass(frozen=True)
class Criterion:
    """One thing the reviewer is asked to judge, and what would prove it."""

    criterion_id: str
    claim: str
    required_evidence: tuple[EvidenceKind, ...]
    required_artifacts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.criterion_id or not self.claim:
            raise PacketError("criterion_id and claim are required")
        if not self.required_evidence:
            raise PacketError(
                f"{self.criterion_id}: a criterion must declare what would prove it")
        if all(k in INSUFFICIENT_ALONE for k in self.required_evidence):
            raise PacketError(
                f"{self.criterion_id}: cannot be proven by counts/prose alone — "
                "declare the artifact a reviewer must actually read")


@dataclass
class Evidence:
    """One supplied artifact."""

    artifact_id: str
    kind: EvidenceKind
    content: str = ""
    detail: str = ""

    def is_substantive(self) -> bool:
        """Present AND non-empty. A declared-but-empty artifact is not evidence."""
        if self.kind in INSUFFICIENT_ALONE:
            return bool(self.detail or self.content)
        return bool(self.content.strip())


@dataclass
class CompletenessResult:
    complete: bool
    missing: list[dict[str, Any]] = field(default_factory=list)
    satisfied: list[str] = field(default_factory=list)
    blocked_by_screen: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"packet_complete": "YES" if self.complete else "NO",
                "missing_evidence": self.missing, "satisfied_criteria": self.satisfied,
                "blocked_by_secret_screen": self.blocked_by_screen,
                "reason": self.reason}


@dataclass
class ReviewPacket:
    """A bounded, criterion-driven review request bound to a candidate SHA."""

    candidate_sha: str
    mission_id: str
    task_id: str
    session_id: Optional[str] = None
    prior_verdicts: list[dict[str, Any]] = field(default_factory=list)
    criteria: list[Criterion] = field(default_factory=list)
    evidence: dict[str, Evidence] = field(default_factory=dict)
    manifest: list[dict[str, Any]] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)

    def add_criterion(self, criterion: Criterion) -> None:
        self.criteria.append(criterion)

    def add_evidence(self, evidence: Evidence) -> None:
        self.evidence[evidence.artifact_id] = evidence

    # -- completeness -----------------------------------------------------
    def check_completeness(self, *, screen: bool = True) -> CompletenessResult:
        """Prove every criterion's declared evidence is actually present.

        Fail-closed at every branch: an unknown artifact, an empty artifact, a
        criterion with no manifest entry, or an artifact the secret screen
        refuses all block dispatch."""
        result = CompletenessResult(complete=True)
        if not self.criteria:
            result.complete = False
            result.reason = "no criteria declared; nothing to prove"
            return result

        for criterion in self.criteria:
            gaps: list[str] = []

            # Every declared artifact must exist and be substantive.
            for artifact_id in criterion.required_artifacts:
                item = self.evidence.get(artifact_id)
                if item is None:
                    gaps.append(f"artifact absent: {artifact_id}")
                elif not item.is_substantive():
                    gaps.append(f"artifact present but empty: {artifact_id}")

            # Every declared evidence KIND must be represented by a substantive
            # artifact. This is what refuses counts-without-source: the criterion
            # declares TEST_SOURCE, and a TEST_COUNT cannot satisfy it.
            supplied_kinds = {e.kind for e in self.evidence.values() if e.is_substantive()}
            for kind in criterion.required_evidence:
                if kind not in supplied_kinds:
                    gaps.append(f"required evidence kind missing: {kind.value}")

            if gaps:
                result.complete = False
                result.missing.append({"criterion_id": criterion.criterion_id,
                                       "claim": criterion.claim, "gaps": gaps})
            else:
                result.satisfied.append(criterion.criterion_id)

        # Any criterion referenced by the manifest but never declared, or declared
        # without a manifest entry, is a controller bookkeeping error -> fail closed.
        declared = {c.criterion_id for c in self.criteria}
        manifested = {m.get("criterion_id") for m in self.manifest}
        if manifested and manifested != declared:
            result.complete = False
            result.missing.append({
                "criterion_id": "MANIFEST", "claim": "manifest covers every criterion",
                "gaps": [f"manifest/criteria mismatch: "
                         f"only_in_manifest={sorted(manifested - declared)} "
                         f"only_in_criteria={sorted(declared - manifested)}"]})

        # Secret screening runs BEFORE dispatch. A refused artifact is reported as
        # withheld — never summarized around and called equivalent evidence.
        if screen:
            for artifact_id, item in sorted(self.evidence.items()):
                if not item.content:
                    continue
                if supervisor_screen.screen_text(item.content, artifact_id).blocked:
                    result.complete = False
                    result.blocked_by_screen.append(artifact_id)

        if result.blocked_by_screen:
            result.missing.append({
                "criterion_id": "SECRET_SCREEN",
                "claim": "all evidence transmissible without weakening screening",
                "gaps": [f"refused by supervisor screen: {a}"
                         for a in result.blocked_by_screen]})

        if not result.reason:
            result.reason = ("all declared criterion evidence present"
                             if result.complete else "declared evidence missing")
        return result

    # -- identity ---------------------------------------------------------
    def to_supervisor_packet(self) -> dict[str, Any]:
        """The bounded artifact actually sent to the independent reviewer."""
        return {
            "schema_version": PACKET_SCHEMA_VERSION, "schema_kind": SCHEMA_KIND,
            "candidate_sha": self.candidate_sha, "mission_id": self.mission_id,
            "task": {"task_id": self.task_id, "session_id": self.session_id},
            "prior_verdicts": self.prior_verdicts,
            "criteria": [{"criterion_id": c.criterion_id, "claim": c.claim,
                          "required_evidence": [k.value for k in c.required_evidence],
                          "required_artifacts": list(c.required_artifacts)}
                         for c in self.criteria],
            "evidence_manifest": self.manifest,
            "source_files": [{"path": e.artifact_id, "content": e.content}
                             for e in self.evidence.values()
                             if e.kind in (EvidenceKind.SOURCE, EvidenceKind.TEST_SOURCE,
                                           EvidenceKind.DIFF) and e.content],
            "supporting_evidence": [{"artifact_id": e.artifact_id, "kind": e.kind.value,
                                     "detail": e.detail}
                                    for e in self.evidence.values()
                                    if e.kind in (EvidenceKind.TEST_RESULT,
                                                  EvidenceKind.TEST_COUNT,
                                                  EvidenceKind.LEDGER_RECORD,
                                                  EvidenceKind.PROSE)],
            **self.context,
        }

    def packet_hash(self) -> str:
        """Deterministic identity over the packet CONTENT.

        Changing any included evidence changes the hash, so a verdict can be
        bound to exactly what was reviewed and cannot be silently reattached to a
        different packet."""
        blob = json.dumps(self.to_supervisor_packet(), sort_keys=True,
                          ensure_ascii=True, default=str)
        return "pkt_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]

    def size_bytes(self) -> int:
        return len(json.dumps(self.to_supervisor_packet(), default=str).encode("utf-8"))


@dataclass
class DispatchResult:
    dispatched: bool
    completeness: CompletenessResult
    packet_hash: Optional[str] = None
    candidate_sha: Optional[str] = None
    decision: Any = None
    next_action: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"review_dispatched": self.dispatched,
                "packet_hash": self.packet_hash, "candidate_sha": self.candidate_sha,
                "next_action": self.next_action, **self.completeness.to_dict()}


def dispatch_review(packet: ReviewPacket, reviewer: Callable[[dict[str, Any]], Any],
                    *, expected_sha: Optional[str] = None,
                    screen: bool = True) -> DispatchResult:
    """Gate, then dispatch. The reviewer is called ONLY on a complete packet.

    ``expected_sha`` binds the packet to the candidate the caller believes it is
    reviewing; a mismatch fails closed, so a packet built from one tree cannot be
    presented as evidence for another."""
    if expected_sha is not None and packet.candidate_sha != expected_sha:
        completeness = CompletenessResult(
            complete=False,
            missing=[{"criterion_id": "CANDIDATE_SHA",
                      "claim": "packet describes the candidate under review",
                      "gaps": [f"packet sha {packet.candidate_sha!r} != "
                               f"expected {expected_sha!r}"]}],
            reason="candidate sha mismatch")
        return DispatchResult(False, completeness, next_action="REPAIR_PACKET")

    completeness = packet.check_completeness(screen=screen)

    if completeness.complete and packet.size_bytes() > MAX_PACKET_BYTES:
        # Fail closed rather than truncate: dropping authority-critical evidence
        # to fit a limit is precisely the defect being prevented.
        completeness.complete = False
        completeness.reason = "packet exceeds size bound"
        completeness.missing.append({
            "criterion_id": "PACKET_SIZE",
            "claim": "complete evidence fits within the transmissible bound",
            "gaps": [f"{packet.size_bytes()} bytes > {MAX_PACKET_BYTES}; "
                     "narrow the criteria rather than truncating evidence"]})

    if not completeness.complete:
        return DispatchResult(False, completeness, packet_hash=packet.packet_hash(),
                              candidate_sha=packet.candidate_sha,
                              next_action="REPAIR_PACKET")

    decision = reviewer(packet.to_supervisor_packet())
    return DispatchResult(True, completeness, packet_hash=packet.packet_hash(),
                          candidate_sha=packet.candidate_sha, decision=decision,
                          next_action="VERDICT_RECORDED")
