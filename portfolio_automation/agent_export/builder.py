"""Atomic snapshot construction. Either a complete, verified snapshot appears, or nothing does.

Build order (each step can only fail *forward* into "no snapshot"):

    temp build dir  →  copy + hash allowlisted artifacts  →  verify each copy
    →  gate on required artifacts  →  write manifest  →  re-read + re-verify
    →  full validator pass on the temp dir  →  atomic rename into place

The validator runs against the TEMPORARY directory, before promotion. A snapshot
therefore cannot reach its final path unless it would already pass the same
verification a downstream consumer will run — the build and the trust check are
the same check, so they cannot drift apart.

Refusal (``SnapshotBuildError``, no snapshot written) rather than a degraded
snapshot is the response to:

* the run is not ``status="complete"`` — its artifacts are not a finished run;
* ``production_git_sha`` cannot be determined — the snapshot could not answer
  "what code produced this?", which is its whole reason to exist;
* a required artifact is missing, unreadable, or fails its copy check;
* any secret-boundary violation;
* an existing snapshot with the same id holds different content.

Everything softer (an absent optional artifact, HEAD having moved on since the
run, uncommitted source edits) is recorded as an AMBER warning inside a snapshot
that still gets built. The rule: refuse when the snapshot would be *misleading*,
degrade when it would merely be *incomplete*.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import (
    OutputNamespace, safe_write_json, validate_output_path,
)

from . import (
    ARTIFACTS_DIRNAME, BUILD_PREFIX, LATEST_POINTER_FILENAME, MANIFEST_FILENAME,
    SCHEMA_VERSION, SNAPSHOTS_DIRNAME,
)
from .allowlist import (
    ARTIFACT_ALLOWLIST, DECLARED_EXCLUSIONS, AllowlistEntry, SecretBoundaryViolation,
    resolve_source_path,
)
from .manifest import (
    RED, artifact_record, build_manifest, derive_run_health, make_snapshot_id,
    sha256_file,
)
from .provenance import ProductionIdentity, collect_production_identity


class SnapshotBuildError(Exception):
    """The snapshot could not be built to contract. No snapshot was written."""


@dataclass
class BuildResult:
    snapshot_id: str
    snapshot_dir: Path
    manifest: dict[str, Any]
    created: bool           # False when an identical snapshot already existed
    dry_run: bool = False

    @property
    def health_status(self) -> str:
        return (self.manifest.get("health") or {}).get("status", "UNKNOWN")


# ---------------------------------------------------------------------------
# Registry join (provenance, not duplication)
# ---------------------------------------------------------------------------


def _load_registry_rows(registry: dict | None) -> dict[str, dict]:
    """Map ``source_path`` → registry row, so exported artifacts keep their declared producer.

    Phase 13 of the contract: a copied artifact is NOT a new producer. Provenance
    is joined from ``artifact_registry.yaml`` rather than restated here, so there
    stays exactly one source of truth for who produces what.
    """
    if registry is None:
        try:
            from portfolio_automation.artifact_registry import load_registry
            registry = load_registry()
        except Exception:
            return {}
    rows = (registry or {}).get("artifacts") or {}
    out: dict[str, dict] = {}
    for row in rows.values():
        path = (row or {}).get("path")
        if isinstance(path, str):
            out[path.replace("\\", "/")] = row
    return out


def _generated_at_of(payload_path: Path) -> str | None:
    """Best-effort ``generated_at`` from a JSON artifact; ``None`` for anything else.

    Read from the artifact's own declared field rather than filesystem mtime —
    mtime records when the file was copied or touched, not when the content was
    produced, and the Agent Lab needs the latter.
    """
    if payload_path.suffix.lower() != ".json":
        return None
    try:
        with open(payload_path, "rb") as handle:
            head = handle.read(4096)
        if not head.lstrip().startswith(b"{"):
            return None
        data = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("generated_at", "created_at", "run_timestamp", "timestamp", "as_of"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def outputs_base(root: Path | str, base_dir: Path | str | None = None) -> Path:
    """The ``outputs/`` base directory the namespace layer resolves against."""
    return Path(base_dir) if base_dir is not None else Path(root) / "outputs"


def export_root(root: Path | str, base_dir: Path | str | None = None) -> Path:
    """The AGENT_EXPORT namespace root, named by the repo's namespace governance."""
    return outputs_base(root, base_dir) / "agent_export"


