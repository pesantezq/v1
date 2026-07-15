"""
Extended Watchlist — dynamic symbol layer sourced from theme engine candidates.

Symbols are promoted here when the theme engine surfaces a high-confidence
candidate that is NOT already in the static watchlist.  They expire after a
configurable TTL (default 7 days) unless reinforced by a subsequent run.

Promotion rules (all must pass):
  - Not already in static watchlist
  - theme confidence >= confidence_threshold (default 0.80)
  - Reinforcement evidence: appears in >=2 themes OR has a "direct" mention
  - Active extended watchlist has headroom (below max_symbols cap)

Budget protection:
  The scanner must call get_active_symbols() to check for extended entries AFTER
  confirming there is budget headroom for the static symbols.  Extended symbols
  are only passed to the scanner when budget allows.

Persistence:
  Uses data/portfolio.db (same SQLite DB as the main state store).
  Table: extended_watchlist — see _DDL for schema.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("watchlist_scanner.extended_watchlist")

_DDL = """
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

_DEFAULT_CONFIDENCE_THRESHOLD = 0.80
_DEFAULT_TTL_DAYS = 7
_DEFAULT_MAX_SYMBOLS = 3
# Distinct days a single-theme candidate must persist to count as reinforced.
# Set to 0 to disable the cross-day persistence path (pre-2026-05-30 behavior).
_DEFAULT_REINFORCE_PERSISTENCE_DAYS = 3


