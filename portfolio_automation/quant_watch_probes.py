# portfolio_automation/quant_watch_probes.py
"""quant_watch_probes — observe-only ledger of sub-RED quant concerns.

Auto-registers a "watch probe" when a deterministic quant condition fires below
the daily-tool-analysis RED trip-wires, re-checks each open probe every run, and
auto-archives it on resolution / scope-change / escalation. Companion to
applied_fix_verifier (which tracks applied fixes); this tracks open concerns.

Observe-only: mutates ONLY its ledger (data/quant_watch_ledger.json) and its
status artifact (outputs/latest/quant_watch_status.json). Never touches
decision / score / allocation / portfolio state. See
docs/superpowers/specs/2026-06-08-quant-watch-probes-design.md.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import OutputNamespace, safe_write_json

# ── status levels ───────────────────────────────────────────────────────────
GREEN, AMBER, RED = "green", "amber", "red"

# ── transition statuses ─────────────────────────────────────────────────────
ACTIVE, RESOLVED, ESCALATED = "active", "resolved", "escalated"

# ── detector ids ────────────────────────────────────────────────────────────
DETECTOR_PRIOR_GAUGE = "prior_gauge_underperformance"
DETECTOR_NEG_RETURN = "negative_mean_return_persistence"
DETECTOR_SECTOR_DRAG = "sector_drag"
DETECTOR_MANUAL = "manual"

# ── thresholds (module constants; config-overridable later) ─────────────────
MIN_RESOLVED_1D = 30           # min resolved sample before a probe may fire
PRIOR_GAUGE_FIRE_PP = -10.0    # fire D1 when current-fp <= prior gauge by this pp
PRIOR_GAUGE_RESOLVE_PP = -2.0  # resolve D1 when delta recovers to >= this pp
PRETRACKER_RED_GATE_PP = 10.0  # daily RED gate (|delta vs pre_tracker| >= this)
SECTOR_MIN_N = 30              # min n_samples for a sector:* loser to fire D3
MAX_PROBE_AGE_DAYS = 60        # TTL: stale probe auto-expires
MAX_OBSERVATIONS = 14          # cap per-probe observation trail
MAX_ARCHIVE = 200             # cap archive length (FIFO roll-off)

_LEDGER_REL = "data/quant_watch_ledger.json"
_STATUS_REL = "quant_watch_status.json"  # under outputs/latest/


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_ledger() -> dict:
    return {"schema_version": "1", "active": [], "archive": []}


def load_ledger(path: str | Path) -> dict:
    """Load the ledger; return an empty default if missing or corrupt.
    Backfills missing top-level keys so callers can rely on the shape."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
        if not isinstance(data, dict):
            return _empty_ledger()
        data.setdefault("schema_version", "1")
        data.setdefault("active", [])
        data.setdefault("archive", [])
        if not isinstance(data["active"], list) or not isinstance(data["archive"], list):
            return _empty_ledger()
        return data
    except FileNotFoundError:
        return _empty_ledger()
    except Exception:
        return _empty_ledger()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def _select_prior_gauge(
    by_fp: dict, current_fp: str | None,
    pretracker_label: str = "pre_tracker_unknown",
) -> tuple[str | None, dict | None]:
    """Return (fp, entry) of the gauge era immediately preceding the current
    one: the by_fingerprint entry that is neither current nor pre_tracker, with
    the latest last_signal_time. (None, None) if no such entry."""
    candidates = [
        (k, v) for k, v in (by_fp or {}).items()
        if k not in (current_fp, pretracker_label) and isinstance(v, dict)
    ]
    if not candidates:
        return None, None
    fp, entry = max(candidates, key=lambda kv: kv[1].get("last_signal_time") or "")
    return fp, entry
