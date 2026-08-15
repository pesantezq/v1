"""The Worker's ONLY surface onto learning state — read-only by construction.

The Worker may READ its lessons and its competence profile (that is the whole point
of learning: the Worker's future context changes). It may never write them.

This is enforced STRUCTURALLY, not by prompt and not by convention:

* :class:`WorkerLearningView` defines no mutator methods at all. There is no
  ``activate``/``write``/``set``/``update``/``append``/``delete`` to call, so a
  compromised or confused Worker has nothing to reach for. An AST test asserts this
  class never grows one.
* The module imports only read accessors from ``store`` — never ``append_lesson``,
  ``transition_lesson``, ``append_competence``, or ``write_learning_config``. The
  same AST test asserts the import list.
* The underlying state files are protected paths, outside the Worker's repair scope.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from portfolio_automation.engineer_worker.learning.config import (
    LearningConfig, read_learning_config)
from portfolio_automation.engineer_worker.learning.contracts import (
    CapabilityReadinessV0, EngineeringLessonV0, WorkerCompetenceProfileV0)
from portfolio_automation.engineer_worker.learning.retriever import (
    RetrievalContext, build_lesson_packet, retrieve)
from portfolio_automation.engineer_worker.learning.store import (
    active_lessons, load_competence)

# Mutating names that must NEVER appear in this module (asserted by test).
FORBIDDEN_MUTATOR_PREFIXES = ("activate", "write", "set_", "update", "append",
                              "delete", "transition", "promote", "certify")


@dataclass(frozen=True)
class WorkerLearningView:
    """Read-only projection of learning state for the Engineer Worker."""
    repo_root: Path
    worker_id: str

    def _cfg(self) -> LearningConfig:
        return read_learning_config(self.repo_root)

    def my_active_lessons(self) -> list[EngineeringLessonV0]:
        """ACTIVE lessons only. CANDIDATE lessons the Worker itself proposed are
        deliberately invisible: seeing its own unvalidated proposal as guidance
        would let the Worker bootstrap its own beliefs."""
        return active_lessons(self.repo_root)

    def lessons_for(self, capability: str, task_class: str, subsystem: str,
                    risk_domain: str, failure_class: str | None = None) -> dict[str, Any]:
        """The bounded lesson packet for a decision context."""
        cfg = self._cfg()
        ctx = RetrievalContext(capability=capability, task_class=task_class,
                               subsystem=subsystem, risk_domain=risk_domain,
                               failure_class=failure_class)
        return build_lesson_packet(retrieve(self.my_active_lessons(), ctx, cfg.retrieval), ctx)

    def my_competence(self) -> dict[str, dict[str, Any]]:
        """Read-only competence statistics, including the unflattering ones."""
        return {cap: perf.to_dict() for cap, perf in sorted(
            load_competence(self.repo_root).items()) if perf.worker_id == self.worker_id}

    def my_readiness_note(self) -> str:
        """Readiness is assessed by the controller-owned gate, never self-declared."""
        return ("Readiness is assessed by the controller-owned graduation gate. "
                "Readiness is not certification, and certification is not authority. "
                "The Worker cannot assess, assert, or change its own readiness.")
