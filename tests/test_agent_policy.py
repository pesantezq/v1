"""Tests for the machine-readable agent authority policy.

Covers NORTHSTAR_0A validation requirements:
- config/agent_policy.yaml parses and has the required roles (req 4)
- Prime has no production/real-portfolio-action authority
- Claude Builder has no real-portfolio-action authority
- AI research workers cannot be represented as production approvers
- human production + real-portfolio-action authority remains explicit
- the home Agent Lab boundary: research workers live in home_agent_lab and are
  NOT permitted merely because a session runs under vps_dev_on_vps
- advisory capital-allocation ownership (future certified Capital & Risk
  Engine) is explicitly distinct from real portfolio-action authority (human)
- Quant Router routes but cannot certify; StratLab certification cannot
  allocate capital or approve production
"""
from __future__ import annotations

import pytest

from portfolio_automation.agent_policy import (
    REQUIRED_DOMAINS,
    REQUIRED_ROLE_FIELDS,
    REQUIRED_ROLES,
    AgentPolicyError,
    load_policy,
    resolve_authority,
    resolve_operational_authority,
    validate_policy,
)


@pytest.fixture(scope="module")
def policy() -> dict:
    return load_policy()


# ── Schema / parse ─────────────────────────────────────────────────────────


def test_policy_parses_and_validates_clean(policy):
    assert validate_policy(policy) == []


def test_policy_version_is_semver(policy):
    parts = policy["policy_version"].split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts)


def test_all_required_roles_present(policy):
    for role in REQUIRED_ROLES:
        assert role in policy["roles"], f"missing required role: {role}"


def test_every_role_has_required_fields(policy):
    for name, role in policy["roles"].items():
        for field in REQUIRED_ROLE_FIELDS:
            assert field in role, f"role {name} missing {field}"


# ── Authority invariants ───────────────────────────────────────────────────


def test_prime_has_no_production_or_real_portfolio_action_authority(policy):
    prime = policy["roles"]["prime"]
    assert prime["production_authority"] is False
    assert prime["real_portfolio_action_authority"] is False
    assert "approve_production" in prime["prohibited_responsibilities"]
    assert "allocate_capital_authoritatively" in prime["prohibited_responsibilities"]
    assert "bypass_stratlab_for_quantitative_certification" in prime["prohibited_responsibilities"]


def test_claude_builder_has_no_real_portfolio_action_authority(policy):
    builder = policy["roles"]["claude_code_builder"]
    assert builder["real_portfolio_action_authority"] is False
    assert builder["production_authority"] is False
    assert "investment_decisions" in builder["prohibited_responsibilities"]


def test_no_ai_role_is_a_production_approver(policy):
    # Requirement 7: AI research workers cannot be represented as production
    # approvers — only the human operator holds production authority.
    for name, role in policy["roles"].items():
        if name == "human_operator":
            continue
        assert role["production_authority"] is False, (
            f"AI role {name} must never hold production authority"
        )
        assert role["real_portfolio_action_authority"] is False, (
            f"AI role {name} must never hold real portfolio-action authority"
        )
    inv = policy["global_invariants"]
    assert inv["production_approval_roles"] == ["human_operator"]
    assert inv["real_portfolio_action_roles"] == ["human_operator"]
    assert inv["ai_workers_cannot_be_production_approvers"] is True


def test_human_production_authority_is_explicit(policy):
    human = policy["roles"]["human_operator"]
    assert human["production_authority"] is True
    assert human["real_portfolio_action_authority"] is True
    assert "production_promotion_approval" in human["allowed_responsibilities"]


def test_global_invariants_advisory_only(policy):
    inv = policy["global_invariants"]
    assert inv["advisory_only"] is True
    assert inv["no_auto_trading"] is True
    assert inv["no_broker_execution"] is True


def test_future_workers_are_not_claimed_integrated(policy):
    # Prime/TradingAgents/FinRobot have authority contracts NOW but must not be
    # represented as running/integrated until their Northstar phases.
    # Req: a related deterministic subsystem existing today (memo/digest layer,
    # Strategy Lab) does NOT make the named future worker/plane active.
    for name in (
        "prime",
        "trading_agents",
        "finrobot",
        "local_llm_worker",
        "evidence_auditor",
        "quant_router",
        "stratlab_certification",
        "memo_product_worker",
    ):
        assert policy["roles"][name]["runtime_status"] == "defined_not_integrated", (
            f"{name} must not be represented as integrated before its phase"
        )
    # Roles whose contract a runtime genuinely satisfies today stay active.
    for name in ("claude_code_builder", "claude_code_reviewer", "human_operator"):
        assert policy["roles"][name]["runtime_status"] == "active_today"


