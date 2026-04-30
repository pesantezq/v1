"""
Tests for portfolio_automation.run_mode_governance.

Coverage:
- RunMode enum values
- normalize_run_mode (canonical + legacy aliases + unknown rejection)
- get_run_mode_policy for all 6 modes
- can_execute_trades is False for every mode
- is_official_mode / is_research_only_mode
- validate_output_write per mode + namespace
- assert_can_write_namespace (pass and raise)
- assert_can_update_portfolio_state (approval gate)
- assert_can_update_watchlist (approval gate)
- assert_can_emit_recommendation
- Discovery lane restrictions
- MANUAL_UPDATE approval behavior
- BACKTEST restrictions
- HISTORICAL_REPLAY restrictions
- DAILY mode remains allowed for latest/policy writes
- RunModeContext creation and fields
- create_run_mode_context factory
"""

import pytest

from portfolio_automation.run_mode_governance import (
    RunMode,
    RunModeContext,
    RunModePolicy,
    RunModeViolation,
    assert_can_emit_recommendation,
    assert_can_update_portfolio_state,
    assert_can_update_watchlist,
    assert_can_write_namespace,
    create_run_mode_context,
    get_run_mode_policy,
    is_official_mode,
    is_research_only_mode,
    normalize_run_mode,
    validate_output_write,
)


# ---------------------------------------------------------------------------
# 1. RunMode enum
# ---------------------------------------------------------------------------

class TestRunModeEnum:
    def test_all_six_modes_exist(self):
        modes = {m.value for m in RunMode}
        assert modes == {
            "daily", "manual_update", "discovery",
            "weekly_review", "backtest", "historical_replay",
        }

    def test_runmode_is_string_enum(self):
        assert RunMode.DAILY == "daily"
        assert RunMode.DISCOVERY == "discovery"

    def test_runmode_values_are_lowercase(self):
        for mode in RunMode:
            assert mode.value == mode.value.lower()


# ---------------------------------------------------------------------------
# 2. normalize_run_mode
# ---------------------------------------------------------------------------

class TestNormalizeRunMode:
    def test_canonical_daily(self):
        assert normalize_run_mode("daily") is RunMode.DAILY

    def test_canonical_manual_update(self):
        assert normalize_run_mode("manual_update") is RunMode.MANUAL_UPDATE

    def test_canonical_discovery(self):
        assert normalize_run_mode("discovery") is RunMode.DISCOVERY

    def test_canonical_weekly_review(self):
        assert normalize_run_mode("weekly_review") is RunMode.WEEKLY_REVIEW

    def test_canonical_backtest(self):
        assert normalize_run_mode("backtest") is RunMode.BACKTEST

    def test_canonical_historical_replay(self):
        assert normalize_run_mode("historical_replay") is RunMode.HISTORICAL_REPLAY

    def test_legacy_weekly_maps_to_weekly_review(self):
        assert normalize_run_mode("weekly") is RunMode.WEEKLY_REVIEW

    def test_legacy_monthly_maps_to_weekly_review(self):
        assert normalize_run_mode("monthly") is RunMode.WEEKLY_REVIEW

    def test_runmode_instance_returned_as_is(self):
        assert normalize_run_mode(RunMode.DAILY) is RunMode.DAILY
        assert normalize_run_mode(RunMode.DISCOVERY) is RunMode.DISCOVERY

    def test_unknown_string_raises(self):
        with pytest.raises(RunModeViolation, match="Unknown run mode"):
            normalize_run_mode("live_trading")

    def test_empty_string_raises(self):
        with pytest.raises(RunModeViolation):
            normalize_run_mode("")

    def test_non_string_raises(self):
        with pytest.raises(RunModeViolation, match="Cannot normalize"):
            normalize_run_mode(42)  # type: ignore[arg-type]

    def test_uppercase_accepted(self):
        assert normalize_run_mode("DAILY") is RunMode.DAILY

    def test_mixed_case_accepted(self):
        assert normalize_run_mode("Discovery") is RunMode.DISCOVERY


# ---------------------------------------------------------------------------
# 3. get_run_mode_policy
# ---------------------------------------------------------------------------

