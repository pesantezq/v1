"""Binds a review packet to the COMMITTED candidate it claims to describe.

WHY THIS EXISTS.

Session 2 built a review packet by reading files off the working tree, resolved
``candidate_sha`` with ``git rev-parse HEAD`` BEFORE committing, and passed that
same string as ``expected_sha``. The existing gate compared the two and agreed,
because both sides came from the same caller. The reviewer therefore certified
the session's STARTING commit while judging content that existed only in an
uncommitted tree, and the resulting PASS named a SHA that did not contain a
single line of the work.

Nothing was faked. The check was simply not load-bearing: comparing a belief to
itself always succeeds. A gate only constrains anything when at least one side
is resolved from an authority the caller does not author — here, git.

WHAT IS PROVEN BEFORE A REVIEWER MAY BE CALLED:

    REVIEW_CANDIDATE_SHA == GIT_HEAD              the packet names the commit
                                                  that is actually checked out
    REVIEW_CANDIDATE_CONTAINS_TASK_DIFF == YES    the implementation exists AT
                                                  that commit, not merely on disk
    EVIDENCE_MATCHES_CANDIDATE == YES             every path-bearing artifact
                                                  equals the committed blob
    DETERMINISTIC_VERIFICATION_PASS_EXISTS == YES a PASS record whose recorded
                                                  file hashes match that commit
    HEAD_UNCHANGED_AT_DISPATCH == YES             re-resolved immediately before
                                                  the reviewer is called

The deterministic-PASS check is a content binding, not an assertion. Verification
necessarily runs BEFORE the commit exists, so a record cannot name its own future
SHA. Instead the record carries the hash of every file it verified, and binding
recomputes those hashes from the committed blobs. If they match, the PASS
provably describes that commit; if they do not, the tree changed after
verification and the PASS describes something else.

Fails closed everywhere: any git error, missing file, or unreadable record is a
REFUSAL, never a pass-through. ``experimental_noncanonical``.
"""
from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol, Sequence

from portfolio_automation.engineer_worker import EXPERIMENTAL_MARKER

SCHEMA_KIND = EXPERIMENTAL_MARKER
BINDING_SCHEMA_VERSION = "engineering.review_candidate.v0"

#: Ledger record kind that carries a deterministic verification outcome.
VERIFICATION_KIND = "DeterministicVerification"
VERIFICATION_PASS = "PASS"


class CandidateRefusal(str, Enum):
    """Why a review was NOT dispatched. Every value is a hard refusal."""

    HEAD_UNRESOLVABLE = "HEAD_UNRESOLVABLE"
    PACKET_SHA_IS_NOT_HEAD = "PACKET_SHA_IS_NOT_HEAD"
    CANDIDATE_MISSING_TASK_DIFF = "CANDIDATE_MISSING_TASK_DIFF"
    EVIDENCE_NOT_FROM_CANDIDATE = "EVIDENCE_NOT_FROM_CANDIDATE"
    NO_DETERMINISTIC_PASS = "NO_DETERMINISTIC_PASS"
    VERIFICATION_NOT_BOUND_TO_CANDIDATE = "VERIFICATION_NOT_BOUND_TO_CANDIDATE"
    HEAD_MOVED_BEFORE_DISPATCH = "HEAD_MOVED_BEFORE_DISPATCH"


