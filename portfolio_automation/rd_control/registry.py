"""R&D Control Plane — authoritative SQLite registry (Phase 0A).

SQLite is the single authoritative store for R&D job lifecycle state. JSONL is
never co-authoritative (telemetry only, emitted elsewhere and non-blocking).

All lifecycle mutations go through :func:`transition`, which validates the edge
against :data:`contracts.LEGAL_TRANSITIONS` inside one transaction and writes an
audit row to ``job_events``. Illegal edges fail closed. Timestamps are injected
by callers so the whole module is deterministic under test.
"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from portfolio_automation.rd_control.contracts import (
    SCHEMA_VERSION, JobType, JobStatus, WorkerAuthority, JobRecord,
    JobNotFoundError, assert_legal_transition, compute_input_manifest_hash,
)

DEFAULT_DB_PATH = "data/rd_control.db"
_DB_SCHEMA_VERSION = 1

_JOB_COLUMNS = (
    "job_id", "job_type", "status", "authority", "created_at", "updated_at",
    "stockbot_sha", "input_snapshot_id", "input_snapshot_hash",
    "worker_id", "worker_version", "model_id", "model_provider",
    "network_profile", "timeout_seconds", "max_output_bytes",
    "input_manifest_hash", "result_hash", "error_class", "error_message",
    "schema_version",
)

# Fields a transition may update alongside the status change (never identity).
_UPDATABLE_ON_TRANSITION = frozenset({
    "result_hash", "error_class", "error_message",
    "worker_id", "worker_version", "model_id", "model_provider",
})


@contextmanager
def connect(db_path: str | Path = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    """Open a connection with the standard PRAGMAs and ensure the schema exists.

    foreign_keys=ON (audit rows reference jobs), WAL journal (safe concurrent
    reads during a write), and a busy timeout so a transient lock waits rather
    than erroring. The schema is migrated on open (idempotent)."""
    p = Path(db_path)
    if p.parent and str(p.parent) not in ("", "."):
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        _migrate(conn)
        yield conn
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Create/upgrade the schema. Only v1 exists today, but the mechanism is
    real: ``schema_meta.version`` gates future migrations."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL);"
    )
    row = conn.execute("SELECT version FROM schema_meta LIMIT 1;").fetchone()
    current = row["version"] if row else 0
    if current < 1:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id              TEXT PRIMARY KEY,
                job_type            TEXT NOT NULL,
                status              TEXT NOT NULL,
                authority           TEXT NOT NULL,
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL,
                stockbot_sha        TEXT,
                input_snapshot_id   TEXT,
                input_snapshot_hash TEXT,
                worker_id           TEXT,
                worker_version      TEXT,
                model_id            TEXT,
                model_provider      TEXT,
                network_profile     TEXT,
                timeout_seconds     INTEGER,
                max_output_bytes    INTEGER,
                input_manifest_hash TEXT,
                result_hash         TEXT,
                error_class         TEXT,
                error_message       TEXT,
                schema_version      TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            CREATE TABLE IF NOT EXISTS job_events (
                event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id      TEXT NOT NULL,
                from_status TEXT,
                to_status   TEXT NOT NULL,
                at          TEXT NOT NULL,
                reason      TEXT,
                actor       TEXT NOT NULL DEFAULT 'system',
                FOREIGN KEY (job_id) REFERENCES jobs(job_id)
            );
            CREATE INDEX IF NOT EXISTS idx_events_job ON job_events(job_id);
            """
        )
        conn.execute("DELETE FROM schema_meta;")
        conn.execute("INSERT INTO schema_meta (version) VALUES (?);", (_DB_SCHEMA_VERSION,))
        conn.commit()


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT version FROM schema_meta LIMIT 1;").fetchone()
    return int(row["version"]) if row else 0


# ---------------------------------------------------------------------------
# Job creation
# ---------------------------------------------------------------------------
def new_job_id() -> str:
    """Stable unique id. Random (uuid4) so two same-instant creates never clash."""
    return "job-" + uuid.uuid4().hex[:16]


