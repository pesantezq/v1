"""Hermetic tests for the Phase 0B sandbox runner (trusted execution contract).

No network namespace / jail is applied here (jail_wrapper=()); these tests prove
the TRUSTED runner logic: materialization, read-only input, job-scoped write
scope, immutable manifest, result validation, deterministic lifecycle via the
Phase 0A CAS transitions, timeout/cancel process-tree termination, output
bounds, and restart reconciliation. OS-level isolation (netns/DNS/privilege) is
certified separately in the Agent-Lab environment (see docs/RD_SANDBOX.md).

Every test uses a temporary DB + temporary jobs_root. Fake workers are tiny
python -c programs, so the suite is fully hermetic and deterministic.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from portfolio_automation.rd_control import registry as reg
from portfolio_automation.rd_control import sandbox as sbx
from portfolio_automation.rd_control.contracts import (
    JobType, JobStatus, WorkerAuthority,
)

T0 = "2026-08-09T13:00:00Z"


@pytest.fixture
def env(tmp_path):
    db = str(tmp_path / "rd.db")
    jobs_root = tmp_path / "jobs"
    jobs_root.mkdir()
    return db, str(jobs_root)


def _make_queued_job(conn, **kw):
    rec = reg.create_job(conn, job_type=JobType.FINANCE_RESEARCH,
                         authority=WorkerAuthority.W0_ANALYZE, created_at=T0,
                         worker_id="test-worker", worker_version="0.0.1", **kw)
    return reg.transition(conn, rec.job_id, JobStatus.QUEUED, at=T0)


def _clock():
    """Monotonic ISO clock for deterministic-but-ordered timestamps."""
    n = {"i": 0}
    def now():
        n["i"] += 1
        return f"2026-08-09T13:00:{n['i']:02d}Z"
    return now


# A fake worker that emits a VALID envelope. It reads the manifest hash from the
# input dir and echoes it, computes the payload hash the way the validator does.
_GOOD_WORKER = textwrap.dedent("""
    import os, json, hashlib
    inp = os.environ["RD_INPUT_DIR"]; out = os.environ["RD_OUTPUT_DIR"]
    man = json.load(open(os.path.join(inp, "manifest.json")))
    payload = {"answer": "ok", "n": 42}
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    ph = "sha256:" + hashlib.sha256(canon.encode()).hexdigest()
    env = {
        "schema_version": "1", "job_id": os.environ["RD_JOB_ID"],
        "worker_id": "test-worker", "worker_version": "0.0.1",
        "started_at": "t0", "completed_at": "t1", "exit_code": 0,
        "input_manifest_hash": man["manifest_hash"],
        "result_payload_hash": ph, "payload": payload,
    }
    open(os.path.join(out, "result.json"), "w").write(json.dumps(env))
