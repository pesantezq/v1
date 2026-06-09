"""Repair policies — the safety gate between a request and a work order.

This module is the single place that decides whether a ``(probe, skill, mode)``
request is *admissible* and whether it *requires human approval*. It owns:

  * the work-order status vocabulary and the legal transition graph,
  * combination validation (unknown ids / non-allowlisted pairs / bad modes),
  * the approval rule.

It deliberately contains NO I/O — pure functions, so it is trivial to test and
impossible for it to execute anything.
"""
from __future__ import annotations

from operator_control.probe_registry import RISK_LEVELS, get_probe
from operator_control.skill_registry import get_skill

# Work-order lifecycle statuses.
STATUSES: tuple[str, ...] = (
    "queued",
    "claimed",
    "running",
    "completed",
    "failed",
    "awaiting_approval",
    "approved",
    "rejected",
    "cancelled",
)

# Legal status transitions. Phase 1 only ever *creates* orders (→ queued or
# awaiting_approval) and supports operator approve/reject/cancel; the
# worker-driven transitions (claimed/running/completed/failed) are defined here
# so the Phase 2 worker runner inherits a validated graph.
_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"claimed", "cancelled", "awaiting_approval"}),
    "awaiting_approval": frozenset({"approved", "rejected", "cancelled"}),
    "approved": frozenset({"claimed", "cancelled"}),
    "rejected": frozenset(),
    "claimed": frozenset({"running", "failed", "cancelled"}),
    "running": frozenset({"completed", "failed"}),
    "completed": frozenset(),
    "failed": frozenset({"queued"}),  # allow a re-queue/retry
    "cancelled": frozenset(),
}

# Statuses from which no further transition is possible.
TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "rejected", "cancelled"})


class WorkOrderValidationError(ValueError):
    """Raised when a requested work order is not admissible."""


def _risk_rank(level: str) -> int:
    try:
        return RISK_LEVELS.index(level)
    except ValueError:
        return len(RISK_LEVELS)  # unknown → treat as highest


def effective_risk_level(probe_id: str, skill_id: str, mode: str) -> str:
    """The work order's risk = max(probe risk, skill risk), bumped for safe_repair."""
    probe = get_probe(probe_id)
    skill = get_skill(skill_id)
    ranks = []
    if probe is not None:
        ranks.append(_risk_rank(probe.risk_level))
    if skill is not None:
        ranks.append(_risk_rank(skill.risk_level))
    base = max(ranks) if ranks else _risk_rank("medium")
    # A mutating mode is never less than medium risk.
    if mode == "safe_repair":
        base = max(base, _risk_rank("medium"))
    base = min(base, len(RISK_LEVELS) - 1)
    return RISK_LEVELS[base]


def requires_approval(probe_id: str, skill_id: str, mode: str) -> bool:
    """Approval is required when ANY of these hold:

      * the skill marks this mode as approval-required,
      * the probe is flagged approval_required,
      * the mode is ``safe_repair`` (any artifact mutation),
      * the effective risk level is ``high``.
    """
    skill = get_skill(skill_id)
    probe = get_probe(probe_id)
    if skill is not None and mode in skill.approval_required_for_modes:
        return True
    if probe is not None and probe.approval_required:
        return True
    if mode == "safe_repair":
        return True
    if effective_risk_level(probe_id, skill_id, mode) == "high":
        return True
    return False


def initial_status(approval_required: bool) -> str:
    return "awaiting_approval" if approval_required else "queued"


def validate_combination(probe_id: str, skill_id: str, mode: str) -> None:
    """Raise :class:`WorkOrderValidationError` if the request is inadmissible.

    Enforced rules (in order):
      1. unknown probe id  → reject
      2. unknown skill id  → reject
      3. probe not in the skill's allowlist → reject
      4. mode not allowed by the skill → reject
      5. mode not offered by the probe → reject
    """
    probe = get_probe(probe_id)
    if probe is None:
        raise WorkOrderValidationError(f"unknown probe_id: {probe_id!r}")
    skill = get_skill(skill_id)
    if skill is None:
        raise WorkOrderValidationError(f"unknown skill_id: {skill_id!r}")
    if probe_id not in skill.allowed_probe_ids:
        raise WorkOrderValidationError(
            f"probe {probe_id!r} is not allowlisted for skill {skill_id!r}"
        )
    if mode not in skill.allowed_modes:
        raise WorkOrderValidationError(
            f"mode {mode!r} is not allowed by skill {skill_id!r} "
            f"(allowed: {', '.join(skill.allowed_modes)})"
        )
    if mode not in probe.allowed_actions:
        raise WorkOrderValidationError(
            f"mode {mode!r} is not offered by probe {probe_id!r} "
            f"(offered: {', '.join(probe.allowed_actions)})"
        )


def can_transition(current: str, new: str) -> bool:
    return new in _TRANSITIONS.get(current, frozenset())


def validate_transition(current: str, new: str) -> None:
    if new not in STATUSES:
        raise WorkOrderValidationError(f"unknown status: {new!r}")
    if not can_transition(current, new):
        raise WorkOrderValidationError(
            f"illegal transition {current!r} → {new!r}"
        )


def derive_requested_action(probe_id: str, skill_id: str, mode: str) -> str:
    """Compose the human-readable requested-action string.

    IMPORTANT: this is derived ENTIRELY from the registries — it never includes
    caller-supplied free text, so no executable command can be smuggled in.
    """
    probe = get_probe(probe_id)
    skill = get_skill(skill_id)
    probe_name = probe.display_name if probe else probe_id
    skill_name = skill.name if skill else skill_id
    verb = {
        "diagnose": "Diagnose",
        "propose_fix": "Propose a fix for",
        "safe_repair": "Safely repair",
    }.get(mode, mode)
    return f"{verb}: {probe_name} — via skill '{skill_name}' (mode={mode})."


__all__ = [
    "STATUSES",
    "TERMINAL_STATUSES",
    "WorkOrderValidationError",
    "effective_risk_level",
    "requires_approval",
    "initial_status",
    "validate_combination",
    "can_transition",
    "validate_transition",
    "derive_requested_action",
]
