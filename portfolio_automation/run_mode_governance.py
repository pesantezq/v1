"""
Run Mode Governance — Operating Mode Separation
===============================================

Centralizes run-mode declarations and enforces two-lane operating boundaries:

  Official Lane:          DAILY, MANUAL_UPDATE, WEEKLY_REVIEW
  Discovery/Research Lane: DISCOVERY, BACKTEST, HISTORICAL_REPLAY

Every pipeline run should declare a RunMode. The governance layer defines
what each mode is permitted to write and mutate — preventing discovery or
backtest runs from accidentally producing official portfolio artifacts.

``can_execute_trades`` is False for every mode. This system is advisory-only.

Usage::

    from portfolio_automation.run_mode_governance import (
        RunMode, normalize_run_mode, get_run_mode_policy,
        create_run_mode_context, assert_can_write_namespace,
        assert_can_update_portfolio_state, assert_can_update_watchlist,
        assert_can_emit_recommendation, is_official_mode,
        is_research_only_mode, validate_output_write,
    )

    ctx = create_run_mode_context("daily")
    assert ctx.policy.can_write_latest
    assert not ctx.policy.can_execute_trades
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Core enums
# ---------------------------------------------------------------------------

class RunMode(str, Enum):
    """Canonical operating modes for the portfolio automation system."""
    DAILY            = "daily"
    MANUAL_UPDATE    = "manual_update"
    DISCOVERY        = "discovery"
    WEEKLY_REVIEW    = "weekly_review"
    BACKTEST         = "backtest"
    HISTORICAL_REPLAY = "historical_replay"


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class RunModeViolation(Exception):
    """Raised when an operation violates the active run mode's policy."""


# ---------------------------------------------------------------------------
# Policy dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunModePolicy:
    """Immutable permission set for a single run mode."""
    mode: RunMode
    description: str

    # --- Namespace write permissions ---
    can_write_latest: bool      # outputs/latest/ — live decision artifacts
    can_write_policy: bool      # outputs/policy/ — audit/budget/governance
    can_write_portfolio: bool   # outputs/portfolio/ — snapshots and reports
    can_write_user_state: bool  # outputs/users/   — per-user official state
    can_write_historical: bool  # outputs/backtest/ — replay/backtest artifacts
    can_write_sandbox: bool     # outputs/sandbox/ — exploratory research
    can_write_discovery: bool   # discovery-only research candidates

    # --- Portfolio mutation permissions ---
    can_update_official_watchlist: bool
    can_change_allocations: bool
    can_change_risk_limits: bool

    # --- Recommendation and execution ---
    can_emit_recommendations: bool
    can_execute_trades: bool  # always False — advisory-only system

    # --- Approval requirement ---
    requires_manual_approval: bool


# ---------------------------------------------------------------------------
# RunModeContext
# ---------------------------------------------------------------------------

@dataclass
class RunModeContext:
    """Active run mode context, resolved policy, and approval state."""
    mode: RunMode
    policy: RunModePolicy
    approved: bool = False  # manual approval gate for MANUAL_UPDATE
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Default policies — one per RunMode
# ---------------------------------------------------------------------------