def snapshots_root(root: Path | str, base_dir: Path | str | None = None) -> Path:
    return export_root(root, base_dir) / SNAPSHOTS_DIRNAME


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def _collect_artifacts(
    root: Path,
    entries: tuple[AllowlistEntry, ...],
) -> tuple[list[tuple[AllowlistEntry, Path]], list[str], list[str]]:
    """Resolve every allowlist entry. Returns (present, missing_required, missing_optional).

    A :exc:`SecretBoundaryViolation` propagates — it is never downgraded into a
    "missing" entry, because a boundary breach means the allowlist itself is
    wrong and the operator must see that, not a quiet omission.
    """
    present: list[tuple[AllowlistEntry, Path]] = []
    missing_required: list[str] = []
    missing_optional: list[str] = []

    for entry in sorted(entries, key=lambda e: e.logical_name):
        try:
            source = resolve_source_path(root, entry.source_path)
        except FileNotFoundError:
            (missing_required if entry.required else missing_optional).append(entry.logical_name)
            continue
        present.append((entry, source))
    return present, missing_required, missing_optional


def _copy_and_hash(
    source: Path,
    destination: Path,
) -> tuple[str, int]:
    """Copy a source artifact into the build dir and prove the copy is faithful.

    The source is hashed, copied, then the *copy* is re-hashed and compared. The
    manifest records the hash of what is actually inside the snapshot, so a
    truncated or corrupted copy fails here instead of shipping a manifest that
    describes a file the snapshot does not contain.
    """
    source_digest = sha256_file(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    copy_digest = sha256_file(destination)
    if copy_digest != source_digest:
        raise SnapshotBuildError(
            f"copy verification failed for {source}: source {source_digest[:12]} "
            f"!= copy {copy_digest[:12]} (artifact changed mid-copy or I/O error)"
        )
    return copy_digest, destination.stat().st_size


def build_agent_snapshot(
    root: Path | str = ".",
    *,
    created_at: str,
    base_dir: Path | str | None = None,
    allowlist: tuple[AllowlistEntry, ...] | None = None,
    registry: dict | None = None,
    run_id_override: str | None = None,
    dry_run: bool = False,
) -> BuildResult:
    """Freeze a completed production run into an immutable, verified snapshot.

    Args:
        root: repo root holding ``outputs/``.
        created_at: caller-supplied UTC ISO timestamp. Injected (never
            ``datetime.now()`` internally) so builds are reproducible in tests.
        base_dir: outputs base directory; defaults to ``<root>/outputs``.
        allowlist: override for tests. Production always uses the module allowlist.
        registry: pre-loaded artifact registry, for hermetic tests.
        run_id_override: label the snapshot with an explicit run id instead of the
            one in ``run_manifest``. Does NOT relax the completeness gate.
        dry_run: resolve, hash, and assemble the manifest in a temp dir, then
            discard it. Nothing is promoted and no pointer is updated.

    Raises:
        SnapshotBuildError: on any refusal condition. No snapshot is left behind.
    """
    root = Path(root).resolve()
    entries = allowlist if allowlist is not None else ARTIFACT_ALLOWLIST
    registry_rows = _load_registry_rows(registry)

    identity: ProductionIdentity = collect_production_identity(root)
    run_id = run_id_override or identity.run_id
    if not run_id:
        raise SnapshotBuildError(
            "cannot determine production run_id (outputs/policy/run_manifest.json "
            "absent, unreadable, or missing run_id) — a snapshot with no run "
            "identity cannot be attributed to a run"
        )
    if identity.run_status != "complete":
        raise SnapshotBuildError(
            f"run {run_id!r} has status {identity.run_status!r}, not 'complete' — "
            "refusing to freeze an unfinished run's artifacts as a completed snapshot"
        )
    production_sha = identity.production_git_sha
    if not production_sha or production_sha == "unknown":
        raise SnapshotBuildError(
            "production_git_sha is undeterminable "
            f"({', '.join(identity.degradations) or 'no git metadata'}) — the "
            "snapshot could not state which code produced it"
        )

    snapshot_id = make_snapshot_id(run_id, production_sha)
    snaps_root = snapshots_root(root, base_dir)
    snaps_root.mkdir(parents=True, exist_ok=True)
    final_dir = snaps_root / snapshot_id

    present, missing_required, missing_optional = _collect_artifacts(root, entries)
    if missing_required:
        raise SnapshotBuildError(
            "required artifact(s) missing: " + ", ".join(sorted(missing_required))
        )

    build_dir = Path(tempfile.mkdtemp(dir=str(snaps_root), prefix=BUILD_PREFIX))
    try:
        artifacts_dir = build_dir / ARTIFACTS_DIRNAME
        records: list[dict[str, Any]] = []
        claimed_paths: set[str] = set()

        for entry, source in present:
            rel = f"{entry.category}/{Path(entry.source_path).name}"
            if rel in claimed_paths:
                raise SnapshotBuildError(
                    f"snapshot path collision on {rel!r} — two allowlist entries map "
                    "to the same file inside the snapshot"
                )
            claimed_paths.add(rel)

            destination = artifacts_dir / rel
            # Belt-and-braces: the destination must stay inside the build dir even
            # if a category or filename were ever crafted to escape it.
            if not destination.resolve().is_relative_to(artifacts_dir.resolve()):
                raise SecretBoundaryViolation(
                    f"snapshot path {rel!r} escapes the artifacts directory")

            digest, size = _copy_and_hash(source, destination)
            registry_row = registry_rows.get(entry.source_path, {})
            records.append(artifact_record(
                name=entry.logical_name,
                source_path=entry.source_path,
                snapshot_path=f"{ARTIFACTS_DIRNAME}/{rel}",
                sha256=digest,
                size_bytes=size,
                required=entry.required,
                category=entry.category,
                producer=entry.producer or registry_row.get("producer"),
                lens=registry_row.get("lens"),
                role=registry_row.get("role"),
                generated_at=_generated_at_of(source),
                note=entry.note,
            ))

        by_name = {r["name"]: r for r in records}
        health = derive_run_health(
            run_status=identity.run_status,
            daily_run_status=_read_json_record(build_dir, by_name.get("daily_run_status")),
            artifact_registry_status=_read_json_record(
                build_dir, by_name.get("artifact_registry_status")),
            missing_required=missing_required,
            missing_optional=missing_optional,
        )
        if health["status"] == RED:
            raise SnapshotBuildError(
                "run health is RED (" + "; ".join(health["critical_failures"]) +
                ") — refusing to publish a snapshot that would present a failed "
                "run as consumable"
            )

        manifest = build_manifest(
            snapshot_id=snapshot_id,
            created_at=created_at,
            production=identity.run_identity(),
            export_context=identity.export_context(),
            health=health,
            artifacts=records,
            excluded=[{
                "name": x.logical_name,
                "source_pattern": x.source_pattern,
                "reason": x.reason.value,
                "detail": x.detail,
            } for x in DECLARED_EXCLUSIONS],
            missing_optional=missing_optional,
            missing_required=missing_required,
        )

        manifest_path = build_dir / MANIFEST_FILENAME
        # Pretty-printed for human review. The fingerprint is computed over the
        # parsed object via canonical_json, so on-disk formatting never affects
        # verification.
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )

        # Re-read from disk and validate the temp build exactly as a consumer
        # would. Promotion happens only if this passes.
        from .validator import ValidationError, validate_agent_snapshot
        try:
            # Enforce coverage against the SAME allowlist this build used, so an
            # injected (test) allowlist is checked against itself rather than
            # against the production one.
            validate_agent_snapshot(build_dir, required_entries=entries)
        except ValidationError as exc:
            raise SnapshotBuildError(
                f"self-validation of the temp build failed: {exc}") from exc

        if dry_run:
            return BuildResult(snapshot_id, final_dir, manifest, created=False, dry_run=True)

        if final_dir.exists():
            existing = _verify_identical_or_fail(final_dir, manifest, snapshot_id)
            return BuildResult(snapshot_id, final_dir, existing, created=False)

        validate_output_path(
            OutputNamespace.AGENT_EXPORT, final_dir,
            base_dir=Path(base_dir) if base_dir is not None else root / "outputs",
        )
        # Atomic promotion: os.rename on the same filesystem is indivisible, so
        # the directory is never observable half-populated under its final name.
        os.rename(build_dir, final_dir)
        build_dir = None  # ownership transferred; skip cleanup
    finally:
        if build_dir is not None and Path(build_dir).exists():
            shutil.rmtree(build_dir, ignore_errors=True)

    _write_latest_pointer(root, base_dir, manifest)
    return BuildResult(snapshot_id, final_dir, manifest, created=True)


