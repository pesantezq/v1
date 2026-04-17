"""
Trial registry for scraped-intelligence candidate configurations.

Provides a persistent audit trail for recommended scraped-intel configs that
emerge from tuning and promotion-review — without ever automatically applying
them to live behaviour.

IMPORTANT
---------
This module is strictly additive and non-destructive.

* It NEVER modifies config.json.
* It NEVER mutates comparison snapshots, soft_signals, or any WatchlistRow.
* It NEVER auto-promotes a candidate to production.
* A human must call the lifecycle helpers explicitly.

All state lives in a single additive table (``trial_registry``) in the same
data/portfolio.db file used by the rest of the project.

Lifecycle
---------
proposed
  └─► approved_for_shadow  ──► approved_for_trial ──► promoted
  └─► rejected                └─► rejected            └─► retired
                               └─► retired

  (promoted can also transition to retired)

Trial modes
-----------
research_only       — logged for audit; no runtime effect at all
shadow_only         — run in parallel but outputs are not used
comparison_default  — used as the comparison baseline (replaces stored config)
live_enriched       — used for live enrichment (replaces stored config)

Usage
-----
    from scraped_intel.trials import TrialRegistry

    reg = TrialRegistry(db_path="data/portfolio.db", output_dir="outputs/latest")

    # Register a candidate that came out of tuning
    entry = reg.register(
        config_payload={"weights": {...}, "max_signal_boost": 0.12, ...},
        source_tuning_report_path="outputs/latest/scraped_intel_tuning_results.json",
        trial_mode="shadow_only",
        reviewer_note="Rank-1 candidate from 2026-04-14 tuning run",
    )

    # Later: approve it for shadow trial
    reg.approve_for_shadow(entry["config_hash"])

    # Generate reports at any time
    reg.write_reports()
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger("scraped_intel.trials")

# ---------------------------------------------------------------------------
# Status and mode constants
# ---------------------------------------------------------------------------

class TrialStatus:
    """Valid status values for a trial registry entry."""
    PROPOSED            = "proposed"
    APPROVED_FOR_SHADOW = "approved_for_shadow"
    APPROVED_FOR_TRIAL  = "approved_for_trial"
    PROMOTED            = "promoted"
    REJECTED            = "rejected"
    RETIRED             = "retired"

    ALL: List[str] = [
        PROPOSED, APPROVED_FOR_SHADOW, APPROVED_FOR_TRIAL,
        PROMOTED, REJECTED, RETIRED,
    ]
    TERMINAL: List[str] = [REJECTED, RETIRED]


class TrialMode:
    """Valid trial mode values."""
    RESEARCH_ONLY       = "research_only"
    SHADOW_ONLY         = "shadow_only"
    COMPARISON_DEFAULT  = "comparison_default"
    LIVE_ENRICHED       = "live_enriched"

    ALL: List[str] = [
        RESEARCH_ONLY, SHADOW_ONLY, COMPARISON_DEFAULT, LIVE_ENRICHED,
    ]


# ---------------------------------------------------------------------------
# Valid status transitions
# ---------------------------------------------------------------------------

# Maps current_status → set of allowed next_statuses
_VALID_TRANSITIONS: Dict[str, List[str]] = {
    TrialStatus.PROPOSED: [
        TrialStatus.APPROVED_FOR_SHADOW,
        TrialStatus.APPROVED_FOR_TRIAL,   # allow direct promotion to trial
        TrialStatus.REJECTED,
    ],
    TrialStatus.APPROVED_FOR_SHADOW: [
        TrialStatus.APPROVED_FOR_TRIAL,
        TrialStatus.REJECTED,
        TrialStatus.RETIRED,
    ],
    TrialStatus.APPROVED_FOR_TRIAL: [
        TrialStatus.PROMOTED,
        TrialStatus.REJECTED,
        TrialStatus.RETIRED,
    ],
    TrialStatus.PROMOTED: [
        TrialStatus.RETIRED,
    ],
    TrialStatus.REJECTED: [],   # terminal
    TrialStatus.RETIRED:  [],   # terminal
}


# ---------------------------------------------------------------------------
# DDL (additive — appended to existing portfolio.db)
# ---------------------------------------------------------------------------

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS trial_registry (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    config_hash                  TEXT    NOT NULL UNIQUE,
    config_payload_json          TEXT    NOT NULL,
    source_tuning_report_path    TEXT,
    source_promotion_review_path TEXT,
    status                       TEXT    NOT NULL DEFAULT 'proposed',
    trial_mode                   TEXT    NOT NULL DEFAULT 'research_only',
    created_at                   TEXT    NOT NULL,
    approved_at                  TEXT,
    started_at                   TEXT,
    ended_at                     TEXT,
    reviewer_note                TEXT,
    final_decision_note          TEXT
);

CREATE INDEX IF NOT EXISTS idx_trial_registry_status
    ON trial_registry (status);

CREATE INDEX IF NOT EXISTS idx_trial_registry_created
    ON trial_registry (created_at);
"""


