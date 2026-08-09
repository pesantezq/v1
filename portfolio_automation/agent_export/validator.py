"""Snapshot verification. Fail closed: unverifiable means unusable.

:func:`validate_agent_snapshot` is the trust boundary a consumer runs *before*
believing anything in a snapshot. It is strictly read-only — it opens files for
reading and never writes, renames, or deletes, so validating a snapshot can
never be what breaks it.

The checks are ordered cheapest-first, but every one of them is fatal. There is
deliberately no "warn and continue" tier here: a warning tier is how a corrupt
snapshot ends up being analysed anyway. Softness belongs in the *health*
artifact, which grades an export lane; it does not belong in the gate that
decides whether a specific snapshot's bytes can be trusted.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from . import (
    ARTIFACT_TYPE, ARTIFACTS_DIRNAME, BUILD_PREFIX, MANIFEST_FILENAME, SCHEMA_VERSION,
)
from .allowlist import ARTIFACT_ALLOWLIST, AllowlistEntry, forbidden_reason
from .manifest import (
    AMBER, GREEN, RED, compute_content_sha256, compute_snapshot_sha256, sha256_file,
)

#: Schema versions this validator understands. A snapshot written by a newer
#: exporter is refused rather than best-effort parsed — silently misreading a
#: changed schema is worse than refusing to read it.
SUPPORTED_SCHEMA_VERSIONS: frozenset[str] = frozenset({SCHEMA_VERSION})

_REQUIRED_TOP_LEVEL = (
    "schema_version", "artifact_type", "snapshot_id", "created_at", "finalized",
    "production", "export_context", "health", "artifacts", "excluded", "counts",
    "content_sha256", "snapshot_sha256",
)
_VALID_HEALTH = {GREEN, AMBER, RED}


class ValidationError(Exception):
    """A snapshot failed verification and must not be consumed."""


def _fail(message: str) -> None:
    raise ValidationError(message)


def load_manifest(snapshot_dir: Path | str) -> dict[str, Any]:
    """Parse a snapshot's manifest, or raise :exc:`ValidationError`."""
    path = Path(snapshot_dir) / MANIFEST_FILENAME
    if not path.is_file():
        _fail(f"{path} is missing — an unfinalised or partial directory is not a snapshot")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _fail(f"{path} is unreadable or not valid JSON: {exc}")
    if not isinstance(manifest, dict):
        _fail(f"{path} does not contain a JSON object")
    return manifest