def _read_json_record(build_dir: Path, record: dict[str, Any] | None) -> dict[str, Any] | None:
    """Load an already-copied artifact from the build dir (never from production again).

    Reading the copy rather than re-reading the source keeps the health verdict
    consistent with the bytes the snapshot actually contains, even if production
    rewrote the file between the copy and now.
    """
    if not record:
        return None
    path = build_dir / record["snapshot_path"]
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _verify_identical_or_fail(
    final_dir: Path, new_manifest: dict[str, Any], snapshot_id: str,
) -> dict[str, Any]:
    """Idempotent re-export, or a hard stop. Never an overwrite.

    An existing snapshot is immutable by contract. If a rebuild of the same
    (run, commit) produces the same fingerprint the export is simply idempotent.
    A different fingerprint means the artifacts of a *finished* run changed
    underneath us — an integrity anomaly the operator must see, so it fails
    closed with both digests rather than silently replacing history.
    """
    existing_path = final_dir / MANIFEST_FILENAME
    try:
        existing = json.loads(existing_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SnapshotBuildError(
            f"snapshot {snapshot_id!r} already exists but its manifest is unreadable "
            f"({exc}); refusing to overwrite — move it aside manually after review"
        ) from exc

    # Compared on CONTENT identity, not the tamper digest: HEAD and the working
    # tree legitimately move between two exports of the same run, and flagging
    # that as an integrity anomaly would cry wolf on every re-export.
    if existing.get("content_sha256") != new_manifest.get("content_sha256"):
        raise SnapshotBuildError(
            f"snapshot {snapshot_id!r} already exists with DIFFERENT content "
            f"(existing {str(existing.get('content_sha256'))[:12]} != rebuilt "
            f"{str(new_manifest.get('content_sha256'))[:12]}). A finalized snapshot "
            "is immutable; refusing to overwrite. Investigate why a completed run's "
            "artifacts changed."
        )
    return existing


def _write_latest_pointer(
    root: Path, base_dir: Path | str | None, manifest: dict[str, Any],
) -> Path:
    """Write ``latest.json`` as a POINTER record, never a second copy of the data.

    A mutable ``latest/`` directory holding duplicated artifacts would be a
    second, silently-drifting source of truth and would break the "immutable
    after finalisation" guarantee the moment it was refreshed. A pointer keeps
    exactly one copy of every artifact and makes the indirection explicit.
    """
    base = Path(base_dir) if base_dir is not None else root / "outputs"
    pointer = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "agent_export_latest_pointer",
        "observe_only": True,
        "snapshot_id": manifest["snapshot_id"],
        "snapshot_relpath": f"{SNAPSHOTS_DIRNAME}/{manifest['snapshot_id']}",
        "snapshot_sha256": manifest["snapshot_sha256"],
        "content_sha256": manifest["content_sha256"],
        "created_at": manifest["created_at"],
        "production_run_id": (manifest.get("production") or {}).get("run_id"),
        "production_git_sha": (manifest.get("production") or {}).get("production_git_sha"),
        "health_status": (manifest.get("health") or {}).get("status"),
        "artifact_count": (manifest.get("counts") or {}).get("artifacts"),
        "note": "Pointer only. The snapshot directory it names is immutable; "
                "this file is the single mutable element of the export lane.",
    }
    return safe_write_json(
        OutputNamespace.AGENT_EXPORT, LATEST_POINTER_FILENAME, pointer, base_dir=base,
    )


def read_latest_pointer(root: Path | str, base_dir: Path | str | None = None) -> dict | None:
    path = export_root(root, base_dir) / LATEST_POINTER_FILENAME
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_snapshots(root: Path | str, base_dir: Path | str | None = None) -> list[str]:
    """Finalised snapshot ids, sorted. In-progress ``.build-*`` dirs are excluded."""
    snaps = snapshots_root(root, base_dir)
    if not snaps.is_dir():
        return []
    return sorted(
        p.name for p in snaps.iterdir()
        if p.is_dir() and not p.name.startswith(BUILD_PREFIX)
        and (p / MANIFEST_FILENAME).is_file()
    )
