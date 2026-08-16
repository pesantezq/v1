"""Revision visibility: what a reader could have seen at a historical instant.

THE SAFETY PROPERTY, and why the ORDER carries it:

    admit by what was knowable at as_of  FIRST
    interpret supersession relationships  SECOND

Evidence is immutable — a correction is a NEW snapshot linking back through
``supersedes_snapshot_id``. That means a revision written today physically
exists in today's corpus while reading history. If supersession were
interpreted first, that later revision would mark its predecessor superseded and
quietly remove it from a view of a time before the revision was knowable. The
correction would have rewritten history.

So a snapshot that fails PIT admission at ``as_of`` contributes NOTHING here: it
is not visible, and — the load-bearing half — it cannot supersede anything
either. Only admitted members may participate in supersession at all.

WHAT THIS DELIBERATELY DOES NOT DO.

There are two different questions, and only the first is answerable from the
contracts as they stand:

    1. SAFETY      which chain members were genuinely knowable at as_of?
    2. RESOLUTION  if several are knowable, which is the current value?

The repository establishes no authoritative answer to (2). No rule says latest
known_at wins, or deepest chain node, or highest retrieved_at, or latest
published_at. Inventing one here would bury a business policy inside a safety
mechanism, where nobody would look for it and every later consumer would inherit
it silently. So this module reports the admitted chain state and marks winner
selection UNRESOLVED, leaving the decision to a future policy layer that can be
reviewed as a policy.

``effective_period`` is likewise NOT consulted. Whether
``effective_period_end`` bounds an as-of read is an open contract question, and
partial-period evidence may legitimately extend past a read instant. Historical
visibility here is governed solely by the established ``known_at`` authority.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Optional

from portfolio_automation.evidence_gateway.admission import (
    AdmissionDecision, AdmissionReason, admit)
from portfolio_automation.northstar.evidence import EvidenceSnapshot

SCHEMA_VERSION = "1.0.0"
CONTRACT_TYPE = "evidence_revision_visibility"

#: Recorded on every result so a consumer cannot mistake silence for a decision.
WINNER_POLICY_STATUS = "UNRESOLVED_NOT_INVENTED"


class LinkState(str, Enum):
    """How a member's supersedes link resolves WITHIN the admitted view."""

    ROOT = "ROOT"                                   # declares no predecessor
    RESOLVED = "RESOLVED"                           # predecessor is admitted here
    PREDECESSOR_NOT_ADMITTED = "PREDECESSOR_NOT_ADMITTED"   # exists, not knowable yet
    PREDECESSOR_NOT_IN_CORPUS = "PREDECESSOR_NOT_IN_CORPUS"  # absent — never invented
    # There is deliberately NO SELF_REFERENTIAL state. A snapshot cannot
    # supersede itself: supersedes_snapshot_id is identity-bearing, so setting it
    # to the snapshot's own id CHANGES that id, and the reference no longer
    # points at the snapshot. Self-reference is unconstructible rather than
    # merely unlikely, and a branch that can never fire would make this module
    # look more defensive than it is.


@dataclass(frozen=True)
class VisibleMember:
    """One admitted snapshot and its link state within the admitted view."""

    snapshot_id: str
    supersedes_snapshot_id: Optional[str]
    link_state: LinkState
    superseded_by: tuple[str, ...] = ()   # admitted members naming this one

    @property
    def is_superseded_within_view(self) -> bool:
        """True only when a snapshot that WAS knowable at as_of supersedes this.

        A later revision cannot set this, because it never reaches the view."""
        return bool(self.superseded_by)

    def to_dict(self) -> dict[str, Any]:
        return {"snapshot_id": self.snapshot_id,
                "supersedes_snapshot_id": self.supersedes_snapshot_id,
                "link_state": self.link_state.value,
                "superseded_by": list(self.superseded_by),
                "is_superseded_within_view": self.is_superseded_within_view}