# ── Deterministic resolution ───────────────────────────────────────────────


def test_resolve_unknown_role_fails_closed(policy):
    with pytest.raises(AgentPolicyError):
        resolve_authority("nonexistent_role", "vps_dev_on_vps", policy)


def test_resolve_unknown_environment_fails_closed(policy):
    with pytest.raises(AgentPolicyError):
        resolve_authority("prime", "nonexistent_env", policy)


def test_resolve_prime_never_gains_authority_anywhere(policy):
    # Execution plane: no git writes from any environment.
    for env in policy["environments"]:
        resolved = resolve_authority("prime", env, policy)
        assert resolved["git_write_authority"] is False
    # Governance plane: no promotion/action authority in any domain.
    for domain in policy["operational_authority_domains"]:
        op = resolve_operational_authority("prime", domain, policy)
        assert op["production_promotion_authority"] is False
        assert op["real_portfolio_action_authority"] is False


def test_resolve_builder_write_gated_by_environment(policy):
    # Execution-mode independence, builder side: dev_on_vps and read_only_ops
    # differ in execution/write capability as intended...
    dev = resolve_authority("claude_code_builder", "vps_dev_on_vps", policy)
    ro = resolve_authority("claude_code_builder", "vps_read_only_ops", policy)
    assert dev["git_write_authority"] is True
    assert dev["code_write_authority"] is True
    assert dev["production_mutation_capability"] is True
    # read_only_ops denies all writes even though the role holds the git grant
    assert ro["git_write_authority"] is False
    assert ro["code_write_authority"] is False
    assert ro["production_mutation_capability"] is False
    # ...but in BOTH modes the builder holds zero governance authority.
    for domain in policy["operational_authority_domains"]:
        op = resolve_operational_authority("claude_code_builder", domain, policy)
        assert op["production_promotion_authority"] is False
        assert op["real_portfolio_action_authority"] is False


def test_resolve_role_not_permitted_in_environment_loses_grants(policy):
    # gpt_architect is a laptop planning role; on the VPS it resolves to no grants.
    resolved = resolve_authority("gpt_architect", "vps_dev_on_vps", policy)
    assert resolved["permitted_in_environment"] is False
    assert resolved["allowed_responsibilities"] == []
    assert resolved["git_write_authority"] is False


def test_validator_rejects_ai_role_with_production_authority(policy):
    import copy

    bad = copy.deepcopy(policy)
    bad["roles"]["prime"]["production_authority"] = True
    errors = validate_policy(bad)
    assert any("prime" in e and "production_authority" in e for e in errors)


def test_validator_rejects_missing_required_role(policy):
    import copy

    bad = copy.deepcopy(policy)
    del bad["roles"]["evidence_auditor"]
    errors = validate_policy(bad)
    assert any("evidence_auditor" in e for e in errors)


# ── Home Agent Lab boundary (2026-08-09 hardening) ─────────────────────────

RESEARCH_WORKERS = ("prime", "trading_agents", "finrobot", "local_llm_worker", "evidence_auditor")


def test_home_agent_lab_exists_and_is_non_production(policy):
    lab = policy["environments"]["home_agent_lab"]
    assert lab["production_mutation_allowed"] is False
    assert lab["git_write_allowed"] is False
    assert lab["code_write_allowed"] is True  # local research artifacts allowed
    # It consumes frozen/sanitized exports, never the live server.
    desc = lab["description"].lower()
    for word in ("frozen", "sanitized", "hash-verified"):
        assert word in desc, f"lab description must state it consumes {word} evidence"


def test_research_workers_permitted_in_home_agent_lab(policy):
    for name in RESEARCH_WORKERS:
        resolved = resolve_authority(name, "home_agent_lab", policy)
        assert resolved["permitted_in_environment"] is True, f"{name} belongs in the lab"
        # ...with research execution capability only: no git writes, no
        # production mutation capability.
        assert resolved["git_write_authority"] is False
        assert resolved["production_mutation_capability"] is False


def test_research_workers_not_permitted_on_vps_dev(policy):
    # Reqs 3-7: running a session under vps_dev_on_vps must NOT make the
    # research workers permitted — they analyze exports in the lab, never the
    # production checkout.
    for name in RESEARCH_WORKERS:
        resolved = resolve_authority(name, "vps_dev_on_vps", policy)
        assert resolved["permitted_in_environment"] is False, (
            f"{name} must fail closed under vps_dev_on_vps"
        )
        assert resolved["allowed_responsibilities"] == []
        assert resolved["git_write_authority"] is False


