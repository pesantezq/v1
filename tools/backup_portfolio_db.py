"""
Backup `data/portfolio.db` via SQLite's online-backup API.

Cron-ready single command:

    python -m tools.backup_portfolio_db                # default: keep 30
    python -m tools.backup_portfolio_db --retain 14    # keep 14 days
    python -m tools.backup_portfolio_db --dry-run      # show what would happen

Behaviour:

  - Uses ``sqlite3.Connection.backup()`` which is safe while writers are
    active.  No need to stop the daily pipeline.
  - Writes to ``outputs/policy/db_backups/portfolio.db.YYYY-MM-DD.sqlite``.
    POLICY namespace so it sits alongside other audit artifacts.
  - Atomic-via-rename: writes to ``*.partial`` then ``os.replace`` to the
    final name.  An interrupted backup never leaves a half-written file
    in the canonical name.
  - Same-day re-runs overwrite the day's backup.
  - Retention: deletes ``portfolio.db.YYYY-MM-DD.sqlite`` files older
    than --retain days.  Never touches files outside that name pattern.
  - Status artifact: appends one JSONL row to
    ``outputs/policy/db_backups_log.jsonl`` with the result of each run.
  - Read-only with respect to the live database; the only writes are to
    the backup directory and the log.

Exit codes:
  0  success (backup written and any retention deletions completed)
  1  database not found, source DB unreadable, or write failure
  2  invalid CLI arguments
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_REPO_ROOT_MARKER = "main.py"
_BACKUP_NAME_RE = re.compile(r"^portfolio\.db\.(\d{4}-\d{2}-\d{2})\.sqlite$")


@dataclass
class BackupResult:
    success: bool
    source_path: str
    backup_path: str
    backup_bytes: int
    retained_count: int
    deleted: list[str]
    dry_run: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "tool": "backup_portfolio_db",
            "success": self.success,
            "source_path": self.source_path,
            "backup_path": self.backup_path,
            "backup_bytes": self.backup_bytes,
            "retained_count": self.retained_count,
            "deleted": list(self.deleted),
            "dry_run": self.dry_run,
            "error": self.error,
        }


def detect_repo_root(explicit: Path | str | None = None) -> Path:
    if explicit is not None:
        candidate = Path(explicit).resolve()
    else:
        candidate = Path(__file__).resolve().parents[1]
    if not (candidate / _REPO_ROOT_MARKER).exists():
        raise FileNotFoundError(
            f"Repo root marker {_REPO_ROOT_MARKER!r} not found in {candidate}. "
            "Pass --repo-root explicitly."
        )
    return candidate


def _backup_dir(repo_root: Path) -> Path:
    return repo_root / "outputs" / "policy" / "db_backups"


def _log_path(repo_root: Path) -> Path:
    return repo_root / "outputs" / "policy" / "db_backups_log.jsonl"


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _backup_one(source: Path, dest: Path) -> int:
    """Copy *source* SQLite DB to *dest* using the online backup API.

    Returns the byte size of the destination file. Caller handles the
    atomic-rename dance via a .partial intermediate.
    """
    partial = dest.with_suffix(dest.suffix + ".partial")
    if partial.exists():
        partial.unlink()
    src_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst_conn = sqlite3.connect(str(partial))
        try:
            src_conn.backup(dst_conn)
            dst_conn.commit()
        finally:
            dst_conn.close()
    finally:
        src_conn.close()
    os.replace(partial, dest)
    return dest.stat().st_size


def _prune(backup_dir: Path, retain: int) -> list[str]:
    """
    Delete portfolio.db.YYYY-MM-DD.sqlite files older than the most recent
    *retain* days.  Returns the list of deleted paths.

    Only files matching the canonical name pattern are touched; everything
    else in the directory is left alone.
    """
    if not backup_dir.exists() or retain < 1:
        return []
    candidates: list[tuple[str, Path]] = []
    for f in backup_dir.iterdir():
        m = _BACKUP_NAME_RE.match(f.name)
        if not m or not f.is_file():
            continue
        candidates.append((m.group(1), f))
    # Newest first by date string (ISO-sortable)
    candidates.sort(key=lambda t: t[0], reverse=True)
    deleted: list[str] = []
    for _, path in candidates[retain:]:
        try:
            path.unlink()
            deleted.append(str(path))
        except OSError as exc:
            logger.warning("could not delete %s: %s", path, exc)
    return deleted


def _append_log(repo_root: Path, result: BackupResult) -> None:
    log = _log_path(repo_root)
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(result.to_dict(), default=str) + "\n")
    except OSError as exc:
        logger.warning("could not append to %s: %s", log, exc)


def backup(
    *,
    repo_root: Path | str | None = None,
    retain: int = 30,
    dry_run: bool = False,
) -> BackupResult:
    """Perform one backup. Never raises."""
    try:
        root = detect_repo_root(repo_root)
    except FileNotFoundError as exc:
        return BackupResult(
            success=False, source_path="?", backup_path="?", backup_bytes=0,
            retained_count=0, deleted=[], dry_run=dry_run, error=str(exc),
        )

    source = root / "data" / "portfolio.db"
    if not source.exists():
        return BackupResult(
            success=False, source_path=str(source), backup_path="?",
            backup_bytes=0, retained_count=0, deleted=[], dry_run=dry_run,
            error=f"source DB not found: {source}",
        )

    backup_dir = _backup_dir(root)
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"portfolio.db.{_today_iso()}.sqlite"

    if dry_run:
        # Count current backups, simulate retention
        existing = [f for f in backup_dir.iterdir() if _BACKUP_NAME_RE.match(f.name)]
        return BackupResult(
            success=True, source_path=str(source), backup_path=str(dest),
            backup_bytes=0,
            retained_count=min(retain, len(existing) + 1),
            deleted=[], dry_run=True,
        )

    try:
        size = _backup_one(source, dest)
    except (sqlite3.Error, OSError) as exc:
        result = BackupResult(
            success=False, source_path=str(source), backup_path=str(dest),
            backup_bytes=0, retained_count=0, deleted=[], dry_run=False,
            error=f"backup_failed: {exc}",
        )
        _append_log(root, result)
        return result

    deleted = _prune(backup_dir, retain)
    retained = max(
        0,
        len([f for f in backup_dir.iterdir() if _BACKUP_NAME_RE.match(f.name)]),
    )
    result = BackupResult(
        success=True, source_path=str(source), backup_path=str(dest),
        backup_bytes=size, retained_count=retained, deleted=deleted,
        dry_run=False,
    )
    _append_log(root, result)
    return result


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tools.backup_portfolio_db",
        description=(
            "Back up data/portfolio.db via SQLite online-backup API. Writes "
            "to outputs/policy/db_backups/portfolio.db.YYYY-MM-DD.sqlite and "
            "appends a status row to outputs/policy/db_backups_log.jsonl."
        ),
    )
    p.add_argument(
        "--retain", type=int, default=30,
        help="Keep at most N daily backups. Older ones are deleted. Default 30.",
    )
    p.add_argument(
        "--repo-root", default=None,
        help="Repo root override. Default: directory above this file.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Plan only; do not write any files.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    if args.retain < 1:
        print("ERROR: --retain must be >= 1", file=sys.stderr)
        return 2
    result = backup(repo_root=args.repo_root, retain=args.retain, dry_run=args.dry_run)
    print(json.dumps(result.to_dict(), indent=2, default=str))
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
