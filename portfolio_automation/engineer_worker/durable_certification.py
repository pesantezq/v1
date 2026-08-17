"""The certification path the EW-0A operating loop is forced through.

WHY THIS EXISTS.

Crash-resilient review persistence was built, tested, and used once -- for its
own review. It was not on the mandatory path. ``run_task`` reached
``certify_attempt``, which built an ephemeral dict and called
``supervisor(packet)`` directly: no persisted bytes, no journal, no terminal
HEAD evidence, no duplicate-dispatch protection. A grep for production callers
of the crash machinery returned nothing outside its own modules and the tests.

A mechanism is not an autonomy guarantee until the autonomous path cannot avoid
it. This module is that seam.

NO SILENT FALLBACK.

There is deliberately no ``if context is None: use the old path`` branch
anywhere. The legacy adapter is a REAL context over a throwaway root, so there
is exactly one code path and no ephemeral branch left to reach by accident. The
distinction is enforced one level up: the operating loop refuses a context that
is not durable, rather than quietly degrading to weaker certification.

If durable certification cannot be initialised, the work stays UNVERIFIED.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from portfolio_automation.engineer_worker import EXPERIMENTAL_MARKER
from portfolio_automation.engineer_worker.review_journal import (
    LifecycleKind, RecoveryState, ReviewJournal, criterion_set_digest,
    review_invocation_id,
)
from portfolio_automation.engineer_worker.review_packet import (
    canonical_packet_bytes, packet_hash_of_bytes,
)
from portfolio_automation.engineer_worker.review_packet_store import (
    BindingFacts, PacketStore, PacketStoreError,
)
from portfolio_automation.engineer_worker import supervisor_screen
from portfolio_automation.engineer_worker.execution_identity import (
    UNAVAILABLE, ExecutionIdentity, build_execution_identity,
    safe_toolset_identity,
)

SCHEMA_KIND = EXPERIMENTAL_MARKER
CERTIFICATION_SCHEMA_VERSION = "engineering.durable_certification.v0"
DEFAULT_JOURNAL_REL = "docs/EW0A_REVIEW_JOURNAL.jsonl"

#: Envelope schema. The EW-0A supervisor packet is wrapped, never converted:
#: gpt_supervisor's system prompt names the exact keys the reviewer must decide
#: from (requirements, acceptance_criteria, diff, tests_run, test_results,
#: changed_files), and none of those exist in a ReviewPacket. Converting would
#: instruct the reviewer to read fields absent from its input.
ENVELOPE_SCHEMA_VERSION = "engineering.ew0a_supervisor_packet.v1"


class CertificationUnavailable(RuntimeError):
    """Durable certification could not be initialised or reconstructed.

    Never degrades to the ephemeral path. Work stays unverified instead."""


@dataclass(frozen=True)
class DispatchOutcome:
    """What the durable path did, and what evidence it left."""

    decision: Any                      # SupervisorDecision, or None when refused
    dispatched: bool
    review_invocation_id: str
    packet_hash: Optional[str] = None
    store_rel: Optional[str] = None
    refusal: Optional[str] = None
    detail: str = ""
    reviewer_called: bool = False
    head_verdict: Optional[str] = None

    @property
    def evidence_refs(self) -> list[str]:
        refs = [self.review_invocation_id]
        if self.packet_hash:
            refs.append(self.packet_hash)
        if self.store_rel:
            refs.append(self.store_rel)
        return refs


@dataclass(frozen=True)
class ReviewContext:
    """Everything the certification path needs to leave durable evidence."""

    store: PacketStore
    journal: ReviewJournal
    mission_id: str
    session_id: str
    reviewer_identity: Mapping[str, str]
    repo: Any = None                   # RepoView; None => terminal HEAD is NO
    #: Proof that the packet describes the committed candidate. Carried on the
    #: context so the operating loop cannot dispatch without one: a review with
    #: no binding certifies a tree nobody checked.
    candidate_binding: Any = None
    durable: bool = True

    def execution_identity(self, *, candidate_sha: str = UNAVAILABLE,
                           task_id: str = UNAVAILABLE,
                           input_id: str = UNAVAILABLE) -> ExecutionIdentity:
        """The configuration behind reviews dispatched through this context.

        Built here rather than at each call site so every producer attributes
        the same way, and so tool configuration is screened for secrets exactly
        once. Attributes this context genuinely does not know stay UNAVAILABLE:
        substituting a configured default would assert a configuration that may
        never have run."""
        ident = dict(self.reviewer_identity)
        return build_execution_identity(
            worker_role="independent_reviewer",
            model_provider=ident.get("provider", UNAVAILABLE),
            model_name=ident.get("model", UNAVAILABLE),
            # Chat APIs do not return the build actually served, so the exact
            # version is normally unavailable. Recording the configured NAME
            # here would claim precision nobody has.
            model_version=ident.get("model_version", UNAVAILABLE),
            instruction_version=ident.get("protocol", UNAVAILABLE),
            toolset=ident.get("toolset", "gpt_supervisor.review"),
            tool_config=ident,
            candidate_sha=candidate_sha, task_id=task_id,
            mission_id=self.mission_id, input_id=input_id)

    @classmethod
    def open(cls, repo_root: str | Path, *, mission_id: str, session_id: str,
             reviewer_identity: Mapping[str, str], repo: Any = None,
             candidate_binding: Any = None,
             journal_rel: str = DEFAULT_JOURNAL_REL) -> "ReviewContext":
        """Initialise durable certification, or refuse.

        Probes that the journal is actually writable NOW rather than
        discovering it at dispatch time, when a failure would land between the
        write-ahead record and the reviewer call."""
        root = Path(repo_root)
        journal_path = root / journal_rel
        try:
            journal_path.parent.mkdir(parents=True, exist_ok=True)
            with open(journal_path, "a", encoding="utf-8") as fh:
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            raise CertificationUnavailable(
                f"review journal is not writable at {journal_rel}: {exc}") from exc
        return cls(store=PacketStore(repo_root=root),
                   journal=ReviewJournal(path=journal_path),
                   mission_id=mission_id, session_id=session_id,
                   reviewer_identity=dict(reviewer_identity), repo=repo,
                   candidate_binding=candidate_binding)

    @classmethod
    def legacy_in_memory(cls, root: str | Path, *, mission_id: str = "legacy",
                         session_id: str = "legacy",
                         reviewer_identity: Optional[Mapping[str, str]] = None,
                         repo: Any = None,
                         candidate_binding: Any = None) -> "ReviewContext":
        """EXPLICIT test/legacy adapter over a throwaway root.

        A real context, so the certification path has no second branch; marked
        non-durable so the operating loop refuses it. A caller has to ask for
        this by name -- it is never reached by omitting an argument."""
        ctx = cls.open(root, mission_id=mission_id, session_id=session_id,
                       reviewer_identity=dict(reviewer_identity or {"model": "stub"}),
                       repo=repo, candidate_binding=candidate_binding)
        return cls(store=ctx.store, journal=ctx.journal, mission_id=mission_id,
                   session_id=session_id, reviewer_identity=ctx.reviewer_identity,
                   repo=repo, candidate_binding=candidate_binding, durable=False)


def binding_envelope(packet: dict[str, Any], *, candidate_sha: str, mission_id: str,
                     session_id: str, attempt_id: str,
                     acceptance_criteria: Sequence[str]) -> dict[str, Any]:
    """Wrap an EW-0A supervisor packet so its bytes carry their own binding.

    Every existing key is preserved verbatim, so the reviewer still sees the
    fields its prompt names. ``attempt_id`` is included because without it two
    repair attempts producing byte-identical evidence would hash to the same
    packet, share a review identity, and the second would silently replay the
    first attempt's verdict."""
    criteria = [{"criterion_id": f"AC{i}", "claim": str(c)}
                for i, c in enumerate(acceptance_criteria)]
    task = dict(packet.get("task") or {})
    task["session_id"] = session_id
    task["attempt_id"] = attempt_id
    return {**packet,
            "schema_version": ENVELOPE_SCHEMA_VERSION, "schema_kind": SCHEMA_KIND,
            "candidate_sha": candidate_sha, "mission_id": mission_id,
            "task": task, "criteria": criteria}


