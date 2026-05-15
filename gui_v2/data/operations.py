"""Operations stub — last few SQLite run_history rows (read-only)."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


def _read_run_history(db_path: Path, limit: int = 5) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    try:
        # Read-only attach via URI; cannot accidentally write.
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError:
        return []
    try:
        rows = conn.execute(
            "SELECT run_id, status, started_at, completed_at "
            "FROM run_history "
            "ORDER BY rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.DatabaseError:
        conn.close()
        return []
    conn.close()
    return [
        {"run_id": r[0], "status": r[1], "started_at": r[2], "completed_at": r[3]}
        for r in rows
    ]


def collect_operations_stub(repo_root: Path) -> dict[str, Any]:
    db = Path(repo_root) / "data" / "portfolio.db"
    return {
        "advisory_only": True,
        "no_trade": True,
        "recent_runs": _read_run_history(db),
    }
