from __future__ import annotations
import sqlite3
from pathlib import Path
from typing import Optional

_DDL = """
CREATE TABLE IF NOT EXISTS api_usage_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    run_mode TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    symbols TEXT,
    cache_hit INTEGER NOT NULL,
    bytes INTEGER NOT NULL DEFAULT 0,
    skipped_reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_ledger_ts ON api_usage_ledger(ts);
"""


class UsageLedger:
    """Append-only per-call FMP usage ledger in a dedicated SQLite DB."""

    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as cx:
            cx.executescript(_DDL)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def record(self, *, run_mode: str, endpoint: str, symbols: list[str] | None,
               cache_hit: bool, bytes_: int, skipped_reason: Optional[str],
               ts: str) -> None:
        try:
            with self._conn() as cx:
                cx.execute(
                    "INSERT INTO api_usage_ledger"
                    "(ts, run_mode, endpoint, symbols, cache_hit, bytes, skipped_reason)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (ts, run_mode, endpoint, ",".join(symbols or []),
                     1 if cache_hit else 0, int(bytes_ or 0), skipped_reason),
                )
        except Exception:
            pass  # telemetry must never break a run

    def calls_in_run(self, *, run_mode: str, since: str) -> int:
        with self._conn() as cx:
            row = cx.execute(
                "SELECT COUNT(*) FROM api_usage_ledger "
                "WHERE run_mode=? AND ts>=? AND cache_hit=0 AND skipped_reason IS NULL",
                (run_mode, since)).fetchone()
        return int(row[0] or 0)

    def monthly_bytes(self, *, month: str) -> int:
        with self._conn() as cx:
            row = cx.execute(
                "SELECT COALESCE(SUM(bytes),0) FROM api_usage_ledger WHERE substr(ts,1,7)=?",
                (month,)).fetchone()
        return int(row[0] or 0)

    def cache_hit_rate(self, *, month: str) -> float:
        with self._conn() as cx:
            total = cx.execute(
                "SELECT COUNT(*) FROM api_usage_ledger WHERE substr(ts,1,7)=?",
                (month,)).fetchone()[0]
            hits = cx.execute(
                "SELECT COUNT(*) FROM api_usage_ledger WHERE substr(ts,1,7)=? AND cache_hit=1",
                (month,)).fetchone()[0]
        return round(hits / total, 4) if total else 0.0

    def skipped_count(self, *, month: str, run_mode: str,
                      reasons: tuple[str, ...] = ("run_budget", "bandwidth_guard")) -> int:
        """Count budget-driven skips for a run_mode in a month.

        Defaults to budget reasons only (``run_budget`` / ``bandwidth_guard``) so
        that transient token-bucket ``rate_limited`` skips are NOT mislabeled as
        budget exhaustion (they drain the per-second bucket at the tail of a tight
        loop, not the run/bandwidth budget). Pass ``reasons=()`` to count every
        non-null skip reason.
        """
        with self._conn() as cx:
            if reasons:
                placeholders = ",".join("?" for _ in reasons)
                row = cx.execute(
                    "SELECT COUNT(*) FROM api_usage_ledger "
                    f"WHERE substr(ts,1,7)=? AND run_mode=? AND skipped_reason IN ({placeholders})",
                    (month, run_mode, *reasons)).fetchone()
            else:
                row = cx.execute(
                    "SELECT COUNT(*) FROM api_usage_ledger "
                    "WHERE substr(ts,1,7)=? AND run_mode=? AND skipped_reason IS NOT NULL",
                    (month, run_mode)).fetchone()
        return int(row[0] or 0)

    def calls_by_run_mode(self, *, month: str) -> dict[str, int]:
        with self._conn() as cx:
            rows = cx.execute(
                "SELECT run_mode, COUNT(*) FROM api_usage_ledger "
                "WHERE substr(ts,1,7)=? AND cache_hit=0 AND skipped_reason IS NULL "
                "GROUP BY run_mode", (month,)).fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def calls_by_endpoint(self, *, month: str) -> dict[str, int]:
        with self._conn() as cx:
            rows = cx.execute(
                "SELECT endpoint, COUNT(*) FROM api_usage_ledger "
                "WHERE substr(ts,1,7)=? AND cache_hit=0 AND skipped_reason IS NULL "
                "GROUP BY endpoint", (month,)).fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def prune(self, *, keep_days: int = 90, now_iso: str) -> int:
        """Delete rows older than keep_days (caller passes now to stay deterministic)."""
        from datetime import datetime, timedelta
        cutoff = (datetime.fromisoformat(now_iso) - timedelta(days=keep_days)).isoformat()
        with self._conn() as cx:
            cur = cx.execute("DELETE FROM api_usage_ledger WHERE ts < ?", (cutoff,))
        return cur.rowcount
