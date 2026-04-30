"""
SQLite-backed state store for portfolio automation.

Provides run history, portfolio snapshots, email deduplication, and peak
tracking — all using the stdlib sqlite3 module only. No external ORM or
third-party dependencies.

DB file: data/portfolio.db
"""

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger('portfolio_automation.state_store')

DB_PATH = Path("data/portfolio.db")


class PortfolioStateStore:
    """
    Single-file SQLite state store.

    Tables
    ------
    run_history     — one row per run attempt (idempotency anchor)
    snapshots       — portfolio metrics captured at end of compute stage
    email_history   — digest hashes to prevent duplicate email sends
    portfolio_peaks — synced from DrawdownTracker for SQL queries

    All timestamps are stored as ISO-8601 TEXT in local wall time,
    consistent with datetime.now().isoformat() used throughout the codebase.
    """

    def __init__(self, db_path: Path = DB_PATH) -> None:
        """
        Open (or create) the SQLite database and ensure all tables exist.

        Args:
            db_path: Path to the .db file. Parent directory is created
                     automatically if it does not exist.
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """
        Context manager that yields a sqlite3 connection and ensures it is
        always closed after use (critical on Windows where an open handle
        prevents temp-file deletion and causes PermissionError).

        Commits on clean exit, rolls back on exception.
        """
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

    def _init_db(self) -> None:
        """
        Create all tables if they do not already exist.
        Safe to call on every startup — all statements use IF NOT EXISTS.
        """
        ddl = """
        PRAGMA journal_mode=WAL;
        PRAGMA busy_timeout=5000;

        CREATE TABLE IF NOT EXISTS run_history (
            run_id       TEXT PRIMARY KEY,
            run_date     TEXT NOT NULL,
            mode         TEXT NOT NULL,
            status       TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
            started_at   TEXT NOT NULL,
            completed_at TEXT,
            user_id      TEXT NOT NULL DEFAULT 'owner'
        );

        CREATE TABLE IF NOT EXISTS snapshots (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id           TEXT NOT NULL REFERENCES run_history(run_id),
            total_value      REAL,
            cash             REAL,
            max_drift        REAL,
            drawdown_regime  TEXT,
            recorded_at      TEXT NOT NULL,
            user_id          TEXT NOT NULL DEFAULT 'owner'
        );

        CREATE TABLE IF NOT EXISTS email_history (
            digest_hash  TEXT NOT NULL,
            mode         TEXT NOT NULL,
            sent_at      TEXT NOT NULL,
            PRIMARY KEY (digest_hash, mode)
        );

        CREATE INDEX IF NOT EXISTS idx_email_sent_at ON email_history(digest_hash, sent_at);

        CREATE TABLE IF NOT EXISTS portfolio_peaks (
            peak_key     TEXT PRIMARY KEY,
            peak_value   REAL NOT NULL,
            updated_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS theme_signals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date        TEXT NOT NULL,
            theme_name      TEXT NOT NULL,
            confidence      REAL NOT NULL,
            rationale       TEXT,
            evidence_items  TEXT,
            direct_mentions TEXT,
            recorded_at     TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_theme_signals_run_date
            ON theme_signals (run_date);

        CREATE TABLE IF NOT EXISTS alert_events (
            fingerprint  TEXT PRIMARY KEY,
            first_seen   TEXT NOT NULL,
            last_seen    TEXT NOT NULL,
            last_emailed TEXT,
            times_seen   INTEGER NOT NULL DEFAULT 1,
            severity     TEXT NOT NULL DEFAULT '',
            state_hash   TEXT NOT NULL DEFAULT '',
            alert_tier   TEXT NOT NULL DEFAULT '',
            reason_code  TEXT NOT NULL DEFAULT '',
            last_signal_score REAL,
            last_confidence_score REAL,
            last_action_taken TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS watchlist_alert_outcomes (
            id                           INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint                  TEXT NOT NULL,
            state_hash                   TEXT NOT NULL DEFAULT '',
            ticker                       TEXT NOT NULL,
            watchlist_source             TEXT NOT NULL DEFAULT 'static',
            surfaced_at                  TEXT NOT NULL,
            last_seen_at                 TEXT NOT NULL,
            notification_status          TEXT NOT NULL DEFAULT 'alerted',
            alert_priority               TEXT NOT NULL DEFAULT '',
            alert_quality_tier           TEXT NOT NULL DEFAULT 'none',
            confirmation_count           INTEGER NOT NULL DEFAULT 0,
            evidence_breadth             INTEGER NOT NULL DEFAULT 0,
            portfolio_priority           REAL NOT NULL DEFAULT 0,
            overlap_penalty              REAL NOT NULL DEFAULT 0,
            diversification_bonus        REAL NOT NULL DEFAULT 0,
            existing_position_relevance  REAL NOT NULL DEFAULT 0,
            budget_fit                   TEXT NOT NULL DEFAULT 'unknown',
            baseline_price               REAL,
            baseline_signal_score        REAL,
            baseline_confidence_score    REAL,
            evaluation_window            TEXT NOT NULL DEFAULT '1d,3d,5d,10d',
            evaluation_price             REAL,
            return_pct                   REAL,
            evaluated_at                 TEXT,
            outcome_label                TEXT,
            outcome_status               TEXT NOT NULL DEFAULT 'pending',
            outcome_pending              INTEGER NOT NULL DEFAULT 1,
            resolved_at                  TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_watchlist_alert_outcomes_pending
            ON watchlist_alert_outcomes (fingerprint, outcome_pending, surfaced_at DESC);

        CREATE TABLE IF NOT EXISTS watchlist_signal_feedback (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_key             TEXT NOT NULL UNIQUE,
            ticker                 TEXT NOT NULL,
            signal_time            TEXT NOT NULL,
            watchlist_source       TEXT NOT NULL DEFAULT 'static',
            signal_score           REAL,
            confidence_score       REAL,
            effective_score        REAL,
            conviction_score       REAL,
            conviction_band        TEXT,
            normalized_allocation  REAL,
            price_at_signal        REAL,
            prediction_intent      TEXT NOT NULL DEFAULT 'up',
            data_mode              TEXT NOT NULL DEFAULT 'live',
            degraded_mode          INTEGER NOT NULL DEFAULT 0,
            regime_label           TEXT NOT NULL DEFAULT 'neutral',
            regime_confidence      REAL,
            regime_data_quality    TEXT NOT NULL DEFAULT 'limited',
            outcome_return_1d      REAL,
            outcome_success_1d     INTEGER,
            direction_correct_1d   INTEGER,
            outcome_price_1d       REAL,
            evaluated_at_1d        TEXT,
            outcome_return_3d      REAL,
            outcome_success_3d     INTEGER,
            direction_correct_3d   INTEGER,
            outcome_price_3d       REAL,
            evaluated_at_3d        TEXT,
            outcome_return_7d      REAL,
            outcome_success_7d     INTEGER,
            direction_correct_7d   INTEGER,
            outcome_price_7d       REAL,
            evaluated_at_7d        TEXT,
            theme_alignment_score  REAL,
            theme_top_name         TEXT,
            theme_type             TEXT,
            portfolio_fit_score    REAL,
            portfolio_fit_label    TEXT,
            final_rank_score       REAL,
            augmented_signal_score REAL
        );

        CREATE INDEX IF NOT EXISTS idx_watchlist_signal_feedback_ticker_time
            ON watchlist_signal_feedback (ticker, signal_time DESC);

        CREATE TABLE IF NOT EXISTS subsystem_health (
            subsystem            TEXT PRIMARY KEY,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            disabled_until       TEXT,
            last_error           TEXT,
            last_success         TEXT
        );

        CREATE TABLE IF NOT EXISTS structural_violations (
            violation_key    TEXT PRIMARY KEY,
            first_seen       TEXT NOT NULL,
            last_seen        TEXT NOT NULL,
            days_active      INTEGER NOT NULL DEFAULT 0,
            escalation_level INTEGER NOT NULL DEFAULT 0,
            last_emailed     TEXT
        );

        CREATE TABLE IF NOT EXISTS cash_ledger (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            type      TEXT NOT NULL,
            amount    REAL NOT NULL,
            note      TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS extended_watchlist (
            symbol           TEXT PRIMARY KEY,
            is_active        INTEGER NOT NULL DEFAULT 1,
            promoted_at      TEXT NOT NULL,
            expires_at       TEXT NOT NULL,
            last_reinforced  TEXT NOT NULL,
            theme_name       TEXT NOT NULL,
            theme_names      TEXT NOT NULL DEFAULT '[]',
            theme_confidence REAL NOT NULL,
            mention_count    INTEGER NOT NULL DEFAULT 1,
            scan_count       INTEGER NOT NULL DEFAULT 0,
            alert_count      INTEGER NOT NULL DEFAULT 0,
            outcome          TEXT NOT NULL DEFAULT 'none',
            drop_reason      TEXT
        );
        """
        # executescript issues its own commit — use a direct connection here
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript(ddl)
        finally:
            conn.close()

        # ── Schema migration: add drawdown_regime to existing snapshots tables ──
        # ALTER TABLE ADD COLUMN is idempotent-safe via the pragma check below.
        conn = sqlite3.connect(str(self.db_path))
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(snapshots)")}
            if "drawdown_regime" not in cols:
                conn.execute(
                    "ALTER TABLE snapshots ADD COLUMN drawdown_regime TEXT"
                )
                conn.commit()
                logger.info("snapshots: migrated — added drawdown_regime column")
        finally:
            conn.close()

        # ── Schema migration: add user_id to run_history and snapshots ──────────
        # Migration 001 handles existing DBs; this block covers DBs that are
        # started fresh from older code and never had migration 001 applied.
        # Index creation is guarded — CREATE INDEX on a missing column would fail.
        conn = sqlite3.connect(str(self.db_path))
        try:
            for table in ("run_history", "snapshots"):
                cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                if "user_id" not in cols:
                    conn.execute(
                        f"ALTER TABLE {table} "
                        "ADD COLUMN user_id TEXT NOT NULL DEFAULT 'owner'"
                    )
                    conn.commit()
                    logger.info("%s: migrated — added user_id column", table)
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table}_user_id "
                    f"ON {table}(user_id)"
                )
                conn.commit()
        finally:
            conn.close()

        conn = sqlite3.connect(str(self.db_path))
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(watchlist_alert_outcomes)")}
            for column_name, ddl in [
                ("evaluation_price", "ALTER TABLE watchlist_alert_outcomes ADD COLUMN evaluation_price REAL"),
                ("return_pct", "ALTER TABLE watchlist_alert_outcomes ADD COLUMN return_pct REAL"),
                ("evaluated_at", "ALTER TABLE watchlist_alert_outcomes ADD COLUMN evaluated_at TEXT"),
                ("outcome_label", "ALTER TABLE watchlist_alert_outcomes ADD COLUMN outcome_label TEXT"),
            ]:
                if column_name not in cols:
                    conn.execute(ddl)
                    conn.commit()
                    logger.info("watchlist_alert_outcomes: migrated â€” added %s column", column_name)
        finally:
            conn.close()

        conn = sqlite3.connect(str(self.db_path))
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(alert_events)")}
            for column_name, ddl in [
                ("alert_tier", "ALTER TABLE alert_events ADD COLUMN alert_tier TEXT NOT NULL DEFAULT ''"),
                ("reason_code", "ALTER TABLE alert_events ADD COLUMN reason_code TEXT NOT NULL DEFAULT ''"),
                ("last_signal_score", "ALTER TABLE alert_events ADD COLUMN last_signal_score REAL"),
                ("last_confidence_score", "ALTER TABLE alert_events ADD COLUMN last_confidence_score REAL"),
                ("last_action_taken", "ALTER TABLE alert_events ADD COLUMN last_action_taken TEXT NOT NULL DEFAULT ''"),
            ]:
                if column_name not in cols:
                    conn.execute(ddl)
                    conn.commit()
                    logger.info("alert_events: migrated - added %s column", column_name)
        finally:
            conn.close()

        conn = sqlite3.connect(str(self.db_path))
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(watchlist_signal_feedback)")}
            for column_name, ddl in [
                ("watchlist_source", "ALTER TABLE watchlist_signal_feedback ADD COLUMN watchlist_source TEXT NOT NULL DEFAULT 'static'"),
                ("signal_score", "ALTER TABLE watchlist_signal_feedback ADD COLUMN signal_score REAL"),
                ("confidence_score", "ALTER TABLE watchlist_signal_feedback ADD COLUMN confidence_score REAL"),
                ("effective_score", "ALTER TABLE watchlist_signal_feedback ADD COLUMN effective_score REAL"),
                ("conviction_score", "ALTER TABLE watchlist_signal_feedback ADD COLUMN conviction_score REAL"),
                ("conviction_band", "ALTER TABLE watchlist_signal_feedback ADD COLUMN conviction_band TEXT"),
                ("normalized_allocation", "ALTER TABLE watchlist_signal_feedback ADD COLUMN normalized_allocation REAL"),
                ("price_at_signal", "ALTER TABLE watchlist_signal_feedback ADD COLUMN price_at_signal REAL"),
                ("prediction_intent", "ALTER TABLE watchlist_signal_feedback ADD COLUMN prediction_intent TEXT NOT NULL DEFAULT 'up'"),
                ("data_mode", "ALTER TABLE watchlist_signal_feedback ADD COLUMN data_mode TEXT NOT NULL DEFAULT 'live'"),
                ("degraded_mode", "ALTER TABLE watchlist_signal_feedback ADD COLUMN degraded_mode INTEGER NOT NULL DEFAULT 0"),
                ("regime_label", "ALTER TABLE watchlist_signal_feedback ADD COLUMN regime_label TEXT NOT NULL DEFAULT 'neutral'"),
                ("regime_confidence", "ALTER TABLE watchlist_signal_feedback ADD COLUMN regime_confidence REAL"),
                ("regime_data_quality", "ALTER TABLE watchlist_signal_feedback ADD COLUMN regime_data_quality TEXT NOT NULL DEFAULT 'limited'"),
                ("outcome_return_1d", "ALTER TABLE watchlist_signal_feedback ADD COLUMN outcome_return_1d REAL"),
                ("outcome_success_1d", "ALTER TABLE watchlist_signal_feedback ADD COLUMN outcome_success_1d INTEGER"),
                ("direction_correct_1d", "ALTER TABLE watchlist_signal_feedback ADD COLUMN direction_correct_1d INTEGER"),
                ("outcome_price_1d", "ALTER TABLE watchlist_signal_feedback ADD COLUMN outcome_price_1d REAL"),
                ("evaluated_at_1d", "ALTER TABLE watchlist_signal_feedback ADD COLUMN evaluated_at_1d TEXT"),
                ("outcome_return_3d", "ALTER TABLE watchlist_signal_feedback ADD COLUMN outcome_return_3d REAL"),
                ("outcome_success_3d", "ALTER TABLE watchlist_signal_feedback ADD COLUMN outcome_success_3d INTEGER"),
                ("direction_correct_3d", "ALTER TABLE watchlist_signal_feedback ADD COLUMN direction_correct_3d INTEGER"),
                ("outcome_price_3d", "ALTER TABLE watchlist_signal_feedback ADD COLUMN outcome_price_3d REAL"),
                ("evaluated_at_3d", "ALTER TABLE watchlist_signal_feedback ADD COLUMN evaluated_at_3d TEXT"),
                ("outcome_return_7d", "ALTER TABLE watchlist_signal_feedback ADD COLUMN outcome_return_7d REAL"),
                ("outcome_success_7d", "ALTER TABLE watchlist_signal_feedback ADD COLUMN outcome_success_7d INTEGER"),
                ("direction_correct_7d", "ALTER TABLE watchlist_signal_feedback ADD COLUMN direction_correct_7d INTEGER"),
                ("outcome_price_7d", "ALTER TABLE watchlist_signal_feedback ADD COLUMN outcome_price_7d REAL"),
                ("evaluated_at_7d", "ALTER TABLE watchlist_signal_feedback ADD COLUMN evaluated_at_7d TEXT"),
                ("theme_alignment_score", "ALTER TABLE watchlist_signal_feedback ADD COLUMN theme_alignment_score REAL"),
                ("theme_top_name", "ALTER TABLE watchlist_signal_feedback ADD COLUMN theme_top_name TEXT"),
                ("theme_type", "ALTER TABLE watchlist_signal_feedback ADD COLUMN theme_type TEXT"),
                ("portfolio_fit_score", "ALTER TABLE watchlist_signal_feedback ADD COLUMN portfolio_fit_score REAL"),
                ("portfolio_fit_label", "ALTER TABLE watchlist_signal_feedback ADD COLUMN portfolio_fit_label TEXT"),
                ("final_rank_score", "ALTER TABLE watchlist_signal_feedback ADD COLUMN final_rank_score REAL"),
                ("augmented_signal_score", "ALTER TABLE watchlist_signal_feedback ADD COLUMN augmented_signal_score REAL"),
            ]:
                if column_name not in cols:
                    conn.execute(ddl)
                    conn.commit()
                    logger.info("watchlist_signal_feedback: migrated - added %s column", column_name)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # run_history
    # ------------------------------------------------------------------

    def start_run(self, run_id: str, mode: str, user_id: str = "owner") -> bool:
        """
        Insert a new run_history row with status='running'.

        Returns True if the row was inserted (this is a new run).
        Returns False if run_id already exists (PK conflict) — the caller
        should call check_run_status() to decide how to proceed.

        Args:
            run_id:  Unique identifier, format '{YYYY-MM-DD}_{mode}'.
            mode:    Run mode string ('daily', 'weekly', 'monthly').
            user_id: Owner of this run (default 'owner' for single-user deployments).
        """
        now = datetime.now().isoformat()
        run_date = now[:10]
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO run_history (run_id, run_date, mode, status, started_at, user_id) "
                    "VALUES (?, ?, ?, 'running', ?, ?)",
                    (run_id, run_date, mode, now, user_id)
                )
            logger.debug(f"Started run {run_id} in state store")
            return True
        except sqlite3.IntegrityError:
            row = self.check_run_status(run_id)
            if row is not None and row.get("status") == "failed":
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE run_history "
                        "SET mode=?, status='running', started_at=?, completed_at=NULL "
                        "WHERE run_id=?",
                        (mode, now, run_id),
                    )
                logger.debug(f"Restarted failed run {run_id} in state store")
                return True
            logger.debug(f"Run {run_id} already exists in state store")
            return False

    def check_run_status(self, run_id: str) -> Optional[Dict[str, Any]]:
        """
        Return the run_history row for run_id as a dict, or None if not found.

        Dict keys: run_id, run_date, mode, status, started_at, completed_at, user_id.
        """
        # TODO(v2-user-scope): review aggregate query behavior for multi-user support
        # run_id is a global PK ({date}_{mode}) so no user_id filter is needed here.
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM run_history WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    def is_completed(self, run_id: str) -> bool:
        """
        Return True if run_id exists with status='completed'.
        Used for the idempotency check at the top of main().
        """
        row = self.check_run_status(run_id)
        return row is not None and row['status'] == 'completed'

    def is_stale_running(self, run_id: str, stale_minutes: int = 30) -> bool:
        """
        Return True if the row exists with status='running' and started_at
        is older than stale_minutes.

        A stale running row indicates the previous process crashed without
        updating its status — safe to treat as failed and allow a re-run.

        Args:
            run_id:        The run to inspect.
            stale_minutes: Age threshold in minutes (default 30, matching
                           run_lock.STALE_AFTER_MINUTES).
        """
        row = self.check_run_status(run_id)
        if row is None or row['status'] != 'running':
            return False
        try:
            started = datetime.fromisoformat(row['started_at'])
        except (ValueError, TypeError):
            return False
        return datetime.now() - started > timedelta(minutes=stale_minutes)

    def complete_run(self, run_id: str) -> None:
        """
        Mark run_id as completed and record completed_at timestamp.
        No-op if run_id does not exist.
        """
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE run_history SET status='completed', completed_at=? WHERE run_id=?",
                (now, run_id)
            )
        logger.debug(f"Completed run {run_id}")

    def fail_run(self, run_id: str) -> None:
        """
        Mark run_id as failed and record completed_at timestamp.
        Called in the finally/except block when an unhandled exception
        aborts the pipeline, or when a stale running row is detected.
        """
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE run_history SET status='failed', completed_at=? WHERE run_id=?",
                (now, run_id)
            )
        logger.debug(f"Marked run {run_id} as failed")

    # ------------------------------------------------------------------
    # snapshots
    # ------------------------------------------------------------------

    def record_snapshot(
        self,
        run_id: str,
        total_value: float,
        cash: float,
        max_drift: float,
        drawdown_regime: str = "normal",
        user_id: str = "owner",
    ) -> None:
        """
        Insert a portfolio snapshot row tied to run_id.

        Called once per successful run, just before file writes (step 7),
        so the snapshot is captured even if file output or email fails.

        Args:
            run_id:           Parent run identifier.
            total_value:      Total portfolio value in dollars.
            cash:             Available cash in dollars.
            max_drift:        Maximum drift fraction across all holdings.
            drawdown_regime:  Current regime label ('normal', 'modest_dip', etc.).
            user_id:          Owner of this snapshot (default 'owner').
        """
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO snapshots "
                "(run_id, total_value, cash, max_drift, drawdown_regime, recorded_at, user_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, total_value, cash, max_drift, drawdown_regime, now, user_id)
            )
        logger.debug(f"Recorded snapshot for run {run_id}: ${total_value:,.2f}")

    def get_recent_snapshots(
        self,
        mode: Optional[str] = None,
        n: int = 2,
        user_id: str = "owner",
    ) -> List[Dict[str, Any]]:
        """
        Return the last *n* completed-run portfolio snapshots, newest first.

        Joins snapshots with run_history so each row includes run_date and mode.
        Used by the digest builder to populate the "What Changed" section.

        Args:
            mode:    Filter to this run mode ('daily', 'weekly', 'monthly'); None = all.
            n:       Maximum rows to return (default 2).
            user_id: Scope results to this user (default 'owner').

        Returns:
            List of dicts: total_value, cash, max_drift, drawdown_regime,
            recorded_at, run_date, mode.
        """
        query = (
            "SELECT s.total_value, s.cash, s.max_drift, s.drawdown_regime, "
            "       s.recorded_at, r.run_date, r.mode "
            "FROM snapshots s "
            "JOIN run_history r ON s.run_id = r.run_id "
            "WHERE r.status = 'completed' AND s.user_id = ? "
        )
        params: list = [user_id]
        if mode is not None:
            query += "AND r.mode = ? "
            params.append(mode)
        query += "ORDER BY s.recorded_at DESC LIMIT ?"
        params.append(n)

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # email_history
    # ------------------------------------------------------------------

    def was_hash_sent_recently(self, digest_hash: str, days: int = 7) -> bool:
        """
        Return True if digest_hash appears in email_history with a
        sent_at timestamp within the last `days` days.

        Used to prevent re-sending an identical email (e.g. if the weekly
        task fires twice due to Task Scheduler misconfiguration).

        Args:
            digest_hash: SHA-256 hex digest of email content.
            days:        Lookback window in days (default 7).
        """
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM email_history WHERE digest_hash=? AND sent_at>=?",
                (digest_hash, cutoff)
            ).fetchone()
        return row is not None

    def record_email_sent(self, digest_hash: str, mode: str) -> None:
        """
        Insert or update a row recording that digest_hash was sent for mode.

        INSERT OR REPLACE so re-sending with the same hash + mode updates
        the sent_at timestamp to now (which refreshes the 7-day window).

        Args:
            digest_hash: SHA-256 hex digest of email content.
            mode:        Run mode ('daily', 'weekly', 'monthly').
        """
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO email_history (digest_hash, mode, sent_at) "
                "VALUES (?, ?, ?)",
                (digest_hash, mode, now)
            )
        logger.debug(f"Recorded email hash {digest_hash[:12]}... for mode={mode}")

    # ------------------------------------------------------------------
    # portfolio_peaks
    # ------------------------------------------------------------------

    def upsert_peak(self, peak_key: str, peak_value: float) -> None:
        """
        Insert or update a peak value for peak_key.

        Called from main.py after DrawdownTracker.update() to keep the
        SQLite peaks table in sync with drawdown_state.json.

        Args:
            peak_key:   Identifier (e.g. 'all_time_high', 'rolling_12m_high').
            peak_value: Current peak value in dollars.
        """
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO portfolio_peaks (peak_key, peak_value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(peak_key) DO UPDATE SET peak_value=excluded.peak_value, "
                "updated_at=excluded.updated_at",
                (peak_key, peak_value, now)
            )
        logger.debug(f"Upserted peak {peak_key}={peak_value:,.2f}")

    def get_peak(self, peak_key: str) -> Optional[float]:
        """
        Return the current peak value for peak_key, or None if not found.

        Args:
            peak_key: Identifier (e.g. 'all_time_high').
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT peak_value FROM portfolio_peaks WHERE peak_key=?",
                (peak_key,)
            ).fetchone()
        return float(row['peak_value']) if row is not None else None

    # ------------------------------------------------------------------
    # theme_signals
    # ------------------------------------------------------------------

    def record_theme_signals(self, run_date: str, themes: List[Dict[str, Any]]) -> None:
        """
        Insert theme signal rows for run_date.

        Args:
            run_date: YYYY-MM-DD string identifying the run.
            themes:   List of theme dicts with keys: name, confidence, rationale,
                      evidence_items, direct_mentions.
        """
        now = datetime.now().isoformat()
        with self._connect() as conn:
            for theme in themes:
                conn.execute(
                    "INSERT INTO theme_signals "
                    "(run_date, theme_name, confidence, rationale, "
                    " evidence_items, direct_mentions, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_date,
                        theme.get("name", ""),
                        float(theme.get("confidence", 0.0)),
                        theme.get("rationale", ""),
                        json.dumps(theme.get("evidence_items", [])),
                        json.dumps(theme.get("direct_mentions", [])),
                        now,
                    ),
                )
        logger.debug(f"Recorded {len(themes)} theme signals for {run_date}")

    def get_recent_theme_signals(self, days: int = 7) -> List[Dict[str, Any]]:
        """
        Return theme signal rows from the last `days` calendar days.

        Args:
            days: Lookback window in days (default 7).
        """
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT run_date, theme_name, confidence, rationale, "
                "       evidence_items, direct_mentions, recorded_at "
                "FROM theme_signals WHERE run_date >= ? ORDER BY run_date DESC",
                (cutoff,),
            ).fetchall()
        result = []
        for row in rows:
            result.append({
                "run_date": row["run_date"],
                "theme_name": row["theme_name"],
                "confidence": row["confidence"],
                "rationale": row["rationale"],
                "evidence_items": json.loads(row["evidence_items"] or "[]"),
                "direct_mentions": json.loads(row["direct_mentions"] or "[]"),
                "recorded_at": row["recorded_at"],
            })
        return result

    # ------------------------------------------------------------------
    # run heartbeat
    # ------------------------------------------------------------------

    def get_last_successful_run(
        self, mode: str, user_id: str = "owner"
    ) -> Optional[Dict[str, Any]]:
        """Return the most recent completed run_history row for mode, or None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM run_history "
                "WHERE mode=? AND status='completed' AND user_id=? "
                "ORDER BY completed_at DESC LIMIT 1",
                (mode, user_id),
            ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # alert_events (alert cooldown)
    # ------------------------------------------------------------------

    def upsert_alert_event(
        self,
        fingerprint: str,
        severity: str = "",
        state_hash: str = "",
        alert_tier: str = "",
        reason_code: str = "",
        last_signal_score: float | None = None,
        last_confidence_score: float | None = None,
        last_action_taken: str = "",
    ) -> None:
        """Insert or update an alert event row."""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT times_seen FROM alert_events WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO alert_events "
                    "("
                    "fingerprint, first_seen, last_seen, times_seen, severity, state_hash, "
                    "alert_tier, reason_code, last_signal_score, last_confidence_score, last_action_taken"
                    ") "
                    "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        fingerprint,
                        now,
                        now,
                        severity,
                        state_hash,
                        alert_tier,
                        reason_code,
                        last_signal_score,
                        last_confidence_score,
                        last_action_taken,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE alert_events SET last_seen=?, times_seen=times_seen+1, "
                    "severity=?, state_hash=?, alert_tier=?, reason_code=?, "
                    "last_signal_score=?, last_confidence_score=?, last_action_taken=? "
                    "WHERE fingerprint=?",
                    (
                        now,
                        severity,
                        state_hash,
                        alert_tier,
                        reason_code,
                        last_signal_score,
                        last_confidence_score,
                        last_action_taken,
                        fingerprint,
                    ),
                )

    def get_alert_event(self, fingerprint: str) -> Optional[Dict[str, Any]]:
        """Return the alert_events row as a dict, or None if not found."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM alert_events WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
        return dict(row) if row else None

    def should_suppress_alert(
        self,
        fingerprint: str,
        cooldown_days: int,
        severity: str = "",
        state_hash: str = "",
    ) -> bool:
        """Return True if this alert should be suppressed (within cooldown, unchanged)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_emailed, severity, state_hash FROM alert_events "
                "WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
        if row is None or not row["last_emailed"]:
            return False
        try:
            last = datetime.fromisoformat(row["last_emailed"])
        except (ValueError, TypeError):
            return False
        if (datetime.now() - last).days >= cooldown_days:
            return False
        if severity != row["severity"]:
            return False
        if state_hash and state_hash != row["state_hash"]:
            return False
        return True

    def record_alert_emailed(self, fingerprint: str) -> None:
        """Record that this alert was just emailed."""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE alert_events SET last_emailed=? WHERE fingerprint=?",
                (now, fingerprint),
            )

    # ------------------------------------------------------------------
    # watchlist_alert_outcomes (alert lifecycle / follow-through tracking)
    # ------------------------------------------------------------------

    def get_watchlist_alert_outcome(
        self,
        fingerprint: str,
        state_hash: str = "",
    ) -> Optional[Dict[str, Any]]:
        """
        Return the latest pending watchlist alert outcome row for this alert state.

        If state_hash is provided, this looks for an exact active lifecycle episode.
        """
        query = (
            "SELECT * FROM watchlist_alert_outcomes "
            "WHERE fingerprint=? AND outcome_pending=1 "
        )
        params: list[Any] = [fingerprint]
        if state_hash:
            query += "AND state_hash=? "
            params.append(state_hash)
        query += "ORDER BY surfaced_at DESC LIMIT 1"

        with self._connect() as conn:
            row = conn.execute(query, tuple(params)).fetchone()
        return dict(row) if row else None

    def record_watchlist_alert_surface(
        self,
        fingerprint: str,
        state_hash: str,
        alert_data: Dict[str, Any],
        evaluation_window: str = "1d,3d,5d,10d",
    ) -> Dict[str, Any]:
        """
        Create or refresh a pending watchlist alert lifecycle episode.

        Reuses an existing pending row when the fingerprint + state_hash match,
        so repeated unchanged resurfacing does not create duplicate active records.
        A materially changed alert state creates a new lifecycle row.
        """
        now = datetime.now().isoformat()
        existing = self.get_watchlist_alert_outcome(fingerprint, state_hash=state_hash)

        if existing is not None:
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE watchlist_alert_outcomes
                    SET last_seen_at=?,
                        notification_status=?,
                        alert_priority=?,
                        alert_quality_tier=?,
                        confirmation_count=?,
                        evidence_breadth=?,
                        portfolio_priority=?,
                        overlap_penalty=?,
                        diversification_bonus=?,
                        existing_position_relevance=?,
                        budget_fit=?
                    WHERE id=?
                    """,
                    (
                        now,
                        str(alert_data.get("notification_status") or "alerted"),
                        str(alert_data.get("alert_priority") or ""),
                        str(alert_data.get("alert_quality_tier") or "none"),
                        int(alert_data.get("confirmation_count") or 0),
                        int(alert_data.get("evidence_breadth") or 0),
                        float(alert_data.get("portfolio_priority") or 0.0),
                        float(alert_data.get("overlap_penalty") or 0.0),
                        float(alert_data.get("diversification_bonus") or 0.0),
                        float(alert_data.get("existing_position_relevance_bonus") or 0.0),
                        str(alert_data.get("budget_fit") or "unknown"),
                        int(existing["id"]),
                    ),
                )
            refreshed = self.get_watchlist_alert_outcome(fingerprint, state_hash=state_hash)
            return refreshed or existing

        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO watchlist_alert_outcomes (
                    fingerprint, state_hash, ticker, watchlist_source,
                    surfaced_at, last_seen_at, notification_status,
                    alert_priority, alert_quality_tier,
                    confirmation_count, evidence_breadth,
                    portfolio_priority, overlap_penalty, diversification_bonus,
                    existing_position_relevance, budget_fit,
                    baseline_price, baseline_signal_score, baseline_confidence_score,
                    evaluation_window, outcome_status, outcome_pending
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 1)
                """,
                (
                    fingerprint,
                    state_hash or "",
                    str(alert_data.get("ticker") or ""),
                    str(alert_data.get("watchlist_source") or "static"),
                    now,
                    now,
                    str(alert_data.get("notification_status") or "alerted"),
                    str(alert_data.get("alert_priority") or ""),
                    str(alert_data.get("alert_quality_tier") or "none"),
                    int(alert_data.get("confirmation_count") or 0),
                    int(alert_data.get("evidence_breadth") or 0),
                    float(alert_data.get("portfolio_priority") or 0.0),
                    float(alert_data.get("overlap_penalty") or 0.0),
                    float(alert_data.get("diversification_bonus") or 0.0),
                    float(alert_data.get("existing_position_relevance_bonus") or 0.0),
                    str(alert_data.get("budget_fit") or "unknown"),
                    float(alert_data.get("price")) if alert_data.get("price") is not None else None,
                    float(alert_data.get("signal_score")) if alert_data.get("signal_score") is not None else None,
                    float(alert_data.get("confidence_score")) if alert_data.get("confidence_score") is not None else None,
                    evaluation_window,
                ),
            )
            outcome_id = int(cur.lastrowid)

        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM watchlist_alert_outcomes WHERE id=?",
                (outcome_id,),
            ).fetchone()
        return dict(row) if row else {"id": outcome_id}

    def get_watchlist_alert_outcomes(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return recent watchlist alert lifecycle rows, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM watchlist_alert_outcomes ORDER BY surfaced_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_pending_watchlist_alert_outcomes(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return pending watchlist alert lifecycle rows, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM watchlist_alert_outcomes
                WHERE outcome_pending=1
                ORDER BY surfaced_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def resolve_watchlist_alert_outcome(
        self,
        outcome_id: int,
        *,
        evaluation_price: float,
        return_pct: float,
        evaluated_at: Optional[str] = None,
        outcome_label: str,
        outcome_status: str = "resolved_1d",
    ) -> Optional[Dict[str, Any]]:
        """
        Resolve a pending watchlist alert lifecycle row with a first-pass outcome.
        """
        now = evaluated_at or datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE watchlist_alert_outcomes
                SET evaluation_price=?,
                    return_pct=?,
                    evaluated_at=?,
                    outcome_label=?,
                    outcome_status=?,
                    outcome_pending=0,
                    resolved_at=?
                WHERE id=? AND outcome_pending=1
                """,
                (
                    float(evaluation_price),
                    float(return_pct),
                    now,
                    outcome_label,
                    outcome_status,
                    now,
                    int(outcome_id),
                ),
            )
            row = conn.execute(
                "SELECT * FROM watchlist_alert_outcomes WHERE id=?",
                (int(outcome_id),),
            ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # watchlist_signal_feedback (parallel signal learning loop)
    # ------------------------------------------------------------------

    @staticmethod
    def _feedback_window_columns(window_days: int) -> tuple[str, str, str, str, str]:
        if window_days not in {1, 3, 7}:
            raise ValueError(f"Unsupported signal feedback window: {window_days}")
        suffix = f"{window_days}d"
        return (
            f"outcome_return_{suffix}",
            f"outcome_success_{suffix}",
            f"direction_correct_{suffix}",
            f"outcome_price_{suffix}",
            f"evaluated_at_{suffix}",
        )

    def record_watchlist_signal_feedback(
        self,
        *,
        signal_key: str,
        ticker: str,
        signal_time: str,
        watchlist_source: str = "static",
        signal_score: float | None = None,
        confidence_score: float | None = None,
        effective_score: float | None = None,
        conviction_score: float | None = None,
        conviction_band: str | None = None,
        normalized_allocation: float | None = None,
        price_at_signal: float | None = None,
        prediction_intent: str = "up",
        data_mode: str = "live",
        degraded_mode: bool = False,
        regime_label: str = "neutral",
        regime_confidence: float | None = None,
        regime_data_quality: str = "limited",
        theme_alignment_score: float | None = None,
        theme_top_name: str | None = None,
        theme_type: str | None = None,
        portfolio_fit_score: float | None = None,
        portfolio_fit_label: str | None = None,
        final_rank_score: float | None = None,
        augmented_signal_score: float | None = None,
    ) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO watchlist_signal_feedback (
                    signal_key, ticker, signal_time, watchlist_source,
                    signal_score, confidence_score, effective_score,
                    conviction_score, conviction_band, normalized_allocation,
                    price_at_signal, prediction_intent, data_mode, degraded_mode,
                    regime_label, regime_confidence, regime_data_quality,
                    theme_alignment_score, theme_top_name, theme_type,
                    portfolio_fit_score, portfolio_fit_label, final_rank_score,
                    augmented_signal_score
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal_key,
                    ticker,
                    signal_time,
                    watchlist_source,
                    signal_score,
                    confidence_score,
                    effective_score,
                    conviction_score,
                    conviction_band,
                    normalized_allocation,
                    price_at_signal,
                    prediction_intent,
                    data_mode,
                    1 if degraded_mode else 0,
                    regime_label,
                    regime_confidence,
                    regime_data_quality,
                    theme_alignment_score,
                    theme_top_name,
                    theme_type,
                    portfolio_fit_score,
                    portfolio_fit_label,
                    final_rank_score,
                    augmented_signal_score,
                ),
            )
            row = conn.execute(
                "SELECT * FROM watchlist_signal_feedback WHERE signal_key=?",
                (signal_key,),
            ).fetchone()
        return dict(row) if row else None

    def get_watchlist_signal_feedback(
        self,
        *,
        ticker: str | None = None,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM watchlist_signal_feedback "
        params: list[Any] = []
        if ticker:
            query += "WHERE ticker=? "
            params.append(ticker)
        query += "ORDER BY signal_time DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def get_pending_watchlist_signal_feedback(
        self,
        *,
        window_days: int,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        _, _, _, _, evaluated_col = self._feedback_window_columns(window_days)
        query = (
            f"SELECT * FROM watchlist_signal_feedback "
            f"WHERE {evaluated_col} IS NULL "
            f"ORDER BY signal_time ASC, id ASC LIMIT ?"
        )
        with self._connect() as conn:
            rows = conn.execute(query, (limit,)).fetchall()
        return [dict(row) for row in rows]

    def resolve_watchlist_signal_feedback(
        self,
        feedback_id: int,
        *,
        window_days: int,
        outcome_price: float,
        return_pct: float,
        outcome_success: bool,
        direction_correct: bool,
        evaluated_at: str | None = None,
    ) -> Optional[Dict[str, Any]]:
        return_col, success_col, direction_col, price_col, evaluated_col = self._feedback_window_columns(window_days)
        now = evaluated_at or datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE watchlist_signal_feedback
                SET {price_col}=?,
                    {return_col}=?,
                    {success_col}=?,
                    {direction_col}=?,
                    {evaluated_col}=?
                WHERE id=?
                """,
                (
                    float(outcome_price),
                    float(return_pct),
                    1 if outcome_success else 0,
                    1 if direction_correct else 0,
                    now,
                    int(feedback_id),
                ),
            )
            row = conn.execute(
                "SELECT * FROM watchlist_signal_feedback WHERE id=?",
                (int(feedback_id),),
            ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # subsystem_health (circuit breaker)
    # ------------------------------------------------------------------

    def record_subsystem_success(self, subsystem: str) -> None:
        """Reset failure count and clear disabled_until for a subsystem."""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO subsystem_health "
                "(subsystem, consecutive_failures, disabled_until, last_error, last_success) "
                "VALUES (?, 0, NULL, NULL, ?) "
                "ON CONFLICT(subsystem) DO UPDATE SET "
                "consecutive_failures=0, disabled_until=NULL, last_success=?",
                (subsystem, now, now),
            )

    def record_subsystem_failure(
        self,
        subsystem: str,
        error: str = "",
        failure_threshold: int = 3,
        disable_hours: int = 24,
    ) -> None:
        """Record a failure; auto-disables after failure_threshold consecutive failures."""
        now = datetime.now()
        now_iso = now.isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT consecutive_failures FROM subsystem_health WHERE subsystem=?",
                (subsystem,),
            ).fetchone()
            failures = (row["consecutive_failures"] if row else 0) + 1
            disabled_until: Optional[str] = None
            if failures >= failure_threshold:
                disabled_until = (now + timedelta(hours=disable_hours)).isoformat()
                logger.warning(
                    "subsystem_health: %s disabled until %s after %d consecutive failures",
                    subsystem, disabled_until, failures,
                )
            conn.execute(
                "INSERT INTO subsystem_health "
                "(subsystem, consecutive_failures, disabled_until, last_error, last_success) "
                "VALUES (?, ?, ?, ?, NULL) "
                "ON CONFLICT(subsystem) DO UPDATE SET "
                "consecutive_failures=?, disabled_until=?, last_error=?",
                (subsystem, failures, disabled_until, error,
                 failures, disabled_until, error),
            )

    def is_subsystem_disabled(self, subsystem: str) -> bool:
        """Return True if the subsystem circuit breaker is currently open."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT disabled_until FROM subsystem_health WHERE subsystem=?",
                (subsystem,),
            ).fetchone()
        if row is None or not row["disabled_until"]:
            return False
        try:
            return datetime.now() < datetime.fromisoformat(row["disabled_until"])
        except (ValueError, TypeError):
            return False

    def get_subsystem_health(self, subsystem: str) -> Optional[Dict[str, Any]]:
        """Return the full subsystem_health row as a dict, or None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM subsystem_health WHERE subsystem=?",
                (subsystem,),
            ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # structural_violations
    # ------------------------------------------------------------------

    def upsert_structural_violation(self, violation_key: str) -> Dict[str, Any]:
        """Insert or update a structural violation. Returns updated row."""
        now_iso = datetime.now().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT first_seen FROM structural_violations WHERE violation_key=?",
                (violation_key,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO structural_violations "
                    "(violation_key, first_seen, last_seen, days_active, escalation_level) "
                    "VALUES (?, ?, ?, 0, 0)",
                    (violation_key, now_iso, now_iso),
                )
                days_active, escalation = 0, 0
            else:
                try:
                    days_active = (datetime.now() - datetime.fromisoformat(row["first_seen"])).days
                except (ValueError, TypeError):
                    days_active = 0
                escalation = 3 if days_active >= 42 else 2 if days_active >= 21 else 1 if days_active >= 7 else 0
                conn.execute(
                    "UPDATE structural_violations "
                    "SET last_seen=?, days_active=?, escalation_level=? "
                    "WHERE violation_key=?",
                    (now_iso, days_active, escalation, violation_key),
                )
        return self.get_structural_violation(violation_key) or {}

    def clear_structural_violation(self, violation_key: str) -> None:
        """Remove a structural violation (called when it resolves)."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM structural_violations WHERE violation_key=?",
                (violation_key,),
            )

    def get_structural_violation(self, violation_key: str) -> Optional[Dict[str, Any]]:
        """Return the structural_violations row as a dict, or None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM structural_violations WHERE violation_key=?",
                (violation_key,),
            ).fetchone()
        return dict(row) if row else None

    def get_all_structural_violations(self) -> List[Dict[str, Any]]:
        """Return all active structural violation rows."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM structural_violations ORDER BY days_active DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # cash_ledger
    # ------------------------------------------------------------------

    def get_cash_balance(self) -> Optional[float]:
        """Return current cash balance from the ledger, or None if empty."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT SUM(amount) AS total FROM cash_ledger"
            ).fetchone()
        total = row["total"] if row else None
        return float(total) if total is not None else None

    def add_cash_entry(self, type_: str, amount: float, note: str = "") -> None:
        """Append a cash ledger entry. amount positive=inflow, negative=outflow."""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO cash_ledger (timestamp, type, amount, note) "
                "VALUES (?, ?, ?, ?)",
                (now, type_, amount, note),
            )
        logger.debug("cash_ledger: %s %.2f (%s)", type_, amount, note)