class TestGetRunModePolicy:
    def test_returns_policy_for_all_modes(self):
        for mode in RunMode:
            policy = get_run_mode_policy(mode)
            assert isinstance(policy, RunModePolicy)
            assert policy.mode is mode

    def test_policy_is_frozen(self):
        policy = get_run_mode_policy(RunMode.DAILY)
        with pytest.raises((AttributeError, TypeError)):
            policy.can_execute_trades = True  # type: ignore[misc]

    def test_daily_policy_fields(self):
        p = get_run_mode_policy(RunMode.DAILY)
        assert p.can_write_latest is True
        assert p.can_write_policy is True
        assert p.can_write_portfolio is True
        assert p.can_write_user_state is False
        assert p.can_write_historical is False
        assert p.can_write_sandbox is False
        assert p.can_update_official_watchlist is False
        assert p.can_change_allocations is False
        assert p.can_change_risk_limits is False
        assert p.can_emit_recommendations is True
        assert p.requires_manual_approval is False

    def test_manual_update_policy_fields(self):
        p = get_run_mode_policy(RunMode.MANUAL_UPDATE)
        assert p.can_write_latest is True
        assert p.can_write_user_state is True
        assert p.can_update_official_watchlist is True
        assert p.can_change_allocations is True
        assert p.can_change_risk_limits is True
        assert p.can_emit_recommendations is True
        assert p.requires_manual_approval is True

    def test_discovery_policy_fields(self):
        p = get_run_mode_policy(RunMode.DISCOVERY)
        assert p.can_write_latest is False
        assert p.can_write_policy is False
        assert p.can_write_portfolio is False
        assert p.can_write_user_state is False
        assert p.can_write_sandbox is True
        assert p.can_write_discovery is True
        assert p.can_update_official_watchlist is False
        assert p.can_change_allocations is False
        assert p.can_emit_recommendations is False
        assert p.requires_manual_approval is False

    def test_weekly_review_policy_fields(self):
        p = get_run_mode_policy(RunMode.WEEKLY_REVIEW)
        assert p.can_write_latest is True
        assert p.can_write_policy is False
        assert p.can_write_portfolio is True
        assert p.can_write_user_state is False
        assert p.can_change_allocations is False
        assert p.can_emit_recommendations is True
        assert p.requires_manual_approval is False

    def test_backtest_policy_fields(self):
        p = get_run_mode_policy(RunMode.BACKTEST)
        assert p.can_write_latest is False
        assert p.can_write_policy is False
        assert p.can_write_historical is True
        assert p.can_write_sandbox is True
        assert p.can_emit_recommendations is False
        assert p.requires_manual_approval is False

    def test_historical_replay_policy_fields(self):
        p = get_run_mode_policy(RunMode.HISTORICAL_REPLAY)
        assert p.can_write_latest is False
        assert p.can_write_policy is False
        assert p.can_write_historical is True
        assert p.can_write_sandbox is False
        assert p.can_emit_recommendations is False
        assert p.requires_manual_approval is False


# ---------------------------------------------------------------------------
# 4. can_execute_trades is ALWAYS False
# ---------------------------------------------------------------------------

class TestCanExecuteTrades:
    def test_no_mode_can_execute_trades(self):
        for mode in RunMode:
            policy = get_run_mode_policy(mode)
            assert policy.can_execute_trades is False, (
                f"VIOLATION: {mode.value} has can_execute_trades=True"
            )


# ---------------------------------------------------------------------------
# 5. is_official_mode / is_research_only_mode
# ---------------------------------------------------------------------------

class TestLaneDetection:
    def test_daily_is_official(self):
        assert is_official_mode(RunMode.DAILY) is True

    def test_manual_update_is_official(self):
        assert is_official_mode(RunMode.MANUAL_UPDATE) is True

    def test_weekly_review_is_official(self):
        assert is_official_mode(RunMode.WEEKLY_REVIEW) is True

    def test_discovery_is_research_only(self):
        assert is_research_only_mode(RunMode.DISCOVERY) is True

    def test_backtest_is_research_only(self):
        assert is_research_only_mode(RunMode.BACKTEST) is True

    def test_historical_replay_is_research_only(self):
        assert is_research_only_mode(RunMode.HISTORICAL_REPLAY) is True

    def test_official_modes_are_not_research_only(self):
        for mode in [RunMode.DAILY, RunMode.MANUAL_UPDATE, RunMode.WEEKLY_REVIEW]:
            assert is_research_only_mode(mode) is False

    def test_research_modes_are_not_official(self):
        for mode in [RunMode.DISCOVERY, RunMode.BACKTEST, RunMode.HISTORICAL_REPLAY]:
            assert is_official_mode(mode) is False

    def test_all_modes_classified(self):
        for mode in RunMode:
            assert is_official_mode(mode) or is_research_only_mode(mode), (
                f"{mode.value} is neither official nor research-only"
            )

    def test_lanes_are_mutually_exclusive(self):
        for mode in RunMode:
            assert not (is_official_mode(mode) and is_research_only_mode(mode)), (
                f"{mode.value} cannot be both official and research-only"
            )


