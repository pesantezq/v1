"""Engineering Learning Kernel (EW-0A / C0.5 apprenticeship).

Turns verified engineering experience into reusable, evidence-backed lessons that
change FUTURE CONTEXT — and never authority:

    LEARNING MAY CHANGE FUTURE CONTEXT
    LEARNING MAY NOT CHANGE AUTHORITY

These are engineering-organization records. They are NOT canonical Northstar
investment contracts and must never be confused with them (``experimental_noncanonical``).

Authority model (technically enforced, not prompt-enforced):
* Every mutation of learning state requires a TRUSTED CONTROLLER actor, validated
  against the controller-owned protected config ``config/ew0a_learning.json``.
* The Worker's only surface is :class:`~.worker_view.WorkerLearningView`, which has
  no mutator methods at all (asserted by an AST test).
* Lesson activation, competence records, and graduation thresholds are outside the
  worker's repair scope (``policy.is_protected``).
"""
from __future__ import annotations

from portfolio_automation.engineer_worker import EXPERIMENTAL_MARKER

SCHEMA_KIND = EXPERIMENTAL_MARKER
LEARNING_SCHEMA_ERA = "engineering.learning.v0"

__all__ = ["SCHEMA_KIND", "LEARNING_SCHEMA_ERA"]