@dataclass(frozen=True)
class WithheldMember:
    """A snapshot excluded from the view, and why."""

    #: The id ADMISSION was willing to vouch for. None when the snapshot was
    #: refused before its identity was validated — absence here is a fact about
    #: how far admission got, not a missing value to be filled in.
    snapshot_id: Optional[str]
    reason: str
    pit_reason: Optional[str] = None
    #: The id the CORPUS used for this member. Reported separately so a withheld
    #: entry stays correlatable with the supersedes links that name it, without
    #: implying admission validated that identity.
    corpus_snapshot_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {"snapshot_id": self.snapshot_id, "reason": self.reason,
                "pit_reason": self.pit_reason,
                "corpus_snapshot_id": self.corpus_snapshot_id}


@dataclass
class RevisionVisibility:
    """Deterministic account of what was visible at ``as_of``, and why."""

    as_of: datetime
    visible: list[VisibleMember] = field(default_factory=list)
    withheld: list[WithheldMember] = field(default_factory=list)
    unresolved_links: list[dict[str, Any]] = field(default_factory=list)
    winner_policy: str = WINNER_POLICY_STATUS
    schema_version: str = SCHEMA_VERSION
    contract_type: str = CONTRACT_TYPE

    @property
    def visible_ids(self) -> tuple[str, ...]:
        return tuple(m.snapshot_id for m in self.visible)

    @property
    def withheld_ids(self) -> tuple[Optional[str], ...]:
        return tuple(m.snapshot_id for m in self.withheld)

    def member(self, snapshot_id: str) -> Optional[VisibleMember]:
        return next((m for m in self.visible if m.snapshot_id == snapshot_id), None)

    def is_visible(self, snapshot_id: str) -> bool:
        return self.member(snapshot_id) is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_type": self.contract_type,
            "schema_version": self.schema_version,
            "as_of": self.as_of.isoformat() if isinstance(self.as_of, datetime) else None,
            "visible": [m.to_dict() for m in self.visible],
            "withheld": [m.to_dict() for m in self.withheld],
            "unresolved_links": self.unresolved_links,
            "winner_policy": self.winner_policy,
            "winner_policy_note": (
                "No authoritative revision winner-selection policy exists in the "
                "contracts. This result reports which members were knowable; it does "
                "NOT name a current value."),
        }


