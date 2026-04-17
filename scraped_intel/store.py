"""
Scraped Intelligence — SQLite persistence layer.

Uses the same data/portfolio.db file as the rest of the project but manages
its own tables (scraped_records, soft_signals) independently.  Follows the
exact same connection + DDL pattern as state_store.py so code style is
consistent and the single-file DB constraint is maintained.

Tables
------
scraped_records
    One row per unique ScrapedRecord (deduped by record_id).
    Raw scraped evidence — NOT used in any hard-data scoring.

soft_signals
    One row per (symbol, as_of_date).
    Derived features computed by features.py.
    Overwritten on each run (latest wins).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from scraped_intel.models import ScrapedRecord, SoftSignals

logger = logging.getLogger("scraped_intel.store")

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS scraped_records (
    record_id        TEXT PRIMARY KEY,
    symbol           TEXT NOT NULL,
    source_type      TEXT NOT NULL,
    domain           TEXT NOT NULL,
    url              TEXT,
    published_at     TEXT,
    collected_at     TEXT NOT NULL,
    title            TEXT NOT NULL DEFAULT '',
    excerpt          TEXT NOT NULL DEFAULT '',
    extraction_status TEXT NOT NULL DEFAULT 'ok',
    parse_quality    REAL NOT NULL DEFAULT 0.5,
    themes           TEXT NOT NULL DEFAULT '[]',   -- JSON list
    sentiment        REAL,
    recency_hours    REAL,
    extra            TEXT NOT NULL DEFAULT '{}'    -- JSON dict
);

CREATE INDEX IF NOT EXISTS idx_scraped_records_symbol_pub
    ON scraped_records (symbol, published_at);

CREATE TABLE IF NOT EXISTS soft_signals (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol                  TEXT NOT NULL,
    as_of_date              TEXT NOT NULL,
    headline_count_7d       INTEGER NOT NULL DEFAULT 0,
    headline_count_30d      INTEGER NOT NULL DEFAULT 0,
    source_count            INTEGER NOT NULL DEFAULT 0,
    avg_sentiment           REAL,
    theme_alignment_score   REAL NOT NULL DEFAULT 0.0,
    mention_acceleration    REAL NOT NULL DEFAULT 0.0,
    recency_score           REAL NOT NULL DEFAULT 0.0,
    scraped_confidence      REAL NOT NULL DEFAULT 0.0,
    evidence_items          TEXT NOT NULL DEFAULT '[]',   -- JSON list of record_ids
    recorded_at             TEXT NOT NULL,
    UNIQUE(symbol, as_of_date)
);

CREATE INDEX IF NOT EXISTS idx_soft_signals_symbol_date
    ON soft_signals (symbol, as_of_date);

CREATE TABLE IF NOT EXISTS comparison_snapshots (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol                    TEXT NOT NULL,
    as_of_date                TEXT NOT NULL,
    baseline_signal_score     REAL NOT NULL DEFAULT 0.0,
    enriched_signal_score     REAL NOT NULL DEFAULT 0.0,
    signal_delta              REAL NOT NULL DEFAULT 0.0,
    baseline_confidence_score REAL NOT NULL DEFAULT 0.0,
    enriched_confidence_score REAL NOT NULL DEFAULT 0.0,
    confidence_delta          REAL NOT NULL DEFAULT 0.0,
    baseline_rank             INTEGER NOT NULL DEFAULT 0,
    enriched_rank             INTEGER NOT NULL DEFAULT 0,
    rank_change               INTEGER NOT NULL DEFAULT 0,
    soft_composite            REAL NOT NULL DEFAULT 0.0,
    top_features              TEXT NOT NULL DEFAULT '[]',
    source_count              INTEGER NOT NULL DEFAULT 0,
    evidence_count            INTEGER NOT NULL DEFAULT 0,
    scraped_confidence        REAL NOT NULL DEFAULT 0.0,
    soft_signals_available    INTEGER NOT NULL DEFAULT 0,
    recorded_at               TEXT NOT NULL,
    UNIQUE(symbol, as_of_date)
);

CREATE INDEX IF NOT EXISTS idx_comparison_snapshots_date
    ON comparison_snapshots (as_of_date);

CREATE TABLE IF NOT EXISTS comparison_outcomes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id     INTEGER NOT NULL,
    symbol          TEXT NOT NULL,
    as_of_date      TEXT NOT NULL,
    window_days     INTEGER NOT NULL,
    baseline_price  REAL,
    outcome_price   REAL,
    return_pct      REAL,
    outcome_label   TEXT,
    evaluated_at    TEXT,
    outcome_status  TEXT NOT NULL DEFAULT 'pending',
    UNIQUE(snapshot_id, window_days)
);

CREATE INDEX IF NOT EXISTS idx_comparison_outcomes_pending
    ON comparison_outcomes (outcome_status, as_of_date);
"""


