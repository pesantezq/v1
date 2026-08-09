"""Production code + run identity: what exactly produced the artifacts being frozen.

The whole point of the export is to let the Agent Lab compare CODE INTENT against
ACTUAL OUTPUTS. That comparison is worthless unless the snapshot states, without
ambiguity, which commit produced it.

Two distinct SHAs matter and are recorded separately:

``run_git_sha``
    The commit recorded by ``run_manifest.source_commit`` when the run STARTED.
    This is the authoritative "what produced these artifacts" answer.

``head_git_sha``
    The repo's HEAD *right now*, when the export runs. On this deployment the
    VPS is an active dev environment (see CLAUDE.md → Operating Mode), so HEAD
    can legitimately have moved since the 09:00 cron run. When the two differ
    the snapshot is marked degraded rather than refused — the artifacts are
    still exactly what ``run_git_sha`` produced.

Every probe here degrades to a recorded ``"unknown"`` instead of raising, mirroring
``portfolio_automation.run_manifest``. Refusal is the builder's job, not the probe's.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Paths whose modification is EXPECTED churn from running the pipeline, not
# code contamination. A production run writes artifacts by design; treating that
# as a dirty tree would make every snapshot permanently degraded.
_ARTIFACT_CHURN_PREFIXES = ("outputs/", "data/", "logs/", "scraped_intel/", ".agent/")

_GIT_TIMEOUT_S = 10


def _git(root: Path | str, *args: str, strip: bool = True) -> str | None:
    """Run a read-only git command; ``None`` on any failure. Never fetches.

    ``strip=False`` matters for ``status --porcelain``: its first two columns are
    the status code and an unstaged change renders as ``" M path"``. Stripping
    the output would eat that leading space and shift every path by one
    character, silently turning ``README.md`` into ``EADME.md`` and
    ``outputs/...`` into ``utputs/...`` — which would then be misclassified as
    source contamination rather than expected artifact churn.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(root), capture_output=True, text=True, timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() if strip else proc.stdout


def is_git_checkout(root: Path | str) -> bool:
    return _git(root, "rev-parse", "--is-inside-work-tree") == "true"


def resolve_full_sha(root: Path | str, rev: str) -> str | None:
    """Expand an abbreviated rev to a full 40-char SHA, verifying it exists locally."""
    if not rev or rev == "unknown":
        return None
    out = _git(root, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    if out and len(out) == 40:
        return out
    return None


@dataclass
class WorkingTreeState:
    """Classification of uncommitted changes at export time."""

    is_git_checkout: bool = False
    tracked_modified: tuple[str, ...] = ()
    code_modified: tuple[str, ...] = ()
    artifact_churn_count: int = 0
    untracked_count: int = 0

    @property
    def code_clean(self) -> bool:
        """True when no *tracked source file* differs from the commit.

        Artifact churn under ``outputs/`` is excluded — that is what a run does.
        Untracked files are counted but not treated as contamination: they cannot
        change the behaviour of code that already ran.
        """
        return self.is_git_checkout and not self.code_modified

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_git_checkout": self.is_git_checkout,
            "code_clean": self.code_clean,
            # Paths only — never contents. A path list is safe to publish; the
            # diff itself could contain anything.
            "code_modified_paths": list(self.code_modified),
            "code_modified_count": len(self.code_modified),
            "artifact_churn_count": self.artifact_churn_count,
            "untracked_count": self.untracked_count,
        }


def working_tree_state(root: Path | str) -> WorkingTreeState:
    """Classify the working tree into code contamination vs expected artifact churn."""
    if not is_git_checkout(root):
        return WorkingTreeState(is_git_checkout=False)

    porcelain = _git(root, "status", "--porcelain", strip=False)
    if porcelain is None:
        return WorkingTreeState(is_git_checkout=True)

    tracked: list[str] = []
    code: list[str] = []
    churn = 0
    untracked = 0
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        status, path = line[:2], line[3:].strip()
        if " -> " in path:  # rename
            path = path.split(" -> ", 1)[1].strip()
        path = path.strip('"')
        if status.strip() == "??":
            untracked += 1
            continue
        tracked.append(path)
        if path.startswith(_ARTIFACT_CHURN_PREFIXES):
            churn += 1
        else:
            code.append(path)

    return WorkingTreeState(
        is_git_checkout=True,
        tracked_modified=tuple(sorted(tracked)),
        code_modified=tuple(sorted(code)),
        artifact_churn_count=churn,
        untracked_count=untracked,
    )


