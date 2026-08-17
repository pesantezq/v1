"""Deterministic proof that a PR head only ADDS evidence to a reviewed candidate.

WHY THIS EXISTS.

A git-tracked review artifact embeds the candidate SHA it describes, so it can
never live inside that same commit. Certification evidence therefore always
lands in a DESCENDANT of the commit it certifies, and the two are different
identities:

    reviewed_candidate_sha  -- the implementation tree that deterministic
                               verification and semantic review actually assessed
    certification_head_sha  -- a descendant carrying that candidate PLUS the
                               append-only artifacts describing the review

Conflating them is not hypothetical: a completion report once gave the head SHA
beside a PASS that had been issued against its parent, which implied the review
covered code it never saw.

A semantic reviewer cannot fix this -- it cannot certify a commit that did not
exist when it was asked. What CAN bridge the two is a deterministic proof that
every change between them is permitted certification evidence and nothing else.

WHY THE PER-COMMIT WALK.

An endpoint diff is not sufficient, and this is the subtlest part. A packet
added in one tail commit and TAMPERED WITH in a later one shows up in
``diff(candidate, head)`` as a clean addition -- the intermediate rewrite is
invisible. Likewise a ledger that is appended to, rewritten, then restored
passes a naive prefix check at the endpoints. So the proof walks every commit in
the tail and re-checks each step.

WHY ``git cat-file blob`` AND NOT ``git show``.

``git show`` honours textconv filters, so a ``.gitattributes`` entry can make it
return content that is not in the object; and ``git show <sha>:<dir>`` returns a
TREE LISTING with exit 0, so a directory silently reads as file content. Neither
is acceptable in an integrity proof. ``.gitattributes`` is itself on the
forbidden list for exactly this reason.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Sequence

from portfolio_automation.engineer_worker import EXPERIMENTAL_MARKER

SCHEMA_KIND = EXPERIMENTAL_MARKER
TAIL_SCHEMA_VERSION = "engineering.certification_tail.v0"

#: Written once, never modified. A packet is the preimage of a verdict; editing
#: one retroactively changes what a reviewer is recorded as having seen.
IMMUTABLE_EVIDENCE_PREFIXES: tuple[str, ...] = ("docs/review_packets/",)

#: May grow, never shrink or change what is already there.
APPEND_ONLY_EVIDENCE_PATHS: tuple[str, ...] = (
    "docs/EW0A_REVIEW_PACKETS.jsonl",
    "docs/EW0A_REVIEW_JOURNAL.jsonl",
)

#: Anything outside the allowlist already refuses. These are named separately so
#: the refusal says WHICH boundary was crossed instead of only that one was.
#: .gitattributes is here because a textconv driver would undermine the blob
#: reads this proof depends on.
FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "portfolio_automation/", "tests/", "config/", ".agent/", ".github/",
    ".gitattributes", ".gitmodules", ".gitignore",
)

_FULL_SHA = re.compile(r"\A[0-9a-f]{40}\Z")

#: A file may be added or modified. Anything else -- deletion, type change
#: (a file replaced by a symlink reports T), mode change -- is not an append.
_SUPPORTED_STATUSES = frozenset({"A", "M"})


class TailRefusal(str, Enum):
    CANDIDATE_UNRESOLVABLE = "CANDIDATE_UNRESOLVABLE"
    CERTIFICATION_HEAD_UNRESOLVABLE = "CERTIFICATION_HEAD_UNRESOLVABLE"
    CANDIDATE_NOT_ANCESTOR = "CANDIDATE_NOT_ANCESTOR"
    HISTORY_NOT_LINEAR = "HISTORY_NOT_LINEAR"
    PATH_OUTSIDE_EVIDENCE_ALLOWLIST = "PATH_OUTSIDE_EVIDENCE_ALLOWLIST"
    FORBIDDEN_PATH_TOUCHED = "FORBIDDEN_PATH_TOUCHED"
    UNSUPPORTED_CHANGE_KIND = "UNSUPPORTED_CHANGE_KIND"
    EVIDENCE_DELETED = "EVIDENCE_DELETED"
    IMMUTABLE_ARTIFACT_MODIFIED = "IMMUTABLE_ARTIFACT_MODIFIED"
    LEDGER_NOT_APPEND_ONLY = "LEDGER_NOT_APPEND_ONLY"
    GIT_UNAVAILABLE = "GIT_UNAVAILABLE"


@dataclass(frozen=True)
class TailChange:
    path: str
    status: str
    commit: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "status": self.status, "commit": self.commit}


@dataclass(frozen=True)
class TailPolicy:
    immutable_prefixes: tuple[str, ...] = IMMUTABLE_EVIDENCE_PREFIXES
    append_only_paths: tuple[str, ...] = APPEND_ONLY_EVIDENCE_PATHS
    forbidden_prefixes: tuple[str, ...] = FORBIDDEN_PREFIXES

    def with_ledger(self, *ledger_rels: str) -> "TailPolicy":
        """Admit the ACTIVE session ledger only.

        Prior sessions' ledgers are sealed evidence; a glob over every session
        file would let a tail append to a closed session's history."""
        return TailPolicy(self.immutable_prefixes,
                          tuple(self.append_only_paths) + tuple(ledger_rels),
                          self.forbidden_prefixes)


