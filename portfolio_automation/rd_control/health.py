"""R&D Control Plane — read-only health assessment (Phase 0A).

Observe-only: reads the authoritative SQLite registry and reports a GREEN/AMBER/
RED rollup. Never claims GREEN if authoritative state cannot be read. Writes an
optional POLICY artifact via the shared data-governance writer. Reading health
never mutates the registry.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.rd_control import registry as reg
from portfolio_automation.rd_control.contracts import JobStatus, TERMINAL_STATUSES

_SCHEMA_VERSION = "1"
_SOURCE = "rd_control_health"
_HEALTH_REL = "rd_control_health.json"
_EXPECTED_DB_SCHEMA = 1
_DEFAULT_STALE_SECONDS = 21600  # 6h: RUNNING older than this is flagged stale

GREEN, AMBER, RED = "GREEN", "AMBER", "RED"

_FAILED_STATUSES = frozenset({
    JobStatus.FAILED_VALIDATION.value, JobStatus.FAILED_WORKER.value,
    JobStatus.FAILED_SANDBOX.value, JobStatus.TIMED_OUT.value,
})


def build_health(
    db_path: str | Path = reg.DEFAULT_DB_PATH,
    *,
    now: str | None = None,
    stale_seconds: int = _DEFAULT_STALE_SECONDS,
) -> dict[str, Any]:
    """Return an observe-only health dict for the R&D registry at *db_path*.

    ``now`` (ISO) is injected for deterministic staleness in tests; when omitted
    the current UTC time is used at this edge only."""
    now = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "source": _SOURCE,
        "observe_only": True,
        "generated_at": now,
        "db_path": str(db_path),
        "db_accessible": False,
        "schema_status": "unknown",
        "counts_by_status": {},
        "open_jobs": None,
        "stale_running": None,
        "failed_jobs": None,
        "latest_activity": None,
        "warnings": [],
        "errors": [],
        "status": RED,
    }
    try:
        with reg.connect(db_path) as conn:
            out["db_accessible"] = True
            ver = reg.schema_version(conn)
            if ver == _EXPECTED_DB_SCHEMA:
                out["schema_status"] = "ok"
            else:
                out["schema_status"] = f"mismatch (found {ver}, expected {_EXPECTED_DB_SCHEMA})"
                out["errors"].append("schema version mismatch")
            counts = reg.counts_by_status(conn)
            out["counts_by_status"] = counts
            terminal = {s.value for s in TERMINAL_STATUSES}
            out["open_jobs"] = sum(n for s, n in counts.items() if s not in terminal)
            out["failed_jobs"] = sum(n for s, n in counts.items() if s in _FAILED_STATUSES)
            stale = reg.find_stale_running(conn, now=now, max_running_seconds=stale_seconds)
            out["stale_running"] = len(stale)
            row = conn.execute("SELECT MAX(updated_at) AS m FROM jobs;").fetchone()
            out["latest_activity"] = row["m"] if row else None
    except Exception as exc:  # unreadable/corrupt DB -> RED, never GREEN
        out["errors"].append(f"registry unreadable: {type(exc).__name__}: {exc}")
        out["status"] = RED
        return out

    # Rollup
    if out["errors"]:
        out["status"] = RED
    elif (out["stale_running"] or 0) > 0 or (out["failed_jobs"] or 0) > 0:
        if out["stale_running"]:
            out["warnings"].append(f"{out['stale_running']} stale RUNNING job(s) need recovery")
        if out["failed_jobs"]:
            out["warnings"].append(f"{out['failed_jobs']} failed job(s) present")
        out["status"] = AMBER
    else:
        out["status"] = GREEN
    return out


def run_health(
    db_path: str | Path = reg.DEFAULT_DB_PATH,
    *,
    output_root: str | Path = ".",
    write_files: bool = True,
    now: str | None = None,
) -> dict[str, Any]:
    """Build health and (optionally) persist it to the POLICY namespace."""
    payload = build_health(db_path, now=now)
    if write_files:
        from portfolio_automation.data_governance import OutputNamespace, safe_write_json
        base = str(Path(output_root) / "outputs")
        safe_write_json(OutputNamespace.POLICY, _HEALTH_REL, payload, base_dir=base)
    return payload
