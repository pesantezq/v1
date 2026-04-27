from __future__ import annotations

import json

import pytest

from watchlist_scanner.policy_simulation import (
    _OBSERVE_ONLY_NOTE,
    _add_deltas,
    _delta,
    _rank_policies,
    build_config_proposal,
    build_policy_simulation,
)
from watchlist_scanner.weight_tuning import CANDIDATE_WEIGHTS, CURRENT_WEIGHTS

_PRIMARY = 3
_RETURN_COL = f"outcome_return_{_PRIMARY}d"
_SUCCESS_COL = f"outcome_success_{_PRIMARY}d"
_DIRECTION_COL = f"direction_correct_{_PRIMARY}d"


def _make_row(
    *,
    aug: float = 0.7,
    conf: float = 0.8,
    theme: float = 0.5,
    fit: float = 0.6,
    ret: float | None = 2.0,
    success: int = 1,
    direction: int = 1,
) -> dict:
    row: dict = {
        "augmented_signal_score": aug,
        "confidence_score": conf,
        "theme_alignment_score": theme,
        "portfolio_fit_score": fit,
    }
    if ret is not None:
        row[_RETURN_COL] = ret
        row[_SUCCESS_COL] = success
        row[_DIRECTION_COL] = direction
    return row


def _make_rows(n: int, *, resolved: bool = True, ret: float = 2.0, success: int = 1) -> list[dict]:
    return [_make_row(ret=ret if resolved else None, success=success) for _ in range(n)]


def _fake_candidate(
    name: str,
    *,
    hit: float | None = 0.6,
    ret: float | None = 1.5,
    direction: float | None = 0.65,
    sample: int = 10,
    warning: bool = True,
) -> dict:
    return {
        "name": name,
        "weights": CURRENT_WEIGHTS,
        "top_quartile_hit_rate": hit,
        "top_quartile_avg_return": ret,
        "top_quartile_direction_correct_rate": direction,
        "sample_size": sample,
        "low_sample_warning": warning,
    }


# ---------------------------------------------------------------------------
# TestNoData
# ---------------------------------------------------------------------------

class TestNoData:
    def test_empty_rows_returns_safe_simulation(self):
        result = build_policy_simulation([])
        assert result["observe_only"] is True
        assert result["total_rows"] == 0
        assert result["resolved_rows"] == 0
        assert isinstance(result["all_policies"], list)
        assert len(result["all_policies"]) > 0

    def test_empty_rows_recommended_defaults_to_current(self):
        result = build_policy_simulation([])
        assert result["recommended_candidate"] == "current"

    def test_empty_rows_config_proposal_applied_false(self):
        simulation = build_policy_simulation([])
        proposal = build_config_proposal(simulation)
        assert proposal["applied"] is False
        assert proposal["proposal_status"] == "not_applied"

    def test_empty_rows_all_metrics_none(self):
        result = build_policy_simulation([])
        for p in result["all_policies"]:
            assert p["top_quartile_hit_rate"] is None
            assert p["top_quartile_avg_return"] is None

    def test_empty_rows_current_policy_present(self):
        result = build_policy_simulation([])
        assert result["current_policy"]["name"] == "current"


# ---------------------------------------------------------------------------
# TestCurrentVsRecommendedComparison
# ---------------------------------------------------------------------------

class TestCurrentVsRecommendedComparison:
    def test_current_policy_delta_vs_itself_is_zero(self):
        rows = _make_rows(80, resolved=True)
        result = build_policy_simulation(rows)
        cur = result["current_policy"]
        d = cur.get("delta_vs_current") or {}
        assert d.get("hit_rate") == pytest.approx(0.0)
        assert d.get("avg_return") == pytest.approx(0.0)
        assert d.get("direction_correct_rate") == pytest.approx(0.0)

    def test_recommended_policy_present_in_all_policies(self):
        rows = _make_rows(20, resolved=True)
        result = build_policy_simulation(rows)
        recommended_name = result["recommended_candidate"]
        names = {p["name"] for p in result["all_policies"]}
        assert recommended_name in names

    def test_recommended_policy_matches_recommended_name(self):
        rows = _make_rows(20, resolved=True)
        result = build_policy_simulation(rows)
        assert result["recommended_policy"]["name"] == result["recommended_candidate"]

    def test_wt_suggestions_recommended_name_carried_forward(self):
        rows = _make_rows(20, resolved=True)
        wt = {"recommended_candidate": "theme_heavy", "recommendation_reason": "test reason"}
        result = build_policy_simulation(rows, weight_tuning_suggestions=wt)
        assert result["recommended_candidate"] == "theme_heavy"

    def test_unknown_recommended_in_wt_falls_back(self):
        rows = _make_rows(20, resolved=True)
        wt = {"recommended_candidate": "nonexistent_blend"}
        result = build_policy_simulation(rows, weight_tuning_suggestions=wt)
        valid_names = {p["name"] for p in result["all_policies"]}
        assert result["recommended_candidate"] in valid_names

    def test_all_default_candidates_in_all_policies(self):
        result = build_policy_simulation([])
        names = {p["name"] for p in result["all_policies"]}
        expected = {c["name"] for c in CANDIDATE_WEIGHTS}
        assert names == expected


