"""R&D Control Plane — framework-neutral sandbox runner (Phase 0B).

The trusted control plane materializes a bounded job directory, writes an
immutable input manifest, launches an UNTRUSTED worker process, captures a
job-local result artifact, deterministically validates it, and drives the
Phase 0A registry lifecycle via the existing CAS transitions. The worker never
writes authoritative state; its result is DATA, validated by trusted code.

This module is OS-isolation-agnostic: it accepts a ``jail_wrapper`` command
prefix. In hermetic tests the wrapper is empty (worker runs as a plain
subprocess). In the Agent-Lab runtime the wrapper is the network-namespace
entry (``ip netns exec <ns> runuser -u <worker> --``) that supplies the actual
OFFLINE_LOCAL confinement. The confinement itself is certified separately in the
Agent-Lab environment (see docs/RD_SANDBOX.md); this module proves the trusted
execution contract: materialization, read-only input, job-scoped write scope,
bounded output, deterministic lifecycle mapping, and result validation.

Nothing here trades, calls a broker, mutates production, or writes outside the
job directory + the authoritative registry (via CAS only).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Sequence

from portfolio_automation.rd_control import registry as reg
from portfolio_automation.rd_control.contracts import (
    JobRecord, JobStatus, RDControlError,
)

SANDBOX_SCHEMA_VERSION = "1"
RESULT_FILENAME = "result.json"
MANIFEST_FILENAME = "manifest.json"
_RUNNER_DIR = ".runner"          # runner-owned per-job state (pidfile, logs)
_DEFAULT_MAX_OUTPUT_BYTES = 1 << 20   # 1 MiB result cap
_DEFAULT_TIMEOUT_SECONDS = 120


class NetworkProfile(str, Enum):
    """Job network profiles. Phase 0B implements/certifies only OFFLINE_LOCAL."""
    OFFLINE_LOCAL = "OFFLINE_LOCAL"   # local Ollama bridge (by IP) only; no DNS, no internet


class SandboxError(RDControlError):
    """Sandbox setup/isolation failure (maps a job to FAILED_SANDBOX)."""


# ---------------------------------------------------------------------------
# Canonicalisation / integrity
# ---------------------------------------------------------------------------
def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def _sha256_str(s: str) -> str:
    return "sha256:" + hashlib.sha256(s.encode("utf-8")).hexdigest()


def compute_payload_hash(payload: Any) -> str:
    """Integrity hash of a result payload (not authenticity)."""
    return _sha256_str(_canon(payload))


# ---------------------------------------------------------------------------
# Job directory materialization
# ---------------------------------------------------------------------------
@dataclass
class JobDirs:
    root: Path        # <jobs_root>/<job_id>
    input: Path       # read-only to the worker
    workspace: Path   # read/write, job-scoped
    output: Path      # read/write, job-scoped
    runner: Path      # runner-owned (NOT given to the worker)


def materialize_job(jobs_root: str | Path, job_id: str) -> JobDirs:
    """Create the job/{input,workspace,output} tree + a runner-private dir.

    Fresh each call; refuses to reuse a dirty directory (a stale job dir is a
    setup error, not silently reused)."""
    root = Path(jobs_root) / job_id
    if root.exists():
        raise SandboxError(f"job directory already exists: {root}")
    dirs = JobDirs(root=root, input=root / "input", workspace=root / "workspace",
                   output=root / "output", runner=root / _RUNNER_DIR)
    for d in (dirs.input, dirs.workspace, dirs.output, dirs.runner):
        d.mkdir(parents=True, exist_ok=False)
    return dirs


def seal_input_readonly(dirs: JobDirs) -> None:
    """Make input/ and its contents read-only to the worker (bottom-up)."""
    for dirpath, _dn, filenames in os.walk(dirs.input, topdown=False):
        for fn in filenames:
            try:
                os.chmod(os.path.join(dirpath, fn), 0o444)
            except OSError:
                pass
        try:
            os.chmod(dirpath, 0o555)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Immutable input manifest
# ---------------------------------------------------------------------------
def build_input_manifest(
    job: JobRecord,
    *,
    network_profile: NetworkProfile,
    timeout_seconds: int,
    max_output_bytes: int,
    allowed_paths: Sequence[str] = (),
    created_at: str,
) -> dict[str, Any]:
    """Bind the job to its identity/provenance/resource envelope. The
    ``manifest_hash`` is integrity (sha256), not authenticity."""
    core = {
        "schema_version": SANDBOX_SCHEMA_VERSION,
        "job_id": job.job_id,
        "job_type": job.job_type.value,
        "authority": job.authority.value,
        "stockbot_sha": job.stockbot_sha,
        "input_snapshot_id": job.input_snapshot_id,
        "input_snapshot_hash": job.input_snapshot_hash,
        "worker_id": job.worker_id,
        "worker_version": job.worker_version,
        "network_profile": network_profile.value,
        "timeout_seconds": timeout_seconds,
        "max_output_bytes": max_output_bytes,
        "allowed_paths": sorted(allowed_paths),
    }
    manifest = dict(core)
    manifest["created_at"] = created_at            # excluded from hash
    manifest["manifest_hash"] = _sha256_str(_canon(core))
    return manifest


def write_manifest(dirs: JobDirs, manifest: dict[str, Any]) -> Path:
    p = dirs.input / MANIFEST_FILENAME
    p.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Result validation (untrusted worker output -> trusted verdict)
# ---------------------------------------------------------------------------
_REQUIRED_RESULT_FIELDS = (
    "schema_version", "job_id", "worker_id", "worker_version",
    "started_at", "completed_at", "exit_code",
    "input_manifest_hash", "result_payload_hash", "payload",
)
# Substrings that must never appear in a worker-declared path-like field.
_TRAVERSAL_MARKERS = ("../", "..\\", "/etc/", "/srv/stockbot-shadow", "/mnt/c", "/mnt/d")


@dataclass
class ValidationOutcome:
    ok: bool
    errors: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None
    result_hash: str | None = None


def validate_result(
    raw: bytes,
    *,
    job: JobRecord,
    manifest_hash: str,
    max_output_bytes: int,
) -> ValidationOutcome:
    """Deterministically validate an untrusted result envelope. Any failure ->
    ok=False (caller maps to FAILED_VALIDATION). The worker's self-declared
    status is intentionally NOT consulted for authority."""
    errors: list[str] = []
    if len(raw) > max_output_bytes:
        return ValidationOutcome(False, [f"result exceeds max_output_bytes ({len(raw)}>{max_output_bytes})"])
    result_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        return ValidationOutcome(False, [f"invalid JSON/encoding: {type(exc).__name__}"], result_hash=result_hash)
    if not isinstance(obj, dict):
        return ValidationOutcome(False, ["result is not a JSON object"], result_hash=result_hash)
    for f in _REQUIRED_RESULT_FIELDS:
        if f not in obj:
            errors.append(f"missing field: {f}")
    if obj.get("schema_version") != SANDBOX_SCHEMA_VERSION:
        errors.append(f"schema_version mismatch: {obj.get('schema_version')!r}")
    if obj.get("job_id") != job.job_id:
        errors.append(f"job_id mismatch: {obj.get('job_id')!r} != {job.job_id}")
    if obj.get("input_manifest_hash") != manifest_hash:
        errors.append("input_manifest_hash does not match the job's manifest")
    # payload hash integrity
    if "payload" in obj:
        expect = compute_payload_hash(obj["payload"])
        if obj.get("result_payload_hash") != expect:
            errors.append("result_payload_hash does not match payload")
    # path-traversal scan over the whole envelope text
    low = raw.decode("utf-8", "replace")
    for marker in _TRAVERSAL_MARKERS:
        if marker in low:
            errors.append(f"path-traversal / out-of-scope reference detected: {marker!r}")
            break
    # A worker that tries to declare its own authoritative JobStatus is ignored,
    # but we flag it so the audit shows the attempt.
    if isinstance(obj.get("status"), str) and obj["status"].upper() in {s.value for s in JobStatus}:
        errors_note = f"worker attempted to declare authoritative status={obj['status']!r} (ignored)"
        # This is not itself fatal (status is data), but a strict envelope treats
        # a reserved authoritative value as a contract violation.
        errors.append(errors_note)
    return ValidationOutcome(not errors, errors, result=obj if not errors else None, result_hash=result_hash)


# ---------------------------------------------------------------------------
# Process execution (bounded, process-group killable)
# ---------------------------------------------------------------------------
@dataclass
class ExecResult:
    exit_code: int | None
    timed_out: bool
    stdout: bytes
    stderr: bytes
    started_at: str
    completed_at: str


def _run_worker_process(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int,
    max_output_bytes: int,
    now_fn: Callable[[], str],
    pidfile: Path | None = None,
) -> ExecResult:
    """Run *argv* in its own session (process group), bounded by time and output.
    On timeout the entire process group is terminated (SIGTERM then SIGKILL) so
    no grandchildren survive."""
    started = now_fn()
    proc = subprocess.Popen(
        argv, cwd=str(cwd), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,   # new process group -> killpg terminates the tree
    )
    if pidfile is not None:
        try:
            pidfile.write_text(json.dumps({"pid": proc.pid, "pgid": os.getpgid(proc.pid),
                                           "started_at": started}), encoding="utf-8")
        except OSError:
            pass
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_group(proc.pid)
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out, err = b"", b""
    return ExecResult(
        exit_code=None if timed_out else proc.returncode,
        timed_out=timed_out,
        stdout=(out or b"")[:max_output_bytes],
        stderr=(err or b"")[:max_output_bytes],
        started_at=started,
        completed_at=now_fn(),
    )


def _terminate_group(pid: int) -> None:
    """SIGTERM then SIGKILL the whole process group; never raise."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        time.sleep(0.3)
        try:
            os.killpg(os.getpgid(pid), 0)  # still alive?
        except OSError:
            return


