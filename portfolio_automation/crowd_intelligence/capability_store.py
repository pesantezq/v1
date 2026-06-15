"""SQLite persistence for FMP endpoint capability probe results (observe-only).

Dedicated DB (data/crowd_intelligence.db) — isolated from portfolio.db and
fmp_budget.db. Phase 1 owns only the fmp_endpoint_capabilities table.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

_DDL = """
CREATE TABLE IF NOT EXISTS fmp_endpoint_capabilities (
    endpoint_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    http_status INTEGER,
    response_bytes INTEGER NOT NULL DEFAULT 0,
    sample_fields TEXT,
    last_checked_at TEXT,
    error_summary TEXT
);
"""


class CapabilityStore:
    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._path) as cx:
            cx.executescript(_DDL)

    def upsert(self, records: list[dict]) -> int:
        rows = 0
        with sqlite3.connect(self._path) as cx:
            for r in records:
                cx.execute(
                    "INSERT INTO fmp_endpoint_capabilities"
                    "(endpoint_id, status, http_status, response_bytes, sample_fields,"
                    " last_checked_at, error_summary) VALUES (?,?,?,?,?,?,?)"
                    " ON CONFLICT(endpoint_id) DO UPDATE SET status=excluded.status,"
                    " http_status=excluded.http_status, response_bytes=excluded.response_bytes,"
                    " sample_fields=excluded.sample_fields, last_checked_at=excluded.last_checked_at,"
                    " error_summary=excluded.error_summary",
                    (r.get("endpoint_id"), r.get("status"), r.get("http_status"),
                     int(r.get("response_bytes") or 0),
                     json.dumps(r.get("sample_fields") or []),
                     r.get("last_checked_at"), r.get("error_summary")),
                )
                rows += 1
        return rows

    def all_rows(self) -> list[dict]:
        with sqlite3.connect(self._path) as cx:
            cx.row_factory = sqlite3.Row
            out = []
            for row in cx.execute("SELECT * FROM fmp_endpoint_capabilities ORDER BY endpoint_id"):
                d = dict(row)
                try:
                    d["sample_fields"] = json.loads(d.get("sample_fields") or "[]")
                except Exception:
                    d["sample_fields"] = []
                out.append(d)
            return out
