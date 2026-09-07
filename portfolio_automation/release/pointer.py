"""Prove the release pointer targets the approved SHA.

Why this exists
---------------
Scheduler path alignment proves only that scheduler strings sit *lexically*
beneath ``/opt/stockbot/current``. It never resolves the symlink and never
compares its target against the approved release. So every execution surface
could report ``status: OK`` while ``current`` still points at an older or
unauthorized release — and the advertised certification would not establish the
one thing it claims:

    production_code_sha == approved_release_sha

That is the whole invariant, so the pointer needs its own evidence and its own
proof. This module supplies both, and
``scheduler.certify_release_identity`` requires them together.

The chain that must be proven, link by link::

    /opt/stockbot/current            the pointer exists, and is a symlink
      -> releases/<sha>              its target resolves, and lives in the
                                     releases root (not the legacy checkout,
                                     not somewhere arbitrary)
      -> git HEAD at that directory  the resolved tree's actual commit
      == approved_release_sha        which equals the approved release

Every link is a separate failure mode and every one fails closed. In
particular a *missing* SHA is a failure, not a pass: "we could not determine
what is deployed" must never certify as "the right thing is deployed".

Fixture-driven by design. ``PointerEvidence`` is a plain record the future
cutover runbook fills in from the live host; nothing here reads the host, so
the contract is testable without a VPS, root, or systemd.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

POINTER_OK = "OK"
POINTER_FAILED = "FAILED"

#: A full 40-hex git SHA. Abbreviations are rejected: comparing a 12-char
#: prefix to an approved SHA invites a false match and hides which commit is
#: actually deployed.
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def _under(path: str, root: str) -> bool:
    p = PurePosixPath(str(path))
    r = PurePosixPath(str(root).rstrip("/"))
    return p == r or r in p.parents


@dataclass(frozen=True)
class PointerEvidence:
    """Observed facts about the release pointer on a host.

    Collected during cutover, e.g.::

        readlink /opt/stockbot/current
        readlink -f /opt/stockbot/current
        git -C "$(readlink -f /opt/stockbot/current)" rev-parse HEAD

    Every field is Optional/absent-able because the point of the contract is to
    fail closed when a fact could not be established.
    """

    pointer_path: str
    exists: bool = False
    is_symlink: bool = False
    #: Raw symlink target as recorded (may be relative).
    link_target: str | None = None
    #: Fully resolved absolute directory the pointer leads to.
    resolved_path: str | None = None
    resolved_exists: bool = False
    #: git HEAD of the resolved directory, full 40-hex.
    target_sha: str | None = None
    #: Whether the resolved tree had uncommitted tracked modifications.
    target_tracked_dirty: bool | None = None


def certify_pointer(evidence: PointerEvidence, *, approved_sha: str,
                    releases_root: str = "/opt/stockbot/releases",
                    require_symlink: bool = True,
                    require_clean: bool = True) -> dict:
    """Certify pointer -> resolved release -> git SHA == approved SHA.

    Returns ``{"status": "OK"|"FAILED", "errors": [...], ...}``. Never raises
    on bad input: an unusable evidence record is a FAILED certification, which
    is the safe direction.
    """
    errors: list[str] = []

    if not _FULL_SHA.match(str(approved_sha or "")):
        errors.append(
            f"approved_sha is not a full 40-hex commit: {approved_sha!r}")

    if not evidence.exists:
        errors.append(f"pointer missing: {evidence.pointer_path}")
    elif require_symlink and not evidence.is_symlink:
        # In pointer mode the switch must BE a symlink. A real directory named
        # `current` cannot be swapped atomically, so a release would be a
        # partial copy — the exact mixed-release state the model exists to
        # prevent.
        errors.append(
            f"pointer is not a symlink (pointer mode requires one): "
            f"{evidence.pointer_path}")

    if not evidence.resolved_path:
        errors.append("pointer target did not resolve")
    else:
        if not evidence.resolved_exists:
            errors.append(f"pointer target does not exist: {evidence.resolved_path}")
        if not _under(evidence.resolved_path, releases_root):
            errors.append(
                f"pointer target is outside the releases root {releases_root}: "
                f"{evidence.resolved_path}")

    if not evidence.target_sha:
        # Absence of evidence is not evidence of correctness.
        errors.append("git SHA of the pointer target is unavailable — cannot certify")
    elif not _FULL_SHA.match(evidence.target_sha):
        errors.append(
            f"pointer target SHA is not a full 40-hex commit: {evidence.target_sha!r}")
    elif _FULL_SHA.match(str(approved_sha or "")) and evidence.target_sha != approved_sha:
        errors.append(
            f"pointer target SHA {evidence.target_sha} != approved "
            f"{approved_sha}")

    if require_clean and evidence.target_tracked_dirty is None:
        errors.append("tracked-dirty state of the pointer target is unknown")
    elif require_clean and evidence.target_tracked_dirty:
        errors.append(
            f"pointer target has uncommitted tracked modifications: "
            f"{evidence.resolved_path}")

    return {
        "status": POINTER_OK if not errors else POINTER_FAILED,
        "errors": errors,
        "pointer_path": evidence.pointer_path,
        "resolved_path": evidence.resolved_path,
        "target_sha": evidence.target_sha,
        "approved_sha": approved_sha,
    }