# ---------------------------------------------------------------------------
# TestDeltaCalculations
# ---------------------------------------------------------------------------

class TestDeltaCalculations:
    def test_delta_positive_when_policy_better(self):
        policy = _fake_candidate("x", hit=0.75, ret=3.0, direction=0.70)
        current = _fake_candidate("current", hit=0.60, ret=1.5, direction=0.60)
        result = _add_deltas(policy, current)
        d = result["delta_vs_current"]
        assert d["hit_rate"] == pytest.approx(0.15, abs=1e-4)
        assert d["avg_return"] == pytest.approx(1.5, abs=1e-4)
        assert d["direction_correct_rate"] == pytest.approx(0.10, abs=1e-4)

    def test_delta_negative_when_policy_worse(self):
        policy = _fake_candidate("x", hit=0.50, ret=0.5, direction=0.55)
        current = _fake_candidate("current", hit=0.65, ret=2.0, direction=0.70)
        result = _add_deltas(policy, current)
        d = result["delta_vs_current"]
        assert d["hit_rate"] < 0
        assert d["avg_return"] < 0
        assert d["direction_correct_rate"] < 0

    def test_delta_none_when_either_side_none(self):
        assert _delta(None, 0.5) is None
        assert _delta(0.5, None) is None
        assert _delta(None, None) is None

    def test_delta_zero_same_values(self):
        assert _delta(0.6, 0.6) == pytest.approx(0.0)

    def test_add_deltas_does_not_mutate_original(self):
        policy = _fake_candidate("x", hit=0.7)
        original_keys = set(policy.keys())
        _add_deltas(policy, _fake_candidate("current"))
        assert set(policy.keys()) == original_keys
        assert "delta_vs_current" not in policy

    def test_delta_rounded_to_4_places(self):
        result = _delta(0.6667, 0.3333)
        assert result == pytest.approx(0.3334, abs=1e-4)

    def test_all_policies_have_delta_vs_current(self):
        rows = _make_rows(20, resolved=True)
        result = build_policy_simulation(rows)
        for p in result["all_policies"]:
            assert "delta_vs_current" in p
            assert "hit_rate" in p["delta_vs_current"]
            assert "avg_return" in p["delta_vs_current"]
            assert "direction_correct_rate" in p["delta_vs_current"]


# ---------------------------------------------------------------------------
# TestRankPolicies
# ---------------------------------------------------------------------------

class TestRankPolicies:
    def test_sufficient_sample_ranked_above_thin(self):
        thin = _fake_candidate("thin", hit=0.90, sample=5, warning=True)
        sufficient = _fake_candidate("sufficient", hit=0.65, sample=25, warning=False)
        ranked = _rank_policies([thin, sufficient])
        ranks = {p["name"]: p["rank"] for p in ranked}
        assert ranks["sufficient"] < ranks["thin"]

    def test_higher_hit_rate_ranked_higher_within_group(self):
        a = _fake_candidate("a", hit=0.80, sample=25, warning=False)
        b = _fake_candidate("b", hit=0.65, sample=25, warning=False)
        ranked = _rank_policies([b, a])
        ranks = {p["name"]: p["rank"] for p in ranked}
        assert ranks["a"] < ranks["b"]

    def test_ties_broken_by_avg_return(self):
        a = _fake_candidate("a", hit=0.70, ret=1.0, sample=25, warning=False)
        b = _fake_candidate("b", hit=0.70, ret=3.0, sample=25, warning=False)
        ranked = _rank_policies([a, b])
        ranks = {p["name"]: p["rank"] for p in ranked}
        assert ranks["b"] < ranks["a"]

    def test_rank_starts_at_one(self):
        policies = [_fake_candidate(f"p{i}") for i in range(4)]
        ranked = _rank_policies(policies)
        assert min(p["rank"] for p in ranked) == 1

    def test_ranks_are_unique(self):
        policies = [_fake_candidate(f"p{i}") for i in range(6)]
        ranked = _rank_policies(policies)
        rank_values = [p["rank"] for p in ranked]
        assert len(rank_values) == len(set(rank_values))

    def test_rank_does_not_mutate_original(self):
        p = _fake_candidate("x")
        original_keys = set(p.keys())
        _rank_policies([p])
        assert set(p.keys()) == original_keys


