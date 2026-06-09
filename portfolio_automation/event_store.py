"""Learning-loop event store (Phase 11, spec §11).

Append-only JSONL event spine under ``outputs/policy/`` feeding confidence
calibration + pattern learning. Four streams (spec §11):

* ``pattern_events.jsonl``       — pattern activation/evaluation
* ``opportunity_events.jsonl``   — opportunity discovery/dismissal/resolution
* ``outcome_events.jsonl``       — market outcome vs decision/opportunity
* ``user_action_log.jsonl``      — operator interactions

Decisions (§23.9): **append-only**; never rewritten. Yearly **compaction** moves
old lines to ``outputs/policy/archive/``. Every append is non-fatal — a write
failure is swallowed (the daily pipeline must never break on telemetry) and
``observe_only: true`` is forced on every record.

Safety: this module only appends advisory telemetry. It executes nothing, trades
nothing, and never writes ``outputs/latest/decision_plan.json``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.next_stage.contracts import EventStream, LearningEvent

_STREAMS = {s.value for s in EventStream}
_POLICY_DIR = ("outputs", "policy")


def _stream_name(stream: EventStream | str) -> str:
    name = stream.value if isinstance(stream, EventStream) else str(stream)
    if name not in _STREAMS:
        raise ValueError(f"unknown event stream: {name!r} (allowed: {sorted(_STREAMS)})")
    return name


def _stream_path(root: Path, stream: EventStream | str) -> Path:
    return root.joinpath(*_POLICY_DIR, _stream_name(stream))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_event(root: Path, stream: EventStream | str,
                 event: LearningEvent | dict[str, Any]) -> bool:
    """Append one event to ``stream``. Returns True on success, never raises.

    ``observe_only: true`` is forced. Missing ``event_id``/``timestamp`` are
    filled defensively so a partial event never corrupts the stream.
    """
    try:
        if isinstance(event, LearningEvent):
            rec = event.to_dict()
        elif isinstance(event, dict):
            rec = dict(event)
        else:
            return False
        rec["observe_only"] = True
        rec.setdefault("timestamp", _now_iso())
        rec.setdefault("event_id", f"ev-{abs(hash((rec.get('timestamp'), rec.get('source'), rec.get('ticker_or_theme'))))%10**12}")
        path = _stream_path(root, stream)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
        return True
    except Exception:
        return False  # telemetry must never break the pipeline


def read_events(root: Path, stream: EventStream | str,
                limit: int | None = None) -> list[dict[str, Any]]:
    """Read events from ``stream`` (oldest→newest). Tolerates tampered lines."""
    out: list[dict[str, Any]] = []
    try:
        path = _stream_path(root, stream)
        if not path.exists():
            return []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return out
    if limit is not None and limit >= 0:
        return out[-limit:]
    return out


def compact_stream(root: Path, stream: EventStream | str, before_year: int) -> int:
    """Move events with ``timestamp`` year < ``before_year`` to the archive.

    Returns the number of archived lines. Append-only safety preserved: the live
    file is rewritten only to *remove* already-archived old lines (never edits
    retained lines), and the archive append happens first. Non-fatal.
    """
    try:
        name = _stream_name(stream)
        path = _stream_path(root, stream)
        if not path.exists():
            return 0
        keep: list[str] = []
        archive: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                ts = json.loads(line).get("timestamp", "")
                year = int(str(ts)[:4])
            except Exception:
                keep.append(line)  # unparseable → retain (never lose data)
                continue
            (archive if year < before_year else keep).append(line)
        if not archive:
            return 0
        arch_dir = root.joinpath(*_POLICY_DIR, "archive")
        arch_dir.mkdir(parents=True, exist_ok=True)
        arch_path = arch_dir / name
        with arch_path.open("a", encoding="utf-8") as f:
            f.write("\n".join(archive) + "\n")
        path.write_text(("\n".join(keep) + "\n") if keep else "", encoding="utf-8")
        return len(archive)
    except Exception:
        return 0


# Thin convenience recorders (keep call sites readable).

def record_pattern_event(root: Path, **fields: Any) -> bool:
    return append_event(root, EventStream.PATTERN, _event("pattern_learning", fields))


def record_opportunity_event(root: Path, **fields: Any) -> bool:
    return append_event(root, EventStream.OPPORTUNITY, _event("opportunity", fields))


def record_outcome_event(root: Path, **fields: Any) -> bool:
    return append_event(root, EventStream.OUTCOME, _event("outcome_tracker", fields))


def record_user_action(root: Path, **fields: Any) -> bool:
    return append_event(root, EventStream.USER_ACTION, _event("operator", fields))


def _event(default_source: str, fields: dict[str, Any]) -> LearningEvent:
    return LearningEvent(
        event_id=fields.pop("event_id", ""),
        timestamp=fields.pop("timestamp", _now_iso()),
        source=fields.pop("source", default_source),
        run_mode=fields.pop("run_mode", "daily"),
        namespace=fields.pop("namespace", "policy"),
        ticker_or_theme=fields.pop("ticker_or_theme", ""),
        signal_type=fields.pop("signal_type", ""),
        market_context=fields.pop("market_context", {}),
        portfolio_context=fields.pop("portfolio_context", {}),
        confidence=fields.pop("confidence", None),
        recommendation_or_action_or_status=fields.pop("recommendation_or_action_or_status", ""),
        user_decision=fields.pop("user_decision", None),
        outcome_windows=fields.pop("outcome_windows", {}),
        evidence=fields.pop("evidence", []),
        data_quality=fields.pop("data_quality", ""),
    )