# ---------------------------------------------------------------------------
# 6. validate_output_write — soft check
# ---------------------------------------------------------------------------

class TestValidateOutputWrite:
    # String namespace tests
    def test_daily_can_write_latest(self):
        assert validate_output_write(RunMode.DAILY, "latest") is True

    def test_daily_can_write_policy(self):
        assert validate_output_write(RunMode.DAILY, "policy") is True

    def test_daily_cannot_write_historical(self):
        assert validate_output_write(RunMode.DAILY, "historical") is False

    def test_daily_cannot_write_sandbox(self):
        assert validate_output_write(RunMode.DAILY, "sandbox") is False

    def test_discovery_cannot_write_latest(self):
        assert validate_output_write(RunMode.DISCOVERY, "latest") is False

    def test_discovery_can_write_sandbox(self):
        assert validate_output_write(RunMode.DISCOVERY, "sandbox") is True

    def test_backtest_can_write_historical(self):
        assert validate_output_write(RunMode.BACKTEST, "historical") is True

    def test_backtest_cannot_write_latest(self):
        assert validate_output_write(RunMode.BACKTEST, "latest") is False

    def test_backtest_subdir_backtest_counts_as_historical(self):
        assert validate_output_write(RunMode.BACKTEST, "backtest") is True

    def test_historical_replay_can_write_backtest_subdir(self):
        assert validate_output_write(RunMode.HISTORICAL_REPLAY, "backtest") is True

    def test_historical_replay_cannot_write_latest(self):
        assert validate_output_write(RunMode.HISTORICAL_REPLAY, "latest") is False

    def test_manual_update_can_write_user(self):
        assert validate_output_write(RunMode.MANUAL_UPDATE, "user") is True

    def test_daily_cannot_write_user_state(self):
        assert validate_output_write(RunMode.DAILY, "user") is False

    def test_unknown_namespace_returns_false(self):
        assert validate_output_write(RunMode.DAILY, "nonexistent_namespace") is False

    def test_object_namespace_accepted(self):
        """Accepts OutputNamespace enum instances via .value attribute."""
        class _FakeNS:
            value = "latest"
        assert validate_output_write(RunMode.DAILY, _FakeNS()) is True

    def test_weekly_review_can_write_latest(self):
        assert validate_output_write(RunMode.WEEKLY_REVIEW, "latest") is True

    def test_weekly_review_cannot_write_policy(self):
        assert validate_output_write(RunMode.WEEKLY_REVIEW, "policy") is False


# ---------------------------------------------------------------------------
# 7. assert_can_write_namespace — hard check
# ---------------------------------------------------------------------------

class TestAssertCanWriteNamespace:
    def test_daily_to_latest_passes(self):
        assert_can_write_namespace(RunMode.DAILY, "latest")  # no exception

    def test_daily_to_policy_passes(self):
        assert_can_write_namespace(RunMode.DAILY, "policy")

    def test_discovery_to_latest_raises(self):
        with pytest.raises(RunModeViolation, match="not permitted"):
            assert_can_write_namespace(RunMode.DISCOVERY, "latest")

    def test_backtest_to_latest_raises(self):
        with pytest.raises(RunModeViolation, match="not permitted"):
            assert_can_write_namespace(RunMode.BACKTEST, "latest")

    def test_historical_replay_to_policy_raises(self):
        with pytest.raises(RunModeViolation):
            assert_can_write_namespace(RunMode.HISTORICAL_REPLAY, "policy")

    def test_discovery_to_sandbox_passes(self):
        assert_can_write_namespace(RunMode.DISCOVERY, "sandbox")

    def test_backtest_to_historical_passes(self):
        assert_can_write_namespace(RunMode.BACKTEST, "historical")

    def test_path_argument_accepted(self):
        assert_can_write_namespace(RunMode.DAILY, "latest", path="outputs/latest/foo.json")


