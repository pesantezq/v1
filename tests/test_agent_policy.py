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
    REQUIRED_ROLE_FIELDS,
    REQUIRED_ROLES,
    AgentPolicyError,
    load_policy,
    resolve_authority,
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
    for env in policy["environments"]:
        resolved = resolve_authority("prime", env, policy)
        assert resolved["production_authority"] is False
        assert resolved["real_portfolio_action_authority"] is False
        assert resolved["git_write_authority"] is False


def test_resolve_builder_write_gated_by_environment(policy):
    dev = resolve_authority("claude_code_builder", "vps_dev_on_vps", policy)
    ro = resolve_authority("claude_code_builder", "vps_read_only_ops", policy)
    assert dev["git_write_authority"] is True
    # read_only_ops denies git writes even though the role holds the grant
    assert ro["git_write_authority"] is False
    # real portfolio-action authority is false regardless of environment
    assert dev["real_portfolio_action_authority"] is False and ro["real_portfolio_action_authority"] is False


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
        # ...but the lab grants no production/action/git authority to anyone.
        assert resolved["production_authority"] is False
        assert resolved["real_portfolio_action_authority"] is False
        assert resolved["git_write_authority"] is False


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


def test_home_agent_lab_never_grants_production_authority_to_anyone(policy):
    for name in policy["roles"]:
        resolved = resolve_authority(name, "home_agent_lab", policy)
        assert resolved["production_authority"] is False, (
            f"{name}: home_agent_lab must never be a production authority environment"
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