def classify_path(path: str, policy: TailPolicy) -> str:
    if any(path.startswith(p) for p in policy.immutable_prefixes):
        return "IMMUTABLE"
    if path in policy.append_only_paths:
        return "APPEND_ONLY"
    if any(path.startswith(p) or path == p for p in policy.forbidden_prefixes):
        return "FORBIDDEN"
    return "UNKNOWN"


@dataclass(frozen=True)
class GitAncestryView:
    """Git access for the tail proof.

    Deliberately NOT reusing GitRepoView: that helper returns None for every
    non-zero exit, which folds "not an ancestor" (rc 1) together with "git is
    broken" (rc 128). An integrity proof must never report a broken repository
    as a clean negative."""

    repo_root: Path

    def _run(self, *args: str) -> tuple[int, bytes, str]:
        try:
            done = subprocess.run(("git", *args), cwd=str(self.repo_root),
                                  capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            return 128, b"", str(exc)
        return done.returncode, done.stdout, done.stderr.decode("utf-8", "replace")

    def commit_exists(self, sha: str) -> bool:
        if not _FULL_SHA.match(sha or ""):
            # Abbreviations resolve happily in git, which would let a caller
            # supply an ambiguity the proof then treats as exact.
            return False
        rc, _, _ = self._run("rev-parse", "--verify", "--quiet", sha + "^{commit}")
        return rc == 0

    def is_ancestor(self, ancestor: str, descendant: str) -> Optional[bool]:
        rc, _, _ = self._run("merge-base", "--is-ancestor", ancestor, descendant)
        if rc == 0:
            return True
        if rc == 1:
            return False
        return None                     # git failed; NOT the same as "no"

    def commits_between(self, a: str, b: str) -> Optional[tuple[str, ...]]:
        rc, out, _ = self._run("rev-list", "--reverse", f"{a}..{b}")
        if rc != 0:
            return None
        return tuple(out.decode().split())

    def merge_count(self, a: str, b: str) -> Optional[int]:
        rc, out, _ = self._run("rev-list", "--merges", "--count", f"{a}..{b}")
        if rc != 0:
            return None
        return int(out.decode().strip() or 0)

    def changed_paths(self, a: str, b: str) -> Optional[tuple[TailChange, ...]]:
        # --no-renames: rename detection would report a code file MOVED into the
        # evidence directory as a single rename, laundering a deletion plus an
        # illegal addition into one innocuous-looking change.
        # -z: paths with non-ASCII or special characters are otherwise quoted,
        # which would let a crafted name evade an allowlist prefix match.
        rc, out, _ = self._run("diff-tree", "-r", "--no-renames", "-z",
                               "--name-status", a, b)
        if rc != 0:
            return None
        fields = out.decode("utf-8", "surrogateescape").split("\0")
        changes: list[TailChange] = []
        i = 0
        while i + 1 < len(fields):
            status = fields[i].strip()
            path = fields[i + 1]
            if status and path:
                changes.append(TailChange(path=path, status=status[0], commit=b))
            i += 2
        return tuple(changes)

    def blob_bytes(self, sha: str, path: str) -> Optional[bytes]:
        """Raw bytes of a blob. Never `git show`, and never text mode."""
        rc, _, _ = self._run("cat-file", "-e", f"{sha}:{path}")
        if rc != 0:
            return None
        rc, out, _ = self._run("cat-file", "blob", f"{sha}:{path}")
        if rc != 0:
            return None
        return out


@dataclass(frozen=True)
class CertificationTailProof:
    reviewed_candidate_sha: Optional[str]
    certification_head_sha: Optional[str]
    tail_commits: tuple[str, ...] = ()
    changes: tuple[TailChange, ...] = ()
    refusals: tuple[TailRefusal, ...] = ()
    details: tuple[str, ...] = ()
    checks: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.refusals

    @property
    def verdict(self) -> str:
        return "YES" if self.ok else "NO"

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": TAIL_SCHEMA_VERSION, "schema_kind": SCHEMA_KIND,
                "CERTIFICATION_TAIL_VALID": self.verdict,
                "reviewed_candidate_sha": self.reviewed_candidate_sha,
                "certification_head_sha": self.certification_head_sha,
                "tail_commits": list(self.tail_commits),
                "changes": [c.to_dict() for c in self.changes],
                "checks": dict(self.checks),
                "refusals": [r.value for r in self.refusals],
                "details": list(self.details)}


