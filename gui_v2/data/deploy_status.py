"""Dashboard deploy-status detector — auto-update with manual intervention.

Read-only. Compares the code the dashboard is *serving* (the git SHA stamped at
process startup) against ``origin/main`` so the operator can SEE when a restart
is needed and apply it deliberately. Computes nothing that changes state; the
apply is a separate, gated, manually-triggered action (see app.py).

Phase A (this module): detection + a normalized card. The dashboard's existing
120s HTMX refresh surfaces staleness automatically — the one thing that can't
self-fix (stale served code) is exactly what gets flagged.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from gui_v2.data.shared import card

# Stamp written at service startup recording the SHA actually being served.
RUNNING_SHA_STAMP = "outputs/operator_control/.running_sha"
# Cache origin/main lookups so we fetch at most once per this many seconds.
_FETCH_TTL_SECONDS = 90
_FETCH_STAMP = "outputs/operator_control/.last_fetch"


def _git(root, *args, timeout: int = 15) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        cp = subprocess.CompletedProcess(args, returncode=1, stdout="", stderr=str(exc))
        return cp


def write_running_sha(root) -> str | None:
    """Stamp the SHA the service is starting with. Call at app startup."""
    sha = _git(root, "rev-parse", "HEAD").stdout.strip()
    if sha:
        p = Path(root) / RUNNING_SHA_STAMP
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(sha, encoding="utf-8")
    return sha or None


def running_sha(root) -> str | None:
    p = Path(root) / RUNNING_SHA_STAMP
    if p.exists():
        s = p.read_text(encoding="utf-8").strip()
        if s:
            return s
    return _git(root, "rev-parse", "HEAD").stdout.strip() or None


def _maybe_fetch(root) -> None:
    """Best-effort read-only `git fetch origin main`, throttled by TTL."""
    stamp = Path(root) / _FETCH_STAMP
    try:
        if stamp.exists() and (time.time() - stamp.stat().st_mtime) < _FETCH_TTL_SECONDS:
            return
    except OSError:
        pass
    _git(root, "fetch", "origin", "main", timeout=15)
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        pass


def collect_deploy_status(root, fetch: bool = True) -> dict[str, Any]:
    """Return the deploy state. Pure read-only; never mutates code or refs."""
    root = Path(root)
    run = running_sha(root)
    if fetch:
        _maybe_fetch(root)
    latest = _git(root, "rev-parse", "origin/main").stdout.strip() or None

    state, behind, ahead, ff = "unknown", 0, 0, False
    if run and latest:
        if run == latest:
            state = "up_to_date"
        else:
            ff = _git(root, "merge-base", "--is-ancestor", run, latest).returncode == 0
            parts = _git(root, "rev-list", "--left-right", "--count",
                         f"{run}...{latest}").stdout.split()
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                ahead, behind = int(parts[0]), int(parts[1])
            state = "update_available" if ff else "divergent"

    return {
        "running_sha": run,
        "latest_sha": latest,
        "running_short": (run or "")[:8],
        "latest_short": (latest or "")[:8],
        "state": state,
        "commits_behind": behind,
        "commits_ahead": ahead,
        "fast_forward": ff,
        "observe_only": True,
    }


def deploy_card(status: dict[str, Any]) -> dict[str, Any]:
    st = status.get("state")
    if st == "up_to_date":
        cs, label = "ok", "up to date"
        summary = f"serving {status['running_short']} = origin/main"
    elif st == "update_available":
        cs, label = "warning", f"{status['commits_behind']} commit(s) behind"
        summary = (f"serving {status['running_short']} · latest {status['latest_short']} "
                   f"— restart to update (fast-forward)")
    elif st == "divergent":
        cs, label = "warning", "divergent"
        summary = (f"serving {status['running_short']} is not a fast-forward of "
                   f"origin/main — manual review required")
    else:
        cs, label = "info", "unknown"
        summary = "could not determine deploy status (git unavailable / offline)"
    return card("Deployment", status=cs, label=label, summary=summary,
                source_artifacts=["git: served SHA vs origin/main"])


__all__ = [
    "RUNNING_SHA_STAMP",
    "write_running_sha",
    "running_sha",
    "collect_deploy_status",
    "deploy_card",
]
