"""Health coverage for the export lane — the mandatory consumer for a new producer.

CLAUDE.md's Analysis+Health requirement: every producer ships with a matching
check, and every artifact has at least one consumer. This module is the export
lane's check. It grades the lane (is there a current, verifiable snapshot?),
which is a different question from :mod:`.validator`, which grades one snapshot's
bytes.

Status grammar follows the repo's existing GREEN/AMBER/RED convention
(``weekly_etf_bundles/health.py``, ``strategy_lab_health.py``):

    GREEN  a snapshot exists, validates, and is current
    AMBER  no snapshot yet, snapshot is stale, or optional artifacts were absent
    RED    a snapshot exists but fails validation — hash mismatch, invalid
           manifest, missing required artifact, or a secret-boundary breach

RED is reserved for *verifiable corruption*. "Nothing exported yet" is AMBER:
absence is a degraded lane, not a broken one, and grading a fresh install RED
would train the operator to ignore the signal.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import OutputNamespace, safe_write_json

from . import SCHEMA_VERSION
from .allowlist import ARTIFACT_ALLOWLIST, SecretBoundaryViolation
from .builder import export_root, list_snapshots, outputs_base, read_latest_pointer
from .manifest import AMBER, GREEN, RED
from .validator import ValidationError, validate_agent_snapshot

HEALTH_FILENAME = "agent_export_health.json"

#: A daily-cadence export older than this is stale. 36h spans a weekend-adjacent
#: skipped run without alarming, while still catching a lane that has stopped.
STALE_AFTER_HOURS = 36


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def build_export_health(
    root: Path | str = ".",
    *,
    now: datetime | None = None,
    base_dir: Path | str | None = None,
    allowlist: tuple[Any, ...] | None = None,
) -> dict[str, Any]:
    """Grade the export lane. Pure read — never builds or repairs a snapshot."""
    root = Path(root)
    now = now or datetime.now(timezone.utc)
    entries = allowlist if allowlist is not None else ARTIFACT_ALLOWLIST
    warnings: list[str] = []
    errors: list[str] = []

    snapshot_ids = list_snapshots(root, base_dir)
    pointer = read_latest_pointer(root, base_dir) or {}
    pointed_id = pointer.get("snapshot_id")

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "agent_export_health",
        "observe_only": True,
        "feeds_decision_engine": False,
        "generated_at": now.isoformat(),
        "export_root": str(export_root(root, base_dir)),
        "snapshot_count": len(snapshot_ids),
        "latest_snapshot_id": None,
        "latest_snapshot_created_at": None,
        "latest_snapshot_age_hours": None,
        "production_git_sha": None,
        "production_run_id": None,
        "required_artifacts_present": None,
        "required_artifacts_expected": sum(1 for e in entries if e.required),
        "artifact_count": None,
        "hash_validation": "not_run",
        "schema_validation": "not_run",
        "snapshot_health_status": None,
        "status": AMBER,
        "warnings": warnings,
        "errors": errors,
        "disclaimer": (
            "Observe-only health probe for the Agent Lab export lane. Reads and "
            "verifies frozen snapshots; never mutates a snapshot, never touches "
            "any decision, allocation, score, or portfolio state, and performs "
            "no network I/O."
        ),
    }

    if not snapshot_ids:
        warnings.append("AMBER:no_snapshot_yet")
        payload["status"] = AMBER
        return payload

    # Prefer the pointer's target; fall back to the newest id if the pointer is
    # stale or absent, so a lost pointer degrades rather than blinding the check.
    latest_id = pointed_id if pointed_id in snapshot_ids else snapshot_ids[-1]
    if pointed_id and pointed_id not in snapshot_ids:
        warnings.append(f"AMBER:pointer_target_missing:{pointed_id}")

    payload["latest_snapshot_id"] = latest_id
    snapshot_dir = Path(export_root(root, base_dir)) / "snapshots" / latest_id

    try:
        manifest = validate_agent_snapshot(snapshot_dir, required_entries=entries)
        payload["schema_validation"] = "pass"
        payload["hash_validation"] = "pass"
    except SecretBoundaryViolation as exc:
        errors.append(f"RED:secret_boundary_violation:{exc}")
        payload["schema_validation"] = "fail"
        payload["hash_validation"] = "fail"
        payload["status"] = RED
        return payload
    except ValidationError as exc:
        message = str(exc)
        errors.append(f"RED:validation_failed:{message}")
        payload["schema_validation"] = "fail"
        # Distinguish a content-integrity failure from a structural one so the
        # operator knows whether to suspect tampering or a builder bug.
        payload["hash_validation"] = "fail" if "SHA-256" in message or "size" in message \
            else "not_reached"
        payload["status"] = RED
        return payload

    production = manifest.get("production") or {}
    export_context = manifest.get("export_context") or {}
    health = manifest.get("health") or {}
    counts = manifest.get("counts") or {}

    payload["latest_snapshot_created_at"] = manifest.get("created_at")
    payload["production_git_sha"] = production.get("production_git_sha")
    payload["production_run_id"] = production.get("run_id")
    payload["artifact_count"] = counts.get("artifacts")
    payload["required_artifacts_present"] = counts.get("required_present")
    payload["snapshot_health_status"] = health.get("status")

    created = _parse_iso(manifest.get("created_at"))
    if created is not None:
        age_hours = (now - created).total_seconds() / 3600.0
        payload["latest_snapshot_age_hours"] = round(age_hours, 2)
        if age_hours > STALE_AFTER_HOURS:
            warnings.append(f"AMBER:snapshot_stale:{round(age_hours, 1)}h")
        elif age_hours < -1:
            warnings.append("AMBER:snapshot_created_in_future")
    else:
        warnings.append("AMBER:snapshot_created_at_unparseable")

    expected_required = payload["required_artifacts_expected"]
    present_required = counts.get("required_present")
    if isinstance(present_required, int) and present_required < expected_required:
        # Validation already enforces required coverage, so reaching here means
        # the allowlist gained a required entry after this snapshot was frozen.
        warnings.append(
            f"AMBER:required_coverage_grew:{present_required}/{expected_required}")

    for name in manifest.get("missing_optional") or []:
        warnings.append(f"AMBER:optional_artifact_missing:{name}")
    for warning in health.get("warnings") or []:
        warnings.append(f"AMBER:snapshot:{warning}")
    # Export-time provenance degradations (HEAD moved on, uncommitted source
    # edits). These grade the EXPORT, not the run, which is why they live here
    # and not in the snapshot's own health verdict.
    for degradation in export_context.get("degradations") or []:
        warnings.append(f"AMBER:provenance:{degradation}")
    for failure in health.get("critical_failures") or []:
        errors.append(f"RED:snapshot:{failure}")

    payload["status"] = RED if errors else (AMBER if warnings else GREEN)
    return payload


def run_agent_export_health(
    root: Path | str = ".",
    *,
    now: datetime | None = None,
    base_dir: Path | str | None = None,
    allowlist: tuple[Any, ...] | None = None,
    write: bool = True,
) -> dict[str, Any]:
    """Build the health payload and persist it to the POLICY namespace."""
    payload = build_export_health(root, now=now, base_dir=base_dir, allowlist=allowlist)
    if write:
        safe_write_json(
            OutputNamespace.POLICY, HEALTH_FILENAME, payload,
            base_dir=outputs_base(root, base_dir),
        )
    return payload


def load_export_health(
    root: Path | str = ".", base_dir: Path | str | None = None,
) -> dict[str, Any] | None:
    import json
    path = outputs_base(root, base_dir) / "policy" / HEALTH_FILENAME
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


__all__ = [
    "HEALTH_FILENAME", "STALE_AFTER_HOURS", "build_export_health",
    "run_agent_export_health", "load_export_health",
]