def resolve_visibility(snapshots: Iterable[EvidenceSnapshot],
                       as_of: datetime) -> RevisionVisibility:
    """Which revision-chain members were knowable at ``as_of``, and how they link.

    Pure and total over the supplied finite corpus: no storage is consulted, no
    clock is read, and malformed input yields explicit withheld/unresolved
    records rather than an exception."""
    result = RevisionVisibility(as_of=as_of)
    # Materialize once: the corpus is walked twice below, and a generator would
    # be silently empty on the second pass.
    corpus = list(snapshots)
    corpus_ids = {s.snapshot_id for s in corpus if isinstance(s, EvidenceSnapshot)}

    # ---- PHASE 1: ADMISSION ONLY --------------------------------------
    # Nothing about supersession is examined yet. A snapshot that fails here is
    # excluded entirely — it is neither visible NOR able to supersede anything.
    admitted: list[tuple[EvidenceSnapshot, AdmissionDecision]] = []
    # Refusals are KEPT, keyed by id, so the audit record for an unresolved link
    # can state why the predecessor was actually withheld instead of guessing.
    refusals: dict[str, AdmissionDecision] = {}
    for snapshot in corpus:
        decision = admit(snapshot, as_of)
        if decision.admitted:
            admitted.append((snapshot, decision))
            continue
        # Keyed by the CORPUS id, not decision.snapshot_id: admission declines to
        # vouch for an identity it refused before validating (a PIT refusal
        # carries snapshot_id=None), yet the corpus and every supersedes link
        # still refer to the snapshot by that id.
        # getattr: the corpus may legitimately contain a non-snapshot, which is
        # withheld rather than raised on. Such a member has no corpus id at all.
        corpus_id = getattr(snapshot, "snapshot_id", None)
        if isinstance(corpus_id, str):
            refusals[corpus_id] = decision
        # A future revision that is ALSO malformed reports as future evidence,
        # because admit() evaluates timing first — a later integrity failure must
        # not obscure the lookahead finding.
        result.withheld.append(WithheldMember(
            snapshot_id=decision.snapshot_id,
            reason=decision.reason.value,
            pit_reason=decision.pit_reason.value if decision.pit_reason else None,
            corpus_snapshot_id=corpus_id if isinstance(corpus_id, str) else None))

    admitted_ids = {s.snapshot_id for s, _ in admitted}

    # ---- PHASE 2: SUPERSESSION, AMONG ADMITTED MEMBERS ONLY -----------
    superseded_by: dict[str, list[str]] = {}
    for snapshot, _ in admitted:
        target = snapshot.supersedes_snapshot_id
        # `target != snapshot_id` costs nothing and keeps the invariant explicit
        # even though canonical identity makes self-reference unconstructible.
        if target and target in admitted_ids and target != snapshot.snapshot_id:
            superseded_by.setdefault(target, []).append(snapshot.snapshot_id)

    for snapshot, _ in admitted:
        sid = snapshot.snapshot_id
        target = snapshot.supersedes_snapshot_id
        if target is None:
            state = LinkState.ROOT
        elif target in admitted_ids:
            state = LinkState.RESOLVED
        else:
            # The predecessor is either present-but-not-knowable, or simply not in
            # the supplied corpus. Either way it is NEVER invented: the snapshot
            # stays visible and the gap is recorded for audit. Refusing the
            # snapshot instead would let an incomplete corpus suppress evidence
            # that was legitimately visible — the very harm this module prevents.
            in_corpus = target in corpus_ids
            state = (LinkState.PREDECESSOR_NOT_ADMITTED if in_corpus
                     else LinkState.PREDECESSOR_NOT_IN_CORPUS)
            audit: dict[str, Any] = {
                "snapshot_id": sid, "supersedes_snapshot_id": target,
                "state": state.value}
            if in_corpus:
                # An audit record must say why evidence was ACTUALLY withheld.
                # "Not knowable at as_of" is only one of the reasons a predecessor
                # can fail admission — integrity and malformed provenance are
                # others — and asserting timing for all of them would be a
                # plausible-sounding explanation of a different fact. The real
                # AdmissionDecision is carried through instead.
                refusal = refusals.get(target)
                audit["detail"] = ("predecessor is present in corpus but was not "
                                   "admitted at as_of")
                audit["predecessor_admission_reason"] = (
                    refusal.reason.value if refusal is not None else None)
                audit["predecessor_pit_reason"] = (
                    refusal.pit_reason.value
                    if refusal is not None and refusal.pit_reason is not None else None)
                audit["predecessor_withheld_on_timing"] = (
                    refusal is not None
                    and refusal.reason is AdmissionReason.PIT_REFUSED)
            else:
                audit["detail"] = ("predecessor is absent from the supplied corpus; "
                                   "it is recorded as unresolved and never invented")
            result.unresolved_links.append(audit)

        result.visible.append(VisibleMember(
            snapshot_id=sid, supersedes_snapshot_id=target, link_state=state,
            superseded_by=tuple(sorted(superseded_by.get(sid, ())))))

    result.visible.sort(key=lambda m: m.snapshot_id)
    # corpus id leads the key: admission-vouched ids are None for early refusals,
    # so sorting on them alone would leave several withheld members tied and
    # their order dependent on input order, breaking deterministic replay.
    result.withheld.sort(key=lambda m: (m.corpus_snapshot_id or "",
                                        m.snapshot_id or "", m.reason))
    result.unresolved_links.sort(key=lambda d: d["snapshot_id"])
    return result