def dispatch_durably(packet: dict[str, Any], supervisor, *, context: ReviewContext,
                     candidate_sha: str, attempt_id: str, task_id: str,
                     acceptance_criteria: Sequence[str],
                     candidate_binding: Any = None) -> DispatchOutcome:
    """Persist, verify, journal, resolve HEAD, then call the reviewer once.

    Ordering is not incidental. Screening precedes persistence because a
    credential written into a read-only, content-addressed, git-tracked
    artifact cannot be cleaned up afterwards. The write-ahead REVIEWER_CALLED
    record is the last thing before the call, because its absence is what later
    proves the reviewer was never reached."""
    payload = binding_envelope(packet, candidate_sha=candidate_sha,
                               mission_id=context.mission_id,
                               session_id=context.session_id, attempt_id=attempt_id,
                               acceptance_criteria=acceptance_criteria)
    criterion_ids = [c["criterion_id"] for c in payload["criteria"]]
    blob = canonical_packet_bytes(payload)
    phash = packet_hash_of_bytes(blob)
    rid = review_invocation_id(
        candidate_sha=candidate_sha, packet_hash=phash,
        mission_id=context.mission_id, task_id=task_id,
        criterion_digest=criterion_set_digest(criterion_ids),
        reviewer_identity=dict(context.reviewer_identity))

    # The execution configuration that produced this review. Built ONCE and
    # attached to every lifecycle record, so a later audit can group verdicts by
    # configuration instead of reconstructing it from scattered fields.
    identity = context.execution_identity(
        candidate_sha=candidate_sha, task_id=task_id, input_id=phash)

    def refuse(code: str, detail: str, *, called: bool = False,
               head: Optional[str] = None) -> DispatchOutcome:
        context.journal.append(LifecycleKind.DISPATCH_REFUSED,
                               review_invocation_id=rid, packet_hash=phash,
                               reviewer_called="YES" if called else "NO",
                               next_action="REVIEW_NOT_DISPATCHED",
                               refusal=code, detail=detail)
        return DispatchOutcome(None, False, rid, packet_hash=phash, refusal=code,
                               detail=detail, reviewer_called=called, head_verdict=head)

    # Recovery FIRST: a restarted process must not re-ask a question that was
    # already put to an independent reviewer.
    prior = context.journal.recover(rid, store=context.store)
    if prior.state is RecoveryState.VERDICT_ALREADY_RECORDED:
        return DispatchOutcome(prior.verdict, False, rid, packet_hash=phash,
                               refusal="VERDICT_ALREADY_RECORDED",
                               detail=prior.reason, reviewer_called=True)
    if not prior.dispatch_permitted:
        return DispatchOutcome(None, False, rid, packet_hash=phash,
                               refusal=prior.state.value, detail=prior.reason,
                               reviewer_called=prior.reviewer_may_have_been_billed)

    # Screen BEFORE anything is written. The in-gate screen elsewhere inspects
    # per-artifact content only; this walks the whole payload.
    screen = supervisor_screen.screen_packet(payload)
    if screen.blocked:
        return refuse("SECRET_SCREEN_BLOCKED",
                      f"{len(screen.findings)} finding(s); nothing persisted")

    context.journal.append(LifecycleKind.PACKET_BUILT, review_invocation_id=rid,
                           packet_hash=phash, candidate_sha=candidate_sha,
                           task_id=task_id, attempt_id=attempt_id,
                           criteria=criterion_ids, size_bytes=len(blob),
                           execution_identity=identity.to_dict())
    try:
        persisted = context.store.persist(blob, expected_hash=phash, screened=True)
    except PacketStoreError as exc:
        return refuse(exc.refusal.value, exc.detail)
    context.journal.append(LifecycleKind.PACKET_PERSISTED, review_invocation_id=rid,
                           packet_hash=phash,
                           packet_blob_rel=persisted.store_rel,
                           packet_sha256=persisted.packet_sha256,
                           packet_blob_bytes=persisted.size_bytes)
    context.store.append_index({**persisted.to_dict(), "review_invocation_id": rid})

    # Reload and prove. The expected binding comes from the persisted record,
    # not from the object just built -- comparing a belief to itself always
    # agrees.
    verified = context.store.verify(phash, expected_binding=persisted.binding)
    if not verified.ok:
        return refuse("PACKET_NOT_VERIFIABLE",
                      f"{[r.value for r in verified.refusals]} {verified.details}")

    if candidate_binding is None:
        return refuse("NO_CANDIDATE_BINDING",
                      "no candidate binding was supplied; a review may not be "
                      "dispatched without proof the packet describes the "
                      "committed candidate")
    context.journal.append(LifecycleKind.CANDIDATE_BOUND, review_invocation_id=rid,
                           **candidate_binding.to_dict())
    if not candidate_binding.ok:
        return refuse("CANDIDATE_NOT_BOUND",
                      f"{[r.value for r in candidate_binding.refusals]}")

    bound, head = candidate_binding.resolve_head_terminal()
    context.journal.append(LifecycleKind.DISPATCH_ATTEMPTED, review_invocation_id=rid,
                           packet_hash=phash, candidate_sha=candidate_sha,
                           **head.to_dict())
    if head.verdict != "YES":
        return refuse("HEAD_NOT_UNCHANGED_AT_DISPATCH",
                      f"HEAD resolution {head.resolution_reason}", head=head.verdict)

    # WRITE-AHEAD. Last statement before the call.
    context.journal.append(LifecycleKind.REVIEWER_CALLED, review_invocation_id=rid,
                           packet_hash=phash,
                           # Screened, NOT the raw mapping. A caller may put a
                           # key_file path or an api_key in the reviewer identity
                           # -- writing it verbatim would put a credential into
                           # an append-only, replicated ledger, where it cannot
                           # be removed. Only the safe projection is recorded.
                           reviewer_identity=safe_toolset_identity(
                               "gpt_supervisor.review",
                               dict(context.reviewer_identity))["safe_config"],
                           execution_identity=identity.to_dict(),
                           note="WRITE-AHEAD: fsynced before the request left "
                                "this process")
    decision = supervisor(json.loads(verified.blob.decode("utf-8")))

    context.journal.append(LifecycleKind.VERDICT_RETURNED, review_invocation_id=rid,
                           packet_hash=phash,
                           verdict=decision.to_dict() if hasattr(decision, "to_dict")
                           else str(decision))
    context.journal.append(LifecycleKind.VERDICT_PERSISTED, review_invocation_id=rid,
                           packet_hash=phash, candidate_sha=candidate_sha,
                           execution_identity=identity.to_dict(),
                           verdict=decision.to_dict() if hasattr(decision, "to_dict")
                           else str(decision))
    return DispatchOutcome(decision, True, rid, packet_hash=phash,
                           store_rel=persisted.store_rel, reviewer_called=True,
                           head_verdict="YES")
