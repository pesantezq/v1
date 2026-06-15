from __future__ import annotations
import sqlite3
from pathlib import Path
from fmp_client import _DiskCache

_DDL = """
CREATE TABLE IF NOT EXISTS symbol_data_policy (
    symbol TEXT PRIMARY KEY,
    ttl_seconds INTEGER,
    priority TEXT
);
"""


class SymbolDataPolicy:
    """Per-symbol TTL + priority tier (high/medium/low)."""

    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._path) as cx:
            cx.executescript(_DDL)

    def set_policy(self, symbol: str, *, ttl_seconds: int, priority: str) -> None:
        with sqlite3.connect(self._path) as cx:
            cx.execute(
                "INSERT INTO symbol_data_policy(symbol, ttl_seconds, priority) "
                "VALUES (?,?,?) ON CONFLICT(symbol) DO UPDATE SET "
                "ttl_seconds=excluded.ttl_seconds, priority=excluded.priority",
                (symbol.upper(), ttl_seconds, priority))

    def _get(self, symbol: str):
        with sqlite3.connect(self._path) as cx:
            return cx.execute(
                "SELECT ttl_seconds, priority FROM symbol_data_policy WHERE symbol=?",
                (symbol.upper(),)).fetchone()

    def ttl_for(self, symbol: str, *, default: int) -> int:
        row = self._get(symbol)
        return int(row[0]) if row and row[0] is not None else default

    def priority_for(self, symbol: str, *, default: str) -> str:
        row = self._get(symbol)
        return str(row[1]) if row and row[1] else default


def cache_stats(cache_dir: Path, *, fresh_keys: list[str], ttl_seconds: int) -> dict:
    """Report cache file count/size + per-key fresh/stale, reusing fmp_client._DiskCache."""
    dc = _DiskCache(cache_dir)
    files = list(Path(cache_dir).glob("*.json"))
    fresh = {k: (dc.get(k, ttl_seconds) is not None) for k in fresh_keys}
    return {
        "available": True,
        "file_count": len(files),
        "total_size_bytes": sum(f.stat().st_size for f in files),
        "fresh": fresh,
    }