def test_home_agent_lab_grants_no_write_or_mutation_capability(policy):
    # Execution plane: the lab gives NO role git-write or production-mutation
    # capability — research artifacts only.
    for name in policy["roles"]:
        resolved = resolve_authority(name, "home_agent_lab", policy)
        assert resolved["git_write_authority"] is False
        assert resolved["production_mutation_capability"] is False, (
            f"{name}: home_agent_lab must never carry production mutation capability"
        )


def test_research_plane_grants_no_governance_authority_to_anyone(policy):
    # Governance plane: the research domain confers promotion/action authority
    # on NO role — explicitly including human_operator, who holds the global
    # grants but is not a research_plane member.
    for name in policy["roles"]:
        op = resolve_operational_authority(name, "research_plane", policy)
        assert op["production_promotion_authority"] is False, (
            f"{name}: research_plane must never confer production promotion"
        )
        assert op["real_portfolio_action_authority"] is False, (
            f"{name}: research_plane must never confer real portfolio action"
        )


# ── Advisory capital determination vs real portfolio action ────────────────


def test_policy_distinguishes_advisory_allocation_from_real_action(policy):
    # Req 12: Capital & Risk Engine = future authoritative advisory allocator
    # after certification; human = production promotion + real portfolio
    # action; AI research workers = neither.
    cam = policy["global_invariants"]["capital_authority_model"]
    assert cam["advisory_capital_allocation_owner"] == "capital_risk_engine_v2_after_certification"
    assert cam["real_portfolio_action_owner"] == "human_operator"
    assert cam["ai_research_workers"] == "neither"
    assert cam["prediction_engine_note"] == "predictions_are_never_portfolio_actions"


def test_human_is_only_real_portfolio_action_authority(policy):
    holders = [n for n, r in policy["roles"].items() if r["real_portfolio_action_authority"]]
    assert holders == ["human_operator"]


def test_human_is_only_production_promotion_authority(policy):
    holders = [n for n, r in policy["roles"].items() if r["production_authority"]]
    assert holders == ["human_operator"]


# ── Quant Router vs StratLab certification ─────────────────────────────────


def test_quant_router_cannot_certify(policy):
    router = policy["roles"]["quant_router"]
    assert "certify_quantitative_truth" in router["prohibited_responsibilities"]
    assert "issue_incremental_value_verdicts" in router["prohibited_responsibilities"]
    for resp in router["allowed_responsibilities"]:
        assert "certif" not in resp and "verdict" not in resp, (
            f"quant_router allowed responsibility {resp!r} smells like certification authority"
        )
    assert router["runtime_status"] == "defined_not_integrated"


def test_stratlab_certification_cannot_allocate_or_approve(policy):
    plane = policy["roles"]["stratlab_certification"]
    prohibited = plane["prohibited_responsibilities"]
    assert "capital_allocation" in prohibited
    assert "production_approval" in prohibited
    assert "modifying_predictions" in prohibited
    assert "causing_real_portfolio_actions" in prohibited
    assert plane["production_authority"] is False
    assert plane["real_portfolio_action_authority"] is False
    # It may issue certification evidence — that is its purpose.
    assert "issue_certification_evidence" in plane["allowed_responsibilities"]


def test_stratlab_acknowledges_existing_subsystem_without_overclaiming(policy):
    plane = policy["roles"]["stratlab_certification"]
    assert plane["runtime_status"] == "defined_not_integrated"
    assert "Strategy Lab" in plane["existing_subsystem"]


def test_memo_worker_distinguished_from_existing_memo_subsystem(policy):
    worker = policy["roles"]["memo_product_worker"]
    assert worker["runtime_status"] == "defined_not_integrated"
    assert "existing_subsystem" in worker
    assert "home_agent_lab" in worker["environments"]


def test_validator_rejects_ai_role_with_real_portfolio_action_authority(policy):
    import copy

    bad = copy.deepcopy(policy)
    bad["roles"]["trading_agents"]["real_portfolio_action_authority"] = True
    errors = validate_policy(bad)
    assert any("trading_agents" in e and "real_portfolio_action_authority" in e for e in errors)


# ── Operational authority domains (final 0A model correction) ──────────────
# Execution environments (Claude/tool modes) and operational governance
# authority are now mechanically independent concepts.


def test_required_domains_exist_with_correct_capabilities(policy):
    domains = policy["operational_authority_domains"]
    for name in REQUIRED_DOMAINS:
        assert name in domains
    assert domains["production_control_plane"]["production_promotion_allowed"] is True
    assert domains["production_control_plane"]["real_portfolio_action_allowed"] is True
    assert domains["research_plane"]["production_promotion_allowed"] is False
    assert domains["research_plane"]["real_portfolio_action_allowed"] is False


