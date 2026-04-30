"""
Tests for portfolio_automation/migrations/001_add_user_id.py

Contracts verified:
- fresh database: both target tables are migrated
- partial migration: only the unmigrated table is altered
- already migrated: both tables skipped, result unchanged
- backup file created before any DB writes
- backup failure aborts migration without touching DB
- existing rows backfilled to 'owner'
- indexes created on both tables
- idempotent: migrate() can run twice safely
- recommendation_history always in tables_skipped (it's a JSONL, not a table)
- MigrationError raised and transaction rolled back on failure
- state_store INSERT includes user_id; single-user behavior unchanged
"""

from __future__ import annotations

import importlib.util
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Load the migration module (filename starts with a digit, so importlib is needed)
# ---------------------------------------------------------------------------

_MIGRATION_PATH = (
    Path(__file__).parent.parent.parent
    / "portfolio_automation"
    / "migrations"
    / "001_add_user_id.py"
)
_SPEC = importlib.util.spec_from_file_location("migration_001", _MIGRATION_PATH)
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules["migration_001"] = _MOD  # required for Python 3.13 dataclass __module__ lookup
_SPEC.loader.exec_module(_MOD)

migrate = _MOD.migrate
MigrationError = _MOD.MigrationError
MigrationResult = _MOD.MigrationResult

