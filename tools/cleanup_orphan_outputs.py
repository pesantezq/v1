"""
One-shot cleanup for the parents[2] off-by-one regression in
watchlist_scanner default root resolution.

Between 2026-05-12 and 2026-05-15 (commit edf6a5ca through main), five
writers in watchlist_scanner/ defaulted their root to
``Path(__file__).resolve().parents[2]``, which is one level above the
repo root. Every production invocation that did not pass an explicit
``root=`` argument silently created an orphan ``<repo_parent>/outputs/``
tree (e.g. ``/opt/outputs/`` instead of ``/opt/stockbot/outputs/``) and
left the real outputs/latest/ stale.

The five writers are now fixed to use ``parents[1]``. This script removes
the orphan tree.

Safety:

- **Dry run is the default.** No filesystem changes happen without ``--confirm``.
- Refuses to delete the real outputs/ inside the repo. Compares resolved
  paths and asserts the orphan is *outside* the repo root.
- Records what it deleted to ``outputs/policy/cleanup_orphan_outputs.jsonl``
  (POLICY namespace) for audit.
- Does not touch logs, data/, config/, or anything other than the orphan
  ``outputs/`` directory.

Usage::

    python -m tools.cleanup_orphan_outputs                       # dry run (default)
    python -m tools.cleanup_orphan_outputs --confirm             # delete
    python -m tools.cleanup_orphan_outputs --repo-root /opt/stockbot
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Where the audit log is written (POLICY namespace, in the REAL repo).
_AUDIT_LOG_RELATIVE = ("outputs", "policy", "cleanup_orphan_outputs.jsonl")

# Marker file we expect at the repo root, used to validate the inferred repo
# root and refuse to operate against arbitrary directories.
_REPO_ROOT_MARKER = "main.py"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class CleanupResult:
    repo_root: Path
    orphan_root: Path
    orphan_exists: bool
    dry_run: bool
    items: list[Path] = field(default_factory=list)
    deleted: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    refused_reason: str | None = None

    @property
    def total_bytes(self) -> int:
        return sum(_dir_or_file_size(p) for p in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "tool": "cleanup_orphan_outputs",
            "repo_root": str(self.repo_root),
            "orphan_root": str(self.orphan_root),
            "orphan_exists": self.orphan_exists,
            "dry_run": self.dry_run,
            "refused_reason": self.refused_reason,
            "items_found": [str(p) for p in self.items],
            "items_deleted": [str(p) for p in self.deleted],
            "total_bytes": self.total_bytes,
            "errors": list(self.errors),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dir_or_file_size(path: Path) -> int:
    """Return total size of a file or directory (best-effort, no raise)."""
    try:
        if not path.exists():
            return 0
        if path.is_file():
            return path.stat().st_size
        total = 0
        for child in path.rglob("*"):
            try:
                if child.is_file():
                    total += child.stat().st_size
            except OSError:
                continue
        return total
    except OSError:
        return 0


def detect_repo_root(explicit: Path | str | None = None) -> Path:
    """
    Return the repo root.

    If *explicit* is given, validate it contains the repo marker and return it.
    Otherwise walk up from this file's location.  This script lives at
    ``<repo>/tools/cleanup_orphan_outputs.py`` so ``parents[1]`` is the repo
    root.

    Raises ``FileNotFoundError`` when the marker is absent — refuses to
    guess.
    """
    if explicit is not None:
        candidate = Path(explicit).resolve()
    else:
        candidate = Path(__file__).resolve().parents[1]
    marker = candidate / _REPO_ROOT_MARKER
    if not marker.exists():
        raise FileNotFoundError(
            f"Repo root marker {_REPO_ROOT_MARKER!r} not found in {candidate}. "
            "Pass --repo-root explicitly."
        )
    return candidate


def orphan_root_for(repo_root: Path) -> Path:
    """
    Return the orphan outputs directory created by the parents[2] bug.

    For ``repo_root = /opt/stockbot``, the bug wrote to ``/opt/outputs``.
    """
    return repo_root.parent / "outputs"


def find_orphan_items(orphan_root: Path) -> list[Path]:
    """
    List top-level entries inside *orphan_root*.  Empty list when the orphan
    tree does not exist (no cleanup needed).

    Returns paths in sorted order for stable reporting.
    """
    if not orphan_root.exists() or not orphan_root.is_dir():
        return []
    return sorted(orphan_root.iterdir())


def cleanup(
    *,
    repo_root: Path | str | None = None,
    confirm: bool = False,
) -> CleanupResult:
    """
    Remove the orphan ``<repo_parent>/outputs/`` tree.

    With ``confirm=False`` (default), no filesystem mutation occurs — the
    return value lists what *would* be deleted.

    Refuses to operate when:

    - the orphan path resolves *inside* the repo root
    - the orphan path resolves equal to the repo's real outputs/
    - the repo root marker is missing
    """
    root = detect_repo_root(repo_root)
    orphan = orphan_root_for(root).resolve()

    result = CleanupResult(
        repo_root=root,
        orphan_root=orphan,
        orphan_exists=orphan.exists(),
        dry_run=not confirm,
    )

    # Safety: orphan must be OUTSIDE the repo.
    try:
        if orphan.is_relative_to(root):
            result.refused_reason = (
                f"refusing to delete: orphan path {orphan} is inside repo root {root}"
            )
            return result
    except AttributeError:  # Python < 3.9 (defensive — repo requires 3.11+)
        # Fallback containment check
        try:
            orphan.relative_to(root)
            result.refused_reason = (
                f"refusing to delete: orphan path {orphan} is inside repo root {root}"
            )
            return result
        except ValueError:
            pass

    # Safety: orphan must NOT be the same as repo's real outputs.
    if orphan == (root / "outputs").resolve():
        result.refused_reason = (
            f"refusing to delete: orphan path {orphan} matches repo's real outputs/"
        )
        return result

    result.items = find_orphan_items(orphan)

    if not orphan.exists():
        return result

    if not confirm:
        # Dry run — nothing else to do.
        return result

    # Confirmed: remove each top-level entry. We remove children rather than
    # the orphan dir itself in case the parent directory is shared with
    # something we should not touch.
    for item in list(result.items):
        try:
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item)
            else:
                item.unlink(missing_ok=True)
            result.deleted.append(item)
        except OSError as exc:
            result.errors.append(f"failed to delete {item}: {exc}")

    # If orphan dir is now empty, remove it too (best-effort).
    try:
        if orphan.exists() and not any(orphan.iterdir()):
            orphan.rmdir()
    except OSError as exc:
        result.errors.append(f"failed to remove empty orphan root {orphan}: {exc}")

    # Audit log — append one JSONL line under POLICY namespace in the real repo.
    try:
        log_path = root.joinpath(*_AUDIT_LOG_RELATIVE)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(result.to_dict(), default=str) + "\n")
    except OSError as exc:
        result.errors.append(f"failed to write audit log: {exc}")

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tools.cleanup_orphan_outputs",
        description=(
            "Remove the orphan <repo_parent>/outputs/ directory created by the "
            "watchlist_scanner parents[2] off-by-one regression. Dry run by default."
        ),
    )
    p.add_argument(
        "--confirm", action="store_true",
        help="Actually delete. Without this flag the tool only reports.",
    )
    p.add_argument(
        "--repo-root", default=None,
        help="Repo root override. Default: directory above this file. "
             "Must contain main.py.",
    )
    p.add_argument(
        "--verbose", "-v", action="count", default=0,
        help="Increase logging verbosity.",
    )
    return p


def _print_report(result: CleanupResult) -> None:
    print(f"Repo root:    {result.repo_root}")
    print(f"Orphan root:  {result.orphan_root}")
    print(f"Orphan exists: {result.orphan_exists}")
    print(f"Dry run:      {result.dry_run}")
    if result.refused_reason:
        print(f"REFUSED:      {result.refused_reason}")
        return
    if not result.items:
        print("No orphan items found. Nothing to clean up.")
        return
    print(f"Items found ({len(result.items)}):")
    for item in result.items:
        kind = "DIR " if item.is_dir() else "FILE"
        print(f"  {kind} {item}  ({_dir_or_file_size(item)} bytes)")
    print(f"Total bytes:  {result.total_bytes}")
    if result.dry_run:
        print()
        print("Dry run — no files removed. Re-run with --confirm to delete.")
    else:
        print()
        print(f"Deleted ({len(result.deleted)}):")
        for item in result.deleted:
            print(f"  {item}")
        if result.errors:
            print()
            print("Errors:")
            for err in result.errors:
                print(f"  {err}")


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    level = logging.WARNING
    if args.verbose >= 2:
        level = logging.DEBUG
    elif args.verbose == 1:
        level = logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        result = cleanup(repo_root=args.repo_root, confirm=args.confirm)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    _print_report(result)
    return 0 if not result.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
