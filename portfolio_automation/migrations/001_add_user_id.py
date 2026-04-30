"""
Migration 001 — Add user_id to core state tables.

Adds  user_id TEXT NOT NULL DEFAULT 'owner'  to:
    run_history
    snapshots

recommendation_history is NOT a SQLite table (it is a JSONL file at
outputs/policy/recommendation_history.jsonl) and is always recorded
in MigrationResult.tables_skipped.

Safety contract
---------------
- Creates a timestamped backup of the database before any writes.
- All DDL + DML run inside a single explicit transaction; fully rolled
  back on any error.
- Idempotent: PRAGMA table_info() is checked before every ALTER TABLE.
- Does not touch live scoring, allocation, recommendations, or replay.
- Single-user behavior is preserved: all new/existing rows default to
  user_id = 'owner'.

Usage
-----
CLI (note: leading digit means -m flag cannot be used):
    python portfolio_automation/migrations/001_add_user_id.py
    python portfolio_automation/migrations/001_add_user_id.py --db-path /custom/path/portfolio.db

Programmatic (via importlib, since the filename starts with a digit):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "migration_001",
        Path("portfolio_automation/migrations/001_add_user_id.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    result = mod.migrate(Path("data/portfolio.db"))
"""
from __future__ import annotations

import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

_OWNER_DEFAULT = "owner"
_COLUMN = "user_id"

# Tables that get user_id.  Order matters for rollback messaging.
_TARGET_TABLES: tuple[str, ...] = ("run_history", "snapshots")

# recommendation_history is a JSONL file, never a SQLite table.
_KNOWN_NON_TABLES: frozenset[str] = frozenset({"recommendation_history"})


@dataclass
class MigrationResult:
    """Returned by migrate() to describe what happened."""
    tables_modified: list[str] = field(default_factory=list)
    tables_skipped: list[str] = field(default_factory=list)
    rows_backfilled: dict[str, int] = field(default_factory=dict)
    duration_ms: int = 0


class MigrationError(Exception):
    """Raised when migration fails.  Original exception is chained via __cause__."""

    def __init__(
        self,
        message: str,
        table: str | None = None,
    ) -> None:
        super().__init__(message)
        self.table = table


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    return column in cols


def _backup(db_path: Path) -> Path:
    """
    Copy db_path to {db_path.parent}/backups/{stem}_pre_001_{utc_ts}.db.

    Returns the backup path.  Raises OSError on failure — the caller treats
    any failure here as fatal (abort before any writes).
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{db_path.stem}_pre_001_{ts}.db"
    shutil.copy2(str(db_path), str(backup_path))
    return backup_path


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def migrate(db_path: Path) -> MigrationResult:
    """
    Add user_id TEXT NOT NULL DEFAULT 'owner' to run_history and snapshots.

    Steps
    -----
    1. Validate db_path exists.
    2. Create a timestamped backup (abort if backup fails).
    3. Determine which target tables still need the column.
    4. Run all pending ALTER TABLE + UPDATE + CREATE INDEX in one transaction.
    5. Return MigrationResult.

    Raises
    ------
    MigrationError
        If the backup fails, the database does not exist, or any SQL step
        fails (the transaction is rolled back before raising).
    """
    start = time.monotonic()
    result = MigrationResult()

    # recommendation_history is never a SQLite table — always skipped
    result.tables_skipped.extend(_KNOWN_NON_TABLES)

    if not db_path.exists():
        raise MigrationError(
            f"Database not found: {db_path!r}. "
            "Start the application once to create it before running migrations."
        )

    # ── Step 1: Backup before any writes ─────────────────────────────────────
    try:
        backup_path = _backup(db_path)
    except Exception as exc:
        raise MigrationError(
            f"Backup failed — aborting migration before any writes. "
            f"Reason: {exc}"
        ) from exc

    # ── Step 2: Determine pending tables ─────────────────────────────────────
    conn = sqlite3.connect(str(db_path))
    conn.isolation_level = None  # manual transaction control
    try:
        pending: list[str] = []
        for table in _TARGET_TABLES:
            if not _table_exists(conn, table):
                result.tables_skipped.append(table)
                continue
            if _column_exists(conn, table, _COLUMN):
                result.tables_skipped.append(table)
                continue
            pending.append(table)

        if not pending:
            result.duration_ms = int((time.monotonic() - start) * 1000)
            return result

        # ── Step 3: All pending changes in one transaction ────────────────────
        current_table: str | None = None
        try:
            conn.execute("BEGIN")

            for table in pending:
                current_table = table

                # Count pre-existing rows before ALTER so we can report how many
                # rows now carry the new column (modern SQLite 3.37+ stores the
                # default at schema level, so changes() after the UPDATE is 0).
                pre_existing: int = conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]

                conn.execute(
                    f"ALTER TABLE {table} "
                    f"ADD COLUMN {_COLUMN} TEXT NOT NULL DEFAULT '{_OWNER_DEFAULT}'"
                )
                # Ensure any rows that somehow received NULL (older SQLite <3.37)
                # are explicitly set to the default value.
                conn.execute(
                    f"UPDATE {table} SET {_COLUMN} = ? WHERE {_COLUMN} IS NULL",
                    (_OWNER_DEFAULT,),
                )

                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_{_COLUMN} "
                    f"ON {table}({_COLUMN})"
                )
                result.tables_modified.append(table)
                result.rows_backfilled[table] = pre_existing

            conn.execute("COMMIT")

        except Exception as exc:
            # Full rollback — both DDL and DML are undone
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            # Clear partial result; the transaction was rolled back
            result.tables_modified.clear()
            result.rows_backfilled.clear()
            raise MigrationError(
                f"Migration failed while processing table {current_table!r}. "
                f"Transaction rolled back. Backup is at {backup_path!r}. "
                f"Original error: {exc}",
                table=current_table,
            ) from exc

    finally:
        conn.close()

    result.duration_ms = int((time.monotonic() - start) * 1000)
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main() -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Migration 001 — add user_id to run_history and snapshots"
    )
    parser.add_argument(
        "--db-path",
        default="data/portfolio.db",
        help="Path to portfolio.db (default: data/portfolio.db)",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path)
    print(f"Migration 001 — add user_id")
    print(f"  Database : {db_path.resolve()}")
    print()

    try:
        result = migrate(db_path)
    except MigrationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    if result.tables_modified:
        print(f"  Modified : {result.tables_modified}")
        for table, count in result.rows_backfilled.items():
            print(f"    {table}: {count} rows backfilled to '{_OWNER_DEFAULT}'")
    else:
        print("  Modified : (none — already up to date)")

    if result.tables_skipped:
        print(f"  Skipped  : {result.tables_skipped}")

    print(f"  Duration : {result.duration_ms} ms")
    print()
    print("Migration 001 complete.")


if __name__ == "__main__":
    _main()
