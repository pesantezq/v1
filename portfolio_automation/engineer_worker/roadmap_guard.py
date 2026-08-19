"""Refuses engineering work that is not the currently authorized roadmap item.

WHY THIS EXISTS.

``run_mission`` already enforces a mission boundary: it refuses any task whose
``mission_id`` differs from ``policy.mission_id``. That check is real, but both
sides come from the same caller -- the task queue and the runtime policy are
assembled by the same process -- so it proves only that the caller was
self-consistent. A runtime policy naming ``g1_supervisor_measurement`` and a
queue of G1 tasks agree with each other perfectly, and the loop would dispatch
them while EW-0B is the authorized item.

This is the same defect ``review_candidate`` was built to fix, one layer up:
comparing a belief to itself always agrees. A boundary constrains something only
when at least one side is resolved from an authority the caller does not author.
Here that authority is the roadmap record in ``.agent/phase_status.yaml``, which
is a protected path -- the worker cannot edit it to authorize itself.

FAIL CLOSED IN EVERY DIRECTION.

A missing file, unreadable YAML, absent key, or an empty (IDLE) mission id all
resolve to "no mission is authorized", which refuses everything. The failure
mode of this module is a stopped loop, never a widened one.

``experimental_noncanonical``.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from portfolio_automation.engineer_worker import EXPERIMENTAL_MARKER

SCHEMA_KIND = EXPERIMENTAL_MARKER
ROADMAP_SCHEMA_VERSION = "engineering.roadmap_authorization.v0"

#: The roadmap record. A protected path (policy._PROTECTED_PATTERNS covers
#: ".agent/"), so the worker may read it and can never write it.
DEFAULT_ROADMAP_REL = ".agent/phase_status.yaml"
_ROOT_KEY = "stockbot_northstar_redesign"
_STATE_KEY = "engineer_runtime_state"
_MISSION_KEY = "mission_id"


class RoadmapViolation(PermissionError):
    """A mission was dispatched that the roadmap does not currently authorize."""


@dataclass(frozen=True)
class RoadmapAuthorization:
    """Which mission the roadmap currently authorizes, and where that came from.

    ``authorized_mission_id`` is None for every failure and for the IDLE state.
    Those are deliberately the same value: an unreadable roadmap and a roadmap
    that authorizes nothing must both refuse, and giving them distinct
    permissive-looking states is how a parse failure becomes an open gate."""

    authorized_mission_id: Optional[str]
    source: str
    detail: str = ""
    #: True ONLY when resolved from the protected roadmap record on disk.
    #: An in-memory authorization is a caller asserting its own permission, so
    #: the production entry point refuses it. Mirrors ReviewContext.durable:
    #: the object is real either way, and the seam that matters is one level up.
    authoritative: bool = False

    @property
    def any_mission_authorized(self) -> bool:
        return bool(self.authorized_mission_id)

    def authorizes(self, mission_id: Optional[str]) -> bool:
        return bool(mission_id) and mission_id == self.authorized_mission_id

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": ROADMAP_SCHEMA_VERSION, "schema_kind": SCHEMA_KIND,
                "authorized_mission_id": self.authorized_mission_id,
                "source": self.source, "detail": self.detail,
                "authoritative": self.authoritative}

    @classmethod
    def read(cls, repo_root: str | Path,
             rel: str = DEFAULT_ROADMAP_REL) -> "RoadmapAuthorization":
        """Resolve the authorized mission from the roadmap record on disk."""
        path = Path(repo_root) / rel
        try:
            import yaml
        except ImportError as exc:                      # pragma: no cover
            return cls(None, rel, f"yaml unavailable: {exc}")
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            return cls(None, rel, f"roadmap record unreadable: {exc}")
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            return cls(None, rel, f"roadmap record unparseable: {exc}")
        if not isinstance(data, dict):
            return cls(None, rel, "roadmap record is not a mapping")
        state = (data.get(_ROOT_KEY) or {})
        state = state.get(_STATE_KEY) if isinstance(state, dict) else None
        if not isinstance(state, dict):
            return cls(None, rel,
                       f"{_ROOT_KEY}.{_STATE_KEY} is absent; no mission is authorized")
        mission = state.get(_MISSION_KEY)
        if not isinstance(mission, str) or not mission.strip():
            return cls(None, rel,
                       "the roadmap records an IDLE engineer runtime; no mission "
                       "is authorized")
        return cls(mission.strip(), rel, "read from the roadmap record",
                   authoritative=True)

    @classmethod
    def for_mission(cls, mission_id: str) -> "RoadmapAuthorization":
        """EXPLICIT in-memory authorization for tests and bounded harnesses.

        Named, never reached by omitting an argument. Its existence does not
        weaken the guard: the operating entry points require SOME authorization
        object, and only ``read`` can produce one from the protected record.
        ``tests/test_ew0b_hardening.py`` pins the real file against the real
        runtime policy so this constructor cannot paper over live drift."""
        return cls(mission_id, "explicit", "constructed in memory by name",
                   authoritative=False)


def assert_roadmap_authoritative(roadmap: RoadmapAuthorization) -> None:
    """Raise unless this authorization came from the protected record.

    Without this the guard is defeated by its own input type: a caller that can
    construct RoadmapAuthorization.for_mission(x) can authorize x, which is the
    caller authorizing itself -- exactly the self-consistency the guard was
    built to break. The synthetic constructor stays, named and isolated, for
    harnesses and tests; it simply cannot reach the production entry point."""
    if not roadmap.authoritative:
        raise RoadmapViolation(
            f"refusing a non-authoritative roadmap authorization ({roadmap.source}: "
            f"{roadmap.detail}); the authorized implementation item must be "
            "resolved from the protected roadmap record, not supplied by the "
            "caller that wants to be authorized")


def assert_mission_authorized(roadmap: RoadmapAuthorization,
                              mission_id: Optional[str]) -> None:
    """Raise unless the roadmap authorizes exactly ``mission_id``."""
    if roadmap.authorizes(mission_id):
        return
    if not roadmap.any_mission_authorized:
        raise RoadmapViolation(
            f"the roadmap authorizes no mission ({roadmap.detail}); refusing to "
            f"dispatch {mission_id!r}")
    raise RoadmapViolation(
        f"mission {mission_id!r} is not the authorized implementation item "
        f"({roadmap.authorized_mission_id!r} per {roadmap.source}); a future "
        "roadmap item may not be started by naming it in a runtime policy")
