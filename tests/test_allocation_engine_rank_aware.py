"""
Tests for rank-aware advisory policy integration in allocation_engine.suggest_allocation.

Covers:
  - Default behavior (no policy) — source=default, multiplier=1.0, pcts equal
  - Invalid policy (various _valid: False cases) — falls back to default
  - None policy — falls back to default
  - Valid policy + opportunity with final_rank_score — rank-aware enrichment
  - Strong / Good / Neutral / Poor score tiers
  - Baseline suggested_pct is never changed by policy
  - rank_aware_suggested_pct capped by max_position_cap
  - opportunity without final_rank_score — graceful fallback
  - to_dict() includes all new advisory fields
  - No mutation of approved_policy dict or opportunity dict
  - No alert-gating changes (suggested_pct unchanged)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from allocation_engine import suggest_allocation, DEFAULT_CONFIG


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _opportunity(**overrides) -> dict:
    base = {
        "symbol": "AAPL",
        "confidence": 0.80,
        "score": 82.0,
        "sector": "Technology",
    }
    base.update(overrides)
    return base


def _approved_policy(**overrides) -> dict:
    base = {
        "_valid": True,
        "activation_status": "approved_not_live",
        "applied_to_live": False,
        "sample_size": 42,
        "rank_aware": {"capital_efficiency": 0.15},
        "baseline": {"capital_efficiency": 0.12},
        "delta": {"efficiency_delta": 0.03},
    }
    base.update(overrides)
    return base


def _suggest(opportunity=None, policy=None, **kwargs) -> object:
    opp = opportunity if opportunity is not None else _opportunity()
    return suggest_allocation(
        opportunity=opp,
        strategy_type="compounder",
        portfolio_value=100_000.0,
        cash_available=20_000.0,
        approved_policy=policy,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# TestDefaultSourceNoPolicy
# ---------------------------------------------------------------------------

class TestDefaultSourceNoPolicy:
    def test_no_policy_source_is_default(self):
        s = _suggest(policy=None)
        assert s.allocation_policy_source == "default"

    def test_no_policy_multiplier_is_one(self):
        s = _suggest(policy=None)
        assert s.rank_multiplier == pytest.approx(1.0)

    def test_no_policy_rank_aware_equals_baseline(self):
        s = _suggest(policy=None)
        assert s.rank_aware_suggested_pct == pytest.approx(s.baseline_suggested_pct)

    def test_no_policy_baseline_equals_suggested_pct(self):
        s = _suggest(policy=None)
        assert s.baseline_suggested_pct == pytest.approx(s.suggested_pct)

    def test_no_policy_candidate_is_rank_aware(self):
        s = _suggest(policy=None)
        assert s.allocation_policy_candidate == "rank_aware"

    def test_no_policy_reason_indicates_inactive(self):
        s = _suggest(policy=None)
        assert "not active" in s.allocation_policy_reason


# ---------------------------------------------------------------------------
# TestInvalidPolicyFallsBackToDefault
# ---------------------------------------------------------------------------

class TestInvalidPolicyFallsBackToDefault:
    def test_valid_false_source_is_default(self):
        policy = _approved_policy(**{"_valid": False, "reason": "test"})
        s = _suggest(policy=policy)
        assert s.allocation_policy_source == "default"

    def test_valid_false_multiplier_is_one(self):
        policy = _approved_policy(**{"_valid": False, "reason": "test"})
        s = _suggest(policy=policy)
        assert s.rank_multiplier == pytest.approx(1.0)

    def test_valid_missing_key_source_is_default(self):
        policy = {"activation_status": "approved_not_live"}
        s = _suggest(policy=policy)
        assert s.allocation_policy_source == "default"

    def test_none_policy_source_is_default(self):
        s = _suggest(policy=None)
        assert s.allocation_policy_source == "default"


# ---------------------------------------------------------------------------
# TestApprovedPolicyRankScoreTiers
# ---------------------------------------------------------------------------

class TestApprovedPolicyRankScoreTiers:
    def test_strong_score_multiplier_1_25(self):
        opp = _opportunity(final_rank_score=0.80)
        s = _suggest(opportunity=opp, policy=_approved_policy())
        assert s.rank_multiplier == pytest.approx(1.25)
        assert s.allocation_policy_source == "approved_rank_aware"

    def test_good_score_multiplier_1_10(self):
        opp = _opportunity(final_rank_score=0.60)
        s = _suggest(opportunity=opp, policy=_approved_policy())
        assert s.rank_multiplier == pytest.approx(1.10)
        assert s.allocation_policy_source == "approved_rank_aware"

    def test_neutral_score_multiplier_1_00(self):
        opp = _opportunity(final_rank_score=0.40)
        s = _suggest(opportunity=opp, policy=_approved_policy())
        assert s.rank_multiplier == pytest.approx(1.00)
        assert s.allocation_policy_source == "approved_rank_aware"

    def test_poor_score_multiplier_0_75(self):
        opp = _opportunity(final_rank_score=0.20)
        s = _suggest(opportunity=opp, policy=_approved_policy())
        assert s.rank_multiplier == pytest.approx(0.75)
        assert s.allocation_policy_source == "approved_rank_aware"

    def test_boundary_strong_0_75(self):
        opp = _opportunity(final_rank_score=0.75)
        s = _suggest(opportunity=opp, policy=_approved_policy())
        assert s.rank_multiplier == pytest.approx(1.25)

    def test_boundary_good_0_55(self):
        opp = _opportunity(final_rank_score=0.55)
        s = _suggest(opportunity=opp, policy=_approved_policy())
        assert s.rank_multiplier == pytest.approx(1.10)

    def test_boundary_neutral_0_35(self):
        opp = _opportunity(final_rank_score=0.35)
        s = _suggest(opportunity=opp, policy=_approved_policy())
        assert s.rank_multiplier == pytest.approx(1.00)


# ---------------------------------------------------------------------------
# TestRankAwareSuggestedPct
# ---------------------------------------------------------------------------

class TestRankAwareSuggestedPct:
    def test_strong_score_increases_rank_aware_pct(self):
        opp = _opportunity(final_rank_score=0.80)
        s = _suggest(opportunity=opp, policy=_approved_policy())
        assert s.rank_aware_suggested_pct > s.baseline_suggested_pct

    def test_poor_score_decreases_rank_aware_pct(self):
        opp = _opportunity(final_rank_score=0.20)
        s = _suggest(opportunity=opp, policy=_approved_policy())
        assert s.rank_aware_suggested_pct < s.baseline_suggested_pct

    def test_neutral_score_rank_aware_equals_baseline(self):
        opp = _opportunity(final_rank_score=0.40)
        s = _suggest(opportunity=opp, policy=_approved_policy())
        assert s.rank_aware_suggested_pct == pytest.approx(s.baseline_suggested_pct)

    def test_rank_aware_pct_capped_by_max_position_cap(self):
        # Set a tiny max_position_cap so the rank multiplier would exceed it
        opp = _opportunity(final_rank_score=0.90)
        s = _suggest(
            opportunity=opp,
            policy=_approved_policy(),
            config={"compounder_base_pct": 0.06, "max_position_cap": 0.07},
        )
        assert s.rank_aware_suggested_pct <= 0.07

    def test_rank_aware_pct_formula_strong(self):
        # base=0.05, multiplier=1.25, no cap → 0.0625
        opp = _opportunity(final_rank_score=0.80, confidence=0.80)
        s = _suggest(opportunity=opp, policy=_approved_policy())
        expected = min(s.baseline_suggested_pct * 1.25, DEFAULT_CONFIG["max_position_cap"])
        assert s.rank_aware_suggested_pct == pytest.approx(expected, abs=1e-4)

    def test_rank_aware_pct_formula_poor(self):
        # base=0.05, multiplier=0.75 → 0.0375
        opp = _opportunity(final_rank_score=0.20, confidence=0.80)
        s = _suggest(opportunity=opp, policy=_approved_policy())
        expected = min(s.baseline_suggested_pct * 0.75, DEFAULT_CONFIG["max_position_cap"])
        assert s.rank_aware_suggested_pct == pytest.approx(expected, abs=1e-4)


# ---------------------------------------------------------------------------
# TestBaselineSizingPreserved
# ---------------------------------------------------------------------------

class TestBaselineSizingPreserved:
    def test_suggested_pct_unchanged_with_strong_score(self):
        opp_no_policy = _opportunity(final_rank_score=0.80)
        opp_with_policy = _opportunity(final_rank_score=0.80)
        s_no = _suggest(opportunity=opp_no_policy, policy=None)
        s_with = _suggest(opportunity=opp_with_policy, policy=_approved_policy())
        assert s_with.suggested_pct == pytest.approx(s_no.suggested_pct)

    def test_suggested_pct_unchanged_with_poor_score(self):
        opp_no_policy = _opportunity(final_rank_score=0.20)
        opp_with_policy = _opportunity(final_rank_score=0.20)
        s_no = _suggest(opportunity=opp_no_policy, policy=None)
        s_with = _suggest(opportunity=opp_with_policy, policy=_approved_policy())
        assert s_with.suggested_pct == pytest.approx(s_no.suggested_pct)

    def test_suggested_amount_unchanged_with_policy(self):
        opp_no_policy = _opportunity(final_rank_score=0.80)
        opp_with_policy = _opportunity(final_rank_score=0.80)
        s_no = _suggest(opportunity=opp_no_policy, policy=None)
        s_with = _suggest(opportunity=opp_with_policy, policy=_approved_policy())
        assert s_with.suggested_amount == pytest.approx(s_no.suggested_amount)

    def test_baseline_suggested_pct_equals_suggested_pct(self):
        opp = _opportunity(final_rank_score=0.80)
        s = _suggest(opportunity=opp, policy=_approved_policy())
        assert s.baseline_suggested_pct == pytest.approx(s.suggested_pct)

    def test_capped_by_unchanged_with_policy(self):
        opp_no_policy = _opportunity(final_rank_score=0.80)
        opp_with_policy = _opportunity(final_rank_score=0.80)
        s_no = _suggest(opportunity=opp_no_policy, policy=None)
        s_with = _suggest(opportunity=opp_with_policy, policy=_approved_policy())
        assert s_with.capped_by == s_no.capped_by


# ---------------------------------------------------------------------------
# TestNoRankScoreGracefulFallback
# ---------------------------------------------------------------------------

class TestNoRankScoreGracefulFallback:
    def test_no_rank_score_source_not_rank_aware(self):
        opp = _opportunity()  # no final_rank_score key
        s = _suggest(opportunity=opp, policy=_approved_policy())
        assert s.allocation_policy_source != "approved_rank_aware"

    def test_no_rank_score_multiplier_is_one(self):
        opp = _opportunity()
        s = _suggest(opportunity=opp, policy=_approved_policy())
        assert s.rank_multiplier == pytest.approx(1.0)

    def test_no_rank_score_rank_aware_equals_baseline(self):
        opp = _opportunity()
        s = _suggest(opportunity=opp, policy=_approved_policy())
        assert s.rank_aware_suggested_pct == pytest.approx(s.baseline_suggested_pct)

    def test_no_rank_score_reason_explains(self):
        opp = _opportunity()
        s = _suggest(opportunity=opp, policy=_approved_policy())
        assert "final_rank_score" in s.allocation_policy_reason

    def test_none_rank_score_fallback(self):
        opp = _opportunity(final_rank_score=None)
        s = _suggest(opportunity=opp, policy=_approved_policy())
        assert s.allocation_policy_source != "approved_rank_aware"


# ---------------------------------------------------------------------------
# TestAllocationPolicySourceLabel
# ---------------------------------------------------------------------------

class TestAllocationPolicySourceLabel:
    def test_approved_policy_label_is_approved_rank_aware(self):
        opp = _opportunity(final_rank_score=0.70)
        s = _suggest(opportunity=opp, policy=_approved_policy())
        assert s.allocation_policy_source == "approved_rank_aware"

    def test_no_policy_label_is_default(self):
        s = _suggest(policy=None)
        assert s.allocation_policy_source == "default"

    def test_invalid_policy_label_is_default(self):
        s = _suggest(policy={"_valid": False})
        assert s.allocation_policy_source == "default"


# ---------------------------------------------------------------------------
# TestToDict
# ---------------------------------------------------------------------------

class TestToDict:
    def test_to_dict_includes_policy_source(self):
        s = _suggest(policy=None)
        d = s.to_dict()
        assert "allocation_policy_source" in d

    def test_to_dict_includes_candidate(self):
        s = _suggest(policy=None)
        d = s.to_dict()
        assert d["allocation_policy_candidate"] == "rank_aware"

    def test_to_dict_includes_rank_multiplier(self):
        opp = _opportunity(final_rank_score=0.80)
        s = _suggest(opportunity=opp, policy=_approved_policy())
        d = s.to_dict()
        assert "rank_multiplier" in d
        assert d["rank_multiplier"] == pytest.approx(1.25)

    def test_to_dict_includes_baseline_suggested_pct(self):
        s = _suggest(policy=None)
        d = s.to_dict()
        assert "baseline_suggested_pct" in d

    def test_to_dict_includes_rank_aware_suggested_pct(self):
        opp = _opportunity(final_rank_score=0.80)
        s = _suggest(opportunity=opp, policy=_approved_policy())
        d = s.to_dict()
        assert "rank_aware_suggested_pct" in d

    def test_to_dict_includes_allocation_policy_reason(self):
        s = _suggest(policy=None)
        d = s.to_dict()
        assert "allocation_policy_reason" in d

    def test_to_dict_preserves_all_original_fields(self):
        s = _suggest(policy=None)
        d = s.to_dict()
        for key in ("symbol", "strategy_type", "confidence", "suggested_pct",
                    "suggested_amount", "deployable_cash", "capped_by", "rationale"):
            assert key in d

    def test_to_dict_rank_aware_pct_rounded(self):
        opp = _opportunity(final_rank_score=0.80)
        s = _suggest(opportunity=opp, policy=_approved_policy())
        d = s.to_dict()
        # should be 4 decimal places
        val = d["rank_aware_suggested_pct"]
        assert val == round(val, 4)


# ---------------------------------------------------------------------------
# TestNoMutation
# ---------------------------------------------------------------------------

class TestNoMutation:
    def test_approved_policy_dict_not_mutated(self):
        policy = _approved_policy()
        original_keys = set(policy.keys())
        original_rank_aware = dict(policy["rank_aware"])
        opp = _opportunity(final_rank_score=0.80)
        _suggest(opportunity=opp, policy=policy)
        assert set(policy.keys()) == original_keys
        assert policy["rank_aware"] == original_rank_aware

    def test_opportunity_dict_not_mutated(self):
        opp = _opportunity(final_rank_score=0.80)
        original_keys = set(opp.keys())
        _suggest(opportunity=opp, policy=_approved_policy())
        assert set(opp.keys()) == original_keys

    def test_default_config_not_mutated(self):
        original = dict(DEFAULT_CONFIG)
        _suggest(policy=_approved_policy(), opportunity=_opportunity(final_rank_score=0.80))
        assert DEFAULT_CONFIG == original


# ---------------------------------------------------------------------------
# TestAlertGatingUnchanged
# ---------------------------------------------------------------------------

class TestAlertGatingUnchanged:
    def test_rationale_unchanged_with_policy(self):
        opp_no_policy = _opportunity(final_rank_score=0.80, confidence=0.80)
        opp_with_policy = _opportunity(final_rank_score=0.80, confidence=0.80)
        s_no = _suggest(opportunity=opp_no_policy, policy=None)
        s_with = _suggest(opportunity=opp_with_policy, policy=_approved_policy())
        assert s_with.rationale == s_no.rationale

    def test_deployable_cash_unchanged_with_policy(self):
        opp_no_policy = _opportunity(final_rank_score=0.80)
        opp_with_policy = _opportunity(final_rank_score=0.80)
        s_no = _suggest(opportunity=opp_no_policy, policy=None)
        s_with = _suggest(opportunity=opp_with_policy, policy=_approved_policy())
        assert s_with.deployable_cash == pytest.approx(s_no.deployable_cash)

    def test_strategy_type_unchanged_with_policy(self):
        opp = _opportunity(final_rank_score=0.80)
        s = _suggest(opportunity=opp, policy=_approved_policy())
        assert s.strategy_type == "compounder"

    def test_confidence_unchanged_with_policy(self):
        opp_no_policy = _opportunity(final_rank_score=0.80, confidence=0.70)
        opp_with_policy = _opportunity(final_rank_score=0.80, confidence=0.70)
        s_no = _suggest(opportunity=opp_no_policy, policy=None)
        s_with = _suggest(opportunity=opp_with_policy, policy=_approved_policy())
        assert s_with.confidence == pytest.approx(s_no.confidence)


# ---------------------------------------------------------------------------
# TestPolicyReasonContent
# ---------------------------------------------------------------------------

class TestPolicyReasonContent:
    def test_approved_reason_mentions_rank_label(self):
        opp = _opportunity(final_rank_score=0.80)
        s = _suggest(opportunity=opp, policy=_approved_policy())
        assert "strong" in s.allocation_policy_reason.lower()

    def test_approved_reason_mentions_advisory(self):
        opp = _opportunity(final_rank_score=0.80)
        s = _suggest(opportunity=opp, policy=_approved_policy())
        assert "advisory" in s.allocation_policy_reason.lower()

    def test_approved_reason_mentions_multiplier(self):
        opp = _opportunity(final_rank_score=0.80)
        s = _suggest(opportunity=opp, policy=_approved_policy())
        assert "1.25" in s.allocation_policy_reason

    def test_default_reason_indicates_not_active(self):
        s = _suggest(policy=None)
        assert "not active" in s.allocation_policy_reason