# ---------------------------------------------------------------------------
# The runner: JobSpec -> materialize -> execute -> validate -> CAS transition
# ---------------------------------------------------------------------------
def run_job(
    conn,
    job: JobRecord,
    *,
    worker_argv: Sequence[str],
    jobs_root: str | Path,
    now_fn: Callable[[], str],
    network_profile: NetworkProfile = NetworkProfile.OFFLINE_LOCAL,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
    jail_wrapper: Sequence[str] = (),
    input_files: dict[str, bytes] | None = None,
    worker_env: dict[str, str] | None = None,
    allowed_paths: Sequence[str] = (),
    worker_uid: int | None = None,
    worker_gid: int | None = None,
) -> JobRecord:
    """Execute *job* (which must be in QUEUED) end to end and return the final
    record. All authoritative status changes go through Phase 0A CAS transitions;
    the worker only writes into ``output/``.

    ``jail_wrapper`` is prepended to ``worker_argv`` (e.g. the netns/runuser
    prefix). Empty in hermetic tests. The worker is expected to write its result
    envelope to ``output/result.json`` (path passed via env RD_OUTPUT_DIR)."""
    # --- admit + enter RUNNING first, so a setup failure maps legally to
    # FAILED_SANDBOX (a RUNNING-only edge in the Phase 0A machine). RUNNING here
    # means "the runner owns the job and is building/executing its sandbox". ---
    reg.transition(conn, job.job_id, JobStatus.ADMITTED, at=now_fn(), reason="admitted", actor="sandbox")
    reg.transition(conn, job.job_id, JobStatus.RUNNING, at=now_fn(), reason="running", actor="sandbox")

    # --- sandbox setup (failure here -> FAILED_SANDBOX) ---
    try:
        dirs = materialize_job(jobs_root, job.job_id)
        input_root = str(dirs.input.resolve())
        for rel, data in (input_files or {}).items():
            safe = (dirs.input / rel).resolve()
            if os.path.commonpath([str(safe), input_root]) != input_root:
                raise SandboxError(f"input file escapes input dir: {rel}")
            safe.parent.mkdir(parents=True, exist_ok=True)
            safe.write_bytes(data)
        manifest = build_input_manifest(
            job, network_profile=network_profile, timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes, allowed_paths=allowed_paths, created_at=now_fn(),
        )
        write_manifest(dirs, manifest)
        seal_input_readonly(dirs)
        # When the worker runs as a different (lower-priv) user via jail_wrapper,
        # give it ownership of ONLY workspace/ and output/. input/ stays
        # root-owned 0555 (read-only) and the runner-private dir is not chowned.
        if worker_uid is not None:
            for d in (dirs.workspace, dirs.output):
                os.chown(d, worker_uid, worker_gid if worker_gid is not None else -1)
    except Exception as exc:
        return _cas(conn, job.job_id, JobStatus.FAILED_SANDBOX, now_fn(),
                    reason=f"sandbox setup failed: {type(exc).__name__}", actor="sandbox",
                    error_class="sandbox_setup", error_message=str(exc)[:400])

    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "RD_JOB_ID": job.job_id,
        "RD_INPUT_DIR": str(dirs.input),
        "RD_WORKSPACE_DIR": str(dirs.workspace),
        "RD_OUTPUT_DIR": str(dirs.output),
        "RD_NETWORK_PROFILE": network_profile.value,
        "HOME": str(dirs.workspace),
    }
    if worker_env:
        env.update(worker_env)

    argv = list(jail_wrapper) + list(worker_argv)
    try:
        ex = _run_worker_process(
            argv, cwd=dirs.workspace, env=env, timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes, now_fn=now_fn,
            pidfile=dirs.runner / "pid.json",
        )
    except FileNotFoundError:
        return _cas(conn, job.job_id, JobStatus.FAILED_SANDBOX, now_fn(),
                    reason="worker exec failed", actor="sandbox")

    if ex.timed_out:
        return _cas(conn, job.job_id, JobStatus.TIMED_OUT, now_fn(),
                    reason=f"timeout after {timeout_seconds}s", actor="sandbox")
    if ex.exit_code != 0:
        return _cas(conn, job.job_id, JobStatus.FAILED_WORKER, now_fn(),
                    reason=f"worker exit {ex.exit_code}", actor="sandbox",
                    error_class="worker_nonzero_exit", error_message=f"exit {ex.exit_code}")

    # --- collect + validate result (exit 0 does NOT imply success) ---
    result_path = dirs.output / RESULT_FILENAME
    raw: bytes | None
    try:
        raw = result_path.read_bytes()
    except OSError:
        raw = None
    # RESULT_RECEIVED regardless (the worker exited 0); the trusted validator,
    # not the exit code, decides success.
    reg.transition(conn, job.job_id, JobStatus.RESULT_RECEIVED, at=now_fn(),
                   reason="result captured" if raw is not None else "no result file",
                   actor="sandbox",
                   result_hash=("sha256:" + hashlib.sha256(raw).hexdigest()) if raw is not None else None)
    reg.transition(conn, job.job_id, JobStatus.VALIDATING, at=now_fn(),
                   reason="validating", actor="sandbox")
    if raw is None:
        return reg.transition(conn, job.job_id, JobStatus.FAILED_VALIDATION, at=now_fn(),
                              reason="worker produced no result.json", actor="sandbox",
                              error_class="result_validation", error_message="missing result.json")
    vo = validate_result(raw, job=job, manifest_hash=manifest["manifest_hash"],
                         max_output_bytes=max_output_bytes)
    if not vo.ok:
        return reg.transition(conn, job.job_id, JobStatus.FAILED_VALIDATION, at=now_fn(),
                              reason="; ".join(vo.errors)[:400], actor="sandbox",
                              error_class="result_validation", error_message="; ".join(vo.errors)[:400])
    return reg.transition(conn, job.job_id, JobStatus.SUCCEEDED, at=now_fn(),
                          reason="validated", actor="sandbox")


def _cas(conn, job_or_id, to_status: JobStatus, at: str, *, reason=None, actor="sandbox", **updates):
    job_id = job_or_id.job_id if isinstance(job_or_id, JobRecord) else job_or_id
    return reg.transition(conn, job_id, to_status, at=at, reason=reason, actor=actor, **updates)


def cancel_job(conn, job_id: str, *, jobs_root: str | Path, now_fn: Callable[[], str]) -> JobRecord:
    """Operator cancellation: kill the worker process group (if any) then CAS to
    CANCELLED (legal from RUNNING). Safe if the process is already gone."""
    pidfile = Path(jobs_root) / job_id / _RUNNER_DIR / "pid.json"
    try:
        info = json.loads(pidfile.read_text())
        _terminate_group(int(info["pid"]))
    except Exception:
        pass
    return reg.transition(conn, job_id, JobStatus.CANCELLED, at=now_fn(),
                          reason="operator cancel", actor="operator")
