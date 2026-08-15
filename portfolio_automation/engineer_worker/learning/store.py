"""Append-only Learning Kernel persistence.

Reuses the repository's existing durable-record convention (append-only JSONL under
``docs/``, exactly like ``EW0A_CERTIFICATION_OUTCOMES.jsonl`` /
``EW0A_0B3_RECORDS.jsonl``) rather than introducing a second storage framework.

History is NEVER rewritten. A lesson's lifecycle transition is a NEW appended
record; the prior state stays readable for audit. ``load_lessons`` folds the log
into current state by taking the last record per ``lesson_id``.

Every mutation requires a trusted controller actor (``assert_controller_actor``).
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from portfolio_automation.engineer_worker.learning.config import (
    LearningConfig, assert_controller_actor)
from portfolio_automation.engineer_worker.learning.contracts import (
    EngineeringLessonV0, LessonRetrievalRecordV0, OutcomeEvaluationV0,
    TaskClassPerformanceV0, LessonStatus, LearningError)

LESSON_LOG_REL = "docs/EW0A_LEARNING_LESSONS.jsonl"
RETRIEVAL_LOG_REL = "docs/EW0A_LEARNING_RETRIEVALS.jsonl"
EVALUATION_LOG_REL = "docs/EW0A_LEARNING_EVALUATIONS.jsonl"
COMPETENCE_LOG_REL = "docs/EW0A_LEARNING_COMPETENCE.jsonl"

# Lifecycle transitions the controller may record. Anything else is refused, so a
# lesson can never jump straight from CANDIDATE to SUPERSEDED, and a terminal
# state can never silently reopen.
_ALLOWED_TRANSITIONS: dict[LessonStatus, frozenset[LessonStatus]] = {
    LessonStatus.CANDIDATE: frozenset({LessonStatus.ACTIVE, LessonStatus.RETIRED}),
    LessonStatus.ACTIVE: frozenset({LessonStatus.SUPERSEDED, LessonStatus.CONTRADICTED,
                                    LessonStatus.RETIRED}),
    LessonStatus.SUPERSEDED: frozenset(),
    LessonStatus.CONTRADICTED: frozenset(),
    LessonStatus.RETIRED: frozenset(),
}


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            continue          # malformed line is skipped, never fabricated
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _lesson_from_dict(d: dict[str, Any]) -> EngineeringLessonV0 | None:
    fields = EngineeringLessonV0.__dataclass_fields__
    try:
        return EngineeringLessonV0(**{k: v for k, v in d.items() if k in fields})
    except (LearningError, TypeError, ValueError):
        return None           # fail closed: an invalid record is not a usable lesson


# --- lessons -----------------------------------------------------------------
def append_lesson(repo_root: str | Path, lesson: EngineeringLessonV0,
                  cfg: LearningConfig, actor: str, rel: str = LESSON_LOG_REL) -> None:
    """Append a lesson record (creation OR lifecycle transition). Controller only."""
    assert_controller_actor(cfg, actor)
    _append_jsonl(Path(repo_root) / rel, {**lesson.to_dict(), "recorded_by": actor})


def read_lesson_log(repo_root: str | Path, rel: str = LESSON_LOG_REL) -> list[dict[str, Any]]:
    """Full append-only history, oldest first (audit view — never folded)."""
    return [r for r in _read_jsonl(Path(repo_root) / rel)
            if r.get("kind") == "EngineeringLessonV0"]


def load_lessons(repo_root: str | Path, rel: str = LESSON_LOG_REL) -> list[EngineeringLessonV0]:
    """Current state: the LAST record wins per lesson_id. History is preserved in
    the log and remains readable via :func:`read_lesson_log`."""
    current: dict[str, EngineeringLessonV0] = {}
    for rec in read_lesson_log(repo_root, rel):
        lesson = _lesson_from_dict(rec)
        if lesson is not None:
            current[lesson.lesson_id] = lesson
    return list(current.values())


def active_lessons(repo_root: str | Path, rel: str = LESSON_LOG_REL) -> list[EngineeringLessonV0]:
    return [l for l in load_lessons(repo_root, rel) if l.is_retrievable]


def get_lesson(repo_root: str | Path, lesson_id: str,
               rel: str = LESSON_LOG_REL) -> EngineeringLessonV0 | None:
    return next((l for l in load_lessons(repo_root, rel) if l.lesson_id == lesson_id), None)


def transition_lesson(repo_root: str | Path, lesson_id: str, new_status: LessonStatus,
                      cfg: LearningConfig, actor: str, now: str, *,
                      supersedes_lesson_id: str | None = None,
                      contradicts_lesson_id: str | None = None,
                      rel: str = LESSON_LOG_REL) -> EngineeringLessonV0:
    """Record a lifecycle transition as a NEW appended record (never a rewrite).

    Refuses illegal transitions, so evidence history cannot be laundered by moving
    a contradicted lesson back to ACTIVE."""
    assert_controller_actor(cfg, actor)
    current = get_lesson(repo_root, lesson_id, rel)
    if current is None:
        raise LearningError(f"unknown lesson: {lesson_id}")
    old = LessonStatus(current.status)
    if new_status is old:
        return current
    if new_status not in _ALLOWED_TRANSITIONS[old]:
        raise LearningError(f"illegal lesson transition {old.value} -> {new_status.value}")
    updated = replace(
        current, status=new_status.value,
        validated_at=(now if new_status is LessonStatus.ACTIVE else current.validated_at),
        supersedes_lesson_id=supersedes_lesson_id or current.supersedes_lesson_id,
        contradicts_lesson_id=contradicts_lesson_id or current.contradicts_lesson_id,
        version=current.version + 1)
    append_lesson(repo_root, updated, cfg, actor, rel)
    return updated


# --- retrieval / evaluation / competence -------------------------------------
def append_retrieval(repo_root: str | Path, record: LessonRetrievalRecordV0,
                     cfg: LearningConfig, actor: str, rel: str = RETRIEVAL_LOG_REL) -> None:
    assert_controller_actor(cfg, actor)
    _append_jsonl(Path(repo_root) / rel, record.to_dict())


def load_retrievals(repo_root: str | Path, rel: str = RETRIEVAL_LOG_REL) -> list[dict[str, Any]]:
    return [r for r in _read_jsonl(Path(repo_root) / rel)
            if r.get("kind") == "LessonRetrievalRecordV0"]


def append_evaluation(repo_root: str | Path, record: OutcomeEvaluationV0,
                      cfg: LearningConfig, actor: str, rel: str = EVALUATION_LOG_REL) -> None:
    assert_controller_actor(cfg, actor)
    _append_jsonl(Path(repo_root) / rel, record.to_dict())


def load_evaluations(repo_root: str | Path, rel: str = EVALUATION_LOG_REL) -> list[dict[str, Any]]:
    return [r for r in _read_jsonl(Path(repo_root) / rel)
            if r.get("kind") == "OutcomeEvaluationV0"]


def append_competence(repo_root: str | Path, record: TaskClassPerformanceV0,
                      cfg: LearningConfig, actor: str, rel: str = COMPETENCE_LOG_REL) -> None:
    assert_controller_actor(cfg, actor)
    _append_jsonl(Path(repo_root) / rel, record.to_dict())


def load_competence(repo_root: str | Path,
                    rel: str = COMPETENCE_LOG_REL) -> dict[str, TaskClassPerformanceV0]:
    """Fold the competence log to current per-capability state (last record wins)."""
    fields = TaskClassPerformanceV0.__dataclass_fields__
    current: dict[str, TaskClassPerformanceV0] = {}
    for rec in _read_jsonl(Path(repo_root) / rel):
        if rec.get("kind") != "TaskClassPerformanceV0":
            continue
        try:
            perf = TaskClassPerformanceV0(**{k: v for k, v in rec.items() if k in fields})
        except (TypeError, ValueError):
            continue
        current[perf.capability] = perf
    return current


def evidence_index(records: Iterable[dict[str, Any]]) -> set[str]:
    """Build the set of resolvable evidence identifiers from authoritative records.

    Used by lesson validation to prove a cited event ACTUALLY occurred rather than
    trusting a claimed citation."""
    idx: set[str] = set()
    for r in records:
        for key in ("candidate_id", "task_id", "attempt_id", "verification_id",
                    "contract", "sha", "candidate_sha", "lesson_id"):
            v = r.get(key)
            if isinstance(v, str) and v:
                idx.add(v)
        kind = r.get("kind")
        if isinstance(kind, str) and kind:
            idx.add(kind)
    return idx
