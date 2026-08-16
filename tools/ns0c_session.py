"""Session evidence recorder and multi-session projection for Northstar 0C.

Session harness, NOT an EvidenceGateway deliverable. It exists because the
session contract requires every attempted task to persist enough evidence to
reconstruct what happened — including failures, repairs and non-PASS verdicts.

Append-only JSONL under docs/, matching the repository's existing ledger
convention (EW0A_0B3_RECORDS.jsonl, EW0A_0B_PHASE_CERTIFICATION.jsonl). Never
rewrites: a session ledger that edited its own history would not be evidence.

WHY THE PROJECTION IS EPISODE-BASED.

Bounded Session 2 reused Session 1's identifier, and this module compounded the
mistake: it carried the session id and objective as module CONSTANTS and
projected ``next(SessionStarted)`` — the FIRST one in the file. Two distinct
bounded episodes therefore collapsed into a single GUI surface wearing Session
1's name, Session 1's objective and Session 1's start time, while silently
adding Session 2's task counts to it. The GUI was not lying about any single
field; it was presenting two episodes as one.

The repair is to stop treating a session as a filename or a constant. A ledger
holds a SEQUENCE OF EPISODES delimited by ``SessionStarted`` records, and every
identifying fact — id, mission, objective, start time — is read from the episode's
own ``SessionStarted`` evidence. Nothing here supplies an objective the records
do not contain.

Session 2's records keep the identifier they were physically written with. They
are not edited. An appended ``SessionIdentityCorrection`` maps that episode to
its correct logical identity, so a reader learns the truth from the ledger rather
than from a rewritten past.

``session_projection`` remains read-only and NON-AUTHORITATIVE. Fields with no
backend are reported as PENDING_BACKEND rather than synthesized.
"""
from __future__ import annotations

import datetime
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

# Derived from this file's location, never a hardcoded operator checkout: the
# read-model consumes this module, and read-model code must not depend on one
# machine's path. A repo_root may still be passed explicitly by the trusted
# controller boundary.
REPO = Path(__file__).resolve().parents[1]

MISSION_ID = "northstar_0c_pit_evidence_gateway_research_store"

#: Logical identities of the bounded sessions run under this mission. These name
#: EPISODES, not the phase: a session identifier identifies one bounded episode
#: with its own objective, start time and task set.
SESSION1_ID = "ns0c-evgw-foundation-001"
SESSION2_ID = "ns0c-revision-supersession-002"

#: Physical ledger of the current bounded session. Session 2's events were
#: written into Session 1's file before the identity error was found; the file is
#: kept as-is because it is append-only evidence. Identity comes from the
#: episode's records, NOT from this filename.
LEDGER_DIR_REL = "docs"
LEDGER_GLOB = "NORTHSTAR_0C_SESSION_*.jsonl"
ACTIVE_LEDGER_REL = f"docs/NORTHSTAR_0C_SESSION_{SESSION1_ID}.jsonl"


@dataclass(frozen=True)
class SessionContext:
    """Which session ``record`` is currently writing for."""

    session_id: str
    mission_id: str
    ledger_rel: str


#: The bounded session currently recording. A new bounded session sets this to
#: its own identity and ledger rather than inheriting the previous session's.
ACTIVE = SessionContext(SESSION2_ID, MISSION_ID, ACTIVE_LEDGER_REL)

_active: SessionContext = ACTIVE


def use_session(session_id: str, mission_id: str = MISSION_ID,
                ledger_rel: Optional[str] = None) -> SessionContext:
    """Point the recorder at a session. Explicit, never inferred."""
    global _active
    _active = SessionContext(
        session_id, mission_id,
        ledger_rel or f"{LEDGER_DIR_REL}/NORTHSTAR_0C_SESSION_{session_id}.jsonl")
    return _active


def active_session() -> SessionContext:
    return _active


def ledger_path(repo_root: Path | str | None = None) -> Path:
    """Active ledger location for a given checkout."""
    root = Path(repo_root) if repo_root is not None else REPO
    return root / _active.ledger_rel