def test_environments_carry_no_governance_capabilities(policy):
    # The 887b4e90-era coupling (env-level real_portfolio_action_allowed) must
    # be gone — execution environments describe tooling capability only, and
    # the validator forbids reintroducing governance fields on them.
    for env_name, env in policy["environments"].items():
        assert "real_portfolio_action_allowed" not in env, env_name
        assert "production_promotion_allowed" not in env, env_name


def test_human_authority_resolves_through_production_control_plane(policy):
    op = resolve_operational_authority("human_operator", "production_control_plane", policy)
    assert op["member_of_domain"] is True
    assert op["production_promotion_authority"] is True
    assert op["real_portfolio_action_authority"] is True


def test_human_authority_independent_of_claude_vps_mode(policy):
    # THE key regression test: switching Claude between dev_on_vps and
    # read_only_ops changes Claude's execution permissions ONLY. The human
    # operator's governance authority resolves through the production control
    # plane and must be IDENTICAL regardless of Claude's mode — making
    # "Claude becomes read-only → human loses production authority"
    # impossible to reintroduce.
    dev = resolve_authority("claude_code_builder", "vps_dev_on_vps", policy)
    ro = resolve_authority("claude_code_builder", "vps_read_only_ops", policy)
    assert dev["git_write_authority"] != ro["git_write_authority"]  # modes really differ

    human_before = resolve_operational_authority("human_operator", "production_control_plane", policy)
    human_after = resolve_operational_authority("human_operator", "production_control_plane", policy)
    assert human_before == human_after
    assert human_before["production_promotion_authority"] is True
    assert human_before["real_portfolio_action_authority"] is True
    # Mechanical independence: the governance resolver never reads execution
    # environments, so no environment name is even a valid domain input.
    with pytest.raises(AgentPolicyError):
        resolve_operational_authority("human_operator", "vps_read_only_ops", policy)


def test_no_ai_role_gains_authority_from_production_domain(policy):
    # An AI worker resolving against the production domain must not inherit
    # the domain's capabilities: not a member, no role grant, no authority.
    for name in policy["roles"]:
        if name == "human_operator":
            continue
        op = resolve_operational_authority(name, "production_control_plane", policy)
        assert op["member_of_domain"] is False, f"{name} must not be a production_control_plane member"
        assert op["production_promotion_authority"] is False, name
        assert op["real_portfolio_action_authority"] is False, name


def test_unknown_domain_fails_closed(policy):
    with pytest.raises(AgentPolicyError):
        resolve_operational_authority("human_operator", "nonexistent_domain", policy)


def test_validator_rejects_ai_membership_in_production_control_plane(policy):
    import copy

    bad = copy.deepcopy(policy)
    bad["roles"]["prime"]["operational_authority_domains"] = ["production_control_plane"]
    errors = validate_policy(bad)
    assert any("prime" in e and "production_control_plane" in e for e in errors)


def test_validator_rejects_action_authoritative_research_plane(policy):
    import copy

    bad = copy.deepcopy(policy)
    bad["operational_authority_domains"]["research_plane"]["real_portfolio_action_allowed"] = True
    errors = validate_policy(bad)
    assert any("research_plane" in e for e in errors)


def test_validator_rejects_environment_declaring_governance_capability(policy):
    import copy

    bad = copy.deepcopy(policy)
    bad["environments"]["vps_dev_on_vps"]["real_portfolio_action_allowed"] = True
    errors = validate_policy(bad)
    assert any("vps_dev_on_vps" in e and "governance" in e for e in errors)


def test_validator_rejects_missing_domain_structure(policy):
    import copy

    bad = copy.deepcopy(policy)
    del bad["operational_authority_domains"]
    errors = validate_policy(bad)
    assert any("operational_authority_domains" in e for e in errors)

    bad2 = copy.deepcopy(policy)
    del bad2["operational_authority_domains"]["production_control_plane"]["production_promotion_allowed"]
    errors2 = validate_policy(bad2)
    assert any("production_control_plane" in e and "production_promotion_allowed" in e for e in errors2)


def test_human_responsibility_term_is_unambiguous(policy):
    # capital_and_risk_final_authority was ambiguous (advisory allocation logic
    # belongs to the future certified Capital & Risk Engine). The human role
    # carries the narrow real-action term instead.
    allowed = policy["roles"]["human_operator"]["allowed_responsibilities"]
    assert "real_portfolio_action_final_authority" in allowed
    assert "capital_and_risk_final_authority" not in allowed
