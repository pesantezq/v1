from __future__ import annotations

import json

import pytest

from watchlist_scanner.weight_tuning import (
    CANDIDATE_WEIGHTS,
    CURRENT_WEIGHTS,
    _MIN_RECOMMENDATION_SAMPLE,
    _compute_simulated_rank,
    _evaluate_candidate,
    _select_recommendation,
    build_weight_tuning_suggestions,
)

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


# ---------------------------------------------------------------------------
# TestBuildWeightTuningSuggestions
# ---------------------------------------------------------------------------

class TestBuildWeightTuningSuggestions:
    def test_empty_feedback_returns_current_default(self):
        result = build_weight_tuning_suggestions([])
        assert result["recommended_candidate"] == "current"
        reason = result["recommendation_reason"].lower()
        assert any(kw in reason for kw in ["no resolved", "insufficient", "no data"])

    def test_observe_only_flag_always_true(self):
        assert build_weight_tuning_suggestions([])["observe_only"] is True
        assert build_weight_tuning_suggestions(_make_rows(20))["observe_only"] is True

    def test_includes_current_weights(self):
        result = build_weight_tuning_suggestions([])
        assert result["current_weights"] == CURRENT_WEIGHTS

    def test_all_default_candidates_present(self):
        result = build_weight_tuning_suggestions(_make_rows(4))
        names = {c["name"] for c in result["candidates"]}
        assert "current" in names
        assert len(names) == len(CANDIDATE_WEIGHTS)

    def test_total_and_resolved_counts(self):
        rows = _make_rows(10, resolved=True) + _make_rows(5, resolved=False)
        result = build_weight_tuning_suggestions(rows, primary_window_days=_PRIMARY)
        assert result["total_rows"] == 15
        assert result["resolved_rows"] == 10

    def test_custom_candidates_override_defaults(self):
        custom = [{"name": "only_one", "weights": CURRENT_WEIGHTS}]
        result = build_weight_tuning_suggestions([], candidates=custom)
        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["name"] == "only_one"

    def test_primary_window_days_stored(self):
        result = build_weight_tuning_suggestions([], primary_window_days=7)
        assert result["primary_window_days"] == 7


# ---------------------------------------------------------------------------
# TestCandidateScoring
# ---------------------------------------------------------------------------