# ---------------------------------------------------------------------------
# TestConfigProposalShape
# ---------------------------------------------------------------------------

class TestConfigProposalShape:
    def test_applied_always_false(self):
        sim = build_policy_simulation([])
        proposal = build_config_proposal(sim)
        assert proposal["applied"] is False

    def test_proposal_status_is_not_applied(self):
        sim = build_policy_simulation([])
        proposal = build_config_proposal(sim)
        assert proposal["proposal_status"] == "not_applied"

    def test_observe_only_true(self):
        sim = build_policy_simulation([])
        proposal = build_config_proposal(sim)
        assert proposal["observe_only"] is True

    def test_required_keys_present(self):
        sim = build_policy_simulation([])
        proposal = build_config_proposal(sim)
        required = {
            "generated_at", "observe_only", "applied", "proposal_status",
            "source", "recommended_candidate", "recommendation_reason",
            "proposed_weights", "current_weights", "weight_deltas",
            "performance_delta", "advisory_note",
        }
        assert required.issubset(proposal.keys())

    def test_performance_delta_keys_present(self):
        sim = build_policy_simulation([])
        proposal = build_config_proposal(sim)
        pd = proposal["performance_delta"]
        assert "hit_rate_delta" in pd
        assert "avg_return_delta" in pd
        assert "direction_correct_rate_delta" in pd

    def test_weight_delta_keys_match_current_weights(self):
        sim = build_policy_simulation([])
        proposal = build_config_proposal(sim)
        assert set(proposal["weight_deltas"].keys()) == set(CURRENT_WEIGHTS.keys())

    def test_advisory_note_in_proposal(self):
        sim = build_policy_simulation([])
        proposal = build_config_proposal(sim)
        assert _OBSERVE_ONLY_NOTE in proposal["advisory_note"]

    def test_source_is_policy_simulation(self):
        sim = build_policy_simulation([])
        proposal = build_config_proposal(sim)
        assert proposal["source"] == "policy_simulation"

    def test_recommendation_reason_from_wt_suggestions(self):
        sim = build_policy_simulation([])
        wt = {"recommended_candidate": "current", "recommendation_reason": "test reason xyz"}
        proposal = build_config_proposal(sim, weight_tuning_suggestions=wt)
        assert "test reason xyz" in proposal["recommendation_reason"]

    def test_proposal_is_json_serializable(self):
        sim = build_policy_simulation(_make_rows(20, resolved=True))
        proposal = build_config_proposal(sim)
        parsed = json.loads(json.dumps(proposal))
        assert parsed["applied"] is False

    def test_current_weights_is_copy_of_constant(self):
        sim = build_policy_simulation([])
        proposal = build_config_proposal(sim)
        # Mutating the returned current_weights must not alter CURRENT_WEIGHTS
        proposal["current_weights"]["augmented_signal_score"] = 99.9
        assert CURRENT_WEIGHTS["augmented_signal_score"] != 99.9


# ---------------------------------------------------------------------------
# TestNoLiveConfigMutation
# ---------------------------------------------------------------------------

class TestNoLiveConfigMutation:
    def test_proposed_weights_is_independent_copy(self):
        sim = build_policy_simulation([])
        proposal = build_config_proposal(sim)
        proposal["proposed_weights"]["augmented_signal_score"] = 99.9
        assert CURRENT_WEIGHTS["augmented_signal_score"] != 99.9

    def test_build_config_proposal_returns_dict_not_path(self):
        sim = build_policy_simulation([])
        result = build_config_proposal(sim)
        assert isinstance(result, dict)

    def test_build_policy_simulation_returns_dict_not_path(self):
        result = build_policy_simulation([])
        assert isinstance(result, dict)

    def test_no_config_path_in_proposal_keys(self):
        sim = build_policy_simulation([])
        proposal = build_config_proposal(sim)
        # Ensure no key points at a live config target
        for key in proposal:
            assert "config.json" not in str(proposal.get(key, ""))

    def test_simulation_observe_only_flag_cannot_be_unset(self):
        sim = build_policy_simulation([])
        assert sim["observe_only"] is True
        # Even with a mutation attempt on the returned dict, the constant is unaffected
        sim["observe_only"] = False
        new_sim = build_policy_simulation([])
        assert new_sim["observe_only"] is True
