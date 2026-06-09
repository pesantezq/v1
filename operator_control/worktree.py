"""Thin wrapper over `git worktree` for isolated worker runs.

Each work order gets a throwaway worktree at .worktrees/<id> on branch
operator/<id> cut from base (default main). The runner never merges or pushes
these branches; humans review and integrate.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


class WorktreeError(RuntimeError):
    pass


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True
    )


def create_worktree(root, work_order_id: str, base: str = "main"):
    root = Path(root)
    branch = f"operator/{work_order_id}"
    path = root / ".worktrees" / work_order_id
    path.parent.mkdir(parents=True, exist_ok=True)
    r = _git(root, "worktree", "add", "-b", branch, str(path), base)
    if r.returncode != 0:
        raise WorktreeError(r.stderr.strip() or "git worktree add failed")
    return path, branch


def changed_files(worktree_path, base: str = "main") -> list[str]:
    r = _git(Path(worktree_path), "diff", "--name-only", base)
    if r.returncode != 0:
        raise WorktreeError(r.stderr.strip() or "git diff failed")
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def list_worktrees(root) -> list[str]:
    r = _git(Path(root), "worktree", "list", "--porcelain")
    return [
        ln.split(" ", 1)[1]
        for ln in r.stdout.splitlines()
        if ln.startswith("worktree ")
    ]


def remove_worktree(root, worktree_path, force: bool = False) -> None:
    args = ["worktree", "remove", str(worktree_path)]
    if force:
        args.append("--force")
    _git(Path(root), *args)


__all__ = [
    "WorktreeError",
    "create_worktree",
    "changed_files",
    "list_worktrees",
    "remove_worktree",
]