# ---------------------------------------------------------------------------
# 8. assert_can_update_portfolio_state
# ---------------------------------------------------------------------------

class TestAssertCanUpdatePortfolioState:
    def test_manual_update_with_approval_passes(self):
        assert_can_update_portfolio_state(RunMode.MANUAL_UPDATE, approved=True)

    def test_manual_update_without_approval_raises(self):
        with pytest.raises(RunModeViolation, match="manual approval"):
            assert_can_update_portfolio_state(RunMode.MANUAL_UPDATE, approved=False)

    def test_daily_cannot_change_allocations(self):
        with pytest.raises(RunModeViolation, match="cannot change"):
            assert_can_update_portfolio_state(RunMode.DAILY)

    def test_discovery_cannot_change_allocations(self):
        with pytest.raises(RunModeViolation, match="cannot change"):
            assert_can_update_portfolio_state(RunMode.DISCOVERY)

    def test_backtest_cannot_change_allocations(self):
        with pytest.raises(RunModeViolation, match="cannot change"):
            assert_can_update_portfolio_state(RunMode.BACKTEST)

    def test_historical_replay_cannot_change_allocations(self):
        with pytest.raises(RunModeViolation):
            assert_can_update_portfolio_state(RunMode.HISTORICAL_REPLAY)

    def test_weekly_review_cannot_change_allocations(self):
        with pytest.raises(RunModeViolation):
            assert_can_update_portfolio_state(RunMode.WEEKLY_REVIEW)


# ---------------------------------------------------------------------------
# 9. assert_can_update_watchlist
# ---------------------------------------------------------------------------

class TestAssertCanUpdateWatchlist:
    def test_manual_update_with_approval_passes(self):
        assert_can_update_watchlist(RunMode.MANUAL_UPDATE, approved=True)

    def test_manual_update_without_approval_raises(self):
        with pytest.raises(RunModeViolation, match="manual approval"):
            assert_can_update_watchlist(RunMode.MANUAL_UPDATE, approved=False)

    def test_daily_cannot_update_watchlist(self):
        with pytest.raises(RunModeViolation, match="cannot update"):
            assert_can_update_watchlist(RunMode.DAILY)

    def test_discovery_cannot_update_watchlist(self):
        with pytest.raises(RunModeViolation, match="cannot update"):
            assert_can_update_watchlist(RunMode.DISCOVERY)

    def test_backtest_cannot_update_watchlist(self):
        with pytest.raises(RunModeViolation):
            assert_can_update_watchlist(RunMode.BACKTEST)

    def test_weekly_review_cannot_update_watchlist(self):
        with pytest.raises(RunModeViolation):
            assert_can_update_watchlist(RunMode.WEEKLY_REVIEW)


# ---------------------------------------------------------------------------
# 10. assert_can_emit_recommendation
# ---------------------------------------------------------------------------

class TestAssertCanEmitRecommendation:
    def test_daily_can_emit(self):
        assert_can_emit_recommendation(RunMode.DAILY)

    def test_manual_update_can_emit(self):
        assert_can_emit_recommendation(RunMode.MANUAL_UPDATE)

    def test_weekly_review_can_emit(self):
        assert_can_emit_recommendation(RunMode.WEEKLY_REVIEW)

    def test_discovery_cannot_emit(self):
        with pytest.raises(RunModeViolation, match="cannot emit"):
            assert_can_emit_recommendation(RunMode.DISCOVERY)

    def test_backtest_cannot_emit(self):
        with pytest.raises(RunModeViolation, match="cannot emit"):
            assert_can_emit_recommendation(RunMode.BACKTEST)

    def test_historical_replay_cannot_emit(self):
        with pytest.raises(RunModeViolation, match="cannot emit"):
            assert_can_emit_recommendation(RunMode.HISTORICAL_REPLAY)


# ---------------------------------------------------------------------------
# 11. Discovery lane restrictions (consolidated)
# ---------------------------------------------------------------------------