def prove_certification_tail(reviewed_candidate_sha: Optional[str],
                             certification_head_sha: Optional[str],
                             repo: GitAncestryView, *,
                             policy: TailPolicy = TailPolicy()
                             ) -> CertificationTailProof:
    """Prove the head only appends permitted evidence to the reviewed candidate.

    Collects EVERY refusal rather than returning on the first, so a repair does
    not have to rediscover the next problem one run at a time."""
    refusals: list[TailRefusal] = []
    details: list[str] = []
    checks: dict[str, str] = {}

    def fail(r: TailRefusal, detail: str) -> None:
        if r not in refusals:
            refusals.append(r)
        details.append(detail)

    cand, head = reviewed_candidate_sha or "", certification_head_sha or ""
    if not repo.commit_exists(cand):
        fail(TailRefusal.CANDIDATE_UNRESOLVABLE,
             f"reviewed candidate {cand!r} is not a resolvable 40-hex commit")
    if not repo.commit_exists(head):
        fail(TailRefusal.CERTIFICATION_HEAD_UNRESOLVABLE,
             f"certification head {head!r} is not a resolvable 40-hex commit")
    if refusals:
        return CertificationTailProof(reviewed_candidate_sha, certification_head_sha,
                                      refusals=tuple(refusals), details=tuple(details),
                                      checks={"CERTIFICATION_TAIL_VALID": "NO"})

    ancestry = repo.is_ancestor(cand, head)
    if ancestry is None:
        fail(TailRefusal.GIT_UNAVAILABLE,
             "git could not decide ancestry; an unreadable repository is not a "
             "clean negative")
    elif not ancestry:
        fail(TailRefusal.CANDIDATE_NOT_ANCESTOR,
             "the reviewed candidate is not an ancestor of the certification "
             "head; history was rewritten, rebased or replaced")
    checks["CANDIDATE_IS_ANCESTOR_OF_HEAD"] = (
        "YES" if ancestry else ("UNKNOWN" if ancestry is None else "NO"))

    commits = repo.commits_between(cand, head) if ancestry else ()
    if commits is None:
        fail(TailRefusal.GIT_UNAVAILABLE, "could not enumerate the tail commits")
        commits = ()

    merges = repo.merge_count(cand, head) if ancestry else 0
    if merges is None:
        fail(TailRefusal.GIT_UNAVAILABLE, "could not count merges in the tail")
    elif merges:
        # A merge passes the ancestry test while its second parent smuggles in
        # arbitrary changes, and a per-commit diff would only show the
        # first-parent delta.
        fail(TailRefusal.HISTORY_NOT_LINEAR,
             f"{merges} merge commit(s) in the tail; a merge can carry changes "
             "that a first-parent walk never sees")
    checks["TAIL_HISTORY_LINEAR"] = "NO" if merges else "YES"

    all_changes: list[TailChange] = []
    paths_ok = True
    immutable_ok = True
    append_ok = True

    # Walk EVERY commit. An endpoint diff shows a packet added-then-tampered as
    # a clean addition, and a ledger appended-rewritten-restored as a clean
    # append.
    prev = cand
    for commit in commits:
        step = repo.changed_paths(prev, commit)
        if step is None:
            fail(TailRefusal.GIT_UNAVAILABLE, f"could not diff {prev[:12]}..{commit[:12]}")
            prev = commit
            continue
        for change in step:
            all_changes.append(change)
            kind = classify_path(change.path, policy)
            if kind == "FORBIDDEN":
                paths_ok = False
                fail(TailRefusal.FORBIDDEN_PATH_TOUCHED,
                     f"{change.path} changed in {commit[:12]}; implementation, "
                     "tests, config and authority may not change after review")
                continue
            if kind == "UNKNOWN":
                paths_ok = False
                fail(TailRefusal.PATH_OUTSIDE_EVIDENCE_ALLOWLIST,
                     f"{change.path} is not permitted certification evidence")
                continue
            if change.status == "D":
                fail(TailRefusal.EVIDENCE_DELETED,
                     f"{change.path} deleted in {commit[:12]}")
                append_ok = False
                continue
            if change.status not in _SUPPORTED_STATUSES:
                fail(TailRefusal.UNSUPPORTED_CHANGE_KIND,
                     f"{change.path} has change kind {change.status!r} in "
                     f"{commit[:12]} (a type or mode change is not an append)")
                append_ok = False
                continue
            if kind == "IMMUTABLE" and change.status == "M":
                immutable_ok = False
                fail(TailRefusal.IMMUTABLE_ARTIFACT_MODIFIED,
                     f"{change.path} was modified in {commit[:12]}; a packet is "
                     "the preimage of a verdict and is written once")
                continue
            if kind == "APPEND_ONLY" and change.status == "M":
                old = repo.blob_bytes(prev, change.path)
                new = repo.blob_bytes(commit, change.path)
                if new is None:
                    fail(TailRefusal.GIT_UNAVAILABLE,
                         f"could not read {change.path} at {commit[:12]}")
                    append_ok = False
                elif old is not None and not new.startswith(old):
                    append_ok = False
                    fail(TailRefusal.LEDGER_NOT_APPEND_ONLY,
                         f"{change.path} at {commit[:12]} does not extend its "
                         "previous content; records were rewritten, reordered or "
                         "truncated")
        prev = commit

    checks["TAIL_PATHS_WITHIN_ALLOWLIST"] = "YES" if paths_ok else "NO"
    checks["IMMUTABLE_ARTIFACTS_UNMODIFIED"] = "YES" if immutable_ok else "NO"
    checks["LEDGERS_APPEND_ONLY"] = "YES" if append_ok else "NO"
    checks["CERTIFICATION_TAIL_VALID"] = "NO" if refusals else "YES"

    return CertificationTailProof(
        reviewed_candidate_sha=cand, certification_head_sha=head,
        tail_commits=tuple(commits), changes=tuple(all_changes),
        refusals=tuple(refusals), details=tuple(details), checks=checks)