def ledger_paths(repo_root: Path | str | None = None) -> list[Path]:
    """Every session ledger in the checkout, in stable order."""
    root = Path(repo_root) if repo_root is not None else REPO
    return sorted((root / LEDGER_DIR_REL).glob(LEDGER_GLOB))


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

NO_SESSION = "NO_SUCH_SESSION"
PENDING_BACKEND = "PENDING_BACKEND"


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def record(kind: str, **fields: Any) -> dict:
    """Append one immutable session event for the active session."""
    if kind == "SessionState" and fields.get("session_state") not in SESSION_STATES:
        raise ValueError(f"unknown session_state: {fields.get('session_state')}")
    if kind == "TaskStage" and fields.get("stage") not in TASK_STAGES:
        raise ValueError(f"unknown task stage: {fields.get('stage')}")
    event = {"kind": kind, "session_id": _active.session_id,
             "mission_id": _active.mission_id, "recorded_at": now(), **fields}
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")
    return event


def read_events(path: Path | None = None) -> list[dict]:
    p = path if path is not None else ledger_path()
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


# ---------------------------------------------------------------------------
# Episodes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SessionEpisode:
    """One bounded session, delimited by its own SessionStarted record."""

    started: dict
    events: list[dict] = field(default_factory=list)
    ledger: Optional[Path] = None
    corrected_from: Optional[str] = None

    @property
    def recorded_session_id(self) -> Optional[str]:
        return self.started.get("session_id")

    @property
    def session_id(self) -> Optional[str]:
        """Logical identity: the corrected id when a correction applies."""
        return self.started.get("_logical_session_id") or self.recorded_session_id

    @property
    def started_at(self) -> Optional[str]:
        return self.started.get("session_started_at")

    def of_kind(self, kind: str) -> list[dict]:
        return [e for e in self.events if e.get("kind") == kind]


def split_episodes(events: Iterable[dict],
                   ledger: Optional[Path] = None) -> list[SessionEpisode]:
    """Cut a ledger into episodes at each ``SessionStarted``.

    Events preceding the first SessionStarted belong to no episode and are
    dropped rather than attributed to one."""
    episodes: list[SessionEpisode] = []
    current: Optional[SessionEpisode] = None
    for event in events:
        if event.get("kind") == "SessionStarted":
            current = SessionEpisode(started=dict(event), events=[event],
                                     ledger=ledger)
            episodes.append(current)
        elif current is not None:
            current.events.append(event)
    return episodes


def _corrections(all_events: Iterable[dict]) -> dict[str, dict]:
    """Identity corrections keyed by the corrected episode's start instant.

    Keyed on ``session_started_at`` because it is immutable and unique per
    episode, whereas the recorded session_id is exactly the field in dispute."""
    out: dict[str, dict] = {}
    for event in all_events:
        if event.get("kind") != "SessionIdentityCorrection":
            continue
        key = event.get("applies_to_session_started_at")
        if isinstance(key, str):
            out[key] = event
    return out


def load_episodes(repo_root: Path | str | None = None,
                  path: Path | None = None) -> list[SessionEpisode]:
    """Every episode across every ledger, with identity corrections applied."""
    paths = [path] if path is not None else ledger_paths(repo_root)
    raw: list[SessionEpisode] = []
    every_event: list[dict] = []
    for p in paths:
        events = read_events(p)
        every_event.extend(events)
        raw.extend(split_episodes(events, ledger=p))

    fixes = _corrections(every_event)
    out: list[SessionEpisode] = []
    for episode in raw:
        fix = fixes.get(episode.started_at or "")
        if fix and fix.get("corrected_logical_session_id"):
            # The original record keeps the id it was written with; the logical
            # identity is carried alongside it so a reader can see both.
            started = {**episode.started,
                       "_logical_session_id": fix["corrected_logical_session_id"]}
            out.append(SessionEpisode(started=started, events=episode.events,
                                      ledger=episode.ledger,
                                      corrected_from=episode.recorded_session_id))
        else:
            out.append(episode)
    out.sort(key=lambda e: (e.started_at or "", e.session_id or ""))
    return out