def validate_agent_snapshot(
    snapshot_dir: Path | str,
    *,
    expect_snapshot_id: str | None = None,
    required_entries: Iterable[AllowlistEntry] | None = None,
    verify_hashes: bool = True,
) -> dict[str, Any]:
    """Verify a snapshot end to end and return its manifest.

    Args:
        snapshot_dir: the finalised snapshot directory (or a build dir during
            the builder's own pre-promotion self-check).
        expect_snapshot_id: assert the manifest carries this id.
        required_entries: allowlist to enforce required-artifact coverage
            against. Defaults to the module allowlist; injectable for tests.
        verify_hashes: re-hash every artifact. Only disable for a fast structural
            probe where the caller has already verified content another way.

    Raises:
        ValidationError: on any failure. Never returns a partial verdict.
    """
    snapshot_dir = Path(snapshot_dir)
    if not snapshot_dir.is_dir():
        _fail(f"{snapshot_dir} is not a directory")

    manifest = load_manifest(snapshot_dir)

    # ── Schema + finalisation ────────────────────────────────────────────
    for field in _REQUIRED_TOP_LEVEL:
        if field not in manifest:
            _fail(f"manifest missing required field {field!r}")

    if manifest["schema_version"] not in SUPPORTED_SCHEMA_VERSIONS:
        _fail(
            f"unsupported schema_version {manifest['schema_version']!r} "
            f"(this validator understands {sorted(SUPPORTED_SCHEMA_VERSIONS)})"
        )
    if manifest["artifact_type"] != ARTIFACT_TYPE:
        _fail(f"artifact_type {manifest['artifact_type']!r} is not {ARTIFACT_TYPE!r}")
    if manifest.get("finalized") is not True:
        _fail("manifest is not marked finalized — refusing a partial snapshot")

    # Governance invariants must be present and correct in every snapshot. If
    # these ever flip, the export has quietly become something other than an
    # observe-only sink.
    if manifest.get("observe_only") is not True:
        _fail("observe_only is not True — governance invariant broken")
    if manifest.get("feeds_decision_engine") is not False:
        _fail("feeds_decision_engine is not False — authority invariant broken")
    if manifest.get("grants_production_authority") is not False:
        _fail("grants_production_authority is not False — authority invariant broken")

    # ── Identity ─────────────────────────────────────────────────────────
    snapshot_id = manifest["snapshot_id"]
    if not isinstance(snapshot_id, str) or not snapshot_id:
        _fail("snapshot_id is empty")
    if expect_snapshot_id is not None and snapshot_id != expect_snapshot_id:
        _fail(f"snapshot_id {snapshot_id!r} != expected {expect_snapshot_id!r}")
    # A finalised snapshot lives in a directory named for its id. Build dirs are
    # exempt because they are validated before being renamed into place.
    if not snapshot_dir.name.startswith(BUILD_PREFIX) and snapshot_dir.name != snapshot_id:
        _fail(
            f"directory name {snapshot_dir.name!r} does not match snapshot_id "
            f"{snapshot_id!r} — the snapshot has been moved or renamed"
        )

    production = manifest.get("production") or {}
    if not production.get("run_id"):
        _fail("production.run_id is missing — snapshot cannot be attributed to a run")
    sha = production.get("production_git_sha")
    if not sha or sha == "unknown":
        _fail("production.production_git_sha is missing or unknown")

    health = manifest.get("health") or {}
    if health.get("status") not in _VALID_HEALTH:
        _fail(f"health.status {health.get('status')!r} is not one of {sorted(_VALID_HEALTH)}")

    if manifest.get("missing_required"):
        _fail(
            "manifest records missing required artifacts: "
            + ", ".join(manifest["missing_required"])
        )

    # ── Fingerprints ─────────────────────────────────────────────────────
    # snapshot_sha256 catches tampering with ANY recorded fact; content_sha256
    # additionally pins the run-attributable identity. Both must hold: a forger
    # who refreshes one still has to match the other, and neither can be
    # recomputed without also passing the per-artifact hash checks below.
    recomputed = compute_snapshot_sha256(manifest)
    if recomputed != manifest["snapshot_sha256"]:
        _fail(
            f"snapshot_sha256 mismatch: manifest claims "
            f"{str(manifest['snapshot_sha256'])[:12]} but its content hashes to "
            f"{recomputed[:12]} — the manifest has been modified"
        )
    recomputed_content = compute_content_sha256(manifest)
    if recomputed_content != manifest["content_sha256"]:
        _fail(
            f"content_sha256 mismatch: manifest claims "
            f"{str(manifest['content_sha256'])[:12]} but its content hashes to "
            f"{recomputed_content[:12]} — the manifest has been modified"
        )

    # ── Artifacts: existence, hash, size, and name safety ────────────────
    artifacts = manifest["artifacts"]
    if not isinstance(artifacts, list):
        _fail("manifest.artifacts is not a list")

    expected_files: set[Path] = {snapshot_dir / MANIFEST_FILENAME}
    seen_names: set[str] = set()

    for record in artifacts:
        name = record.get("name")
        if not name:
            _fail("an artifact record has no name")
        if name in seen_names:
            _fail(f"duplicate artifact name {name!r} in manifest")
        seen_names.add(name)

        rel = str(record.get("snapshot_path") or "")
        if not rel.startswith(ARTIFACTS_DIRNAME + "/"):
            _fail(f"artifact {name!r} snapshot_path {rel!r} is outside {ARTIFACTS_DIRNAME}/")
        for component in Path(rel).parts:
            reason = forbidden_reason(component)
            if reason is not None:
                _fail(f"artifact {name!r} path component {component!r} violates {reason}")

        target = snapshot_dir / rel
        if not target.resolve().is_relative_to(snapshot_dir.resolve()):
            _fail(f"artifact {name!r} resolves outside the snapshot directory")
        if not target.is_file():
            _fail(f"artifact {name!r} declared at {rel} is missing from the snapshot")
        expected_files.add(target)

        actual_size = target.stat().st_size
        if int(record.get("size_bytes", -1)) != actual_size:
            _fail(
                f"artifact {name!r} size mismatch: manifest says "
                f"{record.get('size_bytes')} bytes, file is {actual_size}"
            )
        if verify_hashes:
            actual = sha256_file(target)
            if actual != record.get("sha256"):
                _fail(
                    f"artifact {name!r} SHA-256 mismatch: manifest says "
                    f"{str(record.get('sha256'))[:12]}, file hashes to {actual[:12]} "
                    "— content has been altered"
                )

    # ── Required coverage ────────────────────────────────────────────────
    entries = list(required_entries) if required_entries is not None else list(ARTIFACT_ALLOWLIST)
    for entry in entries:
        if entry.required and entry.logical_name not in seen_names:
            _fail(f"required artifact {entry.logical_name!r} is absent from the manifest")

    # ── No unexpected files ──────────────────────────────────────────────
    # An extra file nobody declared is exactly how something unreviewed rides
    # across the boundary, so its presence invalidates the snapshot outright.
    for path in snapshot_dir.rglob("*"):
        if path.is_dir():
            continue
        if path not in expected_files:
            _fail(
                f"unexpected file in snapshot: {path.relative_to(snapshot_dir)} "
                "— not declared in the manifest"
            )
        reason = forbidden_reason(path.name)
        if reason is not None:
            _fail(f"forbidden filename in snapshot: {path.name} ({reason})")

    return manifest


def is_valid_snapshot(snapshot_dir: Path | str, **kwargs: Any) -> tuple[bool, str | None]:
    """Boolean convenience wrapper. Returns ``(ok, error_message)``."""
    try:
        validate_agent_snapshot(snapshot_dir, **kwargs)
    except ValidationError as exc:
        return False, str(exc)
    return True, None