def file_digest(text: str) -> str:
    """Stable content digest used to bind a verification record to a commit."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RepoView(Protocol):
    """The minimum a candidate binding needs from version control.

    Narrow on purpose: the gate must be exercisable by tests that construct the
    exact failure it prevents, and a gate that can only run against a live
    repository is a gate whose failure paths are never tested."""

    def head_sha(self) -> Optional[str]: ...

    def file_at(self, sha: str, path: str) -> Optional[str]: ...


@dataclass(frozen=True)
class GitRepoView:
    """RepoView backed by a real checkout. Any git failure reads as absence."""

    repo_root: Path

    def _run(self, *args: str) -> Optional[str]:
        try:
            done = subprocess.run(("git", *args), cwd=str(self.repo_root),
                                  capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout if done.returncode == 0 else None

    def head_sha(self) -> Optional[str]:
        out = self._run("rev-parse", "HEAD")
        return out.strip() if out else None

    def file_at(self, sha: str, path: str) -> Optional[str]:
        """Content of ``path`` AS COMMITTED at ``sha`` — never the working tree."""
        return self._run("show", f"{sha}:{path}")


@dataclass(frozen=True)
class CandidateBinding:
    """Evidence that a packet describes a committed, verified candidate."""

    packet_sha: Optional[str]
    head_at_binding: Optional[str]
    repo: Any = None
    refusals: tuple[CandidateRefusal, ...] = ()
    details: tuple[str, ...] = ()
    checks: dict[str, str] = field(default_factory=dict)
    verification_ref: Optional[str] = None

    @property
    def ok(self) -> bool:
        return not self.refusals

    def recheck_head(self) -> "CandidateBinding":
        """Re-resolve HEAD immediately before dispatch.

        Binding and dispatch are separated by packet assembly and secret
        screening, and a commit, checkout or rebase in that window would leave a
        packet describing a candidate that is no longer checked out."""
        if self.repo is None:
            return self
        head_now = self.repo.head_sha()
        if head_now == self.head_at_binding:
            return self
        return CandidateBinding(
            packet_sha=self.packet_sha, head_at_binding=self.head_at_binding,
            repo=self.repo,
            refusals=self.refusals + (CandidateRefusal.HEAD_MOVED_BEFORE_DISPATCH,),
            details=self.details + (
                f"HEAD was {self.head_at_binding!r} when the packet was bound and "
                f"is {head_now!r} now; the packet no longer describes what is "
                "checked out",),
            checks={**self.checks, "HEAD_UNCHANGED_AT_DISPATCH": "NO"},
            verification_ref=self.verification_ref)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": BINDING_SCHEMA_VERSION, "schema_kind": SCHEMA_KIND,
                "candidate_bound": "YES" if self.ok else "NO",
                "packet_candidate_sha": self.packet_sha,
                "git_head_at_binding": self.head_at_binding,
                "checks": dict(self.checks),
                "refusals": [r.value for r in self.refusals],
                "details": list(self.details),
                "deterministic_verification_ref": self.verification_ref}


def _passing_verifications(records: Iterable[dict], task_id: Optional[str]
                           ) -> list[dict]:
    out = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("kind") != VERIFICATION_KIND:
            continue
        if record.get("result") != VERIFICATION_PASS:
            continue
        if task_id is not None and record.get("task_id") != task_id:
            continue
        out.append(record)
    return out


def bind_candidate(packet: Any, repo: RepoView, *,
                   verifications: Iterable[dict] = (),
                   required_paths: Sequence[str] = ()) -> CandidateBinding:
    """Prove — or refuse — that ``packet`` describes the committed candidate.

    Collects EVERY refusal rather than returning on the first, so a repair does
    not have to rediscover the next problem one dispatch at a time."""
    refusals: list[CandidateRefusal] = []
    details: list[str] = []
    checks: dict[str, str] = {}
    packet_sha = getattr(packet, "candidate_sha", None)

    head = repo.head_sha()
    if not head:
        return CandidateBinding(
            packet_sha=packet_sha, head_at_binding=None, repo=repo,
            refusals=(CandidateRefusal.HEAD_UNRESOLVABLE,),
            details=("git HEAD could not be resolved; a candidate that cannot be "
                     "identified cannot be certified",),
            checks={"REVIEW_CANDIDATE_SHA_EQUALS_GIT_HEAD": "UNKNOWN"})

    # ---- 1. the packet must name the commit that is actually checked out ----
    if packet_sha != head:
        refusals.append(CandidateRefusal.PACKET_SHA_IS_NOT_HEAD)
        details.append(f"packet candidate_sha {packet_sha!r} != git HEAD {head!r}; "
                       "a base or pre-implementation SHA cannot certify work "
                       "committed later")
    checks["REVIEW_CANDIDATE_SHA_EQUALS_GIT_HEAD"] = (
        "YES" if packet_sha == head else "NO")

    # ---- 2. a deterministic PASS, bound by CONTENT to this commit -----------
    task_id = getattr(packet, "task_id", None)
    candidates = _passing_verifications(verifications, task_id)
    verification_ref: Optional[str] = None
    verified_paths: list[str] = []
    if not candidates:
        refusals.append(CandidateRefusal.NO_DETERMINISTIC_PASS)
        details.append("no DeterministicVerification record with result=PASS for "
                       f"task {task_id!r}; semantic review may not precede "
                       "deterministic verification")
        checks["DETERMINISTIC_VERIFICATION_PASS_EXISTS"] = "NO"
        checks["DETERMINISTIC_PASS_BOUND_TO_CANDIDATE"] = "NO"
    else:
        checks["DETERMINISTIC_VERIFICATION_PASS_EXISTS"] = "YES"
        bound = None
        mismatches: list[str] = []
        for record in candidates:
            verified = record.get("verified_files")
            if not isinstance(verified, dict) or not verified:
                mismatches.append(
                    f"{record.get('verification_id') or 'unnamed record'}: carries no "
                    "verified_files, so its PASS cannot be tied to any commit")
                continue
            record_ok = True
            for path, digest in sorted(verified.items()):
                committed = repo.file_at(head, path)
                if committed is None:
                    record_ok = False
                    mismatches.append(f"{path}: absent at {head[:12]}")
                elif file_digest(committed) != digest:
                    record_ok = False
                    mismatches.append(
                        f"{path}: committed content differs from what was verified")
            if record_ok:
                bound = record
                verified_paths = sorted(verified)
                break
        if bound is None:
            refusals.append(CandidateRefusal.VERIFICATION_NOT_BOUND_TO_CANDIDATE)
            details.extend(mismatches)
            details.append("a deterministic PASS certifies the tree it ran against; "
                           "it does not transfer to a different commit")
            checks["DETERMINISTIC_PASS_BOUND_TO_CANDIDATE"] = "NO"
        else:
            verification_ref = bound.get("verification_id") or bound.get("recorded_at")
            checks["DETERMINISTIC_PASS_BOUND_TO_CANDIDATE"] = "YES"

    # ---- 3. the implementation must exist AT the candidate ------------------
    # Union, not a fallback chain. Deriving these ONLY from the verification
    # record would make this check evaporate exactly when the verification is
    # missing or unbindable — the gate would go quiet in the situation it exists
    # to catch. The packet's own path-bearing evidence is always available and
    # states which files the review is about.
    evidence_paths = {
        getattr(a, "source_path", None)
        for a in getattr(packet, "evidence", {}).values()
        if getattr(a, "source_path", None)}
    wanted = sorted(set(required_paths) | set(verified_paths) | evidence_paths)
    missing = [p for p in wanted if repo.file_at(head, p) is None]
    if missing:
        refusals.append(CandidateRefusal.CANDIDATE_MISSING_TASK_DIFF)
        details.append(f"candidate {head[:12]} does not contain: {sorted(missing)}")
    checks["REVIEW_CANDIDATE_CONTAINS_TASK_DIFF"] = (
        "NO" if missing else ("YES" if wanted else "UNKNOWN"))

    # ---- 4. the evidence itself must be the committed content --------------
    # This is the check that would have caught Session 2 on its own: the packet
    # was assembled by reading the working tree, and a working tree is not a
    # candidate.
    drifted: list[str] = []
    for artifact in getattr(packet, "evidence", {}).values():
        path = getattr(artifact, "source_path", None)
        if not path or not getattr(artifact, "content", ""):
            continue
        committed = repo.file_at(head, path)
        if committed is None:
            drifted.append(f"{artifact.artifact_id}: {path} absent at {head[:12]}")
        elif committed != artifact.content:
            drifted.append(
                f"{artifact.artifact_id}: content differs from {path} as committed "
                f"at {head[:12]} (packet was built from the working tree)")
    if drifted:
        refusals.append(CandidateRefusal.EVIDENCE_NOT_FROM_CANDIDATE)
        details.extend(drifted)
    checks["EVIDENCE_MATCHES_CANDIDATE"] = "NO" if drifted else "YES"
    checks["HEAD_UNCHANGED_AT_DISPATCH"] = "PENDING"

    return CandidateBinding(packet_sha=packet_sha, head_at_binding=head, repo=repo,
                            refusals=tuple(refusals), details=tuple(details),
                            checks=checks, verification_ref=verification_ref)
