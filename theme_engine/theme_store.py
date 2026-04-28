"""
Theme Store — persists theme signals to SQLite and writes JSON output files.

Reuses the existing data/portfolio.db database (adds a theme_signals table).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS theme_signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date        TEXT NOT NULL,
    theme_name      TEXT NOT NULL,
    confidence      REAL NOT NULL,
    rationale       TEXT,
    evidence_items  TEXT,
    direct_mentions TEXT,
    recorded_at     TEXT NOT NULL
)
"""

_CREATE_IDX = """
CREATE INDEX IF NOT EXISTS idx_theme_signals_run_date
    ON theme_signals (run_date)
"""


class ThemeStore:
    """Persist theme signals and write output JSON files.

    Args:
        db_path:    Path to the SQLite database (data/portfolio.db).
        output_dir: Directory to write theme_signals.json + watch_candidates.json.
    """

    def __init__(
        self,
        db_path: str = "data/portfolio.db",
        output_dir: str = "outputs/latest",
    ) -> None:
        self.db_path = Path(db_path)
        self.output_dir = Path(output_dir)
        self._ensure_table()

    # ── Public API ────────────────────────────────────────────────────────────

    def save_signals(
        self,
        themes: list[dict[str, Any]],
        watch_candidates: list[dict[str, Any]],
        run_date: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Persist themes to SQLite and write JSON output files.

        When new themes is empty the method preserves the last valid theme
        snapshot so that top_theme never regresses to blank.  Resolution order:
          1. themes from current theme_signals.json (previous fresh/stale run)
          2. themes from SQLite (survives file resets and fresh deploys)
          3. nothing — writes an empty-themes file only on a genuine cold start

        Args:
            themes:           Enriched theme list (with tickers, persistence_7d).
            watch_candidates: Flat ticker list from mapper.
            run_date:         YYYY-MM-DD string; defaults to today.
        """
        if run_date is None:
            run_date = date.today().isoformat()

        now_iso = datetime.now(timezone.utc).isoformat()

        # Write to SQLite (only for fresh themes — stale runs must not overwrite
        # real records with an empty payload)
        conn = sqlite3.connect(str(self.db_path))
        try:
            for theme in themes:
                conn.execute(
                    """
                    INSERT INTO theme_signals
                        (run_date, theme_name, confidence, rationale,
                         evidence_items, direct_mentions, recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_date,
                        theme.get("name", ""),
                        float(theme.get("confidence", 0.0)),
                        theme.get("rationale", ""),
                        json.dumps(theme.get("evidence_items", [])),
                        json.dumps(theme.get("direct_mentions", [])),
                        now_iso,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        # Write output JSON files
        self.output_dir.mkdir(parents=True, exist_ok=True)

        signals_path = self.output_dir / "theme_signals.json"
        signals_payload: dict[str, Any] = {
            "generated_at": now_iso,
            "run_date": run_date,
            "data_mode": (metadata or {}).get("data_mode", "live"),
            "degraded_mode": bool((metadata or {}).get("degraded_mode", False)),
            "degraded_reason": (metadata or {}).get("degraded_reason"),
            "data_sources_used": list((metadata or {}).get("data_sources_used", ["rss", "sp500_cache"])),
            "themes": themes,
            "theme_source": "fresh",
            "no_update": False,
        }

        candidates_path = self.output_dir / "watch_candidates.json"
        candidates_payload: dict[str, Any] = {
            "generated_at": now_iso,
            "run_date": run_date,
            "data_mode": (metadata or {}).get("data_mode", "live"),
            "degraded_mode": bool((metadata or {}).get("degraded_mode", False)),
            "degraded_reason": (metadata or {}).get("degraded_reason"),
            "data_sources_used": list((metadata or {}).get("data_sources_used", ["rss", "sp500_cache"])),
            "watch_candidates": watch_candidates,
            "theme_source": "fresh",
            "no_update": False,
        }

        if not themes:
            existing_signals = self._safe_read_json(signals_path)
            existing_candidates = self._safe_read_json(candidates_path)
            existing_theme_rows: list[dict[str, Any]] = existing_signals.get("themes") or []

            # Primary fallback: on-disk JSON from the previous run.
            # Secondary fallback: SQLite DB — survives file resets and fresh
            # deployments where no theme_signals.json has been written yet.
            if not existing_theme_rows:
                existing_theme_rows = self.get_last_valid_themes()
                logger.debug(
                    "ThemeStore: no themes in JSON; SQLite fallback returned %d theme(s)",
                    len(existing_theme_rows),
                )

            if isinstance(existing_theme_rows, list) and existing_theme_rows:
                signals_payload = {
                    **existing_signals,
                    "generated_at": now_iso,
                    "run_date": run_date,
                    "data_mode": (metadata or {}).get("data_mode", existing_signals.get("data_mode", "live")),
                    "degraded_mode": bool((metadata or {}).get("degraded_mode", existing_signals.get("degraded_mode", False))),
                    "degraded_reason": (metadata or {}).get("degraded_reason", existing_signals.get("degraded_reason")),
                    "data_sources_used": list((metadata or {}).get("data_sources_used", existing_signals.get("data_sources_used", ["rss", "sp500_cache"]))),
                    "theme_source": "stale",
                    "no_update": True,
                    "last_checked_at": now_iso,
                    "themes": existing_theme_rows,
                }
                candidates_payload = {
                    **existing_candidates,
                    "generated_at": now_iso,
                    "run_date": run_date,
                    "data_mode": (metadata or {}).get("data_mode", existing_candidates.get("data_mode", signals_payload["data_mode"])),
                    "degraded_mode": bool((metadata or {}).get("degraded_mode", existing_candidates.get("degraded_mode", signals_payload["degraded_mode"]))),
                    "degraded_reason": (metadata or {}).get("degraded_reason", existing_candidates.get("degraded_reason", signals_payload["degraded_reason"])),
                    "data_sources_used": list((metadata or {}).get("data_sources_used", existing_candidates.get("data_sources_used", signals_payload["data_sources_used"]))),
                    "theme_source": "stale",
                    "no_update": True,
                    "last_checked_at": now_iso,
                    "watch_candidates": list(existing_candidates.get("watch_candidates") or watch_candidates),
                }

        theme_source_label = signals_payload.get("theme_source", "fresh")
        signals_path.write_text(
            json.dumps(signals_payload, indent=2, default=str),
            encoding="utf-8",
        )
        candidates_path.write_text(
            json.dumps(candidates_payload, indent=2, default=str),
            encoding="utf-8",
        )

        logger.info(
            "ThemeStore: saved %d themes + %d candidates to %s (source=%s)",
            len(themes),
            len(watch_candidates),
            self.output_dir,
            theme_source_label,
        )

    def get_recent_signals(self, days: int = 7) -> list[dict[str, Any]]:
        """Return all theme signal rows from the last `days` calendar days."""
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.execute(
                """
                SELECT run_date, theme_name, confidence, rationale,
                       evidence_items, direct_mentions, recorded_at
                FROM theme_signals
                WHERE run_date >= ?
                ORDER BY run_date DESC
                """,
                (cutoff,),
            )
            rows = []
            for row in cursor.fetchall():
                rows.append({
                    "run_date": row[0],
                    "theme_name": row[1],
                    "confidence": row[2],
                    "rationale": row[3],
                    "evidence_items": json.loads(row[4] or "[]"),
                    "direct_mentions": json.loads(row[5] or "[]"),
                    "recorded_at": row[6],
                })
            return rows
        finally:
            conn.close()

    def compute_persistence(self, theme_name: str, days: int = 7) -> int:
        """Count distinct calendar days theme_name appeared in the last `days` days."""
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.execute(
                """
                SELECT COUNT(DISTINCT run_date)
                FROM theme_signals
                WHERE theme_name = ? AND run_date >= ?
                """,
                (theme_name, cutoff),
            )
            row = cursor.fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def get_last_valid_themes(self) -> list[dict[str, Any]]:
        """Return theme records from the most recent run that produced themes.

        Queries SQLite directly, bypassing theme_signals.json.  Used as a
        secondary fallback inside save_signals() when the JSON file is absent
        or contains no themes (e.g. after a fresh deploy or file reset).

        Returns [] if the database has never recorded any themes.
        """
        if not self.db_path.exists():
            return []
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.execute(
                """
                SELECT theme_name, confidence, rationale,
                       evidence_items, direct_mentions
                FROM theme_signals
                WHERE run_date = (SELECT MAX(run_date) FROM theme_signals)
                ORDER BY confidence DESC
                """
            )
            rows = cursor.fetchall()
            return [
                {
                    "name": r[0],
                    "confidence": float(r[1]),
                    "rationale": r[2] or "",
                    "evidence_items": json.loads(r[3] or "[]"),
                    "direct_mentions": json.loads(r[4] or "[]"),
                }
                for r in rows
            ]
        finally:
            conn.close()

    # ── Private ───────────────────────────────────────────────────────────────

    def _ensure_table(self) -> None:
        if not self.db_path.parent.exists():
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(_CREATE_TABLE)
            conn.execute(_CREATE_IDX)
            conn.commit()
        finally:
            conn.close()

    def _safe_read_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}
