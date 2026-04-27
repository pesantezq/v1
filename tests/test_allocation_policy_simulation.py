from __future__ import annotations

import json
from pathlib import Path

import pytest

from watchlist_scanner.allocation_policy_simulation import (
    _compute_preview_size,
    _is_win,
    _load_preview_opportunities,
    build_allocation_policy_simulation,
    generate_allocation_policy_simulation_report,
)
from watchlist_scanner.allocation_preview import (
    MULTIPLIER_GOOD,
    MULTIPLIER_NEUTRAL,
    MULTIPLIER_POOR,
    MULTIPLIER_STRONG,
    RANK_GOOD,
    RANK_NEUTRAL,
    RANK_STRONG,
    _DEFAULT_BASELINE_PCT,
    _DEFAULT_MAX_TICKER_PCT,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row(
    ticker="AAPL",
    outcome_return_3d=2.0,
    normalized_allocation=0.03,
    final_rank_score=0.80,
    **kwargs,
) -> dict:
    base = {
        "ticker": ticker,
        "outcome_return_3d": outcome_return_3d,
        "normalized_allocation": normalized_allocation,
        "final_rank_score": final_rank_score,
    }
    base.update(kwargs)
    return base


def _preview_opp(ticker="AAPL", preview_size=0.025, rank_label="good", rank_multiplier=1.10) -> dict:
    return {
        "ticker": ticker,
        "preview_size": preview_size,
        "rank_label": rank_label,
        "rank_multiplier": rank_multiplier,
    }


# ---------------------------------------------------------------------------
# TestIsWin
# ---------------------------------------------------------------------------

class TestIsWin:
    def test_positive_return_is_win(self):
        assert _is_win(1.0) is True

    def test_zero_return_is_not_win(self):
        assert _is_win(0.0) is False

    def test_negative_return_is_not_win(self):
        assert _is_win(-0.5) is False

    def test_tiny_positive_is_win(self):
        assert _is_win(0.0001) is True


# ---------------------------------------------------------------------------
# TestComputePreviewSize
# ---------------------------------------------------------------------------

class TestComputePreviewSize:
    def test_strong_rank_multiplies_correctly(self):
        size, label, mult = _compute_preview_size(0.02, RANK_STRONG, 0.08)
        assert size == round(0.02 * MULTIPLIER_STRONG, 4)
        assert label == "strong"
        assert mult == MULTIPLIER_STRONG

    def test_good_rank_multiplies_correctly(self):
        size, label, mult = _compute_preview_size(0.02, RANK_GOOD, 0.08)
        assert size == round(0.02 * MULTIPLIER_GOOD, 4)
        assert label == "good"

    def test_neutral_rank_multiplies_correctly(self):
        size, label, mult = _compute_preview_size(0.02, RANK_NEUTRAL, 0.08)
        assert size == round(0.02 * MULTIPLIER_NEUTRAL, 4)
        assert label == "neutral"

    def test_poor_rank_multiplies_correctly(self):
        size, label, mult = _compute_preview_size(0.02, 0.10, 0.08)
        assert size == round(0.02 * MULTIPLIER_POOR, 4)
        assert label == "poor"

    def test_capped_at_max_ticker_pct(self):
        size, _, _ = _compute_preview_size(0.08, RANK_STRONG, 0.08)
        assert size <= 0.08

    def test_returns_three_tuple(self):
        result = _compute_preview_size(0.02, 0.80, 0.08)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# TestBuildSimulationEmpty
# ---------------------------------------------------------------------------

class TestBuildSimulationEmpty:
    def test_no_rows_sample_size_zero(self):
        result = build_allocation_policy_simulation([], {})
        assert result["sample_size"] == 0

    def test_no_rows_observe_only_true(self):
        result = build_allocation_policy_simulation([], {})
        assert result["observe_only"] is True

    def test_no_rows_not_applied_true(self):
        result = build_allocation_policy_simulation([], {})
        assert result["not_applied"] is True

    def test_no_rows_details_empty(self):
        result = build_allocation_policy_simulation([], {})
        assert result["details"] == []

    def test_no_rows_baseline_total_return_zero(self):
        result = build_allocation_policy_simulation([], {})
        assert result["baseline"]["total_return"] == 0.0

    def test_no_rows_rank_aware_total_return_zero(self):
        result = build_allocation_policy_simulation([], {})
        assert result["rank_aware"]["total_return"] == 0.0

    def test_unresolved_rows_ignored(self):
        rows = [{"ticker": "AAPL", "outcome_return_3d": None, "normalized_allocation": 0.02, "final_rank_score": 0.8}]
        result = build_allocation_policy_simulation(rows, {})
        assert result["sample_size"] == 0

    def test_wrong_window_rows_ignored(self):
        # Row has 1d but not 3d
        rows = [{"ticker": "AAPL", "outcome_return_1d": 2.0, "normalized_allocation": 0.02, "final_rank_score": 0.8}]
        result = build_allocation_policy_simulation(rows, {}, primary_window_days=3)
        assert result["sample_size"] == 0


# ---------------------------------------------------------------------------
# TestBaselineFormula
# ---------------------------------------------------------------------------

class TestBaselineFormula:
    def test_baseline_contribution_formula(self):
        rows = [_row(outcome_return_3d=4.0, normalized_allocation=0.03)]
        result = build_allocation_policy_simulation(rows, {})
        d = result["details"][0]
        assert d["baseline_contribution"] == pytest.approx(4.0 * 0.03, abs=1e-4)

    def test_baseline_total_return_sums_contributions(self):
        rows = [
            _row("A", outcome_return_3d=2.0, normalized_allocation=0.02),
            _row("B", outcome_return_3d=3.0, normalized_allocation=0.04),
        ]
        result = build_allocation_policy_simulation(rows, {})
        expected = 2.0 * 0.02 + 3.0 * 0.04
        assert result["baseline"]["total_return"] == pytest.approx(expected, abs=1e-4)

    def test_baseline_avg_return_per_trade(self):
        rows = [
            _row("A", outcome_return_3d=2.0, normalized_allocation=0.02),
            _row("B", outcome_return_3d=4.0, normalized_allocation=0.02),
        ]
        result = build_allocation_policy_simulation(rows, {})
        total = 2.0 * 0.02 + 4.0 * 0.02
        assert result["baseline"]["avg_return_per_trade"] == pytest.approx(total / 2, abs=1e-4)

    def test_negative_return_contribution_is_negative(self):
        rows = [_row(outcome_return_3d=-3.0, normalized_allocation=0.02)]
        result = build_allocation_policy_simulation(rows, {})
        assert result["details"][0]["baseline_contribution"] < 0


# ---------------------------------------------------------------------------
# TestPreviewFormula
# ---------------------------------------------------------------------------

class TestPreviewFormula:
    def test_preview_contribution_formula(self):
        rows = [_row(outcome_return_3d=4.0, normalized_allocation=0.02, final_rank_score=RANK_STRONG)]
        result = build_allocation_policy_simulation(rows, {})
        d = result["details"][0]
        expected_preview = round(min(0.02 * MULTIPLIER_STRONG, _DEFAULT_MAX_TICKER_PCT), 4)
        assert d["preview_contribution"] == pytest.approx(4.0 * expected_preview, abs=1e-4)

    def test_rank_aware_total_return_sums_preview_contributions(self):
        rows = [
            _row("A", outcome_return_3d=2.0, normalized_allocation=0.02, final_rank_score=RANK_STRONG),
            _row("B", outcome_return_3d=3.0, normalized_allocation=0.02, final_rank_score=0.10),
        ]
        result = build_allocation_policy_simulation(rows, {})
        details = result["details"]
        expected = sum(d["preview_contribution"] for d in details)
        assert result["rank_aware"]["total_return"] == pytest.approx(expected, abs=1e-4)

    def test_high_rank_score_increases_preview_size(self):
        row_strong = _row(final_rank_score=RANK_STRONG, normalized_allocation=0.02)
        row_poor = _row(final_rank_score=0.10, normalized_allocation=0.02)
        res_strong = build_allocation_policy_simulation([row_strong], {})
        res_poor = build_allocation_policy_simulation([row_poor], {})
        assert res_strong["details"][0]["preview_size"] > res_poor["details"][0]["preview_size"]

    def test_preview_size_capped_at_max_ticker(self):
        rows = [_row(normalized_allocation=0.08, final_rank_score=RANK_STRONG)]
        result = build_allocation_policy_simulation(rows, {}, max_ticker_pct=0.08)
        assert result["details"][0]["preview_size"] <= 0.08


# ---------------------------------------------------------------------------
# TestCapitalEfficiency
# ---------------------------------------------------------------------------

class TestCapitalEfficiency:
    def test_capital_efficiency_formula(self):
        rows = [
            _row("A", outcome_return_3d=5.0, normalized_allocation=0.05, final_rank_score=RANK_NEUTRAL),
            _row("B", outcome_return_3d=2.0, normalized_allocation=0.05, final_rank_score=RANK_NEUTRAL),
        ]
        result = build_allocation_policy_simulation(rows, {})
        b = result["baseline"]
        expected_eff = round(b["total_return"] / b["total_allocated_pct"], 4)
        assert b["capital_efficiency"] == pytest.approx(expected_eff, abs=1e-4)

    def test_rank_aware_efficiency_formula(self):
        rows = [_row(outcome_return_3d=3.0, normalized_allocation=0.02, final_rank_score=RANK_GOOD)]
        result = build_allocation_policy_simulation(rows, {})
        ra = result["rank_aware"]
        expected_eff = round(ra["total_return"] / ra["total_allocated_pct"], 4)
        assert ra["capital_efficiency"] == pytest.approx(expected_eff, abs=1e-4)

    def test_zero_allocation_efficiency_is_zero(self):
        rows = [_row(normalized_allocation=0.0, final_rank_score=0.0)]
        result = build_allocation_policy_simulation(rows, {})
        assert result["baseline"]["capital_efficiency"] == 0.0


# ---------------------------------------------------------------------------
# TestHitRateWeightedCapital
# ---------------------------------------------------------------------------

class TestHitRateWeightedCapital:
    def test_all_wins_win_capital_pct_is_one(self):
        rows = [
            _row("A", outcome_return_3d=2.0, normalized_allocation=0.02, final_rank_score=RANK_NEUTRAL),
            _row("B", outcome_return_3d=1.0, normalized_allocation=0.02, final_rank_score=RANK_NEUTRAL),
        ]
        result = build_allocation_policy_simulation(rows, {})
        assert result["baseline"]["win_capital_pct"] == pytest.approx(1.0, abs=1e-4)
        assert result["baseline"]["loss_capital_pct"] == pytest.approx(0.0, abs=1e-4)

    def test_all_losses_loss_capital_pct_is_one(self):
        rows = [
            _row("A", outcome_return_3d=-2.0, normalized_allocation=0.02, final_rank_score=RANK_NEUTRAL),
            _row("B", outcome_return_3d=-1.0, normalized_allocation=0.02, final_rank_score=RANK_NEUTRAL),
        ]
        result = build_allocation_policy_simulation(rows, {})
        assert result["baseline"]["win_capital_pct"] == pytest.approx(0.0, abs=1e-4)
        assert result["baseline"]["loss_capital_pct"] == pytest.approx(1.0, abs=1e-4)

    def test_mixed_win_loss_capital_pct(self):
        rows = [
            _row("A", outcome_return_3d=2.0, normalized_allocation=0.02, final_rank_score=RANK_NEUTRAL),
            _row("B", outcome_return_3d=-1.0, normalized_allocation=0.02, final_rank_score=RANK_NEUTRAL),
        ]
        result = build_allocation_policy_simulation(rows, {})
        b = result["baseline"]
        assert b["win_capital_pct"] == pytest.approx(0.5, abs=1e-4)
        assert b["loss_capital_pct"] == pytest.approx(0.5, abs=1e-4)

    def test_win_loss_capital_sums_to_one(self):
        rows = [
            _row("A", outcome_return_3d=1.0, normalized_allocation=0.03, final_rank_score=RANK_GOOD),
            _row("B", outcome_return_3d=-2.0, normalized_allocation=0.01, final_rank_score=RANK_NEUTRAL),
        ]
        result = build_allocation_policy_simulation(rows, {})
        b = result["baseline"]
        assert b["win_capital_pct"] + b["loss_capital_pct"] == pytest.approx(1.0, abs=1e-4)


# ---------------------------------------------------------------------------
# TestDeltaCalculation
# ---------------------------------------------------------------------------

class TestDeltaCalculation:
    def test_total_return_delta_correct(self):
        rows = [_row(outcome_return_3d=3.0, normalized_allocation=0.02, final_rank_score=RANK_STRONG)]
        result = build_allocation_policy_simulation(rows, {})
        expected = round(
            result["rank_aware"]["total_return"] - result["baseline"]["total_return"], 4
        )
        assert result["delta"]["total_return_delta"] == pytest.approx(expected, abs=1e-4)

    def test_efficiency_delta_correct(self):
        rows = [_row(outcome_return_3d=2.0, normalized_allocation=0.02, final_rank_score=RANK_GOOD)]
        result = build_allocation_policy_simulation(rows, {})
        expected = round(
            result["rank_aware"]["capital_efficiency"] - result["baseline"]["capital_efficiency"], 4
        )
        assert result["delta"]["efficiency_delta"] == pytest.approx(expected, abs=1e-4)

    def test_win_capital_delta_correct(self):
        rows = [
            _row("A", outcome_return_3d=2.0, normalized_allocation=0.02, final_rank_score=RANK_STRONG),
            _row("B", outcome_return_3d=-1.0, normalized_allocation=0.02, final_rank_score=RANK_NEUTRAL),
        ]
        result = build_allocation_policy_simulation(rows, {})
        expected = round(
            result["rank_aware"]["win_capital_pct"] - result["baseline"]["win_capital_pct"], 4
        )
        assert result["delta"]["win_capital_delta"] == pytest.approx(expected, abs=1e-4)


# ---------------------------------------------------------------------------
# TestOutputShape
# ---------------------------------------------------------------------------

class TestOutputShape:
    def test_required_top_level_keys(self):
        result = build_allocation_policy_simulation([], {})
        required = {
            "generated_at", "observe_only", "not_applied", "primary_window_days",
            "sample_size", "baseline", "rank_aware", "delta", "details",
        }
        assert required.issubset(result.keys())

    def test_baseline_has_required_keys(self):
        result = build_allocation_policy_simulation([], {})
        required = {
            "total_return", "avg_return_per_trade", "capital_efficiency",
            "total_allocated_pct", "win_capital_pct", "loss_capital_pct",
        }
        assert required.issubset(result["baseline"].keys())

    def test_rank_aware_has_required_keys(self):
        result = build_allocation_policy_simulation([], {})
        required = {
            "total_return", "avg_return_per_trade", "capital_efficiency",
            "total_allocated_pct", "win_capital_pct", "loss_capital_pct",
        }
        assert required.issubset(result["rank_aware"].keys())

    def test_delta_has_required_keys(self):
        result = build_allocation_policy_simulation([], {})
        required = {"total_return_delta", "efficiency_delta", "win_capital_delta"}
        assert required.issubset(result["delta"].keys())

    def test_detail_row_has_required_keys(self):
        rows = [_row()]
        result = build_allocation_policy_simulation(rows, {})
        d = result["details"][0]
        required = {
            "ticker", "outcome_return", "baseline_size", "preview_size",
            "rank_score", "rank_label", "rank_multiplier",
            "baseline_contribution", "preview_contribution", "win",
        }
        assert required.issubset(d.keys())

    def test_result_is_json_serializable(self):
        rows = [_row()]
        result = build_allocation_policy_simulation(rows, {})
        serialized = json.dumps(result)
        roundtripped = json.loads(serialized)
        assert roundtripped["sample_size"] == 1

    def test_primary_window_days_in_output(self):
        result = build_allocation_policy_simulation([], {}, primary_window_days=7)
        assert result["primary_window_days"] == 7


# ---------------------------------------------------------------------------
# TestNoMutation
# ---------------------------------------------------------------------------

class TestNoMutation:
    def test_resolved_rows_list_not_mutated(self):
        rows = [_row("A"), _row("B")]
        original_len = len(rows)
        build_allocation_policy_simulation(rows, {})
        assert len(rows) == original_len

    def test_resolved_row_dict_not_mutated(self):
        row = _row(outcome_return_3d=2.0, normalized_allocation=0.03, final_rank_score=0.80)
        original = dict(row)
        build_allocation_policy_simulation([row], {})
        assert row == original

    def test_preview_opportunities_not_mutated(self):
        preview = {"AAPL": _preview_opp("AAPL")}
        original_keys = set(preview.keys())
        rows = [_row("AAPL")]
        build_allocation_policy_simulation(rows, preview)
        assert set(preview.keys()) == original_keys


# ---------------------------------------------------------------------------
# TestPreviewLookup
# ---------------------------------------------------------------------------

class TestPreviewLookup:
    def test_ticker_in_preview_uses_preview_size(self):
        rows = [_row("AAPL", normalized_allocation=0.02, final_rank_score=0.80)]
        preview = {"AAPL": _preview_opp("AAPL", preview_size=0.035)}
        result = build_allocation_policy_simulation(rows, preview)
        assert result["details"][0]["preview_size"] == pytest.approx(0.035, abs=1e-4)

    def test_ticker_in_preview_uses_rank_label(self):
        rows = [_row("AAPL")]
        preview = {"AAPL": _preview_opp("AAPL", rank_label="strong")}
        result = build_allocation_policy_simulation(rows, preview)
        assert result["details"][0]["rank_label"] == "strong"

    def test_ticker_not_in_preview_recomputes(self):
        rows = [_row("MSFT", normalized_allocation=0.02, final_rank_score=RANK_STRONG)]
        result = build_allocation_policy_simulation(rows, {})
        expected = round(min(0.02 * MULTIPLIER_STRONG, _DEFAULT_MAX_TICKER_PCT), 4)
        assert result["details"][0]["preview_size"] == pytest.approx(expected, abs=1e-4)


# ---------------------------------------------------------------------------
# TestFallbackBaseline
# ---------------------------------------------------------------------------

class TestFallbackBaseline:
    def test_none_normalized_allocation_uses_fallback(self):
        rows = [{"ticker": "AAPL", "outcome_return_3d": 2.0, "normalized_allocation": None, "final_rank_score": 0.8}]
        result = build_allocation_policy_simulation(
            rows, {}, fallback_baseline_pct=_DEFAULT_BASELINE_PCT
        )
        assert result["details"][0]["baseline_size"] == pytest.approx(_DEFAULT_BASELINE_PCT, abs=1e-6)

    def test_missing_normalized_allocation_key_uses_fallback(self):
        rows = [{"ticker": "AAPL", "outcome_return_3d": 2.0, "final_rank_score": 0.8}]
        fallback = 0.025
        result = build_allocation_policy_simulation(rows, {}, fallback_baseline_pct=fallback)
        assert result["details"][0]["baseline_size"] == pytest.approx(fallback, abs=1e-6)


# ---------------------------------------------------------------------------
# TestCustomWindow
# ---------------------------------------------------------------------------

class TestCustomWindow:
    def test_1d_window_reads_outcome_return_1d(self):
        rows = [{"ticker": "AAPL", "outcome_return_1d": 5.0, "normalized_allocation": 0.02, "final_rank_score": 0.8}]
        result = build_allocation_policy_simulation(rows, {}, primary_window_days=1)
        assert result["sample_size"] == 1
        assert result["details"][0]["outcome_return"] == pytest.approx(5.0, abs=1e-4)

    def test_7d_window_reads_outcome_return_7d(self):
        rows = [{"ticker": "AAPL", "outcome_return_7d": 7.5, "normalized_allocation": 0.02, "final_rank_score": 0.7}]
        result = build_allocation_policy_simulation(rows, {}, primary_window_days=7)
        assert result["sample_size"] == 1


# ---------------------------------------------------------------------------
# TestLoadPreviewOpportunities
# ---------------------------------------------------------------------------

class TestLoadPreviewOpportunities:
    def test_missing_file_returns_empty_dict(self, tmp_path):
        result = _load_preview_opportunities(tmp_path / "no_file.json")
        assert result == {}

    def test_malformed_json_returns_empty_dict(self, tmp_path):
        p = tmp_path / "preview.json"
        p.write_text("{bad json", encoding="utf-8")
        result = _load_preview_opportunities(p)
        assert result == {}

    def test_valid_preview_builds_ticker_map(self, tmp_path):
        data = {
            "opportunities": [
                {"ticker": "AAPL", "preview_size": 0.025, "rank_label": "good"},
                {"ticker": "MSFT", "preview_size": 0.030, "rank_label": "strong"},
            ]
        }
        p = tmp_path / "preview.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        result = _load_preview_opportunities(p)
        assert "AAPL" in result
        assert "MSFT" in result

    def test_ticker_map_values_are_opportunity_dicts(self, tmp_path):
        data = {"opportunities": [{"ticker": "AAPL", "preview_size": 0.025}]}
        p = tmp_path / "preview.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        result = _load_preview_opportunities(p)
        assert result["AAPL"]["preview_size"] == 0.025

    def test_non_dict_json_returns_empty(self, tmp_path):
        p = tmp_path / "preview.json"
        p.write_text("[1, 2, 3]", encoding="utf-8")
        result = _load_preview_opportunities(p)
        assert result == {}


# ---------------------------------------------------------------------------
# TestGenerateReport
# ---------------------------------------------------------------------------

class TestGenerateReport:
    def test_missing_db_produces_empty_simulation(self, tmp_path):
        result = generate_allocation_policy_simulation_report(
            root=tmp_path,
            db_path=tmp_path / "no_db.db",
            output_dir=tmp_path,
        )
        assert result["sample_size"] == 0
        assert result["observe_only"] is True

    def test_output_file_written(self, tmp_path):
        generate_allocation_policy_simulation_report(
            root=tmp_path,
            db_path=tmp_path / "no_db.db",
            output_dir=tmp_path,
        )
        out_path = tmp_path / "allocation_policy_simulation.json"
        assert out_path.exists()

    def test_output_file_valid_json(self, tmp_path):
        generate_allocation_policy_simulation_report(
            root=tmp_path,
            db_path=tmp_path / "no_db.db",
            output_dir=tmp_path,
        )
        content = (tmp_path / "allocation_policy_simulation.json").read_text(encoding="utf-8")
        data = json.loads(content)
        assert "sample_size" in data

    def test_output_dir_created_if_missing(self, tmp_path):
        nested_out = tmp_path / "deep" / "output"
        generate_allocation_policy_simulation_report(
            root=tmp_path,
            db_path=tmp_path / "no_db.db",
            output_dir=nested_out,
        )
        assert nested_out.is_dir()

    def test_with_preview_file_loads_opportunities(self, tmp_path):
        preview_dir = tmp_path / "outputs" / "performance"
        preview_dir.mkdir(parents=True)
        preview = {
            "opportunities": [
                {"ticker": "AAPL", "preview_size": 0.025, "rank_label": "good", "rank_multiplier": 1.10}
            ]
        }
        (preview_dir / "allocation_policy_preview.json").write_text(
            json.dumps(preview), encoding="utf-8"
        )
        result = generate_allocation_policy_simulation_report(
            root=tmp_path,
            db_path=tmp_path / "no_db.db",
            output_dir=tmp_path / "outputs" / "performance",
        )
        # No resolved signals (no DB), so simulation is empty even with preview
        assert result["sample_size"] == 0
