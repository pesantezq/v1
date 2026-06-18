"""Disposable isolated git workspaces for the container worker. The clone's git
metadata is self-contained and lives under workspace_root — the production
repository's .git is never shared or mounted. Create -> (worker edits) ->
extract+validate diff -> destroy."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from operator_control.worker_container import WO_ID_RE

_MAX_DIFF = 200_000


def _git(cwd, *args, timeout=60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, timeout=timeout)


def create_isolated_workspace(repo_root: str, workspace_root: str, work_order_id: str) -> str:
    if not WO_ID_RE.match(work_order_id or ""):
        raise ValueError(f"invalid work_order_id: {work_order_id!r}")
    dest = Path(workspace_root) / work_order_id
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # --no-local + file:// forces a real clone with self-contained objects/refs;
    # no hardlinks to and no sharing with the production object store.
    cp = subprocess.run(["git", "clone", "--no-local", "--quiet",
                         f"file://{Path(repo_root).resolve()}", str(dest)],
                        capture_output=True, text=True, timeout=300)
    if cp.returncode != 0:
        raise RuntimeError(f"isolated clone failed: {cp.stderr.strip()[:200]}")
    return str(dest)


def extract_validated_diff(ws_path: str) -> str:
    mb = _git(ws_path, "rev-parse", "main").stdout.strip()
    out = _git(ws_path, "diff", "--stat", f"{mb}..HEAD").stdout if mb else ""
    return out[:_MAX_DIFF]


def destroy_workspace(path: str, workspace_root: str) -> None:
    rp = Path(path).resolve()
    root = Path(workspace_root).resolve()
    if root not in rp.parents:
        raise ValueError(f"refusing to destroy path outside workspace_root: {rp}")
    shutil.rmtree(rp, ignore_errors=True)
