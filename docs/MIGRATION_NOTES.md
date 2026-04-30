# Migration Notes

## Migration 001 — Add `user_id` to core state tables

**File:** `portfolio_automation/migrations/001_add_user_id.py`  
**Applied to:** `run_history`, `snapshots`  
**Date added:** 2026-04-30  
**Status:** Stable

### What it does

Adds `user_id TEXT NOT NULL DEFAULT 'owner'` to the two core SQLite state tables:

| Table | Column added | Index created |
|-------|-------------|---------------|
| `run_history` | `user_id TEXT NOT NULL DEFAULT 'owner'` | `idx_run_history_user_id` |
| `snapshots` | `user_id TEXT NOT NULL DEFAULT 'owner'` | `idx_snapshots_user_id` |

`recommendation_history` is a JSONL file (`outputs/policy/recommendation_history.jsonl`), not a
SQLite table, and is always listed in `MigrationResult.tables_skipped`.

### Safety contract

- Creates a timestamped backup before any writes:
  `{db_path.parent}/backups/{stem}_pre_001_{utc_ts}.db`
- All DDL + DML run inside a single explicit transaction; fully rolled back on any error.
- Idempotent: `PRAGMA table_info()` checked before every `ALTER TABLE`; already-migrated tables are
  skipped, not re-altered.
- Does not touch live scoring, allocation, recommendations, or replay.
- Single-user behavior is preserved: all existing and new rows default to `user_id = 'owner'`.

### Running the migration

**Existing databases (one-time, operator-run):**

```bash
python portfolio_automation/migrations/001_add_user_id.py
# or with a custom path:
python portfolio_automation/migrations/001_add_user_id.py --db-path /path/to/portfolio.db
```

**Fresh installs:** No action needed. `PortfolioStateStore._init_db()` creates tables with
`user_id` already in the DDL.

**Startup auto-migration (fallback):** `_init_db()` also contains a guarded `ALTER TABLE` block
that adds `user_id` if missing — this covers any DB that was started from older code without
running migration 001 explicitly.

### Programmatic usage (importlib, because filename starts with a digit)

```python
import importlib.util, sys
from pathlib import Path

path = Path("portfolio_automation/migrations/001_add_user_id.py")
spec = importlib.util.spec_from_file_location("migration_001", path)
mod = importlib.util.module_from_spec(spec)
sys.modules["migration_001"] = mod  # required for Python 3.13 dataclass resolution
spec.loader.exec_module(mod)

result = mod.migrate(Path("data/portfolio.db"))
print(result.tables_modified, result.rows_backfilled)
```

### Return value

`migrate()` returns a `MigrationResult` dataclass:

| Field | Type | Description |
|-------|------|-------------|
| `tables_modified` | `list[str]` | Tables that had `user_id` added |
| `tables_skipped` | `list[str]` | Tables already migrated, non-existent, or non-table |
| `rows_backfilled` | `dict[str, int]` | Pre-existing row count per modified table |
| `duration_ms` | `int` | Wall-clock duration in milliseconds |

Note: `rows_backfilled` reports the number of pre-existing rows at migration time (not the
`changes()` count from `UPDATE`, which is 0 in SQLite 3.37+ because the default is stored at
schema level rather than per-row).

### Query-layer changes in `state_store.py`

All methods that INSERT or SELECT from the affected tables now accept and filter by `user_id`,
defaulting to `"owner"` so existing call sites require no changes:

| Method | Change |
|--------|--------|
| `start_run(run_id, mode, user_id="owner")` | `user_id` added to INSERT |
| `record_snapshot(..., user_id="owner")` | `user_id` added to INSERT |
| `get_last_successful_run(mode, user_id="owner")` | `AND user_id=?` added to WHERE |
| `get_recent_snapshots(mode, n, user_id="owner")` | `AND s.user_id=?` added to WHERE |
| `check_run_status(run_id)` | Unchanged — `run_id` is a global PK; no user scope needed |

Aggregate queries in `policy_evaluator/outcome_attributor.py` that span all rows have a
`# TODO(v2-user-scope)` comment marking where per-user filtering would be added.

### Tests

```bash
python -m pytest -q tests/migrations/test_001_add_user_id.py
# 31 tests covering: fresh DB, backfill, partial migration, already migrated,
# backup failure, transaction rollback, nonexistent DB, state_store query layer, idempotency
```

### Out of scope for this migration

- Authentication, sessions, or login UI
- GUI changes
- Scoring, allocation, or recommendation logic
- Multi-user access control (Phase 0 lays the schema groundwork only)

### Recommended next step

Phase 0 is complete. If multi-user support is introduced in a future phase, the query-layer
groundwork (user_id column + index + filtered queries) is already in place. The recommended next
step is to add a `users` table and wire session identity to `user_id` at that time.