class TestDiscoveryRestrictions:
    def test_cannot_write_latest(self):
        assert validate_output_write(RunMode.DISCOVERY, "latest") is False

    def test_cannot_write_policy(self):
        assert validate_output_write(RunMode.DISCOVERY, "policy") is False

    def test_cannot_write_portfolio(self):
        assert validate_output_write(RunMode.DISCOVERY, "portfolio") is False

    def test_cannot_write_user_state(self):
        assert validate_output_write(RunMode.DISCOVERY, "user") is False

    def test_cannot_write_historical(self):
        assert validate_output_write(RunMode.DISCOVERY, "historical") is False

    def test_can_write_sandbox(self):
        assert validate_output_write(RunMode.DISCOVERY, "sandbox") is True

    def test_cannot_update_official_watchlist(self):
        p = get_run_mode_policy(RunMode.DISCOVERY)
        assert p.can_update_official_watchlist is False

    def test_cannot_change_allocations(self):
        p = get_run_mode_policy(RunMode.DISCOVERY)
        assert p.can_change_allocations is False

    def test_cannot_emit_official_recommendations(self):
        with pytest.raises(RunModeViolation):
            assert_can_emit_recommendation(RunMode.DISCOVERY)

    def test_is_research_only(self):
        assert is_research_only_mode(RunMode.DISCOVERY) is True
        assert is_official_mode(RunMode.DISCOVERY) is False


# ---------------------------------------------------------------------------
# 12. MANUAL_UPDATE approval behavior
# ---------------------------------------------------------------------------

class TestManualUpdateApproval:
    def test_approved_portfolio_state_update(self):
        assert_can_update_portfolio_state(RunMode.MANUAL_UPDATE, approved=True)

    def test_unapproved_portfolio_state_blocked(self):
        with pytest.raises(RunModeViolation):
            assert_can_update_portfolio_state(RunMode.MANUAL_UPDATE, approved=False)

    def test_approved_watchlist_update(self):
        assert_can_update_watchlist(RunMode.MANUAL_UPDATE, approved=True)

    def test_unapproved_watchlist_blocked(self):
        with pytest.raises(RunModeViolation):
            assert_can_update_watchlist(RunMode.MANUAL_UPDATE, approved=False)

    def test_policy_requires_approval_flag(self):
        p = get_run_mode_policy(RunMode.MANUAL_UPDATE)
        assert p.requires_manual_approval is True

    def test_can_write_user_state(self):
        p = get_run_mode_policy(RunMode.MANUAL_UPDATE)
        assert p.can_write_user_state is True

    def test_can_change_risk_limits(self):
        p = get_run_mode_policy(RunMode.MANUAL_UPDATE)
        assert p.can_change_risk_limits is True

    def test_no_trade_execution(self):
        p = get_run_mode_policy(RunMode.MANUAL_UPDATE)
        assert p.can_execute_trades is False


# ---------------------------------------------------------------------------
# 13. BACKTEST restrictions
# ---------------------------------------------------------------------------

class TestBacktestRestrictions:
    def test_can_write_historical(self):
        assert validate_output_write(RunMode.BACKTEST, "historical") is True

    def test_can_write_backtest_subdir(self):
        assert validate_output_write(RunMode.BACKTEST, "backtest") is True

    def test_cannot_write_latest(self):
        assert validate_output_write(RunMode.BACKTEST, "latest") is False

    def test_cannot_write_policy(self):
        assert validate_output_write(RunMode.BACKTEST, "policy") is False

    def test_cannot_write_portfolio(self):
        assert validate_output_write(RunMode.BACKTEST, "portfolio") is False

    def test_cannot_emit_recommendations(self):
        with pytest.raises(RunModeViolation):
            assert_can_emit_recommendation(RunMode.BACKTEST)

    def test_is_research_only(self):
        assert is_research_only_mode(RunMode.BACKTEST) is True


# ---------------------------------------------------------------------------
# 14. HISTORICAL_REPLAY restrictions
# ---------------------------------------------------------------------------