def create_job(
    conn: sqlite3.Connection,
    *,
    job_type: JobType,
    authority: WorkerAuthority,
    created_at: str,
    job_id: str | None = None,
    stockbot_sha: str | None = None,
    input_snapshot_id: str | None = None,
    input_snapshot_hash: str | None = None,
    worker_id: str | None = None,
    worker_version: str | None = None,
    model_id: str | None = None,
    model_provider: str | None = None,
    network_profile: str | None = None,
    timeout_seconds: int | None = None,
    max_output_bytes: int | None = None,
) -> JobRecord:
    """Insert a new job in status CREATED and stamp its input-manifest hash.

    ``job_id`` may be injected (tests) but must be unique; the PRIMARY KEY
    enforces this and a duplicate raises ``sqlite3.IntegrityError``."""
    rec = JobRecord(
        job_id=job_id or new_job_id(),
        job_type=job_type,
        status=JobStatus.CREATED,
        authority=authority,
        created_at=created_at,
        updated_at=created_at,
        stockbot_sha=stockbot_sha,
        input_snapshot_id=input_snapshot_id,
        input_snapshot_hash=input_snapshot_hash,
        worker_id=worker_id,
        worker_version=worker_version,
        model_id=model_id,
        model_provider=model_provider,
        network_profile=network_profile,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        schema_version=SCHEMA_VERSION,
    )
    rec.input_manifest_hash = compute_input_manifest_hash(rec)
    row = rec.to_row()
    placeholders = ", ".join("?" for _ in _JOB_COLUMNS)
    with conn:  # transaction
        conn.execute(
            f"INSERT INTO jobs ({', '.join(_JOB_COLUMNS)}) VALUES ({placeholders});",
            tuple(row[c] for c in _JOB_COLUMNS),
        )
        conn.execute(
            "INSERT INTO job_events (job_id, from_status, to_status, at, reason, actor) "
            "VALUES (?, ?, ?, ?, ?, ?);",
            (rec.job_id, None, JobStatus.CREATED.value, created_at, "create", "system"),
        )
    return rec


def get_job(conn: sqlite3.Connection, job_id: str) -> JobRecord:
    row = conn.execute("SELECT * FROM jobs WHERE job_id = ?;", (job_id,)).fetchone()
    if row is None:
        raise JobNotFoundError(f"no such job: {job_id}")
    return JobRecord.from_row(dict(row))


def list_jobs(conn: sqlite3.Connection, status: JobStatus | None = None) -> list[JobRecord]:
    if status is None:
        rows = conn.execute("SELECT * FROM jobs ORDER BY created_at, job_id;").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY created_at, job_id;",
            (status.value,),
        ).fetchall()
    return [JobRecord.from_row(dict(r)) for r in rows]


def counts_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status;").fetchall()
    return {r["status"]: int(r["n"]) for r in rows}


def job_events(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM job_events WHERE job_id = ? ORDER BY event_id;", (job_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# The ONE authoritative mutation path
# ---------------------------------------------------------------------------
def transition(
    conn: sqlite3.Connection,
    job_id: str,
    to_status: JobStatus,
    *,
    at: str,
    reason: str | None = None,
    actor: str = "system",
    **updates: Any,
) -> JobRecord:
    """Move a job to ``to_status`` along a legal edge, atomically, with an audit
    row. Illegal edges raise :class:`IllegalTransitionError` (fail closed). Only
    whitelisted non-identity fields may be updated alongside the status.

    A worker/LLM never calls a "set my status" API — it hands back a result and
    the control plane decides the status via this validated path only."""
    bad = set(updates) - _UPDATABLE_ON_TRANSITION
    if bad:
        raise ValueError(f"fields not updatable on transition: {sorted(bad)}")
    current = get_job(conn, job_id)
    assert_legal_transition(current.status, to_status)  # raises if illegal
    set_cols = ["status = ?", "updated_at = ?"]
    params: list[Any] = [to_status.value, at]
    for k, v in updates.items():
        set_cols.append(f"{k} = ?")
        params.append(v)
    params.append(job_id)
    with conn:  # single transaction: update + audit
        conn.execute(f"UPDATE jobs SET {', '.join(set_cols)} WHERE job_id = ?;", tuple(params))
        conn.execute(
            "INSERT INTO job_events (job_id, from_status, to_status, at, reason, actor) "
            "VALUES (?, ?, ?, ?, ?, ?);",
            (job_id, current.status.value, to_status.value, at, reason, actor),
        )
    return get_job(conn, job_id)


# ---------------------------------------------------------------------------
# Restart / recovery (bounded reconciliation)
# ---------------------------------------------------------------------------
def find_stale_running(
    conn: sqlite3.Connection, *, now: str, max_running_seconds: int
) -> list[JobRecord]:
    """RUNNING jobs whose ``updated_at`` is older than ``max_running_seconds``
    relative to injected ``now``. Deterministic (no wall clock)."""
    from datetime import datetime, timezone

    def _parse(ts: str) -> datetime | None:
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None

    now_dt = _parse(now)
    stale: list[JobRecord] = []
    for rec in list_jobs(conn, JobStatus.RUNNING):
        upd = _parse(rec.updated_at)
        if now_dt is None or upd is None:
            continue
        if (now_dt - upd).total_seconds() > max_running_seconds:
            stale.append(rec)
    return stale


def recover_stale_running(
    conn: sqlite3.Connection, *, now: str, max_running_seconds: int, reason: str = "stale_recovery"
) -> list[str]:
    """Mark orphaned/stale RUNNING jobs as INTERRUPTED (a legal RUNNING edge).
    No automatic retries. Returns the list of job_ids reconciled."""
    recovered: list[str] = []
    for rec in find_stale_running(conn, now=now, max_running_seconds=max_running_seconds):
        transition(conn, rec.job_id, JobStatus.INTERRUPTED, at=now, reason=reason, actor="recovery")
        recovered.append(rec.job_id)
    return recovered
