"""Operations page — run history, output snapshots, portfolio peaks.

Read-only. All SQLite reads open the DB via the `file:?mode=ro` URI so
no write can happen even by accident. The stub function
`collect_operations_stub` is preserved for backward compatibility.

`collect_operations_view` ports the Streamlit page_run_history()
sections: recent run rows (with mode column), history snapshot
directory listing, portfolio peaks table.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


def _connect_ro(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.exists():
        return None
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return None


def _read_run_history(db_path: Path, limit: int = 5) -> list[dict[str, Any]]:
    conn = _connect_ro(db_path)
    if conn is None:
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


def _read_run_history_extended(db_path: Path, limit: int = 30) -> list[dict[str, Any]]:
    """Includes the `mode` column when present; tolerates older schemas."""
    conn = _connect_ro(db_path)
    if conn is None:
        return []
    try:
        # Probe schema columns; some older DBs may lack `mode`.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(run_history)")}
    except sqlite3.DatabaseError:
        conn.close()
        return []
    has_mode = "mode" in cols
    select = "run_id, mode, status, started_at, completed_at" if has_mode \
             else "run_id, status, started_at, completed_at"
    try:
        rows = conn.execute(
            f"SELECT {select} FROM run_history "
            "ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.DatabaseError:
        conn.close()
        return []
    conn.close()
    out: list[dict[str, Any]] = []
    for r in rows:
        if has_mode:
            out.append({
                "run_id": r[0], "mode": r[1], "status": r[2],
                "started_at": r[3], "completed_at": r[4],
            })
        else:
            out.append({
                "run_id": r[0], "mode": None, "status": r[1],
                "started_at": r[2], "completed_at": r[3],
            })
    return out


def _read_portfolio_peaks(db_path: Path, limit: int = 20) -> list[dict[str, Any]]:
    conn = _connect_ro(db_path)
    if conn is None:
        return []
    try:
        cols_rows = list(conn.execute("PRAGMA table_info(portfolio_peaks)"))
    except sqlite3.DatabaseError:
        conn.close()
        return []
    if not cols_rows:
        conn.close()
        return []
    cols = [r[1] for r in cols_rows]
    try:
        rows = conn.execute(
            f"SELECT {', '.join(cols)} FROM portfolio_peaks "
            "ORDER BY rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.DatabaseError:
        conn.close()
        return []
    conn.close()
    return [dict(zip(cols, r)) for r in rows]


def _history_snapshots(repo_root: Path) -> list[dict[str, Any]]:
    history = Path(repo_root) / "outputs" / "history"
    if not history.exists() or not history.is_dir():
        return []
    try:
        date_dirs = sorted(
            [d for d in history.iterdir() if d.is_dir()],
            key=lambda d: d.name,
            reverse=True,
        )
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for d in date_dirs:
        try:
            files = [f for f in d.iterdir() if f.is_file()]
            size_kb = sum(f.stat().st_size for f in files) / 1024.0
        except OSError:
            continue
        out.append({
            "date": d.name,
            "file_count": len(files),
            "size_kb": round(size_kb, 1),
        })
    return out


def collect_operations_stub(repo_root: Path) -> dict[str, Any]:
    db = Path(repo_root) / "data" / "portfolio.db"
    return {
        "advisory_only": True,
        "no_trade": True,
        "recent_runs": _read_run_history(db),
    }


def collect_operations_view(repo_root: Path, *, history_limit: int = 30) -> dict[str, Any]:
    """Full Operations page data: extended run history + snapshots + peaks."""
    db = Path(repo_root) / "data" / "portfolio.db"
    recent_runs_ext = _read_run_history_extended(db, limit=history_limit)
    return {
        "advisory_only": True,
        "no_trade": True,
        # Keep `recent_runs` key for backward compat with the stub template
        "recent_runs": recent_runs_ext[:5],
        # New extended data
        "run_history": recent_runs_ext,
        "history_snapshots": _history_snapshots(repo_root),
        "portfolio_peaks": _read_portfolio_peaks(db),
        "run_history_limit": history_limit,
    }