# ---------------------------------------------------------------------------
# Config hashing
# ---------------------------------------------------------------------------

def hash_config(config_payload: Dict[str, Any]) -> str:
    """
    Compute a deterministic SHA-256 hash of a candidate config dict.

    Keys are sorted recursively so that ``{"a": 1, "b": 2}`` and
    ``{"b": 2, "a": 1}`` produce the same hash.

    Args:
        config_payload: The candidate config dict (weights, boosts, etc.).

    Returns:
        A 16-character hex prefix of the SHA-256 digest, e.g. ``"a3f9b1c2d4e5f6a7"``.
    """
    canonical = json.dumps(config_payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TrialRegistry:
    """
    Persistent audit registry for scraped-intel candidate configurations.

    Stores entries in ``trial_registry`` table in the shared portfolio.db.
    Connections are explicitly closed (Windows file-lock issue — mirrors
    the exact same pattern used in state_store.py and scraped_intel/store.py).

    Usage::

        reg = TrialRegistry(db_path="data/portfolio.db", output_dir="outputs/latest")
        entry = reg.register(config_payload={...}, trial_mode="shadow_only")
        reg.approve_for_shadow(entry["config_hash"])
        reg.write_reports()
    """

    def __init__(
        self,
        db_path:    str | Path = "data/portfolio.db",
        output_dir: str | Path = "outputs/latest",
    ) -> None:
        self.db_path    = Path(db_path)
        self.output_dir = Path(output_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._init_tables()

    # ------------------------------------------------------------------
    # Connection helper — mirrors state_store.py / scraped_intel/store.py
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
    # Core CRUD
    # ------------------------------------------------------------------

    def register(
        self,
        config_payload:               Dict[str, Any],
        source_tuning_report_path:    Optional[str] = None,
        source_promotion_review_path: Optional[str] = None,
        trial_mode:                   str = TrialMode.RESEARCH_ONLY,
        reviewer_note:                Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Register a candidate config for review.

        Deduplicates by ``config_hash`` — registering the same config twice
        returns the existing entry without modification.

        Args:
            config_payload:               The candidate config dict.
            source_tuning_report_path:    Path to the tuning report that
                                          produced this candidate (informational).
            source_promotion_review_path: Path to the promotion review that
                                          validated this candidate (informational).
            trial_mode:                   Initial trial mode (see TrialMode).
            reviewer_note:                Free-text note for the audit trail.

        Returns:
            The full registry entry dict (new or existing).

        Raises:
            ValueError: If ``trial_mode`` is not a recognised value.
        """
        if trial_mode not in TrialMode.ALL:
            raise ValueError(
                f"Invalid trial_mode {trial_mode!r}. "
                f"Must be one of {TrialMode.ALL}."
            )

        cfg_hash  = hash_config(config_payload)
        now       = datetime.now().isoformat()

        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM trial_registry WHERE config_hash = ?",
                (cfg_hash,),
            ).fetchone()

            if existing:
                logger.debug(
                    "TrialRegistry.register: duplicate config_hash=%s — returning existing entry",
                    cfg_hash,
                )
                return self._row_to_dict(existing)

            conn.execute(
                """
                INSERT INTO trial_registry (
                    config_hash, config_payload_json,
                    source_tuning_report_path, source_promotion_review_path,
                    status, trial_mode, created_at, reviewer_note
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    cfg_hash,
                    json.dumps(config_payload, sort_keys=True),
                    source_tuning_report_path,
                    source_promotion_review_path,
                    TrialStatus.PROPOSED,
                    trial_mode,
                    now,
                    reviewer_note,
                ),
            )

        logger.info(
            "TrialRegistry: registered config_hash=%s status=proposed mode=%s",
            cfg_hash, trial_mode,
        )
        return self.get(cfg_hash)  # type: ignore[return-value]

    def get(self, config_hash: str) -> Optional[Dict[str, Any]]:
        """Return the registry entry for ``config_hash``, or None if absent."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM trial_registry WHERE config_hash = ?",
                (config_hash,),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def get_all(
        self,
        status:     Optional[str] = None,
        trial_mode: Optional[str] = None,
        limit:      int = 500,
    ) -> List[Dict[str, Any]]:
        """
        Return registry entries, newest first.

        Args:
            status:     Filter by status value (optional).
            trial_mode: Filter by trial mode (optional).
            limit:      Maximum rows to return.
        """
        query  = "SELECT * FROM trial_registry"
        params: List[Any] = []
        where: List[str] = []
        if status:
            where.append("status = ?")
            params.append(status)
        if trial_mode:
            where.append("trial_mode = ?")
            params.append(trial_mode)
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def update_status(
        self,
        config_hash:         str,
        new_status:          str,
        reviewer_note:       Optional[str] = None,
        final_decision_note: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Transition a registry entry to a new status.

        Validates that the transition is legal (see ``_VALID_TRANSITIONS``).

        Sets timestamp columns automatically:
        - ``approved_at`` when transitioning to approved_for_shadow / approved_for_trial
        - ``started_at`` when transitioning to approved_for_trial (marks trial start)
        - ``ended_at`` when transitioning to promoted / rejected / retired

        Args:
            config_hash:         The config hash to update.
            new_status:          Target status.
            reviewer_note:       Appended to existing note (with newline separator).
            final_decision_note: Written to final_decision_note column.

        Returns:
            The updated entry dict.

        Raises:
            KeyError:   If ``config_hash`` does not exist.
            ValueError: If ``new_status`` is not valid, or if the transition
                        from the current status to ``new_status`` is not allowed.
        """
        if new_status not in TrialStatus.ALL:
            raise ValueError(
                f"Invalid status {new_status!r}. Must be one of {TrialStatus.ALL}."
            )

        entry = self.get(config_hash)
        if entry is None:
            raise KeyError(f"No trial registry entry found for config_hash={config_hash!r}")

        current = entry["status"]
        allowed = _VALID_TRANSITIONS.get(current, [])
        if new_status not in allowed:
            raise ValueError(
                f"Transition from {current!r} to {new_status!r} is not allowed. "
                f"Allowed transitions from {current!r}: {allowed}."
            )

        now = datetime.now().isoformat()

        # Compute timestamp column updates
        approved_at = entry.get("approved_at")
        started_at  = entry.get("started_at")
        ended_at    = entry.get("ended_at")

        if new_status in (TrialStatus.APPROVED_FOR_SHADOW, TrialStatus.APPROVED_FOR_TRIAL):
            if approved_at is None:
                approved_at = now
        if new_status == TrialStatus.APPROVED_FOR_TRIAL:
            if started_at is None:
                started_at = now
        if new_status in (TrialStatus.PROMOTED, TrialStatus.REJECTED, TrialStatus.RETIRED):
            ended_at = now

        # Merge reviewer notes
        existing_note = entry.get("reviewer_note") or ""
        if reviewer_note:
            merged_note = (
                f"{existing_note}\n[{now}] {reviewer_note}".strip()
                if existing_note
                else f"[{now}] {reviewer_note}"
            )
        else:
            merged_note = existing_note or None

        with self._connect() as conn:
            conn.execute(
                """
                UPDATE trial_registry SET
                    status              = ?,
                    approved_at         = ?,
                    started_at          = ?,
                    ended_at            = ?,
                    reviewer_note       = ?,
                    final_decision_note = ?
                WHERE config_hash = ?
                """,
                (
                    new_status,
                    approved_at,
                    started_at,
                    ended_at,
                    merged_note,
                    final_decision_note or entry.get("final_decision_note"),
                    config_hash,
                ),
            )

        logger.info(
            "TrialRegistry: %s → %s (hash=%s)",
            current, new_status, config_hash,
        )
        return self.get(config_hash)  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Convenience lifecycle helpers
    # ------------------------------------------------------------------

    def approve_for_shadow(
        self, config_hash: str, note: Optional[str] = None
    ) -> Dict[str, Any]:
        """Transition proposed → approved_for_shadow."""
        return self.update_status(
            config_hash, TrialStatus.APPROVED_FOR_SHADOW, reviewer_note=note
        )

    def approve_for_trial(
        self, config_hash: str, note: Optional[str] = None
    ) -> Dict[str, Any]:
        """Transition approved_for_shadow (or proposed) → approved_for_trial."""
        return self.update_status(
            config_hash, TrialStatus.APPROVED_FOR_TRIAL, reviewer_note=note
        )

    def start_trial(
        self, config_hash: str, note: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Alias for approve_for_trial — semantically marks the trial as started.
        Only valid from proposed or approved_for_shadow.
        """
        return self.update_status(
            config_hash, TrialStatus.APPROVED_FOR_TRIAL, reviewer_note=note
        )

    def end_trial(
        self, config_hash: str, note: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Mark an active trial as retired (ended without promotion).
        Valid from approved_for_trial.
        """
        return self.update_status(
            config_hash, TrialStatus.RETIRED,
            reviewer_note=note,
            final_decision_note=note,
        )

    def mark_promoted(
        self, config_hash: str, note: Optional[str] = None
    ) -> Dict[str, Any]:
        """Transition approved_for_trial → promoted."""
        return self.update_status(
            config_hash, TrialStatus.PROMOTED,
            reviewer_note=note,
            final_decision_note=note,
        )

    def mark_rejected(
        self, config_hash: str, note: Optional[str] = None
    ) -> Dict[str, Any]:
        """Transition any non-terminal status → rejected."""
        return self.update_status(
            config_hash, TrialStatus.REJECTED,
            reviewer_note=note,
            final_decision_note=note,
        )

    def mark_retired(
        self, config_hash: str, note: Optional[str] = None
    ) -> Dict[str, Any]:
        """Transition promoted (or approved_*) → retired."""
        return self.update_status(
            config_hash, TrialStatus.RETIRED,
            reviewer_note=note,
            final_decision_note=note,
        )

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def write_reports(self, dry_run: bool = False) -> Dict[str, Optional[Path]]:
        """
        Write ``scraped_intel_trial_registry.json`` and
        ``scraped_intel_trial_registry.md`` to ``self.output_dir``.

        Args:
            dry_run: If True, builds the report dict but skips disk writes.

        Returns:
            Dict with keys ``json_path`` and ``md_path``
            (Path objects when written; None when dry_run=True).
        """
        entries = self.get_all(limit=1000)
        report  = _build_registry_report(entries)

        if dry_run:
            logger.debug("TrialRegistry.write_reports: dry_run — skipping disk writes")
            return {"json_path": None, "md_path": None}

        json_path = write_registry_json(report, self.output_dir)
        md_path   = write_registry_md(report, self.output_dir)
        return {"json_path": json_path, "md_path": md_path}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        try:
            d["config_payload"] = json.loads(d.get("config_payload_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            d["config_payload"] = {}
        return d


# ---------------------------------------------------------------------------
# Report builders (pure functions — no DB access)
# ---------------------------------------------------------------------------

def _build_registry_report(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build the full registry report dict from a list of entry dicts."""
    now = datetime.now().isoformat()

    status_counts: Dict[str, int] = {s: 0 for s in TrialStatus.ALL}
    active_entries: List[Dict[str, Any]] = []

    for e in entries:
        status = e.get("status", "proposed")
        status_counts[status] = status_counts.get(status, 0) + 1
        if status not in TrialStatus.TERMINAL:
            active_entries.append(e)

    return {
        "generated_at":    now,
        "total_entries":   len(entries),
        "status_counts":   status_counts,
        "active_count":    len(active_entries),
        "entries":         entries,
        "active_entries":  active_entries,
    }


def write_registry_json(
    report:     Dict[str, Any],
    output_dir: Path,
) -> Path:
    """Write registry report as JSON.  Returns the path written."""
    path = output_dir / "scraped_intel_trial_registry.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    logger.info(
        "scraped_intel_trial_registry.json written (%d entries, %d active)",
        report.get("total_entries", 0),
        report.get("active_count", 0),
    )
    return path


def write_registry_md(
    report:     Dict[str, Any],
    output_dir: Path,
) -> Path:
    """Write registry report as Markdown.  Returns the path written."""
    lines: List[str] = [
        "# Scraped Intelligence — Trial Registry",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}  ",
        f"Total entries: {report.get('total_entries', 0)}  ",
        f"Active (non-terminal): {report.get('active_count', 0)}  ",
        "",
    ]

    # Status summary table
    counts = report.get("status_counts", {})
    if counts:
        lines += [
            "## Status Summary",
            "",
            "| Status | Count |",
            "|--------|------:|",
        ]
        for status in TrialStatus.ALL:
            n = counts.get(status, 0)
            if n > 0:
                lines.append(f"| `{status}` | {n} |")
        lines.append("")

    # Active entries
    active = report.get("active_entries", [])
    if active:
        lines += [
            "## Active Trials",
            "",
            "| Hash | Mode | Status | Created | Approved | Started |",
            "|------|------|--------|---------|----------|---------|",
        ]
        for e in active:
            lines.append(
                f"| `{e.get('config_hash', '')}` "
                f"| `{e.get('trial_mode', '')}` "
                f"| `{e.get('status', '')}` "
                f"| {_fmt_ts(e.get('created_at'))} "
                f"| {_fmt_ts(e.get('approved_at'))} "
                f"| {_fmt_ts(e.get('started_at'))} |"
            )
        lines.append("")

    # Full entry list
    all_entries = report.get("entries", [])
    if all_entries:
        lines += ["## All Entries", ""]
        for e in all_entries:
            status     = e.get("status", "")
            cfg_hash   = e.get("config_hash", "")
            mode       = e.get("trial_mode", "")
            created    = _fmt_ts(e.get("created_at"))
            note       = e.get("reviewer_note") or ""
            final_note = e.get("final_decision_note") or ""
            tuning_ref = e.get("source_tuning_report_path") or "—"
            promo_ref  = e.get("source_promotion_review_path") or "—"

            lines += [
                f"### `{cfg_hash}` — `{status}`",
                "",
                f"**Mode:** `{mode}`  ",
                f"**Created:** {created}  ",
            ]
            if e.get("approved_at"):
                lines.append(f"**Approved:** {_fmt_ts(e['approved_at'])}  ")
            if e.get("started_at"):
                lines.append(f"**Trial started:** {_fmt_ts(e['started_at'])}  ")
            if e.get("ended_at"):
                lines.append(f"**Ended:** {_fmt_ts(e['ended_at'])}  ")
            lines += [
                f"**Tuning source:** `{tuning_ref}`  ",
                f"**Promotion source:** `{promo_ref}`  ",
                "",
            ]
            if note:
                lines += [f"**Review notes:** {note}  ", ""]
            if final_note:
                lines += [f"**Final decision:** {final_note}  ", ""]

            # Summarise key config fields
            payload = e.get("config_payload") or {}
            if payload:
                lines += ["**Config snapshot:**", "", "```json"]
                lines.append(json.dumps(payload, indent=2))
                lines += ["```", ""]

    lines += [
        "---",
        "_Trial registry is audit-only.  "
        "No config.json mutations.  "
        "No watchlist or snapshot modifications._",
    ]

    path = output_dir / "scraped_intel_trial_registry.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("scraped_intel_trial_registry.md written")
    return path


# ---------------------------------------------------------------------------
# Convenience pipeline entry point
# ---------------------------------------------------------------------------

def register_trial_candidate(
    config_payload:               Dict[str, Any],
    db_path:                      str | Path = "data/portfolio.db",
    output_dir:                   str | Path = "outputs/latest",
    source_tuning_report_path:    Optional[str] = None,
    source_promotion_review_path: Optional[str] = None,
    trial_mode:                   str = TrialMode.RESEARCH_ONLY,
    reviewer_note:                Optional[str] = None,
    write_report:                 bool = True,
    dry_run:                      bool = False,
) -> Dict[str, Any]:
    """
    Convenience function: register a candidate and optionally write registry reports.

    Args:
        config_payload:               The candidate config dict to register.
        db_path:                      Path to portfolio.db.
        output_dir:                   Where to write reports.
        source_tuning_report_path:    Path to source tuning report (informational).
        source_promotion_review_path: Path to source promotion review (informational).
        trial_mode:                   Trial mode for the new entry.
        reviewer_note:                Initial reviewer note.
        write_report:                 If True, write JSON + MD reports after registering.
        dry_run:                      If True, skip all disk writes.

    Returns:
        The new (or existing duplicate) registry entry dict.
    """
    reg   = TrialRegistry(db_path=db_path, output_dir=Path(output_dir))
    entry = reg.register(
        config_payload=config_payload,
        source_tuning_report_path=source_tuning_report_path,
        source_promotion_review_path=source_promotion_review_path,
        trial_mode=trial_mode,
        reviewer_note=reviewer_note,
    )
    if write_report and not dry_run:
        reg.write_reports()
    return entry


# ---------------------------------------------------------------------------
# Internal formatting helpers
# ---------------------------------------------------------------------------

def _fmt_ts(ts: Optional[str]) -> str:
    """Format an ISO timestamp to YYYY-MM-DD HH:MM, or '—' if None."""
    if not ts:
        return "—"
    try:
        return datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts
