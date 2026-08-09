"""Manifest schema, canonical serialisation, and the snapshot fingerprint.

Pure functions only — no I/O, no clock, no git. Every timestamp is injected by
the caller so an identical set of inputs produces an identical manifest, which
is what makes the determinism test meaningful rather than decorative.

Two fingerprints, two jobs
-------------------------
``snapshot_sha256`` covers the ENTIRE manifest except ``created_at`` and the
digest fields themselves. It is the **tamper detector**: altering any recorded
fact — an artifact hash, the run id, the git SHA, the health verdict, an
exclusion reason, the working-tree observation — invalidates the snapshot. A
narrower digest over only file hashes would let a forger rewrite the provenance
while every file still verified.

``content_sha256`` additionally excludes ``export_context``. It is the **content
identity**: a pure function of (which run, which code, which artifacts, what
health). It answers "is this the same export?" without being perturbed by facts
about the machine at export time. The duplicate-snapshot gate compares this one,
so re-exporting an unchanged run is idempotent even though HEAD and the working
tree have moved on — which, on a VPS that doubles as a dev box, they always have.

Using ``snapshot_sha256`` for that gate would fire a false integrity alarm on
every re-export, and a false alarm is how a real one gets ignored.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import ARTIFACT_TYPE, SCHEMA_VERSION

#: Fields omitted from the tamper fingerprint. Keep this set as small as
#: possible — every entry is a field that could otherwise be rewritten undetected.
DIGEST_EXCLUDED_FIELDS: frozenset[str] = frozenset({
    "created_at", "snapshot_sha256", "content_sha256",
})

#: Additionally omitted from the content-identity fingerprint: observations about
#: the exporting machine, which legitimately differ between two exports of the
#: same run. Still covered by ``snapshot_sha256``.
CONTENT_DIGEST_EXCLUDED_FIELDS: frozenset[str] = DIGEST_EXCLUDED_FIELDS | {"export_context"}

GREEN, AMBER, RED = "GREEN", "AMBER", "RED"

_HASH_CHUNK = 1024 * 1024


def canonical_json(payload: Any) -> str:
    """Deterministic JSON: sorted keys, no incidental whitespace, UTF-8 safe.

    Used for hashing only. The manifest written to disk is pretty-printed for
    human review; both forms carry identical data, and the validator rehashes
    from the parsed object, never from the file bytes, so formatting is
    irrelevant to verification.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path | str) -> str:
    """Streaming SHA-256 so a large artifact never has to be held in memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_snapshot_id(run_id: str, production_git_sha: str) -> str:
    """Derive the immutable snapshot id from run identity + code identity.

    ``<run_id>__<git_sha[:12]>``

    The id is a pure function of (which run, which code), so re-exporting the
    same run at the same commit yields the SAME id. That is what turns the
    duplicate-id check into a real integrity gate: a second export landing on an
    existing id MUST be byte-identical, and if it is not, something changed
    underneath a run that is supposed to be finished — which fails closed.
    """
    safe_run = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in str(run_id))
    sha = (production_git_sha or "unknown")[:12]
    safe_sha = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in sha)
    return f"{safe_run}__{safe_sha}"


def derive_run_health(
    *,
    run_status: str | None,
    daily_run_status: dict[str, Any] | None,
    artifact_registry_status: dict[str, Any] | None,
    missing_required: list[str] | None = None,
    missing_optional: list[str] | None = None,
) -> dict[str, Any]:
    """Summarise how healthy the PRODUCTION RUN was, from its own health artifacts.

    This is the run's health as the run reported it — the exporter transcribes
    pipeline health, it does not re-derive or second-guess it.

    Deliberately excludes export-time provenance (HEAD having moved, uncommitted
    edits). Those describe the exporting machine, not the run, and a run that
    finished cleanly at 09:00 does not become less healthy because someone edited
    a file at 17:00. They surface in ``export_context.degradations`` and in the
    export lane's own health artifact instead.
    """
    critical: list[str] = []
    warnings: list[str] = []

    if run_status != "complete":
        critical.append(f"run_not_complete:{run_status or 'unknown'}")

    for name in missing_required or []:
        critical.append(f"required_artifact_missing:{name}")

    drs = (daily_run_status or {}).get("overall_status")
    if isinstance(drs, str):
        norm = drs.strip().lower()
        if norm in {"failed", "fail", "red", "error"}:
            critical.append(f"daily_run_status:{norm}")
        elif norm not in {"ok", "green", "pass", "passed", "complete"}:
            warnings.append(f"daily_run_status:{norm}")

    ars = (artifact_registry_status or {}).get("overall_status")
    if isinstance(ars, str):
        norm = ars.strip().lower()
        if norm in {"red", "critical", "failed"}:
            critical.append(f"artifact_registry:{norm}")
        elif norm not in {"green", "ok", "pass"}:
            warnings.append(f"artifact_registry:{norm}")

    missing_required_count = (artifact_registry_status or {}).get("counts", {}).get(
        "missing_required")
    if isinstance(missing_required_count, int) and missing_required_count > 0:
        critical.append(f"registry_missing_required:{missing_required_count}")

    for name in missing_optional or []:
        warnings.append(f"optional_artifact_missing:{name}")

    status = RED if critical else (AMBER if warnings else GREEN)
    return {
        "status": status,
        "critical_failures": sorted(set(critical)),
        "warnings": sorted(set(warnings)),
    }


def build_manifest(
    *,
    snapshot_id: str,
    created_at: str,
    production: dict[str, Any],
    export_context: dict[str, Any],
    health: dict[str, Any],
    artifacts: list[dict[str, Any]],
    excluded: list[dict[str, Any]],
    missing_optional: list[str],
    missing_required: list[str],
) -> dict[str, Any]:
    """Assemble a complete manifest with deterministic ordering, then fingerprint it.

    ``finalized`` is written ``True`` here because a manifest only ever reaches
    disk inside the atomic finalisation step — an interrupted build never
    produces one. The field exists so a consumer can assert finalisation without
    inferring it from directory naming.
    """
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": ARTIFACT_TYPE,
        "observe_only": True,
        "feeds_decision_engine": False,
        "grants_production_authority": False,
        "finalized": True,
        "snapshot_id": snapshot_id,
        "created_at": created_at,
        "production": production,
        "export_context": export_context,
        "health": health,
        "artifacts": sorted(artifacts, key=lambda a: a["name"]),
        "excluded": sorted(excluded, key=lambda e: (e["reason"], e["name"])),
        "missing_optional": sorted(missing_optional),
        "missing_required": sorted(missing_required),
        "counts": {
            "artifacts": len(artifacts),
            "required_present": sum(1 for a in artifacts if a.get("required")),
            "optional_present": sum(1 for a in artifacts if not a.get("required")),
            "excluded_classes": len(excluded),
            "missing_optional": len(missing_optional),
            "missing_required": len(missing_required),
            "total_bytes": sum(int(a.get("size_bytes") or 0) for a in artifacts),
        },
        "content_sha256": "",
        "snapshot_sha256": "",
    }
    manifest["content_sha256"] = compute_content_sha256(manifest)
    manifest["snapshot_sha256"] = compute_snapshot_sha256(manifest)
    return manifest


def digest_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    """Projection covered by the tamper fingerprint."""
    return {k: v for k, v in manifest.items() if k not in DIGEST_EXCLUDED_FIELDS}


def content_digest_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    """Projection covered by the content-identity fingerprint."""
    return {k: v for k, v in manifest.items() if k not in CONTENT_DIGEST_EXCLUDED_FIELDS}


def compute_snapshot_sha256(manifest: dict[str, Any]) -> str:
    """Tamper fingerprint. Stable across ``created_at`` and JSON formatting only."""
    return sha256_bytes(canonical_json(digest_payload(manifest)).encode("utf-8"))


def compute_content_sha256(manifest: dict[str, Any]) -> str:
    """Content identity. Additionally stable across export-time observations."""
    return sha256_bytes(canonical_json(content_digest_payload(manifest)).encode("utf-8"))


def artifact_record(
    *,
    name: str,
    source_path: str,
    snapshot_path: str,
    sha256: str,
    size_bytes: int,
    required: bool,
    category: str,
    producer: str | None,
    lens: str | None = None,
    role: str | None = None,
    generated_at: str | None = None,
    note: str = "",
) -> dict[str, Any]:
    """One artifact row. Field order is fixed by ``canonical_json``'s key sort."""
    return {
        "name": name,
        "source_path": source_path,
        "snapshot_path": snapshot_path,
        "sha256": sha256,
        "size_bytes": int(size_bytes),
        "required": bool(required),
        "category": category,
        "producer": producer,
        "lens": lens,
        "role": role,
        "generated_at": generated_at,
        "note": note,
    }