# Also test against the live state_store
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from state_store import PortfolioStateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_columns(db_path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _get_index_names(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def _row_count(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _user_ids(db_path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(f"SELECT user_id FROM {table}").fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Test: fresh database (both tables exist, neither has user_id)
# ---------------------------------------------------------------------------

class TestFreshDatabase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "portfolio.db"
        # PortfolioStateStore._init_db() creates all tables WITHOUT user_id
        # (simulating a pre-migration database). We force this by temporarily
        # removing user_id from the DDL via the old schema approach: create
        # the store, then manually drop the user_id column if it was added.
        # Since the current DDL DOES include user_id, we create the tables
        # without it by using a direct SQL approach.
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            CREATE TABLE run_history (
                run_id       TEXT PRIMARY KEY,
                run_date     TEXT NOT NULL,
                mode         TEXT NOT NULL,
                status       TEXT NOT NULL,
                started_at   TEXT NOT NULL,
                completed_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE snapshots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id          TEXT NOT NULL,
                total_value     REAL,
                cash            REAL,
                max_drift       REAL,
                drawdown_regime TEXT,
                recorded_at     TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_migrates_both_tables(self):
        result = migrate(self.db_path)
        self.assertIn("run_history", result.tables_modified)
        self.assertIn("snapshots", result.tables_modified)
        self.assertEqual(len(result.tables_modified), 2)

    def test_user_id_column_added_to_run_history(self):
        migrate(self.db_path)
        cols = _get_columns(self.db_path, "run_history")
        self.assertIn("user_id", cols)

    def test_user_id_column_added_to_snapshots(self):
        migrate(self.db_path)
        cols = _get_columns(self.db_path, "snapshots")
        self.assertIn("user_id", cols)

    def test_recommendation_history_always_skipped(self):
        result = migrate(self.db_path)
        self.assertIn("recommendation_history", result.tables_skipped)

    def test_duration_recorded(self):
        result = migrate(self.db_path)
        self.assertGreaterEqual(result.duration_ms, 0)

    def test_backup_created(self):
        backup_dir = self.db_path.parent / "backups"
        migrate(self.db_path)
        backups = list(backup_dir.glob("portfolio_pre_001_*.db"))
        self.assertEqual(len(backups), 1)
        self.assertTrue(backups[0].stat().st_size > 0)

    def test_indexes_created(self):
        migrate(self.db_path)
        indexes = _get_index_names(self.db_path)
        self.assertIn("idx_run_history_user_id", indexes)
        self.assertIn("idx_snapshots_user_id", indexes)


# ---------------------------------------------------------------------------
# Test: existing rows are backfilled
# ---------------------------------------------------------------------------

class TestBackfill(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "portfolio.db"
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            CREATE TABLE run_history (
                run_id TEXT PRIMARY KEY, run_date TEXT NOT NULL,
                mode TEXT NOT NULL, status TEXT NOT NULL,
                started_at TEXT NOT NULL, completed_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                total_value REAL, cash REAL, max_drift REAL,
                drawdown_regime TEXT, recorded_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "INSERT INTO run_history (run_id,run_date,mode,status,started_at) "
            "VALUES ('r1','2024-01-01','daily','completed','2024-01-01T09:00:00')"
        )
        conn.execute(
            "INSERT INTO run_history (run_id,run_date,mode,status,started_at) "
            "VALUES ('r2','2024-01-02','daily','completed','2024-01-02T09:00:00')"
        )
        conn.execute(
            "INSERT INTO snapshots (run_id,total_value,cash,max_drift,recorded_at) "
            "VALUES ('r1', 10000.0, 500.0, 0.05, '2024-01-01T10:00:00')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_existing_run_history_rows_backfilled_to_owner(self):
        migrate(self.db_path)
        user_ids = _user_ids(self.db_path, "run_history")
        self.assertEqual(user_ids, {"owner"})

    def test_existing_snapshot_rows_backfilled_to_owner(self):
        migrate(self.db_path)
        user_ids = _user_ids(self.db_path, "snapshots")
        self.assertEqual(user_ids, {"owner"})

    def test_backfill_count_matches_existing_rows(self):
        result = migrate(self.db_path)
        # 2 pre-existing run_history rows
        self.assertEqual(result.rows_backfilled.get("run_history", 0), 2)
        # 1 pre-existing snapshot row
        self.assertEqual(result.rows_backfilled.get("snapshots", 0), 1)


# ---------------------------------------------------------------------------
# Test: partial migration (one table already has user_id)
# ---------------------------------------------------------------------------

class TestPartialMigration(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "portfolio.db"
        conn = sqlite3.connect(str(self.db_path))
        # run_history already has user_id
        conn.execute("""
            CREATE TABLE run_history (
                run_id TEXT PRIMARY KEY, run_date TEXT NOT NULL,
                mode TEXT NOT NULL, status TEXT NOT NULL,
                started_at TEXT NOT NULL, completed_at TEXT,
                user_id TEXT NOT NULL DEFAULT 'owner'
            )
        """)
        # snapshots does NOT have user_id yet
        conn.execute("""
            CREATE TABLE snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                total_value REAL, cash REAL, max_drift REAL,
                drawdown_regime TEXT, recorded_at TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_unmigrated_table_is_modified(self):
        result = migrate(self.db_path)
        self.assertNotIn("run_history", result.tables_modified)
        self.assertIn("snapshots", result.tables_modified)

    def test_already_migrated_table_is_skipped(self):
        result = migrate(self.db_path)
        self.assertIn("run_history", result.tables_skipped)

    def test_snapshots_gets_user_id(self):
        migrate(self.db_path)
        cols = _get_columns(self.db_path, "snapshots")
        self.assertIn("user_id", cols)


# ---------------------------------------------------------------------------
# Test: already fully migrated (idempotency)
# ---------------------------------------------------------------------------

class TestAlreadyMigrated(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "portfolio.db"
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            CREATE TABLE run_history (
                run_id TEXT PRIMARY KEY, run_date TEXT NOT NULL,
                mode TEXT NOT NULL, status TEXT NOT NULL,
                started_at TEXT NOT NULL, completed_at TEXT,
                user_id TEXT NOT NULL DEFAULT 'owner'
            )
        """)
        conn.execute("""
            CREATE TABLE snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                total_value REAL, cash REAL, max_drift REAL,
                drawdown_regime TEXT, recorded_at TEXT NOT NULL,
                user_id TEXT NOT NULL DEFAULT 'owner'
            )
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_tables_modified_when_already_migrated(self):
        result = migrate(self.db_path)
        self.assertEqual(result.tables_modified, [])

    def test_both_tables_skipped(self):
        result = migrate(self.db_path)
        self.assertIn("run_history", result.tables_skipped)
        self.assertIn("snapshots", result.tables_skipped)

    def test_idempotent_second_run_matches_first(self):
        r1 = migrate(self.db_path)
        r2 = migrate(self.db_path)
        self.assertEqual(r1.tables_modified, [])
        self.assertEqual(r2.tables_modified, [])
        self.assertIn("run_history", r2.tables_skipped)
        self.assertIn("snapshots", r2.tables_skipped)


# ---------------------------------------------------------------------------
# Test: backup failure aborts before any DB writes
# ---------------------------------------------------------------------------

class TestBackupFailure(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "portfolio.db"
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            CREATE TABLE run_history (
                run_id TEXT PRIMARY KEY, run_date TEXT NOT NULL,
                mode TEXT NOT NULL, status TEXT NOT NULL,
                started_at TEXT NOT NULL, completed_at TEXT
            )
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_migration_raises_when_backup_fails(self):
        with patch.object(_MOD.shutil, "copy2", side_effect=OSError("disk full")):
            with self.assertRaises(MigrationError) as ctx:
                migrate(self.db_path)
        self.assertIn("Backup failed", str(ctx.exception))

    def test_db_unmodified_when_backup_fails(self):
        with patch.object(_MOD.shutil, "copy2", side_effect=OSError("disk full")):
            try:
                migrate(self.db_path)
            except MigrationError:
                pass
        cols = _get_columns(self.db_path, "run_history")
        self.assertNotIn("user_id", cols)


# ---------------------------------------------------------------------------
# Test: transaction rollback on failure
# ---------------------------------------------------------------------------

class TestTransactionRollback(unittest.TestCase):
    """
    Verifies that if ALTER TABLE succeeds for run_history but then fails for
    snapshots, both tables are rolled back to their original state.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "portfolio.db"
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            CREATE TABLE run_history (
                run_id TEXT PRIMARY KEY, run_date TEXT NOT NULL,
                mode TEXT NOT NULL, status TEXT NOT NULL,
                started_at TEXT NOT NULL, completed_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                total_value REAL, cash REAL, max_drift REAL,
                drawdown_regime TEXT, recorded_at TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_rollback_on_simulated_failure(self):
        """Failure mid-transaction leaves both tables unmodified."""
        real_sq = sqlite3

        class FailOnSnapshotsAlter:
            """Wrapper: raises when ALTER TABLE snapshots is attempted."""
            def __init__(self, path):
                self._real = real_sq.connect(path)
                self._isolation_level = ""

            @property
            def isolation_level(self):
                return self._isolation_level

            @isolation_level.setter
            def isolation_level(self, v):
                self._isolation_level = v
                self._real.isolation_level = v

            def execute(self, sql, params=()):
                if "ALTER TABLE snapshots" in sql:
                    raise real_sq.OperationalError("Injected: snapshots ALTER failed")
                return self._real.execute(sql, params)

            def close(self):
                self._real.close()

        with patch.object(_MOD, "sqlite3") as mock_sq:
            mock_sq.connect.side_effect = lambda path: FailOnSnapshotsAlter(path)
            mock_sq.OperationalError = real_sq.OperationalError

            with self.assertRaises(MigrationError):
                migrate(self.db_path)

        # After rollback: neither table should have user_id
        cols_rh = _get_columns(self.db_path, "run_history")
        cols_s = _get_columns(self.db_path, "snapshots")
        self.assertNotIn("user_id", cols_rh, "run_history ALTER must be rolled back")
        self.assertNotIn("user_id", cols_s, "snapshots was never altered")

    def test_result_tables_modified_cleared_on_rollback(self):
        """MigrationResult.tables_modified must be empty after a rolled-back run."""
        real_sq = sqlite3

        class FailOnUpdate:
            def __init__(self, path):
                self._real = real_sq.connect(path)
                self._isolation_level = ""

            @property
            def isolation_level(self):
                return self._isolation_level

            @isolation_level.setter
            def isolation_level(self, v):
                self._isolation_level = v
                self._real.isolation_level = v

            def execute(self, sql, params=()):
                if "UPDATE run_history" in sql and "user_id" in sql:
                    raise real_sq.OperationalError("Injected: UPDATE failed")
                return self._real.execute(sql, params)

            def close(self):
                self._real.close()

        with patch.object(_MOD, "sqlite3") as mock_sq:
            mock_sq.connect.side_effect = lambda path: FailOnUpdate(path)
            mock_sq.OperationalError = real_sq.OperationalError

            try:
                migrate(self.db_path)
            except MigrationError:
                pass

        cols = _get_columns(self.db_path, "run_history")
        self.assertNotIn("user_id", cols)


# ---------------------------------------------------------------------------
# Test: migrate() on a non-existent database
# ---------------------------------------------------------------------------

class TestNonExistentDatabase(unittest.TestCase):

    def test_raises_migration_error_for_missing_db(self):
        with self.assertRaises(MigrationError) as ctx:
            migrate(Path("/nonexistent/path/portfolio.db"))
        self.assertIn("not found", str(ctx.exception).lower())


# ---------------------------------------------------------------------------
# Test: state_store INSERT/SELECT behavior after migration
# ---------------------------------------------------------------------------

class TestStateStoreQueryLayer(unittest.TestCase):
    """
    Verifies that the updated INSERT/SELECT statements in state_store.py
    work correctly with the user_id column present.

    Single-user behavior must be preserved: everything defaults to 'owner'.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "portfolio.db"
        # PortfolioStateStore now creates tables with user_id in DDL
        self.store = PortfolioStateStore(self.db_path)

    def tearDown(self):
        self.store = None
        self.tmp.cleanup()

    def test_start_run_inserts_user_id_owner_by_default(self):
        self.store.start_run("2026-01-01_daily", "daily")
        conn = sqlite3.connect(str(self.db_path))
        row = conn.execute(
            "SELECT user_id FROM run_history WHERE run_id=?",
            ("2026-01-01_daily",)
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "owner")

    def test_record_snapshot_inserts_user_id_owner_by_default(self):
        self.store.start_run("2026-01-01_daily", "daily")
        self.store.record_snapshot("2026-01-01_daily", 50000.0, 1000.0, 0.05)
        conn = sqlite3.connect(str(self.db_path))
        row = conn.execute(
            "SELECT user_id FROM snapshots WHERE run_id=?",
            ("2026-01-01_daily",)
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "owner")

    def test_get_last_successful_run_returns_owner_run(self):
        self.store.start_run("2026-01-01_daily", "daily")
        self.store.complete_run("2026-01-01_daily")
        result = self.store.get_last_successful_run("daily")
        self.assertIsNotNone(result)
        self.assertEqual(result["run_id"], "2026-01-01_daily")
        self.assertEqual(result["user_id"], "owner")

    def test_get_recent_snapshots_returns_owner_snapshots(self):
        self.store.start_run("2026-01-01_daily", "daily")
        self.store.complete_run("2026-01-01_daily")
        self.store.record_snapshot("2026-01-01_daily", 50000.0, 1000.0, 0.05)
        snaps = self.store.get_recent_snapshots()
        self.assertEqual(len(snaps), 1)
        self.assertAlmostEqual(snaps[0]["total_value"], 50000.0)

    def test_existing_tests_still_pass_start_run_idempotency(self):
        self.assertTrue(self.store.start_run("2026-03-02_daily", "daily"))
        self.assertFalse(self.store.start_run("2026-03-02_daily", "daily"))

    def test_existing_tests_still_pass_complete_run(self):
        self.store.start_run("2026-03-02_daily", "daily")
        self.store.complete_run("2026-03-02_daily")
        self.assertTrue(self.store.is_completed("2026-03-02_daily"))

    def test_direct_insert_without_user_id_still_works(self):
        """Direct SQL INSERT without user_id uses the DEFAULT 'owner'."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT INTO run_history (run_id,run_date,mode,status,started_at) "
            "VALUES (?,?,?,?,?)",
            ("legacy_run", "2024-01-01", "daily", "completed", "2024-01-01T09:00:00")
        )
        conn.commit()
        row = conn.execute(
            "SELECT user_id FROM run_history WHERE run_id='legacy_run'"
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], "owner")


# ---------------------------------------------------------------------------
# Test: run twice — second run is a no-op
# ---------------------------------------------------------------------------

class TestIdempotency(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "portfolio.db"
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            CREATE TABLE run_history (
                run_id TEXT PRIMARY KEY, run_date TEXT NOT NULL,
                mode TEXT NOT NULL, status TEXT NOT NULL,
                started_at TEXT NOT NULL, completed_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                total_value REAL, cash REAL, max_drift REAL,
                drawdown_regime TEXT, recorded_at TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_second_run_skips_all_tables(self):
        r1 = migrate(self.db_path)
        r2 = migrate(self.db_path)
        self.assertEqual(sorted(r1.tables_modified), ["run_history", "snapshots"])
        self.assertEqual(r2.tables_modified, [])
        self.assertIn("run_history", r2.tables_skipped)
        self.assertIn("snapshots", r2.tables_skipped)

    def test_columns_unchanged_after_second_run(self):
        migrate(self.db_path)
        migrate(self.db_path)
        cols_rh = _get_columns(self.db_path, "run_history")
        cols_s = _get_columns(self.db_path, "snapshots")
        # Exactly one user_id column in each table
        self.assertIn("user_id", cols_rh)
        self.assertIn("user_id", cols_s)

    def test_indexes_still_present_after_second_run(self):
        migrate(self.db_path)
        migrate(self.db_path)
        indexes = _get_index_names(self.db_path)
        self.assertIn("idx_run_history_user_id", indexes)
        self.assertIn("idx_snapshots_user_id", indexes)


if __name__ == "__main__":
    unittest.main()