@dataclass
class ProductionIdentity:
    """Frozen answer to "what code, and which run, produced these artifacts?"."""

    run_id: str | None = None
    run_started_at: str | None = None
    run_completed_at: str | None = None
    run_status: str | None = None
    pipeline_mode: str | None = None
    config_hash: str | None = None
    runtime: dict[str, Any] = field(default_factory=dict)

    run_git_sha: str | None = None          # full 40-char when resolvable
    run_git_sha_recorded: str | None = None  # verbatim from run_manifest
    run_git_sha_source: str = "unknown"     # run_manifest | repo_head | unknown
    head_git_sha: str | None = None
    git_branch: str | None = None
    code_moved_since_run: bool | None = None

    working_tree: WorkingTreeState = field(default_factory=WorkingTreeState)
    degradations: tuple[str, ...] = ()

    @property
    def production_git_sha(self) -> str:
        """The SHA the snapshot asserts produced it; ``"unknown"`` if undeterminable."""
        return self.run_git_sha or self.run_git_sha_recorded or "unknown"

    def run_identity(self) -> dict[str, Any]:
        """STABLE facts: which run, which code, which config produced the artifacts.

        Everything here is fixed the moment the run finishes. It is therefore the
        only provenance the content fingerprint covers — re-exporting the same run
        tomorrow must reproduce this block byte for byte.
        """
        return {
            "run_id": self.run_id,
            "run_started_at": self.run_started_at,
            "run_completed_at": self.run_completed_at,
            "run_status": self.run_status,
            "pipeline_mode": self.pipeline_mode,
            "config_hash": self.config_hash,
            "runtime": dict(sorted(self.runtime.items())),
            "production_git_sha": self.production_git_sha,
            "production_git_sha_recorded": self.run_git_sha_recorded,
            "production_git_sha_source": self.run_git_sha_source,
        }

    def export_context(self) -> dict[str, Any]:
        """VOLATILE observations made when the exporter ran, not when the run ran.

        Kept strictly out of the content fingerprint. On this deployment the VPS
        is an active dev environment, so HEAD and the working tree move between
        the 09:00 cron run and any later export. Folding those into the content
        digest would make a re-export of an unchanged run look like tampering —
        a false integrity alarm, which is the fastest way to teach an operator to
        ignore real ones. They remain covered by ``snapshot_sha256``, so they
        still cannot be edited after the fact.
        """
        return {
            "head_git_sha_at_export": self.head_git_sha,
            "git_branch": self.git_branch,
            "code_moved_since_run": self.code_moved_since_run,
            "working_tree": self.working_tree.to_dict(),
            "degradations": list(self.degradations),
        }


def read_run_manifest(root: Path | str) -> dict[str, Any] | None:
    """Read ``outputs/policy/run_manifest.json``; ``None`` if absent or corrupt.

    Read directly rather than via ``run_manifest.read_manifest`` so the exporter
    can be pointed at an arbitrary snapshot root in tests without importing the
    live pipeline's cwd assumptions.
    """
    path = Path(root) / "outputs" / "policy" / "run_manifest.json"
    try:
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def collect_production_identity(root: Path | str) -> ProductionIdentity:
    """Assemble run + code identity, recording every degradation it hits.

    Never raises: an undeterminable SHA is reported as ``"unknown"`` with a
    ``git_sha_undeterminable`` degradation, and the BUILDER decides whether that
    is fatal. Keeping the decision out of the probe means the same probe serves
    the strict build path and the tolerant health path.
    """
    root = Path(root)
    degradations: list[str] = []

    manifest = read_run_manifest(root)
    if manifest is None:
        degradations.append("run_manifest_absent_or_unreadable")
        manifest = {}

    identity = ProductionIdentity(
        run_id=manifest.get("run_id"),
        run_started_at=manifest.get("started_at"),
        run_completed_at=manifest.get("completed_at"),
        run_status=manifest.get("status"),
        pipeline_mode=manifest.get("pipeline_mode"),
        config_hash=manifest.get("config_hash"),
        runtime=dict(manifest.get("runtime") or {}),
    )

    identity.working_tree = working_tree_state(root)
    if not identity.working_tree.is_git_checkout:
        degradations.append("not_a_git_checkout")
    elif not identity.working_tree.code_clean:
        degradations.append("working_tree_code_modified")

    recorded = manifest.get("source_commit")
    identity.run_git_sha_recorded = recorded if recorded and recorded != "unknown" else None

    identity.head_git_sha = _git(root, "rev-parse", "HEAD") or None
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    identity.git_branch = branch or None

    if identity.run_git_sha_recorded:
        full = resolve_full_sha(root, identity.run_git_sha_recorded)
        if full:
            identity.run_git_sha = full
            identity.run_git_sha_source = "run_manifest"
        else:
            # The run's commit is named but not present locally (history rewritten,
            # shallow clone). Keep the recorded value, flag that we could not verify.
            identity.run_git_sha_source = "run_manifest_unverified"
            degradations.append("run_commit_not_resolvable_locally")
    elif identity.head_git_sha:
        # No run-recorded commit. HEAD is the honest fallback but it is an
        # ASSUMPTION, not evidence, so it is labelled as such.
        identity.run_git_sha = identity.head_git_sha
        identity.run_git_sha_source = "repo_head"
        degradations.append("git_sha_assumed_from_head")
    else:
        degradations.append("git_sha_undeterminable")

    if identity.run_git_sha and identity.head_git_sha:
        identity.code_moved_since_run = identity.run_git_sha != identity.head_git_sha
        if identity.code_moved_since_run:
            degradations.append("head_moved_since_run")

    identity.degradations = tuple(dict.fromkeys(degradations))  # dedupe, keep order
    return identity