_POLICIES: dict[RunMode, RunModePolicy] = {

    RunMode.DAILY: RunModePolicy(
        mode=RunMode.DAILY,
        description=(
            "Official daily portfolio intelligence run. "
            "Writes latest/policy/portfolio artifacts. "
            "No trade execution, no portfolio state mutation, no watchlist changes."
        ),
        can_write_latest=True,
        can_write_policy=True,
        can_write_portfolio=True,
        can_write_user_state=False,
        can_write_historical=False,
        can_write_sandbox=False,
        can_write_discovery=False,
        can_update_official_watchlist=False,
        can_change_allocations=False,
        can_change_risk_limits=False,
        can_emit_recommendations=True,
        can_execute_trades=False,
        requires_manual_approval=False,
    ),

    RunMode.MANUAL_UPDATE: RunModePolicy(
        mode=RunMode.MANUAL_UPDATE,
        description=(
            "Official user-approved update path. Source-of-truth mode for user changes. "
            "May update official portfolio state only when explicitly approved."
        ),
        can_write_latest=True,
        can_write_policy=True,
        can_write_portfolio=True,
        can_write_user_state=True,
        can_write_historical=False,
        can_write_sandbox=False,
        can_write_discovery=False,
        can_update_official_watchlist=True,
        can_change_allocations=True,
        can_change_risk_limits=True,
        can_emit_recommendations=True,
        can_execute_trades=False,
        requires_manual_approval=True,
    ),

    RunMode.DISCOVERY: RunModePolicy(
        mode=RunMode.DISCOVERY,
        description=(
            "Research-only mode. Writes sandbox/discovery artifacts only. "
            "Cannot produce official recommendations, watchlist changes, "
            "or portfolio mutations."
        ),
        can_write_latest=False,
        can_write_policy=False,
        can_write_portfolio=False,
        can_write_user_state=False,
        can_write_historical=False,
        can_write_sandbox=True,
        can_write_discovery=True,
        can_update_official_watchlist=False,
        can_change_allocations=False,
        can_change_risk_limits=False,
        can_emit_recommendations=False,
        can_execute_trades=False,
        requires_manual_approval=False,
    ),

    RunMode.WEEKLY_REVIEW: RunModePolicy(
        mode=RunMode.WEEKLY_REVIEW,
        description=(
            "Report/review mode. Reads official outputs and writes review reports. "
            "Does not mutate official portfolio state."
        ),
        can_write_latest=True,
        can_write_policy=False,
        can_write_portfolio=True,
        can_write_user_state=False,
        can_write_historical=False,
        can_write_sandbox=False,
        can_write_discovery=False,
        can_update_official_watchlist=False,
        can_change_allocations=False,
        can_change_risk_limits=False,
        can_emit_recommendations=True,
        can_execute_trades=False,
        requires_manual_approval=False,
    ),

    RunMode.BACKTEST: RunModePolicy(
        mode=RunMode.BACKTEST,
        description=(
            "Simulation mode. Writes backtest/historical artifacts only. "
            "Cannot write official latest outputs or update policy as live truth."
        ),
        can_write_latest=False,
        can_write_policy=False,
        can_write_portfolio=False,
        can_write_user_state=False,
        can_write_historical=True,
        can_write_sandbox=True,
        can_write_discovery=False,
        can_update_official_watchlist=False,
        can_change_allocations=False,
        can_change_risk_limits=False,
        can_emit_recommendations=False,
        can_execute_trades=False,
        requires_manual_approval=False,
    ),

    RunMode.HISTORICAL_REPLAY: RunModePolicy(
        mode=RunMode.HISTORICAL_REPLAY,
        description=(
            "Offline replay mode. Writes historical namespace only. "
            "Cannot write latest/live/user official outputs."
        ),
        can_write_latest=False,
        can_write_policy=False,
        can_write_portfolio=False,
        can_write_user_state=False,
        can_write_historical=True,
        can_write_sandbox=False,
        can_write_discovery=False,
        can_update_official_watchlist=False,
        can_change_allocations=False,
        can_change_risk_limits=False,
        can_emit_recommendations=False,
        can_execute_trades=False,
        requires_manual_approval=False,
    ),
}

# ---------------------------------------------------------------------------
# Legacy alias map (backward compatibility with existing --run-mode CLI)
# ---------------------------------------------------------------------------

_LEGACY_ALIASES: dict[str, RunMode] = {
    "weekly":  RunMode.WEEKLY_REVIEW,
    "monthly": RunMode.WEEKLY_REVIEW,
}

# ---------------------------------------------------------------------------
# Lane sets
# ---------------------------------------------------------------------------

_OFFICIAL_MODES: frozenset[RunMode] = frozenset({
    RunMode.DAILY,
    RunMode.MANUAL_UPDATE,
    RunMode.WEEKLY_REVIEW,
})

_RESEARCH_ONLY_MODES: frozenset[RunMode] = frozenset({
    RunMode.DISCOVERY,
    RunMode.BACKTEST,
    RunMode.HISTORICAL_REPLAY,
})

# ---------------------------------------------------------------------------
# Internal: namespace → policy attribute mapping
# ---------------------------------------------------------------------------

# Maps OutputNamespace subdir values (and their subdir aliases) to the
# corresponding RunModePolicy boolean attribute name.
_NAMESPACE_ATTR_MAP: dict[str, str] = {
    "latest":    "can_write_latest",
    "policy":    "can_write_policy",
    "portfolio": "can_write_portfolio",
    "user":      "can_write_user_state",
    "users":     "can_write_user_state",
    "historical": "can_write_historical",
    "backtest":  "can_write_historical",   # OutputNamespace.HISTORICAL subdir
    "sandbox":   "can_write_sandbox",
    "live":      "can_write_latest",       # LIVE treated as latest-equivalent
}


def _resolve_namespace_attr(namespace) -> str | None:
    """Return the policy attribute name for *namespace* (OutputNamespace or str)."""
    ns_value = namespace.value if hasattr(namespace, "value") else str(namespace)
    return _NAMESPACE_ATTR_MAP.get(ns_value.lower())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_run_mode_policy(mode: RunMode) -> RunModePolicy:
    """Return the immutable :class:`RunModePolicy` for *mode*."""
    if mode not in _POLICIES:
        raise RunModeViolation(f"No policy defined for run mode {mode!r}")
    return _POLICIES[mode]