class TestHistoricalReplayRestrictions:
    def test_can_write_historical(self):
        assert validate_output_write(RunMode.HISTORICAL_REPLAY, "historical") is True

    def test_can_write_backtest_subdir(self):
        assert validate_output_write(RunMode.HISTORICAL_REPLAY, "backtest") is True

    def test_cannot_write_latest(self):
        assert validate_output_write(RunMode.HISTORICAL_REPLAY, "latest") is False

    def test_cannot_write_policy(self):
        assert validate_output_write(RunMode.HISTORICAL_REPLAY, "policy") is False

    def test_cannot_write_sandbox(self):
        assert validate_output_write(RunMode.HISTORICAL_REPLAY, "sandbox") is False

    def test_cannot_emit_recommendations(self):
        with pytest.raises(RunModeViolation):
            assert_can_emit_recommendation(RunMode.HISTORICAL_REPLAY)

    def test_is_research_only(self):
        assert is_research_only_mode(RunMode.HISTORICAL_REPLAY) is True

    def test_no_sandbox_permission(self):
        p = get_run_mode_policy(RunMode.HISTORICAL_REPLAY)
        assert p.can_write_sandbox is False


# ---------------------------------------------------------------------------
# 15. DAILY mode remains compatible with existing pipeline
# ---------------------------------------------------------------------------

class TestDailyModeCompatibility:
    def test_can_write_latest(self):
        assert validate_output_write(RunMode.DAILY, "latest") is True

    def test_can_write_policy(self):
        assert validate_output_write(RunMode.DAILY, "policy") is True

    def test_can_write_portfolio(self):
        assert validate_output_write(RunMode.DAILY, "portfolio") is True

    def test_can_emit_recommendations(self):
        assert_can_emit_recommendation(RunMode.DAILY)  # no exception

    def test_is_official(self):
        assert is_official_mode(RunMode.DAILY) is True

    def test_no_auto_trading(self):
        p = get_run_mode_policy(RunMode.DAILY)
        assert p.can_execute_trades is False

    def test_no_manual_approval_required(self):
        p = get_run_mode_policy(RunMode.DAILY)
        assert p.requires_manual_approval is False

    def test_normalized_from_string(self):
        ctx = create_run_mode_context("daily")
        assert ctx.mode is RunMode.DAILY


# ---------------------------------------------------------------------------
# 16. RunModeContext
# ---------------------------------------------------------------------------

class TestRunModeContext:
    def test_create_from_string(self):
        ctx = create_run_mode_context("daily")
        assert isinstance(ctx, RunModeContext)
        assert ctx.mode is RunMode.DAILY
        assert isinstance(ctx.policy, RunModePolicy)

    def test_create_from_enum(self):
        ctx = create_run_mode_context(RunMode.DISCOVERY)
        assert ctx.mode is RunMode.DISCOVERY

    def test_approved_defaults_false(self):
        ctx = create_run_mode_context("daily")
        assert ctx.approved is False

    def test_approved_can_be_set_true(self):
        ctx = create_run_mode_context("manual_update", approved=True)
        assert ctx.approved is True

    def test_metadata_defaults_empty(self):
        ctx = create_run_mode_context("daily")
        assert ctx.metadata == {}

    def test_metadata_can_be_set(self):
        ctx = create_run_mode_context("daily", metadata={"run_id": "2026-04-30_daily"})
        assert ctx.metadata["run_id"] == "2026-04-30_daily"

    def test_unknown_mode_raises(self):
        with pytest.raises(RunModeViolation):
            create_run_mode_context("invalid_mode")

    def test_legacy_alias_in_context(self):
        ctx = create_run_mode_context("weekly")
        assert ctx.mode is RunMode.WEEKLY_REVIEW

    def test_policy_attached(self):
        ctx = create_run_mode_context("discovery")
        assert ctx.policy.can_write_sandbox is True
        assert ctx.policy.can_write_latest is False

    def test_context_mode_matches_policy_mode(self):
        for mode in RunMode:
            ctx = create_run_mode_context(mode)
            assert ctx.policy.mode is ctx.mode


# ---------------------------------------------------------------------------
# 17. No auto-trading — global invariant
# ---------------------------------------------------------------------------

class TestNoAutoTrading:
    def test_no_mode_can_execute_trades(self):
        for mode in RunMode:
            p = get_run_mode_policy(mode)
            assert p.can_execute_trades is False, (
                f"CRITICAL: {mode.value} has can_execute_trades=True — "
                "this system must never auto-trade"
            )