def list_sessions(repo_root: Path | str | None = None,
                  path: Path | None = None) -> list[dict[str, Any]]:
    """Identity and objective of each episode, read from its own evidence."""
    return [{"session_id": e.session_id,
             "recorded_session_id": e.recorded_session_id,
             "identity_corrected": e.corrected_from is not None,
             "session_objective": e.started.get("session_objective", PENDING_BACKEND),
             "session_started_at": e.started_at,
             "mission_id": e.started.get("mission_id", PENDING_BACKEND)}
            for e in load_episodes(repo_root, path)]


def _no_session(session_id: Optional[str]) -> dict[str, Any]:
    """Truthful empty projection. Never a partially-filled session shape."""
    return {"read_model": "Northstar0CSessionSummary",
            "schema_kind": "experimental_noncanonical",
            "session_id": session_id, "session_state": NO_SESSION,
            "session_objective": NO_SESSION, "mission_id": NO_SESSION,
            "session_started_at": NO_SESSION, "starting_main_sha": NO_SESSION,
            "current_task_id": None, "current_task_title": None,
            "current_stage": None,
            "tasks_attempted": 0, "tasks_verified": 0, "tasks_repaired": 0,
            "tasks_escalated": 0, "tasks_abstained": 0, "tasks_incomplete": 0,
            "blockers": [], "known_sessions": []}


def session_projection(path: Path | None = None,
                       repo_root: Path | str | None = None,
                       session_id: Optional[str] = None) -> dict:
    """Controller-owned projection of ONE session's state for the GUI.

    NON-AUTHORITATIVE and read-only. ``session_id`` selects the episode; with no
    selection the most recently started episode is projected. An unknown id
    fails closed to an explicit no-session state rather than falling back to
    whichever episode happens to be first.

    A task is reported VERIFIED only when that session's OWN evidence records
    deterministic PASS *and* GPT PASS *and* roadmap post-check PASS. The
    projection never derives success from absence of error, from progress, from
    a task merely finishing, or from another session's verdicts."""
    episodes = load_episodes(repo_root, path)
    if not episodes:
        return _no_session(session_id)

    if session_id is None:
        episode = episodes[-1]
    else:
        episode = next((e for e in episodes if e.session_id == session_id), None)
        if episode is None:
            out = _no_session(session_id)
            out["known_sessions"] = [e.session_id for e in episodes]
            return out

    started = episode.started
    states = episode.of_kind("SessionState")
    stages = episode.of_kind("TaskStage")
    outcomes = episode.of_kind("TaskOutcome")
    blockers = episode.of_kind("CircuitBreaker")

    def _count(status: str) -> int:
        return sum(1 for o in outcomes if o.get("final_status") == status)

    current_stage = stages[-1] if stages else None
    return {
        "read_model": "Northstar0CSessionSummary",
        "schema_kind": "experimental_noncanonical",
        # Identity and objective come from THIS episode's SessionStarted
        # evidence. There is no module constant to fall back to, so the GUI
        # cannot inherit another session's name or purpose.
        "session_id": episode.session_id,
        "recorded_session_id": episode.recorded_session_id,
        "identity_corrected": episode.corrected_from is not None,
        "mission_id": started.get("mission_id", PENDING_BACKEND),
        "session_objective": started.get("session_objective", PENDING_BACKEND),
        "session_started_at": started.get("session_started_at", PENDING_BACKEND),
        "starting_main_sha": started.get("starting_main_sha", PENDING_BACKEND),
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
        "known_sessions": [e.session_id for e in episodes],
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
        wanted = sys.argv[2] if len(sys.argv) > 2 else None
        print(json.dumps(session_projection(session_id=wanted), indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "sessions":
        print(json.dumps(list_sessions(), indent=2))
    else:
        for e in read_events():
            print(json.dumps(e))
