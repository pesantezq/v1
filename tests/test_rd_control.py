"""Hermetic tests for the R&D Control Plane foundation (Phase 0A).

Every test uses a temporary SQLite DB under tmp_path. Never touches
data/rd_control.db or any live repo state. Timestamps are injected for
determinism.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from portfolio_automation.rd_control import registry as reg
from portfolio_automation.rd_control import health as hlth
from portfolio_automation.rd_control.contracts import (
    JobType, JobStatus, WorkerAuthority, JobRecord,
    IllegalTransitionError, JobNotFoundError, TERMINAL_STATUSES,
    is_legal_transition, compute_input_manifest_hash,
)

T0 = "2026-08-09T12:00:00Z"


@pytest.fixture
def db(tmp_path: Path) -> str:
    return str(tmp_path / "rd.db")


def _create(conn, at=T0, **kw):
    return reg.create_job(
        conn, job_type=kw.pop("job_type", JobType.FINANCE_RESEARCH),
        authority=kw.pop("authority", WorkerAuthority.W0_ANALYZE),
        created_at=at, **kw,
    )


# --- init / migration ------------------------------------------------------
def test_registry_init_and_schema_version(db):
    with reg.connect(db) as conn:
        assert reg.schema_version(conn) == 1
        # tables exist
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table';").fetchall()}
        assert {"jobs", "job_events", "schema_meta"} <= names


def test_migration_idempotent(db):
    with reg.connect(db) as conn:
        assert reg.schema_version(conn) == 1
    with reg.connect(db) as conn:  # reopen -> no duplicate schema_meta rows
        rows = conn.execute("SELECT COUNT(*) AS n FROM schema_meta;").fetchone()
        assert rows["n"] == 1


# --- creation / identity ---------------------------------------------------
def test_unique_job_creation_and_defaults(db):
    with reg.connect(db) as conn:
        rec = _create(conn)
        assert rec.status is JobStatus.CREATED
        assert rec.schema_version == "1"
        assert rec.input_manifest_hash and rec.input_manifest_hash.startswith("sha256:")
        assert reg.get_job(conn, rec.job_id).job_id == rec.job_id


def test_duplicate_job_id_rejected(db):
    with reg.connect(db) as conn:
        _create(conn, job_id="job-fixed")
        with pytest.raises(sqlite3.IntegrityError):
            _create(conn, job_id="job-fixed")


def test_two_creates_get_distinct_ids(db):
    with reg.connect(db) as conn:
        a = _create(conn)
        b = _create(conn)
        assert a.job_id != b.job_id


# --- legal transitions -----------------------------------------------------
def test_full_legal_happy_path(db):
    with reg.connect(db) as conn:
        rec = _create(conn)
        for st in (JobStatus.QUEUED, JobStatus.ADMITTED, JobStatus.RUNNING,
                   JobStatus.RESULT_RECEIVED, JobStatus.VALIDATING, JobStatus.SUCCEEDED):
            rec = reg.transition(conn, rec.job_id, st, at=T0)
        assert rec.status is JobStatus.SUCCEEDED
        # audit trail has create + 6 transitions = 7 events
        assert len(reg.job_events(conn, rec.job_id)) == 7


def test_running_can_fail_and_interrupt_edges():
    assert is_legal_transition(JobStatus.RUNNING, JobStatus.FAILED_WORKER)
    assert is_legal_transition(JobStatus.RUNNING, JobStatus.FAILED_SANDBOX)
    assert is_legal_transition(JobStatus.RUNNING, JobStatus.TIMED_OUT)
    assert is_legal_transition(JobStatus.RUNNING, JobStatus.INTERRUPTED)
    assert is_legal_transition(JobStatus.VALIDATING, JobStatus.FAILED_VALIDATION)


# --- illegal transitions (must fail closed) --------------------------------
@pytest.mark.parametrize("src,dst", [
    (JobStatus.CREATED, JobStatus.SUCCEEDED),
    (JobStatus.RUNNING, JobStatus.CREATED),
    (JobStatus.CREATED, JobStatus.RUNNING),
    (JobStatus.QUEUED, JobStatus.SUCCEEDED),
    (JobStatus.RESULT_RECEIVED, JobStatus.SUCCEEDED),
])
def test_illegal_transitions_refused(db, src, dst):
    assert not is_legal_transition(src, dst)
    with reg.connect(db) as conn:
        rec = _create(conn)
        # drive to src via a legal path where needed
        path = {
            JobStatus.CREATED: [],
            JobStatus.QUEUED: [JobStatus.QUEUED],
            JobStatus.RUNNING: [JobStatus.QUEUED, JobStatus.ADMITTED, JobStatus.RUNNING],
            JobStatus.RESULT_RECEIVED: [JobStatus.QUEUED, JobStatus.ADMITTED,
                                        JobStatus.RUNNING, JobStatus.RESULT_RECEIVED],
        }[src]
        for st in path:
            rec = reg.transition(conn, rec.job_id, st, at=T0)
        with pytest.raises(IllegalTransitionError):
            reg.transition(conn, rec.job_id, dst, at=T0)
        # status unchanged after refusal
        assert reg.get_job(conn, rec.job_id).status is src


def test_terminal_states_have_no_exit(db):
    # FAILED_VALIDATION -> SUCCEEDED must be refused (no resurrection)
    with reg.connect(db) as conn:
        rec = _create(conn)
        for st in (JobStatus.QUEUED, JobStatus.ADMITTED, JobStatus.RUNNING,
                   JobStatus.RESULT_RECEIVED, JobStatus.VALIDATING, JobStatus.FAILED_VALIDATION):
            rec = reg.transition(conn, rec.job_id, st, at=T0)
        with pytest.raises(IllegalTransitionError):
            reg.transition(conn, rec.job_id, JobStatus.SUCCEEDED, at=T0)
    assert JobStatus.FAILED_VALIDATION in TERMINAL_STATUSES


def test_transition_cannot_update_identity_fields(db):
    with reg.connect(db) as conn:
        rec = _create(conn)
        with pytest.raises(ValueError):
            reg.transition(conn, rec.job_id, JobStatus.QUEUED, at=T0, stockbot_sha="evil")


def test_transition_can_record_result_hash(db):
    with reg.connect(db) as conn:
        rec = _create(conn)
        for st in (JobStatus.QUEUED, JobStatus.ADMITTED, JobStatus.RUNNING):
            rec = reg.transition(conn, rec.job_id, st, at=T0)
        rec = reg.transition(conn, rec.job_id, JobStatus.RESULT_RECEIVED, at=T0,
                             result_hash="sha256:abc")
        assert rec.result_hash == "sha256:abc"


def test_transition_missing_job_raises(db):
    with reg.connect(db) as conn:
        with pytest.raises(JobNotFoundError):
            reg.transition(conn, "job-nope", JobStatus.QUEUED, at=T0)


# --- audit -----------------------------------------------------------------
def test_audit_records_from_and_to(db):
    with reg.connect(db) as conn:
        rec = _create(conn)
        reg.transition(conn, rec.job_id, JobStatus.QUEUED, at=T0, reason="r1", actor="tester")
        evts = reg.job_events(conn, rec.job_id)
        assert evts[0]["to_status"] == "CREATED"
        assert evts[1]["from_status"] == "CREATED" and evts[1]["to_status"] == "QUEUED"
        assert evts[1]["reason"] == "r1" and evts[1]["actor"] == "tester"


# --- recovery --------------------------------------------------------------
def test_stale_running_recovered_to_interrupted(db):
    with reg.connect(db) as conn:
        rec = _create(conn, at="2026-08-09T00:00:00Z")
        for st in (JobStatus.QUEUED, JobStatus.ADMITTED, JobStatus.RUNNING):
            rec = reg.transition(conn, rec.job_id, st, at="2026-08-09T00:00:00Z")
        # 2h later, threshold 1h -> stale
        ids = reg.recover_stale_running(conn, now="2026-08-09T02:00:00Z", max_running_seconds=3600)
        assert ids == [rec.job_id]
        assert reg.get_job(conn, rec.job_id).status is JobStatus.INTERRUPTED


def test_non_stale_running_remains_running(db):
    with reg.connect(db) as conn:
        rec = _create(conn, at="2026-08-09T00:00:00Z")
        for st in (JobStatus.QUEUED, JobStatus.ADMITTED, JobStatus.RUNNING):
            rec = reg.transition(conn, rec.job_id, st, at="2026-08-09T00:00:00Z")
        # 10 min later, threshold 1h -> not stale
        ids = reg.recover_stale_running(conn, now="2026-08-09T00:10:00Z", max_running_seconds=3600)
        assert ids == []
        assert reg.get_job(conn, rec.job_id).status is JobStatus.RUNNING


# --- persistence across "restart" -----------------------------------------
def test_persistence_across_reconnect(db):
    with reg.connect(db) as conn:
        rec = _create(conn)
        reg.transition(conn, rec.job_id, JobStatus.QUEUED, at=T0)
    # brand-new connection (simulates process/WSL restart)
    with reg.connect(db) as conn:
        got = reg.get_job(conn, rec.job_id)
        assert got.status is JobStatus.QUEUED
        assert got.input_manifest_hash == rec.input_manifest_hash


# --- provenance ------------------------------------------------------------
def test_provenance_fields_preserved(db):
    with reg.connect(db) as conn:
        rec = _create(conn, stockbot_sha="abc1234", input_snapshot_id="snap-1",
                      input_snapshot_hash="sha256:deadbeef", worker_id="w", worker_version="0.1",
                      model_id="qwen2.5:7b", model_provider="ollama", network_profile="OFFLINE_LOCAL",
                      timeout_seconds=600, max_output_bytes=1048576)
        got = reg.get_job(conn, rec.job_id)
        assert got.stockbot_sha == "abc1234"
        assert got.input_snapshot_id == "snap-1"
        assert got.model_provider == "ollama"
        assert got.network_profile == "OFFLINE_LOCAL"
        # manifest hash is deterministic for identical inputs
        assert compute_input_manifest_hash(got) == got.input_manifest_hash


def test_manifest_hash_changes_with_inputs(db):
    with reg.connect(db) as conn:
        a = _create(conn, job_id="job-a", stockbot_sha="sha-a")
        b = _create(conn, job_id="job-b", stockbot_sha="sha-b")
        assert a.input_manifest_hash != b.input_manifest_hash


# --- health ----------------------------------------------------------------
def test_health_green_empty_registry(db):
    with reg.connect(db):
        pass
    h = hlth.build_health(db, now=T0)
    assert h["db_accessible"] is True
    assert h["schema_status"] == "ok"
    assert h["status"] == "GREEN"


def test_health_amber_with_stale_running(db):
    with reg.connect(db) as conn:
        rec = _create(conn, at="2026-08-09T00:00:00Z")
        for st in (JobStatus.QUEUED, JobStatus.ADMITTED, JobStatus.RUNNING):
            rec = reg.transition(conn, rec.job_id, st, at="2026-08-09T00:00:00Z")
    h = hlth.build_health(db, now="2026-08-09T09:00:00Z")  # >6h -> stale
    assert h["status"] == "AMBER"
    assert h["stale_running"] == 1


def test_health_amber_with_failed_job(db):
    with reg.connect(db) as conn:
        rec = _create(conn)
        for st in (JobStatus.QUEUED, JobStatus.ADMITTED, JobStatus.RUNNING,
                   JobStatus.RESULT_RECEIVED, JobStatus.VALIDATING, JobStatus.FAILED_VALIDATION):
            rec = reg.transition(conn, rec.job_id, st, at=T0)
    h = hlth.build_health(db, now=T0)
    assert h["status"] == "AMBER"
    assert h["failed_jobs"] == 1


def test_health_red_on_unreadable_db(tmp_path):
    bad = tmp_path / "not.db"
    bad.write_bytes(b"this is not a sqlite database at all, it is garbage bytes")
    h = hlth.build_health(str(bad), now=T0)
    assert h["status"] == "RED"
    assert h["errors"]
