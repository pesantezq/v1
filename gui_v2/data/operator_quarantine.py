"""Safe, read-only inspection of operator quarantine worktrees.

Reports SEPARATE facts (ancestor / unique commits / changed paths / heuristic
patch-equivalence) rather than a single diff. All worktree paths, branch names,
and work-order IDs come from VALIDATED domain/repo records — never user input.
Git is invoked with argument arrays (shell=False), repo-bound path validation,
timeouts, and output-size caps. Bounded summary only; no raw file bodies.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from operator_control.work_orders import list_work_orders

MAX_OUTPUT_BYTES = 64_000
MAX_PATHS = 200
GIT_TIMEOUT = 15
_WO_ID_RE = re.compile(r"^wo_[A-Za-z0-9_]+$")  # domain ID shape; rejects traversal/injection


def _failed_cp(msg: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode=1, stdout="", stderr=msg)


def _git(root: Path, *args: str, timeout: int = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    try:
        cp = subprocess.run(["git", "-C", str(root), *args],
                            capture_output=True, text=True, timeout=timeout)
        if cp.stdout and len(cp.stdout) > MAX_OUTPUT_BYTES:
            cp = subprocess.CompletedProcess(
                cp.args, cp.returncode,
                cp.stdout[:MAX_OUTPUT_BYTES] + "\n…[truncated]", cp.stderr)
        return cp
    except (subprocess.TimeoutExpired, OSError) as exc:
        return _failed_cp(str(exc))


def _valid_ids(root: Path) -> set[str]:
    try:
        return {o.get("work_order_id") for o in list_work_orders(root)
                if isinstance(o.get("work_order_id"), str)}
    except Exception:
        return set()


def _worktrees(root: Path) -> list[tuple[str, str]]:
    """(branch, worktree_path) for operator/* worktrees, from git porcelain only."""
    cp = _git(root, "worktree", "list", "--porcelain")
    out: list[tuple[str, str]] = []
    cur_path = None
    for line in cp.stdout.splitlines():
        if line.startswith("worktree "):
            cur_path = line[len("worktree "):].strip()
        elif line.startswith("branch ") and cur_path:
            br = line[len("branch "):].strip().removeprefix("refs/heads/")
            if br.startswith("operator/"):
                out.append((br, cur_path))
            cur_path = None
    return out


def _entry(root: Path, branch: str, worktree: str) -> dict[str, Any]:
    wo_id = branch.removeprefix("operator/")
    is_anc = _git(root, "merge-base", "--is-ancestor", branch, "main").returncode == 0
    uniq = _git(root, "rev-list", "--count", f"main..{branch}").stdout.strip()
    unique_commits = int(uniq) if uniq.isdigit() else 0
    mb = _git(root, "merge-base", "main", branch).stdout.strip()
    names = _git(root, "diff", "--name-only", f"{mb}..{branch}").stdout if mb else ""
    changed = [p for p in names.splitlines() if p][:MAX_PATHS]
    stat = (_git(root, "diff", "--stat", f"{mb}..{branch}").stdout if mb else "")[:MAX_OUTPUT_BYTES]
    # Heuristic patch-equivalence: git cherry marks '-' for commits already in main.
    cherry = _git(root, "cherry", "main", branch).stdout.splitlines()
    patch_equiv: bool | None
    if not cherry:
        patch_equiv = None
    else:
        patch_equiv = all(line.startswith("- ") for line in cherry if line.strip())
    return {
        "work_order_id": wo_id, "branch": branch, "worktree": worktree,
        "is_ancestor_of_main": is_anc, "unique_commits": unique_commits,
        "changed_paths": changed, "stat_summary": stat,
        "patch_equivalent_in_main": patch_equiv,  # heuristic, may be None
        "already_in_main": bool(is_anc or patch_equiv),
    }


def quarantine_inventory(root: str | Path) -> list[dict[str, Any]]:
    root = Path(root)
    valid = _valid_ids(root)
    inv: list[dict[str, Any]] = []
    for branch, worktree in _worktrees(root):
        wo_id = branch.removeprefix("operator/")
        if not _WO_ID_RE.match(wo_id) or (valid and wo_id not in valid):
            continue  # only validated, well-formed IDs
        # repo-bound path check
        try:
            rp = Path(worktree).resolve()
            if (root / ".worktrees").resolve() not in rp.parents and rp != (root / ".worktrees").resolve():
                continue
        except OSError:
            continue
        inv.append(_entry(root, branch, worktree))
    return inv


def quarantine_diff(root: str | Path, work_order_id: str) -> dict[str, Any]:
    root = Path(root)
    if not isinstance(work_order_id, str) or not _WO_ID_RE.match(work_order_id):
        return {"found": False, "stat": ""}
    if work_order_id not in _valid_ids(root):
        return {"found": False, "stat": ""}
    branch = f"operator/{work_order_id}"
    if _git(root, "rev-parse", "--verify", "--quiet", branch).returncode != 0:
        return {"found": False, "stat": ""}
    mb = _git(root, "merge-base", "main", branch).stdout.strip()
    stat = (_git(root, "diff", "--stat", f"{mb}..{branch}").stdout if mb else "")[:MAX_OUTPUT_BYTES]
    return {"found": True, "stat": stat}
