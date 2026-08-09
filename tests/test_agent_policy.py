"""Tests for the machine-readable agent authority policy.

Covers NORTHSTAR_0A validation requirements:
- config/agent_policy.yaml parses and has the required roles (req 4)
- Prime has no production/investment authority (req 5)
- Claude Builder has no investment authority (req 6)
- AI research workers cannot be represented as production approvers (req 7)
- human production authority remains explicit (req 8)
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


def test_prime_has_no_production_or_investment_authority(policy):
    prime = policy["roles"]["prime"]
    assert prime["production_authority"] is False
    assert prime["investment_authority"] is False
    assert "approve_production" in prime["prohibited_responsibilities"]
    assert "allocate_capital_authoritatively" in prime["prohibited_responsibilities"]
    assert "bypass_stratlab_for_quantitative_certification" in prime["prohibited_responsibilities"]


def test_claude_builder_has_no_investment_authority(policy):
    builder = policy["roles"]["claude_code_builder"]
    assert builder["investment_authority"] is False
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
        assert role["investment_authority"] is False, (
            f"AI role {name} must never hold investment authority"
        )
    inv = policy["global_invariants"]
    assert inv["production_approval_roles"] == ["human_operator"]
    assert inv["ai_workers_cannot_be_production_approvers"] is True


def test_human_production_authority_is_explicit(policy):
    human = policy["roles"]["human_operator"]
    assert human["production_authority"] is True
    assert human["investment_authority"] is True
    assert "production_promotion_approval" in human["allowed_responsibilities"]


def test_global_invariants_advisory_only(policy):
    inv = policy["global_invariants"]
    assert inv["advisory_only"] is True
    assert inv["no_auto_trading"] is True
    assert inv["no_broker_execution"] is True


def test_future_workers_are_not_claimed_integrated(policy):
    # Prime/TradingAgents/FinRobot have authority contracts NOW but must not be
    # represented as running/integrated until their Northstar phases.
    for name in ("prime", "trading_agents", "finrobot", "local_llm_worker", "evidence_auditor"):
        assert policy["roles"][name]["runtime_status"] == "defined_not_integrated", (
            f"{name} must not be represented as integrated before its phase"
        )


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
        assert resolved["investment_authority"] is False
        assert resolved["git_write_authority"] is False


def test_resolve_builder_write_gated_by_environment(policy):
    dev = resolve_authority("claude_code_builder", "vps_dev_on_vps", policy)
    ro = resolve_authority("claude_code_builder", "vps_read_only_ops", policy)
    assert dev["git_write_authority"] is True
    # read_only_ops denies git writes even though the role holds the grant
    assert ro["git_write_authority"] is False
    # investment authority is false regardless of environment
    assert dev["investment_authority"] is False and ro["investment_authority"] is False


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
