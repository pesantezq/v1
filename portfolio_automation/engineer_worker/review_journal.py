"""Write-ahead journal that makes a review's lifecycle survive process loss.

WHY THIS EXISTS.

After the 2026-08-16 crash the session ledger could say a packet had been built
and a verdict recorded, because a human had written both records by hand. What
it could not say -- because nothing in the code wrote it -- was whether the
reviewer had been CALLED. Those are different facts, and the difference decides
whether a restarted worker may safely call the reviewer again.

THE ORDERING THAT MAKES RECOVERY SOUND.

``REVIEWER_CALLED`` is appended AND fsynced BEFORE the request leaves this
process. That single ordering rule is what gives absence its meaning:

  * no ``REVIEWER_CALLED`` record  -> the reviewer was provably never called
  * record present, no verdict     -> the reviewer may have been called and its
                                      answer is lost; INDETERMINATE, fail closed

Without the write-ahead, absence proves nothing and every crash would have to
be treated as "maybe called", which either blocks all progress or -- far worse
-- gets rationalised into calling again.

WHY STATES ARE NOT INFERRED FROM ONE ANOTHER.

One state is one record is one fsynced append. There is deliberately no summary
record carrying seven booleans: a single write cannot straddle a crash, so a
seven-field summary collapses seven independently-observable facts into one
observation and loses exactly the resolution this module exists to provide.

A TORN TAIL IS NOT AN ABSENCE.

The repository's existing ledger reader skips a line that fails to parse. That
is right for a projection and catastrophic for recovery: the most likely
artifact of a crash mid-append is a half-written final line, and skipping it
reports the state BEFORE the last thing that happened -- the exact misreading
that would authorise re-calling a reviewer. ``read_events_strict`` treats a
torn tail as a refusal.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Sequence

from portfolio_automation.engineer_worker import EXPERIMENTAL_MARKER

SCHEMA_KIND = EXPERIMENTAL_MARKER
JOURNAL_SCHEMA_VERSION = "engineering.review_journal.v0"

#: Writers that promise the write-ahead ordering above stamp this. Records
#: without it were produced by a writer that made no such promise, so their
#: absence of a REVIEWER_CALLED entry carries no information and must recover
#: as indeterminate. Every record already in the crashed 0C ledger is in that
#: category -- correctly.
WAL_CONTRACT = "review_lifecycle.v1"


class LifecycleKind(str, Enum):
    """The seven independently observable states, plus terminal refusals."""

    PACKET_BUILT = "ReviewPacketBuilt"
    PACKET_PERSISTED = "ReviewPacketPersisted"
    CANDIDATE_BOUND = "ReviewCandidateBound"
    DISPATCH_ATTEMPTED = "ReviewDispatchAttempted"
    REVIEWER_CALLED = "ReviewerCalled"
    VERDICT_RETURNED = "ReviewVerdictReturned"
    VERDICT_PERSISTED = "ReviewVerdictPersisted"
    DISPATCH_REFUSED = "ReviewDispatchRefused"


class RecoveryState(str, Enum):
    NOT_DISPATCHED = "NOT_DISPATCHED"
    DISPATCH_ALREADY_OCCURRED = "DISPATCH_ALREADY_OCCURRED"
    VERDICT_ALREADY_RECORDED = "VERDICT_ALREADY_RECORDED"
    RECOVERY_INDETERMINATE_FAIL_CLOSED = "RECOVERY_INDETERMINATE_FAIL_CLOSED"


class JournalError(ValueError):
    pass


def criterion_set_digest(criterion_ids: Sequence[str]) -> str:
    """Order-free identity of the criteria a review was asked to judge."""
    blob = "\n".join(sorted(str(c) for c in criterion_ids)).encode("utf-8")
    return "crit_" + hashlib.sha256(blob).hexdigest()[:16]


def review_invocation_id(*, candidate_sha: str, packet_hash: str, mission_id: str,
                         task_id: str, criterion_digest: str,
                         reviewer_identity: dict[str, str],
                         dispatch_epoch: int = 1) -> str:
    """Deterministic identity of ONE reviewer invocation.

    Contains no timestamp, pid, hostname or path: after a restart the same
    logical review must recompute to the same id, or the worker would conclude
    it had never dispatched and call the reviewer a second time."""
    payload = {
        "candidate_sha": candidate_sha, "packet_hash": packet_hash,
        "mission_id": mission_id, "task_id": task_id,
        "criterion_digest": criterion_digest,
        "reviewer_identity": {k: reviewer_identity[k] for k in sorted(reviewer_identity)},
        "dispatch_epoch": int(dispatch_epoch),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    return "rvi_" + hashlib.sha256(blob).hexdigest()[:32]


def read_events_strict(path: Path) -> tuple[list[dict], bool]:
    """Read a journal for RECOVERY. Returns ``(events, tail_intact)``.

    A parse failure on the LAST line is a torn tail -> ``tail_intact=False``.
    A parse failure anywhere else is corruption and raises: silently continuing
    past it would hide a record that definitely completed."""
    if not path.exists():
        return [], True
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    events: list[dict] = []
    for i, line in enumerate(lines):
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                return events, False
            raise JournalError(f"corrupt journal record at line {i + 1}")
    return events, True


@dataclass(frozen=True)
class RecoveryFinding:
    state: RecoveryState
    review_invocation_id: str
    observed_kinds: tuple[str, ...] = ()
    reviewer_may_have_been_billed: bool = False
    dispatch_permitted: bool = False
    verdict: Optional[dict[str, Any]] = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": JOURNAL_SCHEMA_VERSION, "schema_kind": SCHEMA_KIND,
                "recovery_state": self.state.value,
                "review_invocation_id": self.review_invocation_id,
                "observed_kinds": list(self.observed_kinds),
                "reviewer_may_have_been_billed": self.reviewer_may_have_been_billed,
                "dispatch_permitted": self.dispatch_permitted,
                "reason": self.reason}


@dataclass(frozen=True)
class ReviewJournal:
    """Append-only, fsynced lifecycle journal for one checkout."""

    path: Path

    def append(self, kind: LifecycleKind, *, review_invocation_id: str,
               **fields: Any) -> dict[str, Any]:
        """One state, one fsynced append.

        fsync is not optional here. Without it the write-ahead ordering that
        gives REVIEWER_CALLED's absence its meaning is unenforceable, and the
        whole recovery argument becomes theoretical."""
        record = {"kind": kind.value, "review_invocation_id": review_invocation_id,
                  "wal_contract": WAL_CONTRACT,
                  "schema_version": JOURNAL_SCHEMA_VERSION,
                  "schema_kind": SCHEMA_KIND, **fields}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return record

    def events_for(self, invocation_id: str) -> tuple[list[dict], bool]:
        events, intact = read_events_strict(self.path)
        return [e for e in events
                if e.get("review_invocation_id") == invocation_id], intact

    def recover(self, invocation_id: str) -> RecoveryFinding:
        """Decide, from durable evidence alone, what may happen next."""
        try:
            events, tail_intact = self.events_for(invocation_id)
        except JournalError as exc:
            return RecoveryFinding(
                RecoveryState.RECOVERY_INDETERMINATE_FAIL_CLOSED, invocation_id,
                reason=f"journal unreadable: {exc}")

        kinds = tuple(e.get("kind", "") for e in events)

        if not tail_intact:
            # A half-written final line means the last thing that happened is
            # unknown. It is never read as "nothing happened".
            return RecoveryFinding(
                RecoveryState.RECOVERY_INDETERMINATE_FAIL_CLOSED, invocation_id,
                observed_kinds=kinds, reviewer_may_have_been_billed=True,
                reason="journal tail is torn; the last event cannot be read and "
                       "absence of a later record proves nothing")

        def latest(kind: LifecycleKind) -> Optional[dict]:
            for e in reversed(events):
                if e.get("kind") == kind.value:
                    return e
            return None

        persisted = latest(LifecycleKind.VERDICT_PERSISTED)
        if persisted is not None:
            return RecoveryFinding(
                RecoveryState.VERDICT_ALREADY_RECORDED, invocation_id,
                observed_kinds=kinds, reviewer_may_have_been_billed=True,
                dispatch_permitted=False, verdict=persisted.get("verdict"),
                reason="a verdict for this exact invocation is already durable; "
                       "re-asking would be a reroll, not a recovery")

        returned = latest(LifecycleKind.VERDICT_RETURNED)
        called = latest(LifecycleKind.REVIEWER_CALLED)

        if returned is not None:
            return RecoveryFinding(
                RecoveryState.VERDICT_ALREADY_RECORDED, invocation_id,
                observed_kinds=kinds, reviewer_may_have_been_billed=True,
                dispatch_permitted=False, verdict=returned.get("verdict"),
                reason="the reviewer answered and the response is durable; finish "
                       "by persisting it, never by asking again")

        if called is not None:
            # The genuinely indeterminate window. An independent reviewer was
            # consulted and its answer is gone. There is no safe automatic
            # continuation: calling again produces a second judgement of one
            # candidate with only the second recorded.
            return RecoveryFinding(
                RecoveryState.RECOVERY_INDETERMINATE_FAIL_CLOSED, invocation_id,
                observed_kinds=kinds, reviewer_may_have_been_billed=True,
                dispatch_permitted=False,
                reason="the reviewer was called and no response is durable; the "
                       "verdict is unknown and must not be re-requested "
                       "automatically -- operator escalation")

        attempted = latest(LifecycleKind.DISPATCH_ATTEMPTED)
        if attempted is not None:
            if attempted.get("wal_contract") != WAL_CONTRACT:
                return RecoveryFinding(
                    RecoveryState.RECOVERY_INDETERMINATE_FAIL_CLOSED, invocation_id,
                    observed_kinds=kinds, reviewer_may_have_been_billed=True,
                    reason="dispatch was attempted by a writer that made no "
                           "write-ahead promise, so the absence of a "
                           "REVIEWER_CALLED record proves nothing")
            return RecoveryFinding(
                RecoveryState.DISPATCH_ALREADY_OCCURRED, invocation_id,
                observed_kinds=kinds, reviewer_may_have_been_billed=False,
                dispatch_permitted=True,
                reason="dispatch was attempted under the write-ahead contract and "
                       "no REVIEWER_CALLED record exists, so the reviewer was "
                       "provably never called")

        return RecoveryFinding(
            RecoveryState.NOT_DISPATCHED, invocation_id, observed_kinds=kinds,
            dispatch_permitted=True,
            reason="no dispatch evidence for this invocation")
