"""Deterministic diagnostic adapters. The model NEVER supplies raw shell.

Each adapter runs a FIXED argv (never shell=True), bounds its output, sanitizes
paths, and returns a ``DiagnosticSource``. Operational failures degrade to
``ok=False`` with a short error string (never a traceback, never a secret).
Policy violations (path traversal / symlink) raise ``PolicyError`` so the
controller records a hard denial.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from portfolio_automation.engineer_worker.contracts import DiagnosticSource
from portfolio_automation.engineer_worker import policy

_MAX_OUT = 16_384          # cap captured subprocess output
_MAX_LOG_EXCERPT = 8_192   # tail bytes of a daily log handed to the model
_MAX_ARTIFACT = 32_768     # bytes read from a diagnostic artifact
_SUBPROC_TIMEOUT = 60


def _run(argv: list[str], cwd: str | Path, timeout: int = _SUBPROC_TIMEOUT) -> dict[str, Any]:
    """Run a fixed argv (no shell), bounded time + output. Never raises for a
    non-zero exit; raises only on our own misuse."""
    try:
        proc = subprocess.run(
            argv, cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout, check=False,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "HOME": os.environ.get("HOME", "/tmp"),
                 "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"},
        )
        return {"rc": proc.returncode,
                "stdout": (proc.stdout or "")[:_MAX_OUT],
                "stderr": (proc.stderr or "")[:_MAX_OUT]}
    except FileNotFoundError as e:
        return {"rc": 127, "stdout": "", "stderr": f"not found: {e.filename}"}
    except subprocess.TimeoutExpired:
        return {"rc": None, "stdout": "", "stderr": f"timeout after {timeout}s"}


def repo_status(repo_root: str | Path) -> DiagnosticSource:
    porc = _run(["git", "status", "--porcelain"], repo_root)
    head = _run(["git", "rev-parse", "HEAD"], repo_root)
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo_root)
    ok = porc["rc"] == 0 and head["rc"] == 0
    dirty_lines = [l for l in porc["stdout"].splitlines() if l.strip()]
    return DiagnosticSource(
        name="repo_status", ok=ok, provenance="git status --porcelain; git rev-parse HEAD",
        data={
            "head": head["stdout"].strip() or None,
            "branch": branch["stdout"].strip() or None,
            "dirty": bool(dirty_lines),
            "changed_count": len(dirty_lines),
            "changed_sample": dirty_lines[:20],
        },
        error=None if ok else (porc["stderr"] or head["stderr"])[:200],
    )


def disk_status(path: str | Path) -> DiagnosticSource:
    try:
        u = shutil.disk_usage(str(path))
        pct = round(100.0 * u.used / u.total, 1) if u.total else None
        return DiagnosticSource(
            name="disk_status", ok=True, provenance="shutil.disk_usage",
            data={"total_gb": round(u.total / 2**30, 2), "used_gb": round(u.used / 2**30, 2),
                  "free_gb": round(u.free / 2**30, 2), "used_pct": pct},
        )
    except OSError as e:
        return DiagnosticSource(name="disk_status", ok=False,
                                provenance="shutil.disk_usage", error=str(e)[:200])


def ollama_status(url: str | None) -> DiagnosticSource:
    """Query the inference-only Ollama facade /api/version (read-only). Degrades
    gracefully when the facade is unreachable (e.g. hermetic test env)."""
    if not url:
        return DiagnosticSource(name="ollama_status", ok=False,
                                provenance="inference facade /api/version",
                                error="no facade url configured")
    import urllib.request
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/version", timeout=10) as r:
            body = r.read(4096).decode("utf-8", "replace")
        ver = json.loads(body).get("version")
        return DiagnosticSource(name="ollama_status", ok=True,
                                provenance="inference facade /api/version",
                                data={"reachable": True, "version": ver})
    except Exception as e:  # noqa: BLE001 - degrade, never leak
        return DiagnosticSource(name="ollama_status", ok=False,
                                provenance="inference facade /api/version",
                                data={"reachable": False}, error=type(e).__name__)


def rd_control_health(db_path: str | Path) -> DiagnosticSource:
    """Phase 0A read-only health. Uses the certified read-only registry surface."""
    try:
        from portfolio_automation.rd_control import health as hlth
        h = hlth.build_health(str(db_path), now="1970-01-01T00:00:00Z")
        data = h if isinstance(h, dict) else {"health": str(h)[:500]}
        return DiagnosticSource(name="rd_control_health", ok=bool(data.get("db_accessible", True)),
                                provenance="rd_control.health.build_health (read-only)",
                                data=data)
    except Exception as e:  # noqa: BLE001
        return DiagnosticSource(name="rd_control_health", ok=False,
                                provenance="rd_control.health.build_health (read-only)",
                                error=f"{type(e).__name__}: {str(e)[:150]}")


def sandbox_status(verify_script: str | None, src_dir: str | None) -> DiagnosticSource:
    """Read-only sandbox runtime verification (verify.sh). Optional; degrades if
    the installed runtime is not present in this environment."""
    if not verify_script or not src_dir or not Path(verify_script).exists():
        return DiagnosticSource(name="sandbox_status", ok=False,
                                provenance="ops/agent_lab/verify.sh",
                                error="verify.sh not available in this environment")
    res = _run(["bash", verify_script, src_dir], Path(verify_script).parent, timeout=30)
    ok = res["rc"] == 0 and "VERIFY_OK" in res["stdout"]
    return DiagnosticSource(name="sandbox_status", ok=ok, provenance="ops/agent_lab/verify.sh",
                            data={"verify_ok": ok, "tail": res["stdout"].splitlines()[-1:]},
                            error=None if ok else "verify failed")


def runtime_provenance(committed_sha: str | None, deployed_sha: str | None) -> DiagnosticSource:
    ok = bool(committed_sha) and committed_sha == deployed_sha
    return DiagnosticSource(name="runtime_provenance", ok=ok,
                            provenance="sha256(committed) == sha256(deployed)",
                            data={"committed": committed_sha, "deployed": deployed_sha,
                                  "match": ok})


def daily_log_reader(repo_root: str | Path, rel_path: str) -> DiagnosticSource:
    """Read an explicitly-selected daily log; return a bounded tail excerpt."""
    try:
        p = policy.safe_join(repo_root, rel_path, must_exist=True)
    except policy.PolicyError:
        raise
    except Exception as e:  # noqa: BLE001
        return DiagnosticSource(name="daily_log", ok=False,
                                provenance=f"read {rel_path} (bounded tail)",
                                error=type(e).__name__)
    if not p.is_file():
        return DiagnosticSource(name="daily_log", ok=False,
                                provenance=f"read {rel_path}", error="not a regular file")
    size = p.stat().st_size
    with open(p, "rb") as fh:
        if size > _MAX_LOG_EXCERPT:
            fh.seek(-_MAX_LOG_EXCERPT, os.SEEK_END)
        raw = fh.read(_MAX_LOG_EXCERPT)
    text = raw.decode("utf-8", "replace")
    lines = text.splitlines()
    return DiagnosticSource(
        name="daily_log", ok=True, provenance=f"read {rel_path} (bounded tail)",
        data={"path": rel_path, "size_bytes": size, "excerpt_lines": len(lines),
              "tail_excerpt": lines[-120:]},
    )


def daily_artifact_reader(repo_root: str | Path, rel_path: str) -> DiagnosticSource:
    """Read an allowlisted diagnostic artifact (JSON preferred), bounded."""
    try:
        p = policy.safe_join(repo_root, rel_path, must_exist=True)
    except policy.PolicyError:
        raise
    except Exception as e:  # noqa: BLE001
        return DiagnosticSource(name=f"artifact:{rel_path}", ok=False,
                                provenance=f"read {rel_path}", error=type(e).__name__)
    if not p.is_file():
        return DiagnosticSource(name=f"artifact:{rel_path}", ok=False,
                                provenance=f"read {rel_path}", error="not a regular file")
    raw = p.read_bytes()[:_MAX_ARTIFACT]
    data: dict[str, Any]
    try:
        parsed = json.loads(raw.decode("utf-8"))
        data = {"parsed": parsed} if isinstance(parsed, (dict, list)) else {"value": parsed}
    except Exception:  # noqa: BLE001
        data = {"raw_excerpt": raw.decode("utf-8", "replace")[:2000]}
    data["size_bytes"] = p.stat().st_size
    return DiagnosticSource(name=f"artifact:{rel_path}", ok=True,
                            provenance=f"read {rel_path} (bounded)", data=data)


def test_status(repo_root: str | Path, target: str, timeout: int = 120) -> DiagnosticSource:
    """Run a pytest target (already policy-allowlisted by the caller). Bounded."""
    res = _run(["python3", "-m", "pytest", target, "-q", "-p", "no:cacheprovider",
                "-o", "addopts="], repo_root, timeout=timeout)
    tail = res["stdout"].splitlines()[-8:]
    summary = next((l for l in reversed(res["stdout"].splitlines())
                    if (" passed" in l or " failed" in l or " error" in l)), "")
    ok = res["rc"] == 0
    return DiagnosticSource(name=f"test:{target}", ok=ok,
                            provenance=f"pytest {target} (allowlisted)",
                            data={"rc": res["rc"], "summary": summary, "tail": tail})
