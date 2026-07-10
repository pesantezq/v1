# portfolio_automation/suite_run_state.py
"""suite_run_state — tiny persisted tracker of when each cadence suite last ran.

Lets the run-all-daily orchestrator auto-chain a *due* cadence (e.g. run the
weekly suite once >= 7 days have elapsed since it last ran), turning the daily
run into the de-facto weekly scheduler when the suites are on-demand only.

Observe-only: reads/writes ONLY .agent/suite_run_state.json. Pure functions +
one thin write helper, mirroring doc_audit_state / applied_fix_verifier. Never
touches decision / score / allocation / portfolio state.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

_STATE_REL = ".agent/suite_run_state.json"

# Default "due" thresholds in days, keyed by cadence. run-all-daily uses "weekly".
DUE_THRESHOLD_DAYS: dict[str, float] = {"daily": 1, "weekly": 7, "monthly": 30}


def _state_path(root: str | Path) -> Path:
    return Path(root) / _STATE_REL


def load_suite_state(root: str | Path = ".") -> dict:
    """Return the state dict; empty dict if missing or corrupt."""
    try:
        data = json.loads(_state_path(root).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def stamp(cadence: str, root: str | Path = ".", now: datetime | None = None) -> dict:
    """Record that the ``cadence`` suite ran at ``now`` (default: utcnow).
    Returns the updated state. Creates .agent/ if needed."""
    now = now or datetime.now(timezone.utc)
    state = load_suite_state(root)
    state[f"last_{cadence}_run_at"] = now.isoformat()
    p = _state_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    return state


def days_since(cadence: str, root: str | Path = ".", now: datetime | None = None) -> float | None:
    """Days since the ``cadence`` suite last ran, or None if it never ran / the
    stored timestamp is unparseable."""
    now = now or datetime.now(timezone.utc)
    ts = load_suite_state(root).get(f"last_{cadence}_run_at")
    if not ts:
        return None
    try:
        last = datetime.fromisoformat(ts)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (now - last).total_seconds() / 86400.0
    except Exception:
        return None


def is_due(cadence: str, root: str | Path = ".", now: datetime | None = None,
           threshold_days: float | None = None) -> bool:
    """True if the ``cadence`` suite is due to run: it has never run, or at least
    ``threshold_days`` (default per DUE_THRESHOLD_DAYS) have elapsed since it did."""
    thr = threshold_days if threshold_days is not None else DUE_THRESHOLD_DAYS.get(cadence, 7)
    d = days_since(cadence, root=root, now=now)
    return d is None or d >= thr
