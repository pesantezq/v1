"""Read-only loader/validator for the machine-readable agent authority policy.

Source of truth: ``config/agent_policy.yaml`` (see the header comments there).
This module exists ONLY to prove the policy parses and to give tests (and,
later, explicitly-authorized enforcement work) one deterministic resolution
path. It never writes anything, never touches outputs/, and is not wired into
any pipeline stage.

Resolution is deterministic and fail-closed:

* unknown role or environment names raise ``AgentPolicyError`` — no fuzzy match
* absence of a grant is a denial
* a prohibition always wins over a grant
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POLICY_PATH = _REPO_ROOT / "config" / "agent_policy.yaml"

#: Role fields every role block must define (schema v1).
REQUIRED_ROLE_FIELDS = (
    "description",
    "runtime_status",
    "environments",
    "allowed_responsibilities",
    "prohibited_responsibilities",
    "production_authority",
    "real_portfolio_action_authority",
    "git_write_authority",
    "review_required",
    "protected_boundaries",
)

#: Roles the policy must model (Northstar Phase 0A contract).
REQUIRED_ROLES = (
    "prime",
    "trading_agents",
    "finrobot",
    "local_llm_worker",
    "evidence_auditor",
    "quant_router",
    "stratlab_certification",
    "memo_product_worker",
    "claude_code_builder",
    "claude_code_reviewer",
    "human_operator",
)

_VALID_RUNTIME_STATUS = {"active_today", "defined_not_integrated"}


class AgentPolicyError(Exception):
    """Raised when the policy is missing, malformed, or a lookup is unknown."""


def load_policy(path: Path | str | None = None) -> dict[str, Any]:
    """Load the policy YAML. Raises AgentPolicyError on any problem."""
    policy_path = Path(path) if path is not None else DEFAULT_POLICY_PATH
    if not policy_path.exists():
        raise AgentPolicyError(f"agent policy not found: {policy_path}")
    try:
        data = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # pragma: no cover - malformed-yaml guard
        raise AgentPolicyError(f"agent policy is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise AgentPolicyError("agent policy must be a YAML mapping at top level")
    return data


def validate_policy(policy: dict[str, Any]) -> list[str]:
    """Return a list of schema/invariant violations (empty list == valid)."""
    errors: list[str] = []

    version = policy.get("policy_version")
    if not isinstance(version, str) or len(version.split(".")) != 3:
        errors.append("policy_version must be a MAJOR.MINOR.PATCH string")

    invariants = policy.get("global_invariants")
    if not isinstance(invariants, dict):
        errors.append("global_invariants block is required")
        invariants = {}
    for flag in ("advisory_only", "no_auto_trading", "no_broker_execution"):
        if invariants.get(flag) is not True:
            errors.append(f"global_invariants.{flag} must be true")
    if invariants.get("production_approval_roles") != ["human_operator"]:
        errors.append("production_approval_roles must be exactly [human_operator]")
    if invariants.get("real_portfolio_action_roles") != ["human_operator"]:
        errors.append("real_portfolio_action_roles must be exactly [human_operator]")
    cam = invariants.get("capital_authority_model")
    if not isinstance(cam, dict):
        errors.append("capital_authority_model block is required")
    else:
        # The policy must explicitly distinguish future Capital & Risk ADVISORY
        # allocation ownership from human-only REAL portfolio-action authority.
        if cam.get("advisory_capital_allocation_owner") != "capital_risk_engine_v2_after_certification":
            errors.append("capital_authority_model.advisory_capital_allocation_owner must name the future certified Capital & Risk Engine")
        if cam.get("real_portfolio_action_owner") != "human_operator":
            errors.append("capital_authority_model.real_portfolio_action_owner must be human_operator")
        if cam.get("ai_research_workers") != "neither":
            errors.append("capital_authority_model.ai_research_workers must be 'neither'")
    if invariants.get("ai_workers_cannot_be_production_approvers") is not True:
        errors.append("ai_workers_cannot_be_production_approvers must be true")

    environments = policy.get("environments")
    if not isinstance(environments, dict) or not environments:
        errors.append("environments block is required and non-empty")
        environments = {}
    for env_name, env in environments.items():
        if not isinstance(env, dict):
            errors.append(f"environment {env_name} must be a mapping")
            continue
        # Fail-closed contract: the action-authority capability must be an
        # explicit boolean on every environment — never implicit.
        if not isinstance(env.get("real_portfolio_action_allowed"), bool):
            errors.append(
                f"environment {env_name} must declare real_portfolio_action_allowed as a boolean"
            )
    # Hard environment invariants: the research lab and read-only ops mode can
    # NEVER be action-authority environments, for any role incl. the human.
    for locked_env in ("home_agent_lab", "vps_read_only_ops"):
        if locked_env in environments and environments[locked_env].get("real_portfolio_action_allowed") is not False:
            errors.append(f"{locked_env}.real_portfolio_action_allowed must be false (permanent invariant)")
    if "home_agent_lab" in environments and environments["home_agent_lab"].get("production_mutation_allowed") is not False:
        errors.append("home_agent_lab.production_mutation_allowed must be false (permanent invariant)")

    roles = policy.get("roles")
    if not isinstance(roles, dict):
        errors.append("roles block is required")
        roles = {}

    for required in REQUIRED_ROLES:
        if required not in roles:
            errors.append(f"required role missing: {required}")

    for name, role in roles.items():
        if not isinstance(role, dict):
            errors.append(f"role {name} must be a mapping")
            continue
        for field in REQUIRED_ROLE_FIELDS:
            if field not in role:
                errors.append(f"role {name} missing field: {field}")
        if role.get("runtime_status") not in _VALID_RUNTIME_STATUS:
            errors.append(f"role {name} has invalid runtime_status")
        for env in role.get("environments", []):
            if env not in environments:
                errors.append(f"role {name} references unknown environment: {env}")
        # Authority invariants — only the human operator may hold these.
        if name != "human_operator":
            if role.get("production_authority") is not False:
                errors.append(f"role {name} must have production_authority: false")
            if role.get("real_portfolio_action_authority") is not False:
                errors.append(f"role {name} must have real_portfolio_action_authority: false")

    return errors


def resolve_authority(role: str, environment: str, policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """Deterministically resolve a role's effective authority in an environment.

    Fail-closed: unknown role/environment raises; a role not listed for the
    environment resolves with ``permitted_in_environment: False`` and every
    write-capable grant forced to False.
    """
    pol = policy if policy is not None else load_policy()
    roles = pol.get("roles", {})
    environments = pol.get("environments", {})
    if role not in roles:
        raise AgentPolicyError(f"unknown role: {role}")
    if environment not in environments:
        raise AgentPolicyError(f"unknown environment: {environment}")

    role_block = roles[role]
    env_block = environments[environment]
    permitted = environment in role_block.get("environments", [])

    def _gate(grant: bool, env_flag: str) -> bool:
        # A grant survives only if the role holds it AND the environment
        # permits it AND the role is permitted in the environment at all.
        return bool(grant) and bool(env_block.get(env_flag, False)) and permitted

    return {
        "role": role,
        "environment": environment,
        "permitted_in_environment": permitted,
        "runtime_status": role_block.get("runtime_status"),
        "allowed_responsibilities": list(role_block.get("allowed_responsibilities", [])) if permitted else [],
        "prohibited_responsibilities": list(role_block.get("prohibited_responsibilities", [])),
        "production_authority": bool(role_block.get("production_authority", False))
        and bool(env_block.get("production_mutation_allowed", False))
        and permitted,
        "real_portfolio_action_authority": _gate(
            role_block.get("real_portfolio_action_authority", False),
            "real_portfolio_action_allowed",
        ),
        "git_write_authority": _gate(role_block.get("git_write_authority", False), "git_write_allowed"),
        "review_required": role_block.get("review_required"),
        "protected_boundaries": list(pol.get("global_invariants", {}).get("protected_boundaries", [])),
    }
