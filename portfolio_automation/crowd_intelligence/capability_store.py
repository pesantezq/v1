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
CREATE TABLE IF NOT EXISTS crowd_raw_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT, endpoint_id TEXT, symbol TEXT, category TEXT,
    event_time TEXT, normalized_event_type TEXT, raw_json TEXT, fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_raw_symbol ON crowd_raw_events(symbol);
CREATE TABLE IF NOT EXISTS crowd_signal_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL, signal_date TEXT NOT NULL,
    news_score REAL, analyst_score REAL, insider_score REAL, congress_score REAL,
    attention_score REAL, social_sentiment_score REAL, composite_crowd_score REAL,
    confidence REAL, enabled_sources_json TEXT, disabled_sources_json TEXT,
    explanation_json TEXT, created_at TEXT,
    UNIQUE(symbol, signal_date)
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

    # --- Phase 2A: crowd_raw_events + crowd_signal_daily ---------------------
    def record_events(self, events: list[dict]) -> int:
        n = 0
        with sqlite3.connect(self._path) as cx:
            for e in events:
                cx.execute(
                    "INSERT INTO crowd_raw_events(provider, endpoint_id, symbol, category,"
                    " event_time, normalized_event_type, raw_json, fetched_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (e.get("provider"), e.get("endpoint_id"), e.get("symbol"), e.get("category"),
                     e.get("event_time"), e.get("normalized_event_type"),
                     json.dumps(e.get("raw") or {}, default=str), e.get("fetched_at")))
                n += 1
        return n

    def upsert_daily(self, rows: list[dict]) -> int:
        cols = ("symbol", "signal_date", "news_score", "analyst_score", "insider_score",
                "congress_score", "attention_score", "social_sentiment_score",
                "composite_crowd_score", "confidence", "enabled_sources_json",
                "disabled_sources_json", "explanation_json", "created_at")
        with sqlite3.connect(self._path) as cx:
            for r in rows:
                cx.execute(
                    f"INSERT INTO crowd_signal_daily({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
                    " ON CONFLICT(symbol, signal_date) DO UPDATE SET "
                    + ", ".join(f"{c}=excluded.{c}" for c in cols if c not in ("symbol", "signal_date")),
                    tuple(r.get(c) for c in cols))
        return len(rows)

    def daily_rows(self) -> list[dict]:
        with sqlite3.connect(self._path) as cx:
            cx.row_factory = sqlite3.Row
            return [dict(r) for r in cx.execute(
                "SELECT * FROM crowd_signal_daily ORDER BY symbol, signal_date")]

    def raw_event_count(self) -> int:
        with sqlite3.connect(self._path) as cx:
            return int(cx.execute("SELECT COUNT(*) FROM crowd_raw_events").fetchone()[0])
