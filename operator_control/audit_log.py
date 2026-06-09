"""Append-only audit log for the operator-control plane.

Every meaningful event (work-order created, status changed, prompt generated,
approval decision) appends one JSON object to
``outputs/operator_control/audit_log.jsonl``. The file is never rewritten or
truncated — it only grows — so it is a durable, tamper-evident record of every
operator action.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from operator_control import audit_log_path

# Recognized event types (free-form strings are allowed, but these are the ones
# the system emits — kept here for documentation + test assertions).
EVENT_TYPES = (
    "work_order_created",
    "work_order_status_changed",
    "prompt_generated",
    "approval_granted",
    "approval_rejected",
    "work_order_cancelled",
    "validation_rejected",
    "report_attached",
    "worker_protected_path_violation",
    "worker_production_impact",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_event(
    root: Path | str,
    *,
    event_type: str,
    actor: str,
    work_order_id: str | None = None,
    probe_id: str | None = None,
    skill_id: str | None = None,
    mode: str | None = None,
    details: dict[str, Any] | None = None,
    safety_result: str = "observe_only_no_execution",
) -> dict[str, Any]:
    """Append one audit event and return the recorded record.

    ``safety_result`` defaults to the observe-only assertion; callers may pass a
    more specific string (e.g. ``"rejected: unknown probe"``).
    """
    record: dict[str, Any] = {
        "timestamp": _now_iso(),
        "event_type": event_type,
        "work_order_id": work_order_id,
        "probe_id": probe_id,
        "skill_id": skill_id,
        "mode": mode,
        "actor": actor,
        "details": details or {},
        "safety_result": safety_result,
    }
    path = audit_log_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
    return record


def read_events(root: Path | str, limit: int | None = None) -> list[dict[str, Any]]:
    """Read audit events oldest→newest. ``limit`` returns the most recent N."""
    path = audit_log_path(root)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if limit is not None:
        return events[-limit:]
    return events


__all__ = ["EVENT_TYPES", "record_event", "read_events"]