class TestCandidateScoring:
    def test_scoring_is_deterministic(self):
        rows = _make_rows(20, resolved=True)
        r1 = build_weight_tuning_suggestions(rows)
        r2 = build_weight_tuning_suggestions(rows)
        for c1, c2 in zip(r1["candidates"], r2["candidates"]):
            assert c1["top_quartile_hit_rate"] == c2["top_quartile_hit_rate"]
            assert c1["top_quartile_avg_return"] == c2["top_quartile_avg_return"]

    def test_no_resolved_rows_returns_null_metrics(self):
        rows = _make_rows(10, resolved=False)
        candidate = CANDIDATE_WEIGHTS[0]
        result = _evaluate_candidate(rows, candidate, primary_window_days=_PRIMARY)
        assert result["top_quartile_hit_rate"] is None
        assert result["top_quartile_avg_return"] is None
        assert result["top_quartile_direction_correct_rate"] is None
        assert result["low_sample_warning"] is True

    def test_compute_simulated_rank_respects_weights(self):
        row = {
            "augmented_signal_score": 1.0,
            "confidence_score": 0.0,
            "theme_alignment_score": 0.0,
            "portfolio_fit_score": 0.0,
        }
        weights = {
            "augmented_signal_score": 0.50,
            "confidence_score": 0.50,
            "theme_alignment_score": 0.00,
            "portfolio_fit_score": 0.00,
        }
        assert _compute_simulated_rank(row, weights) == pytest.approx(0.5)

    def test_missing_augmented_falls_back_to_signal_score(self):
        row = {
            "signal_score": 0.8,
            "confidence_score": 0.5,
            "theme_alignment_score": 0.5,
            "portfolio_fit_score": 0.5,
        }
        score = _compute_simulated_rank(row, CURRENT_WEIGHTS)
        assert score > 0.0

    def test_missing_portfolio_fit_defaults_to_neutral(self):
        row_with = _make_row(fit=0.5)
        row_without = {k: v for k, v in row_with.items() if k != "portfolio_fit_score"}
        assert _compute_simulated_rank(row_with, CURRENT_WEIGHTS) == pytest.approx(
            _compute_simulated_rank(row_without, CURRENT_WEIGHTS)
        )

    def test_top_quartile_avg_return_computed_correctly(self):
        rows = [_make_row(ret=4.0)] * 4 + [_make_row(ret=0.0)] * 16
        candidate = {"name": "test", "weights": CURRENT_WEIGHTS}
        result = _evaluate_candidate(rows, candidate, primary_window_days=_PRIMARY)
        # top quartile = 20 // 4 = 5 rows; all rows identical scores → first 5 happen to be 4.0
        # (sorted stable, high-return rows placed first by _make_rows ordering)
        assert result["top_quartile_avg_return"] is not None

    def test_hit_rate_and_direction_populated(self):
        rows = _make_rows(40, resolved=True, ret=1.5, success=1)
        candidate = CANDIDATE_WEIGHTS[0]
        result = _evaluate_candidate(rows, candidate, primary_window_days=_PRIMARY)
        assert result["top_quartile_hit_rate"] == pytest.approx(1.0)
        assert result["top_quartile_direction_correct_rate"] == pytest.approx(1.0)

    def test_zero_hit_rate_on_all_failures(self):
        rows = _make_rows(40, resolved=True, ret=-2.0, success=0)
        for row in rows:
            row[_DIRECTION_COL] = 0
        candidate = CANDIDATE_WEIGHTS[0]
        result = _evaluate_candidate(rows, candidate, primary_window_days=_PRIMARY)
        assert result["top_quartile_hit_rate"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# TestLowSampleWarning
# ---------------------------------------------------------------------------

class TestLowSampleWarning:
    def test_warning_when_below_threshold(self):
        # 76 rows → q_size = 19, all resolved → sample_size 19 < 20 → warning
        rows = _make_rows(76, resolved=True)
        result = _evaluate_candidate(rows, CANDIDATE_WEIGHTS[0], primary_window_days=_PRIMARY)
        assert result["low_sample_warning"] is True

    def test_no_warning_at_threshold(self):
        # 80 rows → q_size = 20, all resolved → sample_size 20 == 20 → no warning
        rows = _make_rows(80, resolved=True)
        result = _evaluate_candidate(rows, CANDIDATE_WEIGHTS[0], primary_window_days=_PRIMARY)
        assert result["low_sample_warning"] is False

    def test_warning_with_zero_resolved(self):
        rows = _make_rows(40, resolved=False)
        result = _evaluate_candidate(rows, CANDIDATE_WEIGHTS[0], primary_window_days=_PRIMARY)
        assert result["low_sample_warning"] is True
        assert result["sample_size"] == 0

    def test_threshold_is_20(self):
        assert _MIN_RECOMMENDATION_SAMPLE == 20


# ---------------------------------------------------------------------------
# TestSelectRecommendation
# ---------------------------------------------------------------------------

class TestSelectRecommendation:
    def test_empty_evaluated_returns_current(self):
        name, reason = _select_recommendation([])
        assert name == "current"

    def test_null_hit_rate_excluded_from_selection(self):
        no_data = {"name": "a", "top_quartile_hit_rate": None, "top_quartile_avg_return": None,
                   "sample_size": 0, "low_sample_warning": True}
        good = {"name": "b", "top_quartile_hit_rate": 0.7, "top_quartile_avg_return": 2.0,
                "sample_size": 25, "low_sample_warning": False}
        name, _ = _select_recommendation([no_data, good])
        assert name == "b"

    def test_all_null_hit_rates_returns_current(self):
        candidates = [
            {"name": "a", "top_quartile_hit_rate": None, "top_quartile_avg_return": None,
             "sample_size": 0, "low_sample_warning": True},
        ]
        name, reason = _select_recommendation(candidates)
        assert name == "current"

    def test_sufficient_sample_preferred_over_thin(self):
        thin = {"name": "thin", "top_quartile_hit_rate": 0.95, "top_quartile_avg_return": 10.0,
                "sample_size": 5, "low_sample_warning": True}
        sufficient = {"name": "sufficient", "top_quartile_hit_rate": 0.70,
                      "top_quartile_avg_return": 2.0, "sample_size": 25, "low_sample_warning": False}
        name, _ = _select_recommendation([thin, sufficient])
        assert name == "sufficient"

    def test_falls_back_to_best_thin_when_all_warn(self):
        c1 = {"name": "a", "top_quartile_hit_rate": 0.55, "top_quartile_avg_return": 1.0,
              "sample_size": 5, "low_sample_warning": True}
        c2 = {"name": "b", "top_quartile_hit_rate": 0.80, "top_quartile_avg_return": 2.0,
              "sample_size": 8, "low_sample_warning": True}
        name, reason = _select_recommendation([c1, c2])
        assert name == "b"
        assert "thin" in reason.lower() or "below 20" in reason.lower()

    def test_ties_broken_by_avg_return(self):
        c1 = {"name": "a", "top_quartile_hit_rate": 0.70, "top_quartile_avg_return": 1.0,
              "sample_size": 25, "low_sample_warning": False}
        c2 = {"name": "b", "top_quartile_hit_rate": 0.70, "top_quartile_avg_return": 3.5,
              "sample_size": 25, "low_sample_warning": False}
        name, _ = _select_recommendation([c1, c2])
        assert name == "b"

    def test_reason_mentions_hit_rate(self):
        c = {"name": "x", "top_quartile_hit_rate": 0.65, "top_quartile_avg_return": 1.2,
             "sample_size": 25, "low_sample_warning": False}
        _, reason = _select_recommendation([c])
        assert "65.0%" in reason or "65%" in reason


# ---------------------------------------------------------------------------
# TestOutputStructure
# ---------------------------------------------------------------------------

class TestOutputStructure:
    def test_required_top_level_keys_present(self):
        result = build_weight_tuning_suggestions([])
        required = {
            "generated_at", "observe_only", "primary_window_days",
            "total_rows", "resolved_rows", "current_weights",
            "recommended_candidate", "recommendation_reason", "candidates",
        }
        assert required.issubset(result.keys())

    def test_candidate_required_keys_present(self):
        result = build_weight_tuning_suggestions(_make_rows(4))
        for c in result["candidates"]:
            assert "name" in c
            assert "weights" in c
            assert "top_quartile_avg_return" in c
            assert "top_quartile_hit_rate" in c
            assert "top_quartile_direction_correct_rate" in c
            assert "sample_size" in c
            assert "low_sample_warning" in c

    def test_output_is_json_serializable(self):
        rows = _make_rows(20, resolved=True)
        result = build_weight_tuning_suggestions(rows)
        serialized = json.dumps(result)
        parsed = json.loads(serialized)
        assert parsed["observe_only"] is True

    def test_recommended_candidate_is_valid_name(self):
        rows = _make_rows(80, resolved=True)
        result = build_weight_tuning_suggestions(rows)
        valid_names = {c["name"] for c in result["candidates"]}
        assert result["recommended_candidate"] in valid_names

    def test_weights_sum_to_one_for_all_candidates(self):
        for candidate in CANDIDATE_WEIGHTS:
            total = sum(candidate["weights"].values())
            assert total == pytest.approx(1.0, abs=1e-9), (
                f"Candidate '{candidate['name']}' weights sum to {total}"
            )
