"""Phase 1 — run identity, lineage, and artifact integrity.

Produces an immutable per-run manifest that ties every artifact of a pipeline
run to one coherent ``run_id`` plus the point-in-time provenance needed to
detect mixed-run, stale, or incomplete inputs.

This module is pure/deterministic except for the explicitly injected timestamps
and the git/host probes (which degrade to ``"unknown"`` rather than raising).
It never calls ``datetime.now`` itself — callers supply ISO timestamps so the
manifest is reproducible in tests (Iron rule 8: same inputs -> idempotent
outputs).

Observe-only: writing a manifest does not mutate any decision, allocation,
score, or portfolio state. It records what a run did; it does not change it.
"""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import (
    OutputNamespace, safe_write_json,
)
from portfolio_automation.run_status import make_run_id  # reuse the stable id

RUN_MANIFEST_SCHEMA_VERSION = "1"
_MANIFEST_FILENAME = "run_manifest.json"

# Re-export so callers have one import site for run identity.
__all__ = [
    "make_run_id", "compute_config_hash", "source_commit", "runtime_identity",
    "build_manifest", "write_manifest", "read_manifest", "begin_run",
    "complete_run", "is_complete", "coherent_run_ids",
    "RUN_MANIFEST_SCHEMA_VERSION",
]


# ---------------------------------------------------------------------------
# Provenance probes (degrade honestly, never raise)
# ---------------------------------------------------------------------------


def compute_config_hash(config_path: Path | str) -> str:
    """sha256 of the config file's bytes; ``"missing"`` if absent/unreadable.

    Stable for identical content (idempotent), distinct for different content.
    """
    p = Path(config_path)
    try:
        if not p.exists():
            return "missing"
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except Exception:
        return "unreadable"


def source_commit(root: Path | str = ".") -> str:
    """Short git HEAD sha for *root*; ``"unknown"`` if git is unavailable."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(root), capture_output=True, text=True, timeout=5,
        )
        sha = out.stdout.strip()
        return sha or "unknown"
    except Exception:
        return "unknown"


def runtime_identity() -> dict[str, Any]:
    """Minimal runtime fingerprint (python + platform). Host is best-effort."""
    try:
        host = platform.node() or "unknown"
    except Exception:
        host = "unknown"
    return {
        "python": platform.python_version(),
        "platform": platform.system(),
        "host": host,
    }


# ---------------------------------------------------------------------------
# Manifest construction
# ---------------------------------------------------------------------------


def build_manifest(
    *,
    run_id: str,
    started_at: str,
    data_as_of: str | None,
    source_commit: str,
    config_hash: str,
    pipeline_mode: str,
    status: str = "running",
    completed_at: str | None = None,
    failure_stage: str | None = None,
    upstream_freshness: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a fully-populated run-manifest dict (no I/O)."""
    return {
        "schema_version": RUN_MANIFEST_SCHEMA_VERSION,
        "artifact_type": "run_manifest",
        "observe_only": True,
        "run_id": run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "data_as_of": data_as_of,
        "source_commit": source_commit,
        "config_hash": config_hash,
        "pipeline_mode": pipeline_mode,
        "runtime": runtime if runtime is not None else runtime_identity(),
        "upstream_freshness": upstream_freshness or {},
        "status": status,            # running | complete | failed
        "failure_stage": failure_stage,
    }


# ---------------------------------------------------------------------------
# Persistence (atomic via safe_write_json) + read-back
# ---------------------------------------------------------------------------


def write_manifest(root: Path | str, manifest: dict[str, Any]) -> Path:
    """Atomically persist the manifest to the POLICY namespace."""
    base = str(Path(root) / "outputs")
    return safe_write_json(OutputNamespace.POLICY, _MANIFEST_FILENAME,
                           manifest, base_dir=base)


def read_manifest(root: Path | str) -> dict[str, Any] | None:
    """Read the current run manifest; ``None`` if absent/corrupt."""
    path = Path(root) / "outputs" / "policy" / _MANIFEST_FILENAME
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def begin_run(
    root: Path | str,
    *,
    pipeline_mode: str,
    started_at: str,
    data_as_of: str | None = None,
    config_path: Path | str = "config.json",
) -> dict[str, Any]:
    """Open a run: build a manifest with ``status="running"`` and persist it.

    The ``run_id`` is the stable ``YYYY-MM-DD_<mode>_official`` id (idempotent
    for a given date+mode, so a same-day rerun is last-wins, not a new run).
    """
    rid = make_run_id(pipeline_mode, generated_at=started_at)
    manifest = build_manifest(
        run_id=rid,
        started_at=started_at,
        data_as_of=data_as_of if data_as_of is not None else started_at,
        source_commit=source_commit(root),
        config_hash=compute_config_hash(config_path),
        pipeline_mode=pipeline_mode,
        status="running",
    )
    write_manifest(root, manifest)
    return manifest


def complete_run(
    root: Path | str,
    *,
    completed_at: str,
    status: str = "complete",
    failure_stage: str | None = None,
) -> dict[str, Any]:
    """Close the current run: stamp ``completed_at`` + terminal ``status``.

    A run that ends ``failed`` records its ``failure_stage`` and is NOT
    considered complete by :func:`is_complete` (so consumers / the
    complete-run guard never treat a failed run's artifacts as authoritative).
    """
    manifest = read_manifest(root) or {}
    manifest = {**manifest, "completed_at": completed_at, "status": status,
                "failure_stage": failure_stage}
    write_manifest(root, manifest)
    return manifest


def is_complete(manifest: dict[str, Any] | None) -> bool:
    """True only for a manifest whose run finished with ``status="complete"``."""
    return bool(manifest) and manifest.get("status") == "complete"


# ---------------------------------------------------------------------------
# Mixed-run guard
# ---------------------------------------------------------------------------


def coherent_run_ids(expected_run_id: str, artifacts: list[dict[str, Any]]) -> bool:
    """True iff every artifact carries ``run_id == expected_run_id``.

    An artifact missing ``run_id`` is treated as NON-coherent (degrade
    honestly) — Phase 1 stamps run_id on critical artifacts, so an unstamped
    input is either legacy or from a different run and must not be silently
    combined with a fresh production run.
    """
    if not artifacts:
        return True
    return all(a.get("run_id") == expected_run_id for a in artifacts)
