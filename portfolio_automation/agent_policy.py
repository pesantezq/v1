"""Read-only loader/validator for the machine-readable agent authority policy.

Source of truth: ``config/agent_policy.yaml`` (see the header comments there).
This module exists ONLY to prove the policy parses and to give tests (and,
later, explicitly-authorized enforcement work) deterministic resolution paths.
It never writes anything, never touches outputs/, and is not wired into any
pipeline stage.

Two mechanically distinct resolvers, mirroring the policy's two concepts:

* ``resolve_authority(role, environment)`` — EXECUTION authority: what an
  agent/tooling session may do from an execution environment (code/git
  writes, worker placement, validation claims). Claude's VPS mode
  (dev_on_vps vs read_only_ops) lives here and ONLY here.
* ``resolve_operational_authority(role, domain)`` — OPERATIONAL GOVERNANCE
  authority: production promotion + real portfolio action, resolved through
  operational_authority_domains. Independent of execution mode — switching
  Claude to read_only_ops cannot revoke human governance authority.

Both are deterministic and fail-closed:

* unknown role/environment/domain names raise ``AgentPolicyError``
* absence of a grant (or of domain membership) is a denial
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
    "operational_authority_domains",
    "git_write_authority",
    "review_required",
    "protected_boundaries",
)

#: Operational authority domains the policy must model.
REQUIRED_DOMAINS = ("production_control_plane", "research_plane")

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
        # Execution environments describe agent/tool session capability ONLY.
        # Governance capabilities live in operational_authority_domains — an
        # environment declaring one would reintroduce the execution/governance
        # coupling this schema forbids (two competing mechanisms).
        if "real_portfolio_action_allowed" in env or "production_promotion_allowed" in env:
            errors.append(
                f"environment {env_name} must not declare governance capabilities "
                "(they belong in operational_authority_domains)"
            )
    if "home_agent_lab" in environments and environments["home_agent_lab"].get("production_mutation_allowed") is not False:
        errors.append("home_agent_lab.production_mutation_allowed must be false (permanent invariant)")

    domains = policy.get("operational_authority_domains")
    if not isinstance(domains, dict):
        errors.append("operational_authority_domains block is required")
        domains = {}
    for required in REQUIRED_DOMAINS:
        if required not in domains:
            errors.append(f"required operational authority domain missing: {required}")
    for dom_name, dom in domains.items():
        if not isinstance(dom, dict):
            errors.append(f"domain {dom_name} must be a mapping")
            continue
        for cap in ("production_promotion_allowed", "real_portfolio_action_allowed"):
            if not isinstance(dom.get(cap), bool):
                errors.append(f"domain {dom_name} must declare {cap} as a boolean")
    # Permanent invariant: the research plane can never become an
    # action/production-authority domain.
    if "research_plane" in domains:
        rp = domains["research_plane"]
        if rp.get("production_promotion_allowed") is not False or rp.get("real_portfolio_action_allowed") is not False:
            errors.append("research_plane must be permanently non-production (both capabilities false)")

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
        role_domains = role.get("operational_authority_domains", [])
        if not isinstance(role_domains, list):
            errors.append(f"role {name} operational_authority_domains must be a list")
            role_domains = []
        for dom in role_domains:
            if dom not in domains:
                errors.append(f"role {name} references unknown operational authority domain: {dom}")
        # Only the human operator may be a member of the production control
        # plane — an AI role must not acquire governance authority merely
        # because the domain itself carries it.
        if name != "human_operator" and "production_control_plane" in role_domains:
            errors.append(f"role {name} must not be a member of production_control_plane")
        # Authority invariants — only the human operator may hold these.
        if name != "human_operator":
            if role.get("production_authority") is not False:
                errors.append(f"role {name} must have production_authority: false")
            if role.get("real_portfolio_action_authority") is not False:
                errors.append(f"role {name} must have real_portfolio_action_authority: false")

    return errors


def resolve_authority(role: str, environment: str, policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """Deterministically resolve a role's effective EXECUTION authority in an
    execution environment (what a session may do from there).

    Governance authority (production promotion, real portfolio action) is NOT
    part of this resolution — use :func:`resolve_operational_authority`; an
    execution mode like read_only_ops constrains Claude's tooling, never the
    human operator's governance authority.

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
        "code_write_authority": permitted and bool(env_block.get("code_write_allowed", False)),
        "git_write_authority": _gate(role_block.get("git_write_authority", False), "git_write_allowed"),
        "production_mutation_capability": bool(env_block.get("production_mutation_allowed", False)) and permitted,
        "validation_claims": env_block.get("validation_claims"),
        "review_required": role_block.get("review_required"),
        "protected_boundaries": list(pol.get("global_invariants", {}).get("protected_boundaries", [])),
    }


def resolve_operational_authority(role: str, domain: str, policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """Deterministically resolve a role's OPERATIONAL GOVERNANCE authority in an
    operational authority domain.

    ``true`` requires ALL of: the role grant, role membership in the domain,
    and the domain capability. Unknown role/domain raises; absence of any
    element is denial (fail closed). Independent of execution environments —
    Claude's VPS mode cannot change what this returns.
    """
    pol = policy if policy is not None else load_policy()
    roles = pol.get("roles", {})
    domains = pol.get("operational_authority_domains", {})
    if role not in roles:
        raise AgentPolicyError(f"unknown role: {role}")
    if domain not in domains:
        raise AgentPolicyError(f"unknown operational authority domain: {domain}")

    role_block = roles[role]
    dom_block = domains[domain]
    member = domain in role_block.get("operational_authority_domains", [])

    def _grant(role_field: str, dom_cap: str) -> bool:
        return (
            bool(role_block.get(role_field, False))
            and member
            and bool(dom_block.get(dom_cap, False))
        )

    return {
        "role": role,
        "domain": domain,
        "member_of_domain": member,
        "production_promotion_authority": _grant("production_authority", "production_promotion_allowed"),
        "real_portfolio_action_authority": _grant("real_portfolio_action_authority", "real_portfolio_action_allowed"),
    }