class ExtendedWatchlist:
    """
    Manages the dynamic extended watchlist layer.

    Args:
        db_path:              Path to data/portfolio.db.
        ttl_days:             Days before an unreinforced symbol expires.
        max_symbols:          Max active extended symbols at any time.
        confidence_threshold: Min theme confidence to consider promotion.
    """

    def __init__(
        self,
        db_path: str | Path = "data/portfolio.db",
        ttl_days: int = _DEFAULT_TTL_DAYS,
        max_symbols: int = _DEFAULT_MAX_SYMBOLS,
        confidence_threshold: float = _DEFAULT_CONFIDENCE_THRESHOLD,
        reinforce_persistence_days: int = _DEFAULT_REINFORCE_PERSISTENCE_DAYS,
    ) -> None:
        self.db_path = Path(db_path)
        self.ttl_days = ttl_days
        self.max_symbols = max_symbols
        self.confidence_threshold = confidence_threshold
        self.reinforce_persistence_days = reinforce_persistence_days
        self._ensure_table()

    # ── DB helpers ─────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_table(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute(_DDL)
            conn.commit()
        finally:
            conn.close()

    # ── Read ───────────────────────────────────────────────────────────────────

    def get_active_symbols(self) -> list[dict[str, Any]]:
        """Return all active (non-expired, non-dropped) entries, sorted by confidence desc."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM extended_watchlist WHERE is_active=1 "
                "ORDER BY theme_confidence DESC"
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def get_active_tickers(self) -> list[str]:
        """Return just the ticker strings of active entries."""
        return [r["symbol"] for r in self.get_active_symbols()]

    def get_history(self, limit: int = 30) -> list[dict[str, Any]]:
        """Return the most-recent rows (active + inactive) for outcome reporting."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM extended_watchlist ORDER BY promoted_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
        return self._decode_rows(rows)

    def get_outcome_history(self, days: int = 14) -> list[dict[str, Any]]:
        """Return rows with a meaningful outcome (not 'none') from the last N days.

        Sorted by priority: alerted first, then scanned, then expired.
        Used to build the learning/outcome section of the digest.
        """
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT * FROM extended_watchlist
                WHERE outcome != 'none'
                  AND promoted_at >= ?
                ORDER BY
                    CASE outcome
                        WHEN 'alerted' THEN 1
                        WHEN 'scanned' THEN 2
                        ELSE 3
                    END,
                    promoted_at DESC
                """,
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()
        return self._decode_rows(rows)

    @staticmethod
    def _decode_rows(rows: list) -> list[dict[str, Any]]:
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["theme_names"] = json.loads(d.get("theme_names") or "[]")
            except Exception:
                d["theme_names"] = []
            result.append(d)
        return result

    # ── Promotion ──────────────────────────────────────────────────────────────

    def evaluate_candidates(
        self,
        candidates: list[dict[str, Any]],
        static_watchlist: list[str],
        run_date: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Evaluate watch_candidates for extended watchlist promotion.

        Args:
            candidates:       watch_candidates list from the theme engine.
            static_watchlist: tickers in the configured static watchlist.
            run_date:         YYYY-MM-DD string; defaults to today.

        Returns dict with keys:
            promoted     list[str]            — newly promoted tickers
            reinforced   list[str]            — already-active tickers refreshed
            expired      list[str]            — tickers just expired by TTL
            skipped      list[{symbol,reason}]— eligible but not promoted
            run_date     str
        """
        if run_date is None:
            run_date = datetime.now().strftime("%Y-%m-%d")

        # Step 1 — expire stale entries
        expired = self.expire_stale()

        static_upper = {s.upper() for s in static_watchlist}
        active = self.get_active_symbols()
        active_set = {r["symbol"] for r in active}

        promoted: list[str] = []
        reinforced: list[str] = []
        skipped: list[dict[str, Any]] = []

        # Pass 1 — classify ALL candidates into skip reasons before promotion logic
        for c in candidates:
            sym = c.get("ticker", "").upper()
            conf = float(c.get("confidence", 0.0))
            if not sym:
                continue
            if sym in static_upper:
                skipped.append({
                    "symbol": sym,
                    "reason": "in_static_watchlist",
                    "confidence": conf,
                })
            elif conf < self.confidence_threshold:
                skipped.append({
                    "symbol": sym,
                    "reason": "below_confidence_threshold",
                    "confidence": conf,
                })

        # Pass 2 — promotion logic on eligible candidates only
        eligible = [
            c for c in candidates
            if c.get("ticker", "").upper() not in static_upper
            and float(c.get("confidence", 0.0)) >= self.confidence_threshold
        ]
        eligible.sort(key=lambda c: float(c.get("confidence", 0.0)), reverse=True)

        for c in eligible:
            sym = c["ticker"].upper()
            conf = float(c.get("confidence", 0.0))
            themes: list[str] = c.get("themes") or []
            sources: list[str] = c.get("sources") or []
            primary_theme = themes[0] if themes else "unknown"

            # Reinforcement evidence: multi-theme OR direct mention OR a
            # single-theme candidate that has persisted across enough distinct
            # days (cross-day persistence — see _DEFAULT_REINFORCE_PERSISTENCE_DAYS).
            persistence = int(c.get("persistence_7d", 0) or 0)
            is_reinforced = (
                len(themes) >= 2
                or "direct" in sources
                or (
                    self.reinforce_persistence_days > 0
                    and persistence >= self.reinforce_persistence_days
                )
            )

            if sym in active_set:
                # Already promoted — refresh TTL and confidence
                self._reinforce(sym, conf, themes)
                reinforced.append(sym)
                continue

            if not is_reinforced:
                skipped.append({
                    "symbol": sym,
                    "reason": "insufficient_reinforcement",
                    "confidence": conf,
                })
                continue

            current_active_count = len(active_set) + len(promoted)
            if current_active_count >= self.max_symbols:
                skipped.append({
                    "symbol": sym,
                    "reason": "extended_watchlist_full",
                    "confidence": conf,
                })
                continue

            # Promote
            self._promote(sym, primary_theme, themes, conf)
            active_set.add(sym)
            promoted.append(sym)

        return {
            "promoted": promoted,
            "reinforced": reinforced,
            "expired": expired,
            "skipped": skipped,
            "run_date": run_date,
        }

    def promote_operator_approved(
        self,
        symbol: str,
        theme: str = "operator_approved",
        confidence: float = 0.9,
        static_watchlist: list[str] | None = None,
    ) -> dict[str, Any]:
        """Promote a symbol on explicit human approval.

        An operator approval is authoritative reinforcement: it bypasses the
        multi-day-persistence / multi-theme gate (which exists to qualify
        *automatic* discovery), but still respects every other gate —
        static-watchlist membership, already-active (reinforce instead of
        duplicate), and the ``max_symbols`` capacity cap.

        Returns ``{status: promoted|reinforced|skipped, reason, symbol}``.
        """
        sym = (symbol or "").upper()
        if not sym:
            return {"status": "skipped", "reason": "empty_symbol", "symbol": sym}

        static_upper = {s.upper() for s in (static_watchlist or [])}
        if sym in static_upper:
            return {"status": "skipped", "reason": "in_static_watchlist", "symbol": sym}

        active_set = {r["symbol"] for r in self.get_active_symbols()}
        if sym in active_set:
            self._reinforce(sym, confidence, [theme])
            return {"status": "reinforced", "reason": "already_active", "symbol": sym}

        if len(active_set) >= self.max_symbols:
            return {"status": "skipped", "reason": "extended_watchlist_full", "symbol": sym}

        self._promote(sym, theme, [theme], confidence)
        return {"status": "promoted", "reason": "operator_approved", "symbol": sym}

    def _promote(
        self,
        symbol: str,
        theme_name: str,
        theme_names: list[str],
        confidence: float,
    ) -> None:
        now = datetime.now().isoformat()
        expires = (datetime.now() + timedelta(days=self.ttl_days)).isoformat()
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO extended_watchlist
                    (symbol, is_active, promoted_at, expires_at, last_reinforced,
                     theme_name, theme_names, theme_confidence, mention_count,
                     scan_count, alert_count, outcome, drop_reason)
                VALUES (?, 1, ?, ?, ?, ?, ?, ?, 1, 0, 0, 'none', NULL)
                ON CONFLICT(symbol) DO UPDATE SET
                    is_active=1,
                    expires_at=excluded.expires_at,
                    last_reinforced=excluded.last_reinforced,
                    theme_name=excluded.theme_name,
                    theme_names=excluded.theme_names,
                    theme_confidence=MAX(theme_confidence, excluded.theme_confidence),
                    mention_count=mention_count+1,
                    drop_reason=NULL,
                    outcome=CASE WHEN outcome='expired' THEN 'none' ELSE outcome END
                """,
                (symbol, now, expires, now, theme_name, json.dumps(theme_names), confidence),
            )
            conn.commit()
        finally:
            conn.close()
        logger.info(
            "ExtendedWatchlist: promoted %s (conf=%.2f, themes=%s)",
            symbol, confidence, theme_names,
        )

    def _reinforce(
        self,
        symbol: str,
        confidence: float,
        theme_names: list[str],
    ) -> None:
        now = datetime.now().isoformat()
        expires = (datetime.now() + timedelta(days=self.ttl_days)).isoformat()
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE extended_watchlist SET
                    last_reinforced=?,
                    expires_at=?,
                    theme_confidence=MAX(theme_confidence, ?),
                    mention_count=mention_count+1
                WHERE symbol=? AND is_active=1
                """,
                (now, expires, confidence, symbol),
            )
            conn.commit()
        finally:
            conn.close()
        logger.debug("ExtendedWatchlist: reinforced %s (conf=%.2f)", symbol, confidence)

    # ── Expiry ─────────────────────────────────────────────────────────────────

    def expire_stale(self) -> list[str]:
        """Deactivate any entries whose expires_at has passed. Returns expired symbols."""
        now = datetime.now().isoformat()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT symbol FROM extended_watchlist WHERE is_active=1 AND expires_at < ?",
                (now,),
            ).fetchall()
            expired = [r["symbol"] for r in rows]
            if expired:
                conn.execute(
                    "UPDATE extended_watchlist "
                    "SET is_active=0, outcome='expired', drop_reason='ttl_expired' "
                    "WHERE is_active=1 AND expires_at < ?",
                    (now,),
                )
                conn.commit()
            for sym in expired:
                logger.info("ExtendedWatchlist: expired %s (TTL elapsed)", sym)
        finally:
            conn.close()
        return expired

    # ── Outcome tracking ───────────────────────────────────────────────────────

    def record_scan(self, symbol: str, alerted: bool = False) -> None:
        """Record that symbol was scanned this run (and optionally triggered an alert).

        Outcome follows a monotonically improving state:
            none → scanned → alerted
        "alerted" is never downgraded back to "scanned" on subsequent runs.
        """
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE extended_watchlist SET
                    scan_count = scan_count + 1,
                    alert_count = alert_count + ?,
                    outcome = CASE
                        WHEN outcome = 'alerted' THEN 'alerted'
                        WHEN ? = 1              THEN 'alerted'
                        ELSE                         'scanned'
                    END
                WHERE symbol = ?
                """,
                (1 if alerted else 0, 1 if alerted else 0, symbol),
            )
            conn.commit()
        finally:
            conn.close()

    def drop_for_budget(self, symbol: str) -> None:
        """Deactivate symbol because budget was insufficient after core scan."""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE extended_watchlist SET is_active=0, drop_reason='budget' WHERE symbol=?",
                (symbol,),
            )
            conn.commit()
        finally:
            conn.close()
        logger.info("ExtendedWatchlist: dropped %s — budget pressure", symbol)

    # ── Auto-approval (simulation) primitives ──────────────────────────────────
    #
    # These serve the bounded GPT auto-approval channel, which operates ONLY against
    # a SEPARATE simulation DB (never the production DB read by the scanner). They are
    # deliberately distinct from `promote_operator_approved` (the human path) so the
    # auto-approval channel can never travel a human-approval code path.

    _COLUMNS = (
        "symbol", "is_active", "promoted_at", "expires_at", "last_reinforced",
        "theme_name", "theme_names", "theme_confidence", "mention_count",
        "scan_count", "alert_count", "outcome", "drop_reason",
    )

    def get_symbol(self, symbol: str) -> dict[str, Any] | None:
        """Return the full row for *symbol* (active OR inactive), or None if absent.

        Used to capture the exact before/after state for compare-and-swap rollback.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM extended_watchlist WHERE symbol=?", (symbol.upper(),)
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

    def promote_auto_approved(
        self, symbol: str, confidence: float = 0.9,
        theme: str = "auto_approval_sim",
    ) -> dict[str, Any]:
        """Promote a symbol via the SIMULATION auto-approval channel (NOT human).

        Skips (never duplicates) an already-active symbol; otherwise inserts/reactivates.
        Returns ``{status: promoted|skipped, reason, symbol}``.
        """
        sym = (symbol or "").upper()
        if not sym:
            return {"status": "skipped", "reason": "empty_symbol", "symbol": sym}
        row = self.get_symbol(sym)
        if row and row["is_active"] == 1:
            return {"status": "skipped", "reason": "already_active", "symbol": sym}
        self._promote(sym, theme, [theme], confidence)
        return {"status": "promoted", "reason": "auto_approved_sim", "symbol": sym}

    def demote_vetoed(self, symbol: str) -> dict[str, Any]:
        """Deactivate a symbol because an auto-approval was vetoed (reversible marker)."""
        sym = (symbol or "").upper()
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE extended_watchlist SET is_active=0, drop_reason='vetoed' WHERE symbol=?",
                (sym,),
            )
            conn.commit()
        finally:
            conn.close()
        return {"status": "demoted", "symbol": sym, "drop_reason": "vetoed"}

    def restore_state(self, symbol: str, before_state: dict | None) -> None:
        """Restore a symbol to an exact prior row state (or delete it if there was none).

        This is the low-level primitive the event-aware rollback uses AFTER its
        compare-and-swap check confirms the current state still matches the applied one.
        """
        sym = (symbol or "").upper()
        conn = self._connect()
        try:
            if before_state is None:
                conn.execute("DELETE FROM extended_watchlist WHERE symbol=?", (sym,))
            else:
                placeholders = ", ".join("?" for _ in self._COLUMNS)
                conn.execute(
                    f"INSERT OR REPLACE INTO extended_watchlist "
                    f"({', '.join(self._COLUMNS)}) VALUES ({placeholders})",
                    tuple(before_state.get(c) for c in self._COLUMNS),
                )
            conn.commit()
        finally:
            conn.close()

    # ── Days-in-watchlist helper ───────────────────────────────────────────────

    @staticmethod
    def days_since(iso_ts: str) -> int:
        """Return days elapsed since an ISO-8601 timestamp string."""
        try:
            then = datetime.fromisoformat(iso_ts)
            return max(0, (datetime.now() - then).days)
        except Exception:
            return 0