""")


def _worker_argv(src: str):
    return [sys.executable, "-c", src]


# --- 1. materialization + 2. read-only input + 3. job-scoped write ----------
def test_happy_path_succeeds_and_scopes(env):
    db, jobs_root = env
    with reg.connect(db) as conn:
        job = _make_queued_job(conn)
        final = sbx.run_job(conn, job, worker_argv=_worker_argv(_GOOD_WORKER),
                            jobs_root=jobs_root, now_fn=_clock())
        assert final.status is JobStatus.SUCCEEDED, final.error_message
        d = Path(jobs_root) / job.job_id
        assert (d / "input" / "manifest.json").exists()
        assert (d / "output" / "result.json").exists()
        # input dir is read-only
        mode = stat.S_IMODE((d / "input" / "manifest.json").stat().st_mode)
        assert not (mode & stat.S_IWUSR)


def test_input_is_readonly_to_worker(env):
    db, jobs_root = env
    # worker tries to write into input/ -> should fail, but worker still exits 0
    worker = textwrap.dedent("""
        import os, json, hashlib
        inp=os.environ['RD_INPUT_DIR']; out=os.environ['RD_OUTPUT_DIR']
        try:
            open(os.path.join(inp,'evil.txt'),'w').write('x'); wrote=True
        except Exception: wrote=False
        man=json.load(open(os.path.join(inp,'manifest.json')))
        payload={'wrote_input': wrote}
        canon=json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=True)
        env={'schema_version':'1','job_id':os.environ['RD_JOB_ID'],'worker_id':'test-worker',
             'worker_version':'0.0.1','started_at':'t','completed_at':'t','exit_code':0,
             'input_manifest_hash':man['manifest_hash'],
             'result_payload_hash':'sha256:'+hashlib.sha256(canon.encode()).hexdigest(),'payload':payload}
        open(os.path.join(out,'result.json'),'w').write(json.dumps(env))
    """)
    with reg.connect(db) as conn:
        job = _make_queued_job(conn)
        final = sbx.run_job(conn, job, worker_argv=_worker_argv(worker),
                            jobs_root=jobs_root, now_fn=_clock())
        assert final.status is JobStatus.SUCCEEDED
        res = json.loads((Path(jobs_root)/job.job_id/"output"/"result.json").read_text())
        assert res["payload"]["wrote_input"] is False  # input write denied


def test_workspace_and_output_writable(env):
    db, jobs_root = env
    worker = textwrap.dedent("""
        import os, json, hashlib
        ws=os.environ['RD_WORKSPACE_DIR']; out=os.environ['RD_OUTPUT_DIR']; inp=os.environ['RD_INPUT_DIR']
        open(os.path.join(ws,'scratch.txt'),'w').write('ok')
        man=json.load(open(os.path.join(inp,'manifest.json')))
        payload={'ws': True}
        canon=json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=True)
        env={'schema_version':'1','job_id':os.environ['RD_JOB_ID'],'worker_id':'test-worker',
             'worker_version':'0.0.1','started_at':'t','completed_at':'t','exit_code':0,
             'input_manifest_hash':man['manifest_hash'],
             'result_payload_hash':'sha256:'+hashlib.sha256(canon.encode()).hexdigest(),'payload':payload}
        open(os.path.join(out,'result.json'),'w').write(json.dumps(env))
    """)
    with reg.connect(db) as conn:
        job = _make_queued_job(conn)
        final = sbx.run_job(conn, job, worker_argv=_worker_argv(worker),
                            jobs_root=jobs_root, now_fn=_clock())
        assert final.status is JobStatus.SUCCEEDED
        assert (Path(jobs_root)/job.job_id/"workspace"/"scratch.txt").read_text() == "ok"


# --- 6/7/8/9. result validation failures ------------------------------------
def _bad_worker(payload_env_overrides: str):
    return textwrap.dedent(f"""
        import os, json, hashlib
        inp=os.environ['RD_INPUT_DIR']; out=os.environ['RD_OUTPUT_DIR']
        man=json.load(open(os.path.join(inp,'manifest.json')))
        payload={{'x':1}}
        canon=json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=True)
        env={{'schema_version':'1','job_id':os.environ['RD_JOB_ID'],'worker_id':'w','worker_version':'0',
             'started_at':'t','completed_at':'t','exit_code':0,
             'input_manifest_hash':man['manifest_hash'],
             'result_payload_hash':'sha256:'+hashlib.sha256(canon.encode()).hexdigest(),'payload':payload}}
        {payload_env_overrides}
        open(os.path.join(out,'result.json'),'w').write(json.dumps(env))
    """)


def test_wrong_job_id_fails_validation(env):
    db, jobs_root = env
    with reg.connect(db) as conn:
        job = _make_queued_job(conn)
        final = sbx.run_job(conn, job, worker_argv=_worker_argv(_bad_worker("env['job_id']='job-somethingelse'")),
                            jobs_root=jobs_root, now_fn=_clock())
        assert final.status is JobStatus.FAILED_VALIDATION
        assert "job_id mismatch" in (final.error_message or "")


def test_manifest_hash_mismatch_fails(env):
    db, jobs_root = env
    with reg.connect(db) as conn:
        job = _make_queued_job(conn)
        final = sbx.run_job(conn, job, worker_argv=_worker_argv(_bad_worker("env['input_manifest_hash']='sha256:deadbeef'")),
                            jobs_root=jobs_root, now_fn=_clock())
        assert final.status is JobStatus.FAILED_VALIDATION


def test_malformed_json_fails(env):
    db, jobs_root = env
    worker = "import os;open(os.path.join(os.environ['RD_OUTPUT_DIR'],'result.json'),'w').write('{not json')"
    with reg.connect(db) as conn:
        job = _make_queued_job(conn)
        final = sbx.run_job(conn, job, worker_argv=_worker_argv(worker),
                            jobs_root=jobs_root, now_fn=_clock())
        assert final.status is JobStatus.FAILED_VALIDATION


def test_oversized_result_rejected(env):
    db, jobs_root = env
    worker = "import os;open(os.path.join(os.environ['RD_OUTPUT_DIR'],'result.json'),'w').write('x'*5000)"
    with reg.connect(db) as conn:
        job = _make_queued_job(conn)
        final = sbx.run_job(conn, job, worker_argv=_worker_argv(worker), jobs_root=jobs_root,
                            now_fn=_clock(), max_output_bytes=1024)
        assert final.status is JobStatus.FAILED_VALIDATION


def test_missing_result_fails_validation(env):
    db, jobs_root = env
    worker = "import sys;sys.exit(0)"  # exit 0 but writes nothing
    with reg.connect(db) as conn:
        job = _make_queued_job(conn)
        final = sbx.run_job(conn, job, worker_argv=_worker_argv(worker),
                            jobs_root=jobs_root, now_fn=_clock())
        assert final.status is JobStatus.FAILED_VALIDATION


def test_worker_declaring_authoritative_status_rejected(env):
    db, jobs_root = env
    with reg.connect(db) as conn:
        job = _make_queued_job(conn)
        final = sbx.run_job(conn, job, worker_argv=_worker_argv(_bad_worker("env['status']='SUCCEEDED'")),
                            jobs_root=jobs_root, now_fn=_clock())
        assert final.status is JobStatus.FAILED_VALIDATION
        assert "authoritative status" in (final.error_message or "")


def test_path_traversal_in_result_rejected(env):
    db, jobs_root = env
    with reg.connect(db) as conn:
        job = _make_queued_job(conn)
        w = _bad_worker("payload['p']='/srv/stockbot-shadow/v1/secret'; "
                        "env['payload']=payload; "
                        "canon=json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=True); "
                        "env['result_payload_hash']='sha256:'+hashlib.sha256(canon.encode()).hexdigest()")
        final = sbx.run_job(conn, job, worker_argv=_worker_argv(w), jobs_root=jobs_root, now_fn=_clock())
        assert final.status is JobStatus.FAILED_VALIDATION


# --- 12/13. worker failures -------------------------------------------------
def test_nonzero_exit_maps_to_failed_worker(env):
    db, jobs_root = env
    worker = "import sys;sys.exit(3)"
    with reg.connect(db) as conn:
        job = _make_queued_job(conn)
        final = sbx.run_job(conn, job, worker_argv=_worker_argv(worker),
                            jobs_root=jobs_root, now_fn=_clock())
        assert final.status is JobStatus.FAILED_WORKER
        assert "exit 3" in (final.error_message or "")


def test_exec_failure_maps_to_failed_sandbox(env):
    db, jobs_root = env
    with reg.connect(db) as conn:
        job = _make_queued_job(conn)
        final = sbx.run_job(conn, job, worker_argv=["/nonexistent/binary/xyz"],
                            jobs_root=jobs_root, now_fn=_clock())
        assert final.status is JobStatus.FAILED_SANDBOX


# --- 10/11. timeout + cancel (process-tree termination) ---------------------
def test_timeout_maps_to_timed_out_and_kills_tree(env):
    db, jobs_root = env
    # worker spawns a child then sleeps; both must die on timeout
    worker = textwrap.dedent("""
        import os, subprocess, time
        subprocess.Popen(['sleep','300'])
        time.sleep(300)
    """)
    with reg.connect(db) as conn:
        job = _make_queued_job(conn)
        final = sbx.run_job(conn, job, worker_argv=_worker_argv(worker),
                            jobs_root=jobs_root, now_fn=_clock(), timeout_seconds=2)
        assert final.status is JobStatus.TIMED_OUT


def test_exit_zero_does_not_imply_success(env):
    # a worker that exits 0 with an empty-but-invalid result stays non-SUCCEEDED
    db, jobs_root = env
    worker = "import os;open(os.path.join(os.environ['RD_OUTPUT_DIR'],'result.json'),'w').write('{}')"
    with reg.connect(db) as conn:
        job = _make_queued_job(conn)
        final = sbx.run_job(conn, job, worker_argv=_worker_argv(worker),
                            jobs_root=jobs_root, now_fn=_clock())
        assert final.status is JobStatus.FAILED_VALIDATION


# --- 14/15. CAS integration + no phantom audit ------------------------------
def test_lifecycle_audit_trail(env):
    db, jobs_root = env
    with reg.connect(db) as conn:
        job = _make_queued_job(conn)
        sbx.run_job(conn, job, worker_argv=_worker_argv(_GOOD_WORKER),
                    jobs_root=jobs_root, now_fn=_clock())
        tos = [e["to_status"] for e in reg.job_events(conn, job.job_id)]
        assert tos == ["CREATED", "QUEUED", "ADMITTED", "RUNNING",
                       "RESULT_RECEIVED", "VALIDATING", "SUCCEEDED"]


def test_failed_validation_has_no_succeeded_event(env):
    db, jobs_root = env
    with reg.connect(db) as conn:
        job = _make_queued_job(conn)
        sbx.run_job(conn, job, worker_argv=_worker_argv(_bad_worker("env['job_id']='x'")),
                    jobs_root=jobs_root, now_fn=_clock())
        tos = [e["to_status"] for e in reg.job_events(conn, job.job_id)]
        assert "SUCCEEDED" not in tos
        assert tos[-1] == "FAILED_VALIDATION"


# --- 16. restart reconciliation (reuses Phase 0A) ---------------------------
def test_orphan_running_reconciled_after_restart(env):
    db, jobs_root = env
    # Simulate a job stuck RUNNING (runner died mid-execution).
    with reg.connect(db) as conn:
        job = _make_queued_job(conn, job_id="job-orphan")
        for st in (JobStatus.ADMITTED, JobStatus.RUNNING):
            reg.transition(conn, job.job_id, st, at="2026-08-09T00:00:00Z")
    # New process (reconnect); reconcile stale RUNNING.
    with reg.connect(db) as conn:
        ids = reg.recover_stale_running(conn, now="2026-08-09T02:00:00Z", max_running_seconds=3600)
        assert ids == ["job-orphan"]
        assert reg.get_job(conn, "job-orphan").status is JobStatus.INTERRUPTED


# --- cancel ----------------------------------------------------------------
def test_cancel_running_job(env):
    db, jobs_root = env
    # Materialize a RUNNING job with a live long-sleeping process + pidfile, then cancel.
    import subprocess as sp, time
    with reg.connect(db) as conn:
        job = _make_queued_job(conn, job_id="job-cancel")
        for st in (JobStatus.ADMITTED, JobStatus.RUNNING):
            reg.transition(conn, job.job_id, st, at=T0)
        d = sbx.materialize_job(jobs_root, job.job_id)
        proc = sp.Popen(["sleep", "300"], start_new_session=True)
        (d.runner / "pid.json").write_text(json.dumps({"pid": proc.pid, "pgid": proc.pid, "started_at": T0}))
        final = sbx.cancel_job(conn, job.job_id, jobs_root=jobs_root, now_fn=_clock())
        assert final.status is JobStatus.CANCELLED
        time.sleep(0.5)
        assert proc.poll() is not None  # process was killed


# --- hostile result-file ingestion (P0.7): the reader must not be a confused
#     deputy for a worker-controlled output/ path -------------------------------
def test_read_result_safe_rejects_symlink(tmp_path):
    secret = tmp_path / "secret"; secret.write_text("TOPSECRET")
    link = tmp_path / "result.json"
    os.symlink(secret, link)  # worker plants a symlink to a file it must not read
    assert sbx._read_result_safe(link, 1 << 20) is None


def test_read_result_safe_rejects_fifo(tmp_path):
    fifo = tmp_path / "result.json"
    os.mkfifo(fifo)  # a FIFO would block a naive reader forever
    assert sbx._read_result_safe(fifo, 1 << 20) is None


def test_read_result_safe_rejects_directory(tmp_path):
    d = tmp_path / "result.json"; d.mkdir()
    assert sbx._read_result_safe(d, 1 << 20) is None


def test_read_result_safe_rejects_oversize(tmp_path):
    f = tmp_path / "result.json"; f.write_bytes(b"x" * 4096)
    assert sbx._read_result_safe(f, 1024) is None


def test_read_result_safe_reads_regular_file(tmp_path):
    f = tmp_path / "result.json"; f.write_bytes(b'{"ok":1}')
    assert sbx._read_result_safe(f, 1 << 20) == b'{"ok":1}'


def test_symlinked_result_maps_to_failed_validation(env):
    # A worker that replaces result.json with a symlink to a host file must NOT
    # cause the trusted runner to read that host file — the job fails validation.
    db, jobs_root = env
    secret = Path(jobs_root) / "host_secret.txt"
    secret.write_text('{"schema_version":"1"}')  # even valid-looking JSON must not be followed
    worker = textwrap.dedent(f"""
        import os
        out = os.environ['RD_OUTPUT_DIR']
        os.symlink({str(secret)!r}, os.path.join(out, 'result.json'))
    """)
    with reg.connect(db) as conn:
        job = _make_queued_job(conn)
        final = sbx.run_job(conn, job, worker_argv=_worker_argv(worker),
                            jobs_root=jobs_root, now_fn=_clock())
        assert final.status is JobStatus.FAILED_VALIDATION


# --- env sanitization (P1.3): the worker runs with a clean, explicit env; the
#     runner never leaks its own process environment (e.g. secrets) ------------
def test_worker_env_does_not_inherit_parent_secrets(env, monkeypatch):
    db, jobs_root = env
    monkeypatch.setenv("FAKE_AWS_SECRET_ACCESS_KEY", "should-not-leak-123")
    monkeypatch.setenv("FAKE_PRIME_TOKEN", "nope")
    worker = textwrap.dedent("""
        import os, json, hashlib
        inp=os.environ['RD_INPUT_DIR']; out=os.environ['RD_OUTPUT_DIR']; ws=os.environ['RD_WORKSPACE_DIR']
        open(os.path.join(ws,'env_keys.txt'),'w').write('\\n'.join(sorted(os.environ.keys())))
        man=json.load(open(os.path.join(inp,'manifest.json')))
        payload={'k': sorted(os.environ.keys())}
        canon=json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=True)
        env={'schema_version':'1','job_id':os.environ['RD_JOB_ID'],'worker_id':'test-worker',
             'worker_version':'0.0.1','started_at':'t','completed_at':'t','exit_code':0,
             'input_manifest_hash':man['manifest_hash'],
             'result_payload_hash':'sha256:'+hashlib.sha256(canon.encode()).hexdigest(),'payload':payload}
        open(os.path.join(out,'result.json'),'w').write(json.dumps(env))
    """)
    with reg.connect(db) as conn:
        job = _make_queued_job(conn)
        final = sbx.run_job(conn, job, worker_argv=_worker_argv(worker),
                            jobs_root=jobs_root, now_fn=_clock())
        assert final.status is JobStatus.SUCCEEDED
        keys = (Path(jobs_root)/job.job_id/"workspace"/"env_keys.txt").read_text().splitlines()
        assert "FAKE_AWS_SECRET_ACCESS_KEY" not in keys
        assert "FAKE_PRIME_TOKEN" not in keys
        # The runner-supplied allowlist is present...
        assert {"PATH","HOME","RD_JOB_ID","RD_INPUT_DIR","RD_WORKSPACE_DIR",
                "RD_OUTPUT_DIR","RD_NETWORK_PROFILE"} <= set(keys)
        # ...and nothing secret-shaped leaked from the parent process. (Python may
        # add LC_CTYPE itself via PEP 538 locale coercion; that is not inheritance.)
        assert not [k for k in keys if any(t in k.upper()
                    for t in ("SECRET","TOKEN","PASSWORD","AWS","PRIME","API_KEY"))]


# --- manifest integrity -----------------------------------------------------
def test_manifest_hash_deterministic_excludes_created_at(env):
    db, jobs_root = env
    with reg.connect(db) as conn:
        job = _make_queued_job(conn, job_id="job-man")
        m1 = sbx.build_input_manifest(reg.get_job(conn, "job-man"),
                                      network_profile=sbx.NetworkProfile.OFFLINE_LOCAL,
                                      timeout_seconds=60, max_output_bytes=1024, created_at="t-A")
        m2 = sbx.build_input_manifest(reg.get_job(conn, "job-man"),
                                      network_profile=sbx.NetworkProfile.OFFLINE_LOCAL,
                                      timeout_seconds=60, max_output_bytes=1024, created_at="t-B")
        assert m1["created_at"] != m2["created_at"]
        assert m1["manifest_hash"] == m2["manifest_hash"]
