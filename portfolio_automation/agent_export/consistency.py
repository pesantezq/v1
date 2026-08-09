"""Runtime/code consistency: does the Agent Lab's shadow checkout match production?

The Agent Lab compares CODE INTENT against ACTUAL OUTPUTS. That comparison is
only sound when the code being read is the code that produced the artifacts.
This module answers one narrow question — how does a local shadow SHA relate to
the SHA recorded in a snapshot — and deliberately does nothing else.

**It never fetches, pulls, checks out, or writes.** Every git call here is
read-only against the local object store. Refreshing the shadow to match
production is a separate, future orchestration step (``refresh-agent-inputs``,
see ``docs/STOCKBOT_AGENT_EXPORT.md``); keeping comparison and mutation apart
means an analysis run can never silently move the code out from under itself.
"""
from __future__ import annotations

import subprocess
from enum import Enum
from pathlib import Path
from typing import Any, Callable

_GIT_TIMEOUT_S = 10


class ShadowStatus(str, Enum):
    """Relationship of the local shadow checkout to the production snapshot."""

    MATCH = "MATCH"                  # identical commit; analysis is sound
    SHADOW_BEHIND = "SHADOW_BEHIND"  # shadow is an ancestor of production
    SHADOW_AHEAD = "SHADOW_AHEAD"    # production is an ancestor of shadow
    UNKNOWN = "UNKNOWN"              # missing SHA, no ancestry info, or diverged


AncestryProbe = Callable[[str, str], bool | None]
"""``(candidate_ancestor, descendant) -> True | False | None`` (``None`` = cannot tell)."""


def git_ancestry_probe(repo_root: Path | str) -> AncestryProbe:
    """Local, read-only ancestry probe over an existing checkout.

    Returns ``None`` rather than ``False`` when either commit is absent from the
    local object store — "I don't have that commit" and "that commit is not an
    ancestor" are different answers, and collapsing them would let a shallow
    clone masquerade as a divergence.
    """

    def probe(ancestor: str, descendant: str) -> bool | None:
        for rev in (ancestor, descendant):
            try:
                have = subprocess.run(
                    ["git", "cat-file", "-e", f"{rev}^{{commit}}"],
                    cwd=str(repo_root), capture_output=True, timeout=_GIT_TIMEOUT_S,
                )
            except (OSError, subprocess.SubprocessError):
                return None
            if have.returncode != 0:
                return None
        try:
            result = subprocess.run(
                ["git", "merge-base", "--is-ancestor", ancestor, descendant],
                cwd=str(repo_root), capture_output=True, timeout=_GIT_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        return None

    return probe


def compare_shadow_to_production(
    shadow_sha: str | None,
    production_sha: str | None,
    *,
    ancestry: AncestryProbe | None = None,
    repo_root: Path | str | None = None,
) -> dict[str, Any]:
    """Classify the shadow checkout against a snapshot's ``production_git_sha``.

    Args:
        shadow_sha: HEAD of the local Agent Lab shadow checkout.
        production_sha: ``production.production_git_sha`` from a validated manifest.
        ancestry: ancestry probe. Defaults to a local read-only git probe when
            ``repo_root`` is given; without either, only MATCH/UNKNOWN are
            distinguishable (which is the honest answer, not a degraded one).
        repo_root: checkout to run the default probe against.

    Returns a dict with ``status``, ``reason``, ``analysis_safe``, and the two
    SHAs. ``analysis_safe`` is True only for MATCH — a behind/ahead shadow can
    still be *useful*, but conclusions drawn from it are about different code
    than produced the artifacts, and the caller must opt into that knowingly.
    """
    shadow = (shadow_sha or "").strip() or None
    production = (production_sha or "").strip() or None

    if not shadow or not production or shadow == "unknown" or production == "unknown":
        return _result(ShadowStatus.UNKNOWN, "missing_sha", shadow, production)

    # Compare on the shorter length so an abbreviated SHA on either side still
    # matches its full form, rather than reporting a spurious divergence.
    width = min(len(shadow), len(production))
    if width >= 7 and shadow[:width].lower() == production[:width].lower():
        return _result(ShadowStatus.MATCH, "identical_commit", shadow, production)

    if ancestry is None and repo_root is not None:
        ancestry = git_ancestry_probe(repo_root)
    if ancestry is None:
        return _result(ShadowStatus.UNKNOWN, "no_ancestry_probe", shadow, production)

    shadow_is_ancestor = ancestry(shadow, production)
    if shadow_is_ancestor is True:
        return _result(ShadowStatus.SHADOW_BEHIND, "shadow_is_ancestor_of_production",
                       shadow, production)

    production_is_ancestor = ancestry(production, shadow)
    if production_is_ancestor is True:
        return _result(ShadowStatus.SHADOW_AHEAD, "production_is_ancestor_of_shadow",
                       shadow, production)

    if shadow_is_ancestor is False and production_is_ancestor is False:
        # Both directions answered "no" — the histories genuinely diverged.
        return _result(ShadowStatus.UNKNOWN, "histories_diverged", shadow, production)

    return _result(ShadowStatus.UNKNOWN, "commits_not_present_locally", shadow, production)


def _result(
    status: ShadowStatus, reason: str, shadow: str | None, production: str | None,
) -> dict[str, Any]:
    return {
        "status": status.value,
        "reason": reason,
        "analysis_safe": status is ShadowStatus.MATCH,
        "local_shadow_sha": shadow,
        "production_snapshot_sha": production,
    }


def compare_against_snapshot(
    manifest: dict[str, Any],
    shadow_sha: str | None,
    *,
    ancestry: AncestryProbe | None = None,
    repo_root: Path | str | None = None,
) -> dict[str, Any]:
    """Convenience wrapper reading ``production_git_sha`` out of a validated manifest."""
    production = (manifest.get("production") or {}).get("production_git_sha")
    return compare_shadow_to_production(
        shadow_sha, production, ancestry=ancestry, repo_root=repo_root)