class ScrapedIntelStore:
    """
    Persistence layer for scraped records and computed soft signals.

    Designed to be instantiated once per pipeline run.  Uses the same
    shared SQLite file as PortfolioStateStore (data/portfolio.db by default)
    but creates its own tables and never touches existing ones.
    """

    def __init__(self, db_path: str | Path = "data/portfolio.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_tables()

    # ------------------------------------------------------------------
    # Connection helper (mirrors state_store.py pattern)
    # ------------------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_tables(self) -> None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript(_DDL)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # ScrapedRecord persistence
    # ------------------------------------------------------------------

    def save_record(self, record: ScrapedRecord) -> bool:
        """
        Upsert a ScrapedRecord.  Returns True if newly inserted, False if
        the record_id already existed (dedup).
        """
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT record_id FROM scraped_records WHERE record_id = ?",
                (record.record_id,),
            ).fetchone()
            if existing:
                return False
            conn.execute(
                """
                INSERT INTO scraped_records (
                    record_id, symbol, source_type, domain, url,
                    published_at, collected_at, title, excerpt,
                    extraction_status, parse_quality, themes,
                    sentiment, recency_hours, extra
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record.record_id,
                    record.symbol,
                    record.source_type,
                    record.domain,
                    record.url,
                    record.published_at,
                    record.collected_at,
                    record.title,
                    record.excerpt,
                    record.extraction_status,
                    record.parse_quality,
                    json.dumps(record.themes or []),
                    record.sentiment,
                    record.recency_hours,
                    json.dumps(record.extra or {}),
                ),
            )
        return True

    def save_records(self, records: list[ScrapedRecord]) -> int:
        """Save a batch of records; returns count of newly inserted rows."""
        return sum(1 for r in records if self.save_record(r))

    def load_records(
        self,
        symbol: str,
        since_date: Optional[str] = None,
        limit: int = 200,
    ) -> list[ScrapedRecord]:
        """
        Load stored ScrapedRecords for a symbol.

        Args:
            symbol:     Ticker to load records for.
            since_date: ISO date string (YYYY-MM-DD); filters on published_at.
            limit:      Maximum rows to return (newest first).
        """
        query = "SELECT * FROM scraped_records WHERE symbol = ?"
        params: list[Any] = [symbol]
        if since_date:
            query += " AND published_at >= ?"
            params.append(since_date)
        query += " ORDER BY published_at DESC, collected_at DESC LIMIT ?"
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        return [self._row_to_record(r) for r in rows]

    # ------------------------------------------------------------------
    # SoftSignals persistence
    # ------------------------------------------------------------------

    def save_soft_signals(self, signals: SoftSignals) -> None:
        """Upsert soft signals for (symbol, as_of_date)."""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO soft_signals (
                    symbol, as_of_date,
                    headline_count_7d, headline_count_30d, source_count,
                    avg_sentiment, theme_alignment_score, mention_acceleration,
                    recency_score, scraped_confidence, evidence_items, recorded_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol, as_of_date) DO UPDATE SET
                    headline_count_7d     = excluded.headline_count_7d,
                    headline_count_30d    = excluded.headline_count_30d,
                    source_count          = excluded.source_count,
                    avg_sentiment         = excluded.avg_sentiment,
                    theme_alignment_score = excluded.theme_alignment_score,
                    mention_acceleration  = excluded.mention_acceleration,
                    recency_score         = excluded.recency_score,
                    scraped_confidence    = excluded.scraped_confidence,
                    evidence_items        = excluded.evidence_items,
                    recorded_at           = excluded.recorded_at
                """,
                (
                    signals.symbol,
                    signals.as_of_date,
                    signals.headline_count_7d,
                    signals.headline_count_30d,
                    signals.source_count,
                    signals.avg_sentiment,
                    signals.theme_alignment_score,
                    signals.mention_acceleration,
                    signals.recency_score,
                    signals.scraped_confidence,
                    json.dumps(signals.evidence_items or []),
                    now,
                ),
            )

    def load_soft_signals(
        self, symbol: str, as_of_date: str
    ) -> Optional[SoftSignals]:
        """Load the most recent soft signals row for (symbol, as_of_date)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM soft_signals WHERE symbol=? AND as_of_date=? "
                "ORDER BY recorded_at DESC LIMIT 1",
                (symbol, as_of_date),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_signals(row)

    def load_recent_soft_signals(
        self, symbol: str, limit: int = 30
    ) -> list[SoftSignals]:
        """Return soft signals rows for a symbol, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM soft_signals WHERE symbol=? "
                "ORDER BY as_of_date DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        return [self._row_to_signals(r) for r in rows]

    # ------------------------------------------------------------------
    # Row converters
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ScrapedRecord:
        d = dict(row)
        return ScrapedRecord(
            record_id=d["record_id"],
            symbol=d["symbol"],
            source_type=d["source_type"],
            domain=d["domain"],
            url=d.get("url"),
            published_at=d.get("published_at"),
            collected_at=d["collected_at"],
            title=d.get("title", ""),
            excerpt=d.get("excerpt", ""),
            extraction_status=d.get("extraction_status", "ok"),
            parse_quality=float(d.get("parse_quality") or 0.5),
            themes=json.loads(d.get("themes") or "[]"),
            sentiment=d.get("sentiment"),
            recency_hours=d.get("recency_hours"),
            extra=json.loads(d.get("extra") or "{}"),
        )

    # ------------------------------------------------------------------
    # comparison_snapshots + comparison_outcomes
    # ------------------------------------------------------------------

    def save_comparison_snapshot(self, row_dict: Dict[str, Any], as_of_date: str) -> int:
        """
        Upsert one ComparisonRow (as a plain dict from row.to_dict()) into
        comparison_snapshots and return the row id.

        ``row_dict`` must contain the keys produced by ``ComparisonRow.to_dict()``.
        ``as_of_date`` is the YYYY-MM-DD string for the current scan date.
        """
        now = datetime.now().isoformat()
        symbol = str(row_dict.get("symbol", "")).upper()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO comparison_snapshots (
                    symbol, as_of_date,
                    baseline_signal_score, enriched_signal_score, signal_delta,
                    baseline_confidence_score, enriched_confidence_score, confidence_delta,
                    baseline_rank, enriched_rank, rank_change,
                    soft_composite, top_features,
                    source_count, evidence_count, scraped_confidence,
                    soft_signals_available, recorded_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol, as_of_date) DO UPDATE SET
                    baseline_signal_score     = excluded.baseline_signal_score,
                    enriched_signal_score     = excluded.enriched_signal_score,
                    signal_delta              = excluded.signal_delta,
                    baseline_confidence_score = excluded.baseline_confidence_score,
                    enriched_confidence_score = excluded.enriched_confidence_score,
                    confidence_delta          = excluded.confidence_delta,
                    baseline_rank             = excluded.baseline_rank,
                    enriched_rank             = excluded.enriched_rank,
                    rank_change               = excluded.rank_change,
                    soft_composite            = excluded.soft_composite,
                    top_features              = excluded.top_features,
                    source_count              = excluded.source_count,
                    evidence_count            = excluded.evidence_count,
                    scraped_confidence        = excluded.scraped_confidence,
                    soft_signals_available    = excluded.soft_signals_available,
                    recorded_at               = excluded.recorded_at
                """,
                (
                    symbol,
                    as_of_date,
                    float(row_dict.get("baseline_signal_score") or 0.0),
                    float(row_dict.get("enriched_signal_score") or 0.0),
                    float(row_dict.get("signal_delta") or 0.0),
                    float(row_dict.get("baseline_confidence_score") or 0.0),
                    float(row_dict.get("enriched_confidence_score") or 0.0),
                    float(row_dict.get("confidence_delta") or 0.0),
                    int(row_dict.get("baseline_rank") or 0),
                    int(row_dict.get("enriched_rank") or 0),
                    int(row_dict.get("rank_change") or 0),
                    float(row_dict.get("soft_composite") or 0.0),
                    json.dumps(row_dict.get("top_features") or []),
                    int(row_dict.get("source_count") or 0),
                    int(row_dict.get("evidence_count") or 0),
                    float(row_dict.get("scraped_confidence") or 0.0),
                    1 if row_dict.get("soft_signals_available") else 0,
                    now,
                ),
            )
            row_id: int = conn.execute(
                "SELECT id FROM comparison_snapshots WHERE symbol=? AND as_of_date=?",
                (symbol, as_of_date),
            ).fetchone()["id"]
        return row_id

    def _ensure_outcome_slot(
        self, snapshot_id: int, symbol: str, as_of_date: str, window_days: int
    ) -> None:
        """Create a pending comparison_outcomes row if it does not already exist."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO comparison_outcomes
                    (snapshot_id, symbol, as_of_date, window_days, outcome_status)
                VALUES (?,?,?,?,'pending')
                """,
                (snapshot_id, symbol, as_of_date, window_days),
            )

    def save_comparison_snapshots(
        self,
        row_dicts: List[Dict[str, Any]],
        as_of_date: str,
        windows: Optional[List[int]] = None,
    ) -> List[int]:
        """
        Upsert a batch of ComparisonRow dicts and create pending outcome slots.

        Args:
            row_dicts:   List of dicts from ``ComparisonRow.to_dict()``.
            as_of_date:  YYYY-MM-DD scan date.
            windows:     Return-window days (default ``[1, 5, 20]``).

        Returns:
            List of snapshot row ids (one per input dict, same order).
        """
        _windows = windows if windows is not None else [1, 5, 20]
        ids: List[int] = []
        for rd in row_dicts:
            row_id = self.save_comparison_snapshot(rd, as_of_date)
            ids.append(row_id)
            symbol = str(rd.get("symbol", "")).upper()
            for w in _windows:
                self._ensure_outcome_slot(row_id, symbol, as_of_date, w)
        return ids

    def load_comparison_snapshots(
        self,
        symbol: Optional[str] = None,
        since_date: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """
        Load comparison_snapshots rows, newest first.

        Args:
            symbol:     Filter by ticker (case-insensitive).
            since_date: Filter on as_of_date >= since_date (YYYY-MM-DD).
            limit:      Maximum rows to return.
        """
        query = "SELECT * FROM comparison_snapshots"
        params: List[Any] = []
        conditions: List[str] = []
        if symbol:
            conditions.append("symbol=?")
            params.append(symbol.upper())
        if since_date:
            conditions.append("as_of_date>=?")
            params.append(since_date)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY as_of_date DESC, id DESC LIMIT ?"
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        result: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["top_features"] = json.loads(d.get("top_features") or "[]")
            d["soft_signals_available"] = bool(d.get("soft_signals_available", 0))
            result.append(d)
        return result

    def get_pending_comparison_outcomes(self, limit: int = 500) -> List[Dict[str, Any]]:
        """
        Return pending (unresolved) comparison outcome slots, oldest first.

        Each row includes the parent snapshot's signal_delta and soft_composite
        so that the evaluator can group by blend quality without a second query.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    co.*,
                    cs.baseline_signal_score,
                    cs.signal_delta,
                    cs.soft_composite,
                    cs.scraped_confidence
                FROM comparison_outcomes co
                JOIN comparison_snapshots cs ON co.snapshot_id = cs.id
                WHERE co.outcome_status = 'pending'
                ORDER BY co.as_of_date ASC, co.snapshot_id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def resolve_comparison_outcome(
        self,
        outcome_id: int,
        *,
        baseline_price: float,
        outcome_price: float,
        return_pct: float,
        outcome_label: str,
        evaluated_at: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Mark a pending comparison_outcomes row as resolved.

        Returns the updated row dict, or None if no pending row matched.
        """
        now = evaluated_at or datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE comparison_outcomes
                SET baseline_price  = ?,
                    outcome_price   = ?,
                    return_pct      = ?,
                    outcome_label   = ?,
                    evaluated_at    = ?,
                    outcome_status  = 'resolved'
                WHERE id = ? AND outcome_status = 'pending'
                """,
                (
                    float(baseline_price),
                    float(outcome_price),
                    float(return_pct),
                    outcome_label,
                    now,
                    int(outcome_id),
                ),
            )
            row = conn.execute(
                "SELECT * FROM comparison_outcomes WHERE id=?",
                (int(outcome_id),),
            ).fetchone()
        return dict(row) if row else None

    def get_resolved_outcomes_with_raw_signals(
        self,
        since_date: Optional[str] = None,
        window_days: Optional[int] = None,
        limit: int = 5000,
    ) -> List[Dict[str, Any]]:
        """
        Load resolved comparison outcomes joined with raw soft signal feature
        values from the soft_signals table.

        Each returned dict combines comparison_outcomes + comparison_snapshots
        baseline scores + the raw soft feature values needed to recompute
        enriched scores under alternative weight configurations.

        Rows without a matching soft_signals entry have all raw_* fields as
        None (LEFT JOIN) — the tuner treats these as "not boosted" under any
        candidate config.

        Args:
            since_date:  Filter on as_of_date >= since_date (YYYY-MM-DD).
            window_days: Filter on a specific return window (1, 5, or 20).
            limit:       Maximum rows to return.
        """
        query = """
            SELECT
                co.id                       AS outcome_id,
                co.snapshot_id,
                co.symbol,
                co.as_of_date,
                co.window_days,
                co.return_pct,
                co.outcome_label,
                cs.baseline_signal_score,
                cs.baseline_confidence_score,
                cs.signal_delta             AS stored_signal_delta,
                cs.soft_composite           AS stored_soft_composite,
                cs.scraped_confidence       AS stored_scraped_confidence,
                ss.scraped_confidence       AS raw_scraped_confidence,
                ss.recency_score            AS raw_recency_score,
                ss.theme_alignment_score    AS raw_theme_alignment_score,
                ss.mention_acceleration     AS raw_mention_acceleration,
                ss.source_count             AS raw_source_count
            FROM comparison_outcomes co
            JOIN comparison_snapshots cs ON co.snapshot_id = cs.id
            LEFT JOIN soft_signals ss
                ON ss.symbol = co.symbol AND ss.as_of_date = co.as_of_date
            WHERE co.outcome_status = 'resolved'
        """
        params: List[Any] = []
        if since_date:
            query += " AND co.as_of_date >= ?"
            params.append(since_date)
        if window_days is not None:
            query += " AND co.window_days = ?"
            params.append(int(window_days))
        query += " ORDER BY co.as_of_date DESC LIMIT ?"
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        return [dict(r) for r in rows]

    def get_resolved_comparison_outcomes(
        self,
        since_date: Optional[str] = None,
        window_days: Optional[int] = None,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        """
        Load resolved comparison outcomes joined with their parent snapshots.

        Each returned dict combines all comparison_outcomes columns with the
        parent comparison_snapshots signal/feature columns for analysis.

        Args:
            since_date:  Filter on as_of_date >= since_date (YYYY-MM-DD).
            window_days: Filter on a specific return window (1, 5, or 20).
            limit:       Maximum rows to return.
        """
        query = """
            SELECT
                co.id           AS outcome_id,
                co.snapshot_id,
                co.symbol,
                co.as_of_date,
                co.window_days,
                co.baseline_price,
                co.outcome_price,
                co.return_pct,
                co.outcome_label,
                co.evaluated_at,
                co.outcome_status,
                cs.baseline_signal_score,
                cs.enriched_signal_score,
                cs.signal_delta,
                cs.baseline_confidence_score,
                cs.enriched_confidence_score,
                cs.confidence_delta,
                cs.soft_composite,
                cs.top_features,
                cs.source_count,
                cs.evidence_count,
                cs.scraped_confidence,
                cs.soft_signals_available
            FROM comparison_outcomes co
            JOIN comparison_snapshots cs ON co.snapshot_id = cs.id
            WHERE co.outcome_status = 'resolved'
        """
        params: List[Any] = []
        if since_date:
            query += " AND co.as_of_date >= ?"
            params.append(since_date)
        if window_days is not None:
            query += " AND co.window_days = ?"
            params.append(int(window_days))
        query += " ORDER BY co.as_of_date DESC LIMIT ?"
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()

        result: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["top_features"] = json.loads(d.get("top_features") or "[]")
            d["soft_signals_available"] = bool(d.get("soft_signals_available", 0))
            result.append(d)
        return result

    @staticmethod
    def _row_to_signals(row: sqlite3.Row) -> SoftSignals:
        d = dict(row)
        return SoftSignals(
            symbol=d["symbol"],
            as_of_date=d["as_of_date"],
            headline_count_7d=int(d.get("headline_count_7d") or 0),
            headline_count_30d=int(d.get("headline_count_30d") or 0),
            source_count=int(d.get("source_count") or 0),
            avg_sentiment=d.get("avg_sentiment"),
            theme_alignment_score=float(d.get("theme_alignment_score") or 0),
            mention_acceleration=float(d.get("mention_acceleration") or 0),
            recency_score=float(d.get("recency_score") or 0),
            scraped_confidence=float(d.get("scraped_confidence") or 0),
            evidence_items=json.loads(d.get("evidence_items") or "[]"),
        )
