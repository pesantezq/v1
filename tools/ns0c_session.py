"""Session evidence recorder for the bounded Northstar 0C autonomous session.

Session harness, NOT an EvidenceGateway deliverable. It exists because the
session contract requires every attempted task to persist enough evidence to
reconstruct what happened — including failures, repairs and non-PASS verdicts.

Append-only JSONL under docs/, matching the repository's existing ledger
convention (EW0A_0B3_RECORDS.jsonl, EW0A_0B_PHASE_CERTIFICATION.jsonl). Never
rewrites: a session ledger that edited its own history would not be evidence.

Also exposes ``session_projection`` so the controller-owned read-model layer can
project live session/task state to the Worker Control Center GUI WITHOUT the GUI
reading authoritative state directly. Truthful by construction: fields with no
backend are reported as PENDING_BACKEND rather than synthesized.
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from typing import Any

# Derived from this file's location, never a hardcoded operator checkout: the
# read-model consumes this module, and read-model code must not depend on one
# machine's path. A repo_root may still be passed explicitly by the trusted
# controller boundary.
REPO = Path(__file__).resolve().parents[1]
SESSION_ID = "ns0c-evgw-foundation-001"
MISSION_ID = "northstar_0c_pit_evidence_gateway_research_store"
SESSION_OBJECTIVE = "EvidenceGateway Foundation"
LEDGER_REL = f"docs/NORTHSTAR_0C_SESSION_{SESSION_ID}.jsonl"
LEDGER = REPO / LEDGER_REL


def ledger_path(repo_root: Path | str | None = None) -> Path:
    """Ledger location for a given checkout; defaults to this repository."""
    return (Path(repo_root) / LEDGER_REL) if repo_root is not None else LEDGER

# Session lifecycle states surfaced to the GUI.
SESSION_STATES = (
    "STARTING", "SELECTING_TASK", "PRECHECK", "TASKSPEC_FROZEN", "IMPLEMENTING",
    "VERIFYING", "GPT_REVIEW", "ROADMAP_POSTCHECK", "LEARNING", "BETWEEN_TASKS",
    "CHECKPOINTING", "BLOCKED", "COMPLETE",
)

TASK_STAGES = (
    "ROADMAP_PRECHECK", "TASKSPEC_FREEZE", "RISK_CLASSIFICATION", "IMPLEMENTATION",
    "DETERMINISTIC_VERIFICATION", "GPT_SEMANTIC_REVIEW", "ROADMAP_POSTCHECK",
    "LEARNING_KERNEL_EVALUATION", "COMPLETE",
)


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def record(kind: str, **fields: Any) -> dict:
    """Append one immutable session event."""
    if kind == "SessionState" and fields.get("session_state") not in SESSION_STATES:
        raise ValueError(f"unknown session_state: {fields.get('session_state')}")
    if kind == "TaskStage" and fields.get("stage") not in TASK_STAGES:
        raise ValueError(f"unknown task stage: {fields.get('stage')}")
    event = {"kind": kind, "session_id": SESSION_ID, "mission_id": MISSION_ID,
             "recorded_at": now(), **fields}
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")
    return event


def read_events(path: Path | None = None) -> list[dict]:
    p = path or LEDGER
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue          # malformed line skipped, never fabricated
        if isinstance(obj, dict):
            out.append(obj)
    return out


PENDING_BACKEND = "PENDING_BACKEND"


def session_projection(path: Path | None = None,
                       repo_root: Path | str | None = None) -> dict:
    """Controller-owned projection of live session/task state for the GUI.

    NON-AUTHORITATIVE and read-only. A task is reported VERIFIED only when the
    controller evidence records deterministic PASS *and* GPT PASS *and* roadmap
    post-check PASS — the projection never derives success from absence of
    error, from progress, or from a task merely finishing."""
    events = read_events(path if path is not None else ledger_path(repo_root))
    started = next((e for e in events if e["kind"] == "SessionStarted"), None)
    states = [e for e in events if e["kind"] == "SessionState"]
    stages = [e for e in events if e["kind"] == "TaskStage"]
    outcomes = [e for e in events if e["kind"] == "TaskOutcome"]
    blockers = [e for e in events if e["kind"] == "CircuitBreaker"]

    def _count(status: str) -> int:
        return sum(1 for o in outcomes if o.get("final_status") == status)

    current_stage = stages[-1] if stages else None
    return {
        "read_model": "Northstar0CSessionSummary",
        "schema_kind": "experimental_noncanonical",
        "session_id": SESSION_ID,
        "mission_id": MISSION_ID,
        "session_objective": SESSION_OBJECTIVE,
        "session_started_at": started["session_started_at"] if started else PENDING_BACKEND,
        "starting_main_sha": started["starting_main_sha"] if started else PENDING_BACKEND,
        "session_state": states[-1]["session_state"] if states else "STARTING",
        "current_task_id": current_stage.get("task_id") if current_stage else None,
        "current_task_title": current_stage.get("title") if current_stage else None,
        "current_stage": current_stage.get("stage") if current_stage else None,
        "tasks_attempted": len({o.get("task_id") for o in outcomes}),
        "tasks_verified": _count("VERIFIED"),
        "tasks_repaired": sum(1 for o in outcomes if o.get("repairs")),
        "tasks_escalated": _count("ESCALATION_REQUIRED"),
        "tasks_abstained": _count("ABSTAINED"),
        "tasks_incomplete": _count("INCOMPLETE"),
        "blockers": [b.get("breaker") for b in blockers],
        # Boundaries surfaced prominently; these are read from controller
        # evidence, never asserted by the GUI.
        "authority": "A1_ASSISTED_ENGINEERING",
        "c1_status": "DISABLED",
        "auto_merge": False,
        "production_mutation": False,
        "capital_action": False,
        # No backend exists for these; truthful partial state.
        "worker_heartbeat": PENDING_BACKEND,
        "supervisor_latency_ms": PENDING_BACKEND,
    }


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "projection":
        print(json.dumps(session_projection(), indent=2))
    else:
        for e in read_events():
            print(json.dumps(e))