def normalize_run_mode(value: str | RunMode) -> RunMode:
    """
    Normalize *value* to a :class:`RunMode`.

    Accepts:
    - A :class:`RunMode` instance (returned as-is)
    - A canonical RunMode value string (``"daily"``, ``"discovery"``, …)
    - A legacy CLI alias (``"weekly"`` → WEEKLY_REVIEW, ``"monthly"`` → WEEKLY_REVIEW)

    Raises :exc:`RunModeViolation` for unknown values.
    """
    if isinstance(value, RunMode):
        return value
    if not isinstance(value, str):
        raise RunModeViolation(
            f"Cannot normalize run mode from {type(value).__name__!r}: {value!r}"
        )
    normalized = value.lower().strip()
    if normalized in _LEGACY_ALIASES:
        return _LEGACY_ALIASES[normalized]
    try:
        return RunMode(normalized)
    except ValueError:
        known = sorted(
            [m.value for m in RunMode] + list(_LEGACY_ALIASES.keys())
        )
        raise RunModeViolation(
            f"Unknown run mode {value!r}. Known modes: {known}"
        ) from None


def validate_output_write(
    mode: RunMode,
    namespace,
    path: str | None = None,
) -> bool:
    """
    Return True if *mode* may write to *namespace*, False otherwise.

    Accepts an :class:`~portfolio_automation.data_governance.OutputNamespace`
    instance or a plain string (e.g. ``"latest"``). Never raises.

    Use :func:`assert_can_write_namespace` for hard enforcement.
    """
    policy = _POLICIES.get(mode)
    if policy is None:
        return False
    attr = _resolve_namespace_attr(namespace)
    if attr is None:
        return False
    return bool(getattr(policy, attr, False))


def assert_can_write_namespace(
    mode: RunMode,
    namespace,
    path: str | None = None,
) -> None:
    """
    Assert that *mode* may write to *namespace*.

    Raises :exc:`RunModeViolation` if not permitted.
    """
    if not validate_output_write(mode, namespace, path=path):
        ns_value = namespace.value if hasattr(namespace, "value") else str(namespace)
        raise RunModeViolation(
            f"Run mode {mode.value!r} is not permitted to write to namespace "
            f"{ns_value!r}. Policy: {get_run_mode_policy(mode).description}"
        )


def assert_can_update_portfolio_state(mode: RunMode, *, approved: bool = False) -> None:
    """
    Assert that *mode* may mutate official portfolio allocations.

    For MANUAL_UPDATE, *approved* must be ``True``.

    Raises :exc:`RunModeViolation` if not permitted or approval is missing.
    """
    policy = get_run_mode_policy(mode)
    if not policy.can_change_allocations:
        raise RunModeViolation(
            f"Run mode {mode.value!r} cannot change portfolio allocations. "
            "Use MANUAL_UPDATE with explicit operator approval."
        )
    if policy.requires_manual_approval and not approved:
        raise RunModeViolation(
            f"Run mode {mode.value!r} requires explicit manual approval "
            "to change portfolio allocations. Pass approved=True."
        )


def assert_can_update_watchlist(mode: RunMode, *, approved: bool = False) -> None:
    """
    Assert that *mode* may update the official watchlist.

    For MANUAL_UPDATE, *approved* must be ``True``.

    Raises :exc:`RunModeViolation` if not permitted or approval is missing.
    """
    policy = get_run_mode_policy(mode)
    if not policy.can_update_official_watchlist:
        raise RunModeViolation(
            f"Run mode {mode.value!r} cannot update the official watchlist. "
            "Use MANUAL_UPDATE with explicit operator approval."
        )
    if policy.requires_manual_approval and not approved:
        raise RunModeViolation(
            f"Run mode {mode.value!r} requires explicit manual approval "
            "to update the official watchlist. Pass approved=True."
        )


def assert_can_emit_recommendation(mode: RunMode) -> None:
    """
    Assert that *mode* may emit official recommendations.

    Raises :exc:`RunModeViolation` for DISCOVERY, BACKTEST, HISTORICAL_REPLAY.
    """
    policy = get_run_mode_policy(mode)
    if not policy.can_emit_recommendations:
        raise RunModeViolation(
            f"Run mode {mode.value!r} cannot emit official recommendations. "
            "Outputs are sandbox/research-only in this mode."
        )


def is_research_only_mode(mode: RunMode) -> bool:
    """Return True if *mode* is a research/sandbox lane (never official artifacts)."""
    return mode in _RESEARCH_ONLY_MODES


def is_official_mode(mode: RunMode) -> bool:
    """Return True if *mode* is an official lane (produces authoritative artifacts)."""
    return mode in _OFFICIAL_MODES


def create_run_mode_context(
    mode: str | RunMode,
    *,
    approved: bool = False,
    metadata: dict[str, Any] | None = None,
) -> RunModeContext:
    """
    Normalize *mode* and return a :class:`RunModeContext`.

    Raises :exc:`RunModeViolation` for unknown mode values.
    """
    resolved = normalize_run_mode(mode)
    policy = get_run_mode_policy(resolved)
    return RunModeContext(
        mode=resolved,
        policy=policy,
        approved=approved,
        metadata=metadata or {},
    )
