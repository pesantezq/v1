"""
Tests for watchlist_scanner/system_summary.py.

Covers:
  - All pure computation functions with valid and empty inputs
  - Safe handling of missing artifacts (empty dicts)
  - Top theme / top opportunity selection correctness
  - Best portfolio fit selection
  - System state flags
  - Capital preview computation
  - Policy insight extraction
  - Data health counts
  - Change detection (new top theme, new opp, weight change, policy change)
  - Markdown rendering (non-empty, contains key sections)
  - JSON schema validity (required top-level keys present)
  - build_system_decision_summary integration
  - generate_system_decision_summary with write_files=False
  - No mutation of input dicts
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from watchlist_scanner.system_summary import (
    _default_root,
    _merge_theme_sources,
    _normalize_theme_record,
    build_system_decision_summary,
    compute_best_portfolio_fit,
    compute_capital_preview,
    compute_changes,
    compute_data_health,
    compute_policy_insight,
    compute_system_state,
    compute_top_opportunity,
    compute_top_theme,
    generate_system_decision_summary,
    render_markdown,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _signals(*overrides_list) -> dict:
    """Build a minimal watchlist_signals dict with one signal per override."""
    base_signal = {
        "ticker": "AAPL",
        "filter_allowed": True,
        "final_rank_score": 0.75,
        "signal_score": 0.70,
        "confidence_score": 0.68,
        "theme_alignment_label": "aligned",
        "portfolio_fit_label": "strong",
        "portfolio_fit_score": 0.80,
        "portfolio_fit_reason": "Good sector alignment",
        "rank_multiplier": 1.10,
        "conviction_band": "high_conviction",
    }
    results = []
    for ov in overrides_list:
        s = dict(base_signal)
        s.update(ov)
        results.append(s)
    return {"results": results}


def _themes(*theme_dicts) -> dict:
    """Build a theme_opportunities dict from given theme dicts."""
    return {"themes": list(theme_dicts)}


def _theme(name="AI", score=0.80, **kwargs) -> dict:
    return {
        "name": name,
        "type": "classified",
        "score": score,
        "persistence": 0.6,
        "acceleration": 0.2,
        "tickers": ["NVDA", "MSFT"],
        **kwargs,
    }


def _ranking_config(**kwargs) -> dict:
    base = {
        "applied_to_live": False,
        "recommended_candidate": "portfolio_fit_heavy",
        "approved_at": "2026-04-27T12:00:00",
    }
    base.update(kwargs)
    return base


def _alloc_policy(**kwargs) -> dict:
    base = {
        "activation_status": "approved_not_live",
        "applied_to_live": False,
        "sample_size": 42,
        "low_sample_warning": False,
    }
    base.update(kwargs)
    return base


def _alloc_preview(**kwargs) -> dict:
    base = {
        "observe_only": True,
        "not_applied": True,
        "candidate_count": 5,
        "total_baseline_pct": 0.10,
        "total_preview_pct": 0.12,
    }
    base.update(kwargs)
    return base


def _alloc_simulation(**kwargs) -> dict:
    base = {
        "observe_only": True,
        "not_applied": True,
        "sample_size": 35,
        "baseline": {"capital_efficiency": 0.12},
        "rank_aware": {"capital_efficiency": 0.15},
        "delta": {"efficiency_delta": 0.03, "total_return_delta": 0.05},
    }
    base.update(kwargs)
    return base


def _weight_tuning(**kwargs) -> dict:
    base = {
        "recommended_candidate": "portfolio_fit_heavy",
        "recommendation_reason": "Best hit rate",
        "total_rows": 100,
        "resolved_rows": 40,
        "candidates": [
            {
                "name": "portfolio_fit_heavy",
                "top_quartile_hit_rate": 0.65,
                "top_quartile_avg_return": 0.5,
                "sample_size": 30,
                "low_sample_warning": False,
            }
        ],
    }
    base.update(kwargs)
    return base


def _full_artifacts(**overrides) -> dict:
    base = {
        "signals":          _signals({}),
        "themes":           _themes(_theme()),
        "portfolio":        {},
        "ranking_config":   _ranking_config(),
        "alloc_policy":     _alloc_policy(),
        "alloc_preview":    _alloc_preview(),
        "alloc_simulation": _alloc_simulation(),
        "weight_tuning":    _weight_tuning(),
    }
    base.update(overrides)
    return base


def _all_flags(value: bool = True) -> dict:
    return {
        "watchlist_signals": value,
        "theme_opportunities": value,
        "theme_signals": value,
        "theme_data_available": value,
        "portfolio_snapshot": value,
        "approved_ranking_config": value,
        "approved_allocation_policy": value,
        "allocation_preview": value,
        "allocation_simulation": value,
        "weight_tuning_suggestions": value,
    }


def _engine_theme(name="AI Infrastructure", confidence=0.95, persistence_7d=3, **kwargs) -> dict:
    """Build a theme record in the theme_engine (theme_signals.json) schema."""
    return {
        "name": name,
        "confidence": confidence,
        "persistence_7d": persistence_7d,
        "catalog_match": kwargs.pop("catalog_match", name),
        "rationale": "Test rationale.",
        "tickers": kwargs.pop("tickers", ["NVDA", "AMD"]),
        **kwargs,
    }


def _engine_themes(*theme_dicts) -> dict:
    """Build a theme_signals.json-style dict."""
    return {
        "generated_at": "2026-04-27T20:00:00",
        "run_date": "2026-04-27",
        "themes": list(theme_dicts),
    }


# ---------------------------------------------------------------------------
# TestComputeTopTheme
# ---------------------------------------------------------------------------

class TestComputeTopTheme:
    def test_empty_themes_returns_empty(self):
        assert compute_top_theme({}) == {}

    def test_empty_theme_list_returns_empty(self):
        assert compute_top_theme({"themes": []}) == {}

    def test_highest_score_selected(self):
        themes = _themes(_theme("AI", 0.80), _theme("Energy", 0.90), _theme("Crypto", 0.70))
        result = compute_top_theme(themes)
        assert result["name"] == "Energy"
        assert result["score"] == pytest.approx(0.90)

    def test_single_theme_returned(self):
        themes = _themes(_theme("AI", 0.75))
        result = compute_top_theme(themes)
        assert result["name"] == "AI"

    def test_tickers_capped_at_10(self):
        many_tickers = [f"TICK{i}" for i in range(20)]
        themes = _themes(_theme("AI", 0.80, tickers=many_tickers))
        result = compute_top_theme(themes)
        assert len(result["tickers"]) <= 10

    def test_result_shape(self):
        themes = _themes(_theme())
        result = compute_top_theme(themes)
        for key in ("name", "type", "score", "persistence", "acceleration", "tickers"):
            assert key in result

    def test_type_field_present(self):
        themes = _themes(_theme("AI", 0.80, type="emerging"))
        result = compute_top_theme(themes)
        assert result["type"] == "emerging"

    def test_missing_fields_safe(self):
        themes = {"themes": [{"name": "AI"}]}
        result = compute_top_theme(themes)
        assert result["name"] == "AI"
        assert result["score"] == pytest.approx(0.0)

    def test_none_themes_value_returns_empty(self):
        assert compute_top_theme({"themes": None}) == {}

    def test_alternative_dict_shape(self):
        # Top-level keys as theme names
        themes = {"AI": {"score": 0.82, "type": "classified"}}
        result = compute_top_theme(themes)
        assert result["name"] == "AI"
        assert result["score"] == pytest.approx(0.82)


# ---------------------------------------------------------------------------
# TestComputeTopOpportunity
# ---------------------------------------------------------------------------

class TestComputeTopOpportunity:
    def test_empty_signals_returns_empty(self):
        assert compute_top_opportunity({}) == {}

    def test_empty_results_returns_empty(self):
        assert compute_top_opportunity({"results": []}) == {}

    def test_highest_rank_score_selected(self):
        sigs = _signals(
            {"ticker": "NVDA", "final_rank_score": 0.91, "filter_allowed": True},
            {"ticker": "AAPL", "final_rank_score": 0.80, "filter_allowed": True},
        )
        result = compute_top_opportunity(sigs)
        assert result["ticker"] == "NVDA"
        assert result["final_rank_score"] == pytest.approx(0.91)

    def test_eligible_signals_preferred(self):
        sigs = _signals(
            {"ticker": "NVDA", "final_rank_score": 0.95, "filter_allowed": False},
            {"ticker": "AAPL", "final_rank_score": 0.80, "filter_allowed": True},
        )
        result = compute_top_opportunity(sigs)
        assert result["ticker"] == "AAPL"

    def test_fallback_to_all_when_no_eligible(self):
        sigs = _signals(
            {"ticker": "NVDA", "final_rank_score": 0.95, "filter_allowed": False},
        )
        result = compute_top_opportunity(sigs)
        assert result["ticker"] == "NVDA"

    def test_result_shape(self):
        result = compute_top_opportunity(_signals({}))
        for key in ("ticker", "final_rank_score", "signal_score", "confidence",
                    "theme_alignment_label", "portfolio_fit_label", "rank_multiplier",
                    "conviction_band"):
            assert key in result

    def test_missing_rank_score_safe(self):
        sigs = _signals({"ticker": "AAPL", "final_rank_score": None})
        result = compute_top_opportunity(sigs)
        assert result["ticker"] == "AAPL"
        assert result["final_rank_score"] == pytest.approx(0.0)

    def test_signals_key_alternative(self):
        sigs = {"signals": [{"ticker": "NVDA", "final_rank_score": 0.88, "filter_allowed": True}]}
        result = compute_top_opportunity(sigs)
        assert result["ticker"] == "NVDA"


# ---------------------------------------------------------------------------
# TestComputeBestPortfolioFit
# ---------------------------------------------------------------------------

class TestComputeBestPortfolioFit:
    def test_empty_returns_empty(self):
        assert compute_best_portfolio_fit({}) == {}

    def test_no_fit_scores_returns_empty(self):
        sigs = _signals({"ticker": "AAPL", "portfolio_fit_score": None})
        assert compute_best_portfolio_fit(sigs) == {}

    def test_highest_fit_score_selected(self):
        sigs = _signals(
            {"ticker": "AAPL", "portfolio_fit_score": 0.80},
            {"ticker": "NVDA", "portfolio_fit_score": 0.92},
        )
        result = compute_best_portfolio_fit(sigs)
        assert result["ticker"] == "NVDA"
        assert result["portfolio_fit_score"] == pytest.approx(0.92)

    def test_result_shape(self):
        result = compute_best_portfolio_fit(_signals({}))
        for key in ("ticker", "portfolio_fit_score", "portfolio_fit_label",
                    "portfolio_fit_reason", "final_rank_score"):
            assert key in result

    def test_reason_preserved(self):
        sigs = _signals({"ticker": "AAPL", "portfolio_fit_score": 0.80,
                         "portfolio_fit_reason": "Great sector alignment"})
        result = compute_best_portfolio_fit(sigs)
        assert result["portfolio_fit_reason"] == "Great sector alignment"


# ---------------------------------------------------------------------------
# TestComputeSystemState
# ---------------------------------------------------------------------------

class TestComputeSystemState:
    def test_approved_weights_source(self):
        result = compute_system_state(
            _ranking_config(), _alloc_policy(), _alloc_simulation(), _alloc_preview()
        )
        assert result["ranking_weights_source"] == "approved"

    def test_default_weights_when_no_config(self):
        result = compute_system_state({}, {}, {}, {})
        assert result["ranking_weights_source"] == "default"

    def test_applied_to_live_true_falls_back_to_default(self):
        config = _ranking_config(applied_to_live=True)
        result = compute_system_state(config, {}, {}, {})
        assert result["ranking_weights_source"] == "default"

    def test_allocation_policy_status_extracted(self):
        result = compute_system_state(
            {}, _alloc_policy(activation_status="approved_not_live"), {}, {}
        )
        assert result["allocation_policy_status"] == "approved_not_live"

    def test_not_approved_when_policy_empty(self):
        result = compute_system_state({}, {}, {}, {})
        assert result["allocation_policy_status"] == "not_approved"

    def test_simulation_observe_only_flag(self):
        result = compute_system_state({}, {}, _alloc_simulation(), {})
        assert result["simulation_observe_only"] is True
        assert result["simulation_not_applied"] is True

    def test_applied_to_live_false(self):
        result = compute_system_state({}, _alloc_policy(), {}, {})
        assert result["applied_to_live"] is False

    def test_result_has_required_keys(self):
        result = compute_system_state({}, {}, {}, {})
        required = [
            "ranking_weights_source", "ranking_weights_candidate",
            "allocation_policy_status", "applied_to_live",
            "simulation_observe_only", "simulation_not_applied",
        ]
        for key in required:
            assert key in result

    def test_weights_candidate_extracted(self):
        result = compute_system_state(_ranking_config(), {}, {}, {})
        assert result["ranking_weights_candidate"] == "portfolio_fit_heavy"


# ---------------------------------------------------------------------------
# TestComputeCapitalPreview
# ---------------------------------------------------------------------------

class TestComputeCapitalPreview:
    def test_empty_returns_zeros(self):
        result = compute_capital_preview({}, {})
        assert result["candidate_count"] == 0
        assert result["total_baseline_pct"] == pytest.approx(0.0)

    def test_delta_computed_correctly(self):
        result = compute_capital_preview(
            _alloc_preview(total_baseline_pct=0.10, total_preview_pct=0.12), {}
        )
        assert result["preview_vs_baseline_delta"] == pytest.approx(0.02, abs=1e-4)

    def test_negative_delta(self):
        result = compute_capital_preview(
            _alloc_preview(total_baseline_pct=0.12, total_preview_pct=0.10), {}
        )
        assert result["preview_vs_baseline_delta"] < 0

    def test_simulation_efficiency_delta(self):
        result = compute_capital_preview({}, _alloc_simulation())
        assert result["simulation_efficiency_delta"] == pytest.approx(0.03)

    def test_simulation_return_delta(self):
        result = compute_capital_preview({}, _alloc_simulation())
        assert result["simulation_return_delta"] == pytest.approx(0.05)

    def test_capital_efficiencies_extracted(self):
        result = compute_capital_preview({}, _alloc_simulation())
        assert result["baseline_capital_efficiency"] == pytest.approx(0.12)
        assert result["rank_aware_capital_efficiency"] == pytest.approx(0.15)

    def test_candidate_count(self):
        result = compute_capital_preview(_alloc_preview(candidate_count=7), {})
        assert result["candidate_count"] == 7


# ---------------------------------------------------------------------------
# TestComputePolicyInsight
# ---------------------------------------------------------------------------

class TestComputePolicyInsight:
    def test_best_candidate_from_weight_tuning(self):
        result = compute_policy_insight(_weight_tuning(), {}, {})
        assert result["best_weight_candidate"] == "portfolio_fit_heavy"

    def test_fallback_to_ranking_config(self):
        result = compute_policy_insight({}, _ranking_config(), {})
        assert result["best_weight_candidate"] == "portfolio_fit_heavy"

    def test_default_fallback(self):
        result = compute_policy_insight({}, {}, {})
        assert result["best_weight_candidate"] == "current"

    def test_reason_extracted(self):
        result = compute_policy_insight(_weight_tuning(), {}, {})
        assert result["recommendation_reason"] == "Best hit rate"

    def test_low_sample_flag_when_resolved_below_20(self):
        result = compute_policy_insight(_weight_tuning(resolved_rows=10), {}, {})
        assert result["low_sample_warning"] is True

    def test_no_low_sample_when_resolved_above_20(self):
        result = compute_policy_insight(_weight_tuning(resolved_rows=30), {}, {})
        assert result["low_sample_warning"] is False

    def test_simulation_efficiency_delta(self):
        result = compute_policy_insight({}, {}, _alloc_simulation())
        assert result["simulation_efficiency_delta"] == pytest.approx(0.03)

    def test_best_hit_rate_extracted(self):
        result = compute_policy_insight(_weight_tuning(), {}, {})
        assert result["best_top_quartile_hit_rate"] == pytest.approx(0.65)

    def test_result_has_required_keys(self):
        result = compute_policy_insight({}, {}, {})
        for key in ("best_weight_candidate", "recommendation_reason", "low_sample_warning",
                    "simulation_efficiency_delta", "simulation_total_return_delta"):
            assert key in result


# ---------------------------------------------------------------------------
# TestComputeDataHealth
# ---------------------------------------------------------------------------

class TestComputeDataHealth:
    def test_empty_signals_zero_counts(self):
        result = compute_data_health({}, _all_flags(True))
        assert result["total_signals"] == 0
        assert result["eligible_signals"] == 0

    def test_total_signals_counted(self):
        result = compute_data_health(_signals({}, {}), _all_flags(True))
        assert result["total_signals"] == 2

    def test_eligible_signals_counted(self):
        sigs = _signals(
            {"filter_allowed": True},
            {"filter_allowed": False},
            {"filter_allowed": True},
        )
        result = compute_data_health(sigs, _all_flags(True))
        assert result["eligible_signals"] == 2

    def test_missing_artifacts_listed(self):
        flags = _all_flags(True)
        flags["watchlist_signals"] = False
        flags["theme_opportunities"] = False
        result = compute_data_health({}, flags)
        assert "watchlist_signals" in result["missing_artifacts"]
        assert "theme_opportunities" not in result["missing_artifacts"]
        assert result["optional_artifact_details"][0]["artifact"] == "theme_opportunities"
        assert result["missing_artifact_count"] == 1

    def test_missing_artifact_details_include_path_and_producer(self):
        flags = _all_flags(True)
        flags["watchlist_signals"] = False
        result = compute_data_health({}, flags)
        details = result["missing_artifact_details"]
        assert len(details) == 1
        assert details[0]["path"] == "outputs/latest/watchlist_signals.json"
        assert details[0]["producer_step"] == "watchlist scanner"

    def test_derived_theme_flag_not_counted_as_missing_file(self):
        flags = _all_flags(True)
        flags["theme_data_available"] = False
        result = compute_data_health({}, flags)
        assert result["missing_artifact_count"] == 0
        assert result["missing_artifacts"] == []

    def test_theme_opportunities_optional_when_theme_signals_exists(self):
        flags = _all_flags(True)
        flags["theme_opportunities"] = False
        flags["theme_signals"] = True
        result = compute_data_health({}, flags)
        assert result["missing_artifact_count"] == 0
        assert result["missing_artifacts"] == []
        assert result["optional_artifact_details"][0]["artifact"] == "theme_opportunities"
        assert result["optional_artifact_details"][0]["severity"] == "optional_missing"

    def test_approved_ranking_config_defaulting_when_absent(self):
        flags = _all_flags(True)
        flags["approved_ranking_config"] = False
        result = compute_data_health({}, flags)
        assert result["missing_artifact_count"] == 0
        assert result["defaulting_artifact_details"][0]["artifact"] == "approved_ranking_config"
        assert result["defaulting_artifact_details"][0]["severity"] == "defaulting"

    def test_approved_allocation_policy_defaulting_when_absent(self):
        flags = _all_flags(True)
        flags["approved_allocation_policy"] = False
        result = compute_data_health({}, flags)
        assert result["missing_artifact_count"] == 0
        assert result["defaulting_artifact_details"][0]["artifact"] == "approved_allocation_policy"
        assert result["defaulting_artifact_details"][0]["severity"] == "defaulting"

    def test_all_present_no_missing(self):
        result = compute_data_health({}, _all_flags(True))
        assert result["all_artifacts_present"] is True
        assert result["missing_artifact_count"] == 0

    def test_degraded_mode_flag(self):
        result = compute_data_health({"degraded_mode": True}, _all_flags())
        assert result["degraded_mode"] is True

    def test_degraded_mode_false_default(self):
        result = compute_data_health({}, _all_flags())
        assert result["degraded_mode"] is False


# ---------------------------------------------------------------------------
# TestComputeChanges
# ---------------------------------------------------------------------------

class TestComputeChanges:
    def test_no_previous_reports_unavailable(self):
        result = compute_changes({"top_theme": {"name": "AI"}}, {})
        assert result["previous_available"] is False
        assert result["change_count"] == 0

    def test_same_values_no_changes(self):
        current = {
            "top_theme": {"name": "AI"},
            "top_opportunity": {"ticker": "NVDA"},
            "system_state": {
                "ranking_weights_source": "approved",
                "ranking_weights_candidate": "portfolio_fit_heavy",
                "allocation_policy_status": "approved_not_live",
            },
            "best_portfolio_fit": {"ticker": "AAPL"},
        }
        result = compute_changes(current, current)
        assert result["change_count"] == 0
        assert result["previous_available"] is True

    def test_top_theme_change_detected(self):
        prev = {"top_theme": {"name": "AI"}, "top_opportunity": {}, "system_state": {}, "best_portfolio_fit": {}}
        curr = {"top_theme": {"name": "Energy"}, "top_opportunity": {}, "system_state": {}, "best_portfolio_fit": {}}
        result = compute_changes(curr, prev)
        assert any("Top theme" in c for c in result["changes"])
        assert result["change_count"] >= 1

    def test_top_opportunity_change_detected(self):
        prev = {"top_theme": {}, "top_opportunity": {"ticker": "MSFT"}, "system_state": {}, "best_portfolio_fit": {}}
        curr = {"top_theme": {}, "top_opportunity": {"ticker": "NVDA"}, "system_state": {}, "best_portfolio_fit": {}}
        result = compute_changes(curr, prev)
        assert any("Top opportunity" in c for c in result["changes"])

    def test_weight_candidate_change_detected(self):
        prev = {"top_theme": {}, "top_opportunity": {}, "system_state": {"ranking_weights_candidate": "current", "ranking_weights_source": "default", "allocation_policy_status": "not_approved"}, "best_portfolio_fit": {}}
        curr = {"top_theme": {}, "top_opportunity": {}, "system_state": {"ranking_weights_candidate": "portfolio_fit_heavy", "ranking_weights_source": "approved", "allocation_policy_status": "not_approved"}, "best_portfolio_fit": {}}
        result = compute_changes(curr, prev)
        change_texts = " ".join(result["changes"])
        assert "weights" in change_texts.lower() or "candidate" in change_texts.lower()

    def test_allocation_policy_change_detected(self):
        prev = {"top_theme": {}, "top_opportunity": {}, "system_state": {"ranking_weights_source": "", "ranking_weights_candidate": "", "allocation_policy_status": "not_approved"}, "best_portfolio_fit": {}}
        curr = {"top_theme": {}, "top_opportunity": {}, "system_state": {"ranking_weights_source": "", "ranking_weights_candidate": "", "allocation_policy_status": "approved_not_live"}, "best_portfolio_fit": {}}
        result = compute_changes(curr, prev)
        assert any("policy" in c.lower() for c in result["changes"])

    def test_summary_line_pluralised(self):
        prev = {"top_theme": {"name": "AI"}, "top_opportunity": {"ticker": "MSFT"}, "system_state": {}, "best_portfolio_fit": {}}
        curr = {"top_theme": {"name": "Energy"}, "top_opportunity": {"ticker": "NVDA"}, "system_state": {}, "best_portfolio_fit": {}}
        result = compute_changes(curr, prev)
        assert "change" in result["summary_line"].lower()

    def test_no_changes_summary_line(self):
        same = {"top_theme": {"name": "AI"}, "top_opportunity": {"ticker": "NVDA"}, "system_state": {"ranking_weights_source": "approved", "ranking_weights_candidate": "current", "allocation_policy_status": "approved_not_live"}, "best_portfolio_fit": {"ticker": "AAPL"}}
        result = compute_changes(same, same)
        assert "No significant" in result["summary_line"]


# ---------------------------------------------------------------------------
# TestRenderMarkdown
# ---------------------------------------------------------------------------

class TestRenderMarkdown:
    def _full_summary(self):
        arts = _full_artifacts()
        flags = _all_flags(True)
        return build_system_decision_summary(arts, flags, previous_summary=None)

    def test_markdown_not_empty(self):
        summary = self._full_summary()
        md = render_markdown(summary)
        assert len(md) > 100

    def test_contains_title(self):
        md = render_markdown(self._full_summary())
        assert "# System Decision Summary" in md

    def test_contains_top_theme_section(self):
        md = render_markdown(self._full_summary())
        assert "## Top Theme" in md

    def test_contains_top_opportunity_section(self):
        md = render_markdown(self._full_summary())
        assert "## Top Opportunity" in md

    def test_contains_portfolio_fit_section(self):
        md = render_markdown(self._full_summary())
        assert "## Best Portfolio Fit" in md

    def test_contains_capital_allocation_section(self):
        md = render_markdown(self._full_summary())
        assert "## Capital Allocation Preview" in md

    def test_contains_policy_status_section(self):
        md = render_markdown(self._full_summary())
        assert "## Policy Status" in md

    def test_contains_policy_insight_section(self):
        md = render_markdown(self._full_summary())
        assert "## Policy Insight" in md

    def test_contains_data_health_section(self):
        md = render_markdown(self._full_summary())
        assert "## Data Health" in md

    def test_data_health_lists_missing_artifact_paths_and_producers(self):
        summary = self._full_summary()
        summary["data_health"] = {
            "degraded_mode": False,
            "data_mode": "live",
            "total_signals": 0,
            "eligible_signals": 0,
            "missing_artifacts": ["watchlist_signals"],
            "missing_artifact_details": [
                {
                    "artifact": "watchlist_signals",
                    "path": "outputs/latest/watchlist_signals.json",
                    "producer_step": "watchlist scanner",
                }
            ],
            "defaulting_artifact_details": [],
            "optional_artifact_details": [],
        }
        md = render_markdown(summary)
        assert "outputs/latest/watchlist_signals.json" in md
        assert "producer: watchlist scanner" in md

    def test_data_health_lists_defaulting_and_optional_artifacts(self):
        summary = self._full_summary()
        summary["data_health"] = {
            "degraded_mode": False,
            "data_mode": "live",
            "total_signals": 0,
            "eligible_signals": 0,
            "missing_artifacts": [],
            "missing_artifact_details": [],
            "defaulting_artifact_details": [
                {
                    "artifact": "approved_ranking_config",
                    "path": "outputs/performance/approved_ranking_config.json",
                    "producer_step": "ranking config promotion",
                }
            ],
            "optional_artifact_details": [
                {
                    "artifact": "theme_opportunities",
                    "path": "outputs/latest/theme_opportunities.json",
                    "producer_step": "theme discovery",
                }
            ],
        }
        md = render_markdown(summary)
        assert "Defaulting artifacts not present" in md
        assert "outputs/performance/approved_ranking_config.json" in md
        assert "Optional artifacts not present" in md
        assert "outputs/latest/theme_opportunities.json" in md

    def test_contains_changes_section(self):
        md = render_markdown(self._full_summary())
        assert "## Changes Since Last Run" in md

    def test_empty_summary_does_not_crash(self):
        md = render_markdown({})
        assert "# System Decision Summary" in md

    def test_theme_name_in_markdown(self):
        arts = _full_artifacts(themes=_themes(_theme("Semiconductors", 0.9)))
        flags = _all_flags()
        summary = build_system_decision_summary(arts, flags)
        md = render_markdown(summary)
        assert "Semiconductors" in md

    def test_opportunity_ticker_in_markdown(self):
        arts = _full_artifacts(signals=_signals({"ticker": "TSLA", "final_rank_score": 0.88, "filter_allowed": True}))
        flags = _all_flags()
        summary = build_system_decision_summary(arts, flags)
        md = render_markdown(summary)
        assert "TSLA" in md


# ---------------------------------------------------------------------------
# TestBuildSystemDecisionSummary
# ---------------------------------------------------------------------------

class TestBuildSystemDecisionSummary:
    def test_required_top_level_keys(self):
        summary = build_system_decision_summary(_full_artifacts(), _all_flags())
        for key in ("generated_at", "schema_version", "top_theme", "top_opportunity",
                    "best_portfolio_fit", "system_state", "capital_preview",
                    "policy_insight", "data_health", "changes"):
            assert key in summary

    def test_all_artifacts_missing_does_not_crash(self):
        summary = build_system_decision_summary({}, {})
        assert isinstance(summary, dict)
        assert "generated_at" in summary

    def test_schema_version_is_string(self):
        summary = build_system_decision_summary({}, {})
        assert isinstance(summary["schema_version"], str)

    def test_generated_at_is_iso_string(self):
        summary = build_system_decision_summary({}, {})
        gen_at = summary["generated_at"]
        assert isinstance(gen_at, str)
        assert "T" in gen_at

    def test_no_mutation_of_artifacts(self):
        arts = _full_artifacts()
        orig_signals = dict(arts["signals"])
        build_system_decision_summary(arts, _all_flags())
        assert arts["signals"] == orig_signals

    def test_no_mutation_of_flags(self):
        flags = _all_flags()
        orig = dict(flags)
        build_system_decision_summary(_full_artifacts(), flags)
        assert flags == orig

    def test_changes_section_present(self):
        arts = _full_artifacts()
        summary = build_system_decision_summary(arts, _all_flags(), previous_summary=None)
        assert "changes" in summary
        assert "previous_available" in summary["changes"]

    def test_changes_with_previous_summary(self):
        arts = _full_artifacts()
        prev = build_system_decision_summary(arts, _all_flags())
        arts2 = _full_artifacts(themes=_themes(_theme("Energy", 0.95)))
        summary = build_system_decision_summary(arts2, _all_flags(), previous_summary=prev)
        assert summary["changes"]["previous_available"] is True

    def test_top_theme_falls_back_to_previous_summary_when_current_empty(self):
        previous_summary = {
            "top_theme": {
                "name": "AI Infrastructure",
                "type": "classified",
                "score": 0.91,
                "persistence": 0.6,
                "acceleration": 0.2,
                "tickers": ["NVDA"],
            }
        }
        summary = build_system_decision_summary(
            _full_artifacts(themes={"themes": []}),
            _all_flags(),
            previous_summary=previous_summary,
        )
        assert summary["top_theme"]["name"] == "AI Infrastructure"
        assert summary["top_theme"]["theme_source"] == "stale"


# ---------------------------------------------------------------------------
# TestGenerateSystemDecisionSummary
# ---------------------------------------------------------------------------

class TestGenerateSystemDecisionSummary:
    def test_default_root_points_to_repo_root(self):
        expected = Path(__file__).resolve().parents[1]
        assert _default_root() == expected

    def test_dry_run_returns_dict(self, tmp_path):
        result = generate_system_decision_summary(root=tmp_path, write_files=False)
        assert isinstance(result, dict)
        assert "generated_at" in result

    def test_dry_run_writes_no_files(self, tmp_path):
        generate_system_decision_summary(root=tmp_path, write_files=False)
        json_path = tmp_path / "outputs" / "latest" / "system_decision_summary.json"
        md_path   = tmp_path / "outputs" / "latest" / "system_decision_summary.md"
        assert not json_path.exists()
        assert not md_path.exists()

    def test_write_mode_creates_json(self, tmp_path):
        generate_system_decision_summary(root=tmp_path, write_files=True)
        json_path = tmp_path / "outputs" / "latest" / "system_decision_summary.json"
        assert json_path.exists()
        data = json.loads(json_path.read_text())
        assert "generated_at" in data

    def test_write_mode_creates_markdown(self, tmp_path):
        generate_system_decision_summary(root=tmp_path, write_files=True)
        md_path = tmp_path / "outputs" / "latest" / "system_decision_summary.md"
        assert md_path.exists()
        text = md_path.read_text()
        assert "# System Decision Summary" in text

    def test_second_run_reads_previous(self, tmp_path):
        # First run: no previous
        r1 = generate_system_decision_summary(root=tmp_path, write_files=True)
        assert r1["changes"]["previous_available"] is False

        # Second run: should find the previous
        r2 = generate_system_decision_summary(root=tmp_path, write_files=True)
        assert r2["changes"]["previous_available"] is True

    def test_missing_artifacts_handled_gracefully(self, tmp_path):
        # All artifact files missing — should not crash
        result = generate_system_decision_summary(root=tmp_path, write_files=False)
        assert isinstance(result, dict)

    def test_missing_artifacts_logged_with_paths_and_producers(self, tmp_path, caplog):
        caplog.set_level("WARNING")
        generate_system_decision_summary(root=tmp_path, write_files=False)
        joined = " ".join(record.getMessage() for record in caplog.records)
        assert "missing required artifacts" in joined
        assert "outputs/latest/watchlist_signals.json" in joined
        assert "watchlist scanner" in joined

    def test_defaulting_and_optional_artifacts_logged_separately(self, tmp_path, caplog):
        caplog.set_level("WARNING")
        latest = tmp_path / "outputs" / "latest"
        perf = tmp_path / "outputs" / "performance"
        latest.mkdir(parents=True, exist_ok=True)
        perf.mkdir(parents=True, exist_ok=True)
        (latest / "watchlist_signals.json").write_text(json.dumps({"results": []}), encoding="utf-8")
        (latest / "theme_signals.json").write_text(json.dumps({"themes": []}), encoding="utf-8")
        (tmp_path / "outputs" / "portfolio").mkdir(parents=True, exist_ok=True)
        (tmp_path / "outputs" / "portfolio" / "portfolio_snapshot.json").write_text(json.dumps({}), encoding="utf-8")
        (perf / "allocation_policy_preview.json").write_text(json.dumps({}), encoding="utf-8")
        (perf / "allocation_policy_simulation.json").write_text(json.dumps({}), encoding="utf-8")
        (perf / "weight_tuning_suggestions.json").write_text(json.dumps({}), encoding="utf-8")

        generate_system_decision_summary(root=tmp_path, write_files=False)
        joined = " ".join(record.getMessage() for record in caplog.records)
        assert "defaulting artifacts not present" in joined
        assert "approved_ranking_config.json" in joined
        assert "approved_allocation_policy.json" in joined
        assert "optional artifacts not present" in joined
        assert "theme_opportunities.json" in joined

    def test_json_schema_valid(self, tmp_path):
        generate_system_decision_summary(root=tmp_path, write_files=True)
        json_path = tmp_path / "outputs" / "latest" / "system_decision_summary.json"
        data = json.loads(json_path.read_text())
        required_keys = [
            "generated_at", "schema_version", "top_theme", "top_opportunity",
            "best_portfolio_fit", "system_state", "capital_preview",
            "policy_insight", "data_health", "changes",
        ]
        for key in required_keys:
            assert key in data, f"Missing key: {key}"

    def test_previous_summary_theme_used_when_current_theme_files_empty(self, tmp_path):
        out_dir = tmp_path / "outputs" / "latest"
        out_dir.mkdir(parents=True)
        (out_dir / "system_decision_summary.json").write_text(
            json.dumps(
                {
                    "generated_at": "2026-04-27T12:00:00",
                    "top_theme": {
                        "name": "AI Infrastructure",
                        "type": "classified",
                        "score": 0.9,
                        "persistence": 0.5,
                        "acceleration": 0.1,
                        "tickers": ["NVDA"],
                    }
                }
            ),
            encoding="utf-8",
        )
        (out_dir / "theme_opportunities.json").write_text(json.dumps({"themes": []}), encoding="utf-8")
        (out_dir / "theme_signals.json").write_text(json.dumps({"themes": []}), encoding="utf-8")

        result = generate_system_decision_summary(root=tmp_path, write_files=False)

        assert result["top_theme"]["name"] == "AI Infrastructure"
        assert result["top_theme"]["theme_source"] == "stale"

    def test_history_theme_used_when_latest_and_previous_summary_are_empty(self, tmp_path):
        latest_dir = tmp_path / "outputs" / "latest"
        history_dir = tmp_path / "outputs" / "history" / "2026-04-26"
        latest_dir.mkdir(parents=True)
        history_dir.mkdir(parents=True)
        (latest_dir / "theme_signals.json").write_text(json.dumps({"themes": []}), encoding="utf-8")
        (history_dir / "theme_signals.json").write_text(
            json.dumps(_engine_themes(_engine_theme("Semicap Equipment", confidence=0.88))),
            encoding="utf-8",
        )

        result = generate_system_decision_summary(root=tmp_path, write_files=False)

        assert result["top_theme"]["name"] == "Semicap Equipment"
        assert result["top_theme"]["theme_source"] == "stale"

    def test_stale_theme_signals_json_produces_populated_top_theme(self, tmp_path):
        """theme_signals.json with no_update=True/stale themes populates top_theme."""
        out_dir = tmp_path / "outputs" / "latest"
        out_dir.mkdir(parents=True)
        stale_payload = {
            **_engine_themes(_engine_theme("Defense", confidence=0.82, persistence_7d=2)),
            "theme_source": "stale",
            "no_update": True,
            "last_checked_at": "2026-04-28T09:00:00+00:00",
        }
        (out_dir / "theme_signals.json").write_text(
            json.dumps(stale_payload), encoding="utf-8"
        )

        result = generate_system_decision_summary(root=tmp_path, write_files=False)

        assert result["top_theme"]["name"] == "Defense"
        assert result["top_theme"]["theme_source"] == "stale"

    def test_top_theme_never_empty_when_any_fallback_has_valid_theme(self, tmp_path):
        """Verify all three fallback tiers work: stale JSON > history > previous summary."""
        out_dir = tmp_path / "outputs" / "latest"
        out_dir.mkdir(parents=True)

        # Tier 1: stale theme_signals.json
        stale = {
            **_engine_themes(_engine_theme("Energy Transition", confidence=0.78)),
            "theme_source": "stale",
            "no_update": True,
        }
        (out_dir / "theme_signals.json").write_text(json.dumps(stale), encoding="utf-8")
        r1 = generate_system_decision_summary(root=tmp_path, write_files=False)
        assert r1["top_theme"]["name"] == "Energy Transition"

        # Tier 2: empty theme_signals.json, previous summary has top_theme
        (out_dir / "theme_signals.json").write_text(json.dumps({"themes": []}), encoding="utf-8")
        prev_summary = {
            "generated_at": "2026-04-27T09:00:00",
            "top_theme": {
                "name": "Healthcare Innovation",
                "type": "classified",
                "score": 0.77,
                "persistence": 0.4,
                "acceleration": 0.0,
                "tickers": [],
            },
        }
        (out_dir / "system_decision_summary.json").write_text(
            json.dumps(prev_summary), encoding="utf-8"
        )
        r2 = generate_system_decision_summary(root=tmp_path, write_files=False)
        assert r2["top_theme"]["name"] == "Healthcare Innovation"
        assert r2["top_theme"]["theme_source"] == "stale"

        # Tier 3: empty latest + empty previous summary, but history dir exists
        (out_dir / "system_decision_summary.json").write_text(
            json.dumps({"generated_at": "2026-04-27T09:00:00", "top_theme": {}}),
            encoding="utf-8",
        )
        hist_dir = tmp_path / "outputs" / "history" / "2026-04-25"
        hist_dir.mkdir(parents=True)
        (hist_dir / "theme_signals.json").write_text(
            json.dumps(_engine_themes(_engine_theme("Industrial Automation", confidence=0.71))),
            encoding="utf-8",
        )
        r3 = generate_system_decision_summary(root=tmp_path, write_files=False)
        assert r3["top_theme"]["name"] == "Industrial Automation"
        assert r3["top_theme"]["theme_source"] == "stale"


# ---------------------------------------------------------------------------
# TestNormalizeThemeRecord — unit tests for _normalize_theme_record
# ---------------------------------------------------------------------------

class TestNormalizeThemeRecord:
    def test_discovery_schema_score_used(self):
        raw = {"name": "AI", "score": 0.85, "type": "classified",
               "persistence_score": 0.60, "acceleration_score": 0.25, "tickers": ["NVDA"]}
        norm = _normalize_theme_record(raw)
        assert norm["score"] == pytest.approx(0.85)
        assert norm["persistence"] == pytest.approx(0.60)
        assert norm["acceleration"] == pytest.approx(0.25)
        assert norm["type"] == "classified"

    def test_engine_schema_confidence_becomes_score(self):
        raw = _engine_theme("Cybersecurity", confidence=0.88, persistence_7d=4)
        norm = _normalize_theme_record(raw)
        assert norm["score"] == pytest.approx(0.88)

    def test_engine_schema_persistence_7d_normalized(self):
        raw = _engine_theme(persistence_7d=7)
        norm = _normalize_theme_record(raw)
        assert norm["persistence"] == pytest.approx(1.0)

    def test_engine_schema_persistence_7d_zero(self):
        raw = _engine_theme(persistence_7d=0)
        norm = _normalize_theme_record(raw)
        assert norm["persistence"] == pytest.approx(0.0)

    def test_engine_schema_persistence_7d_partial(self):
        raw = _engine_theme(persistence_7d=3)
        norm = _normalize_theme_record(raw)
        assert norm["persistence"] == pytest.approx(3.0 / 7.0)

    def test_engine_schema_persistence_7d_capped_at_1(self):
        raw = _engine_theme(persistence_7d=14)
        norm = _normalize_theme_record(raw)
        assert norm["persistence"] == pytest.approx(1.0)

    def test_engine_schema_catalog_match_becomes_type(self):
        raw = _engine_theme("AI Infrastructure", catalog_match="AI Infrastructure")
        norm = _normalize_theme_record(raw)
        assert norm["type"] == "AI Infrastructure"

    def test_engine_schema_no_acceleration(self):
        raw = _engine_theme()
        norm = _normalize_theme_record(raw)
        assert norm["acceleration"] == pytest.approx(0.0)

    def test_tickers_preserved(self):
        raw = _engine_theme(tickers=["NVDA", "AMD", "MSFT"])
        norm = _normalize_theme_record(raw)
        assert norm["tickers"] == ["NVDA", "AMD", "MSFT"]

    def test_missing_name_defaults_to_unknown(self):
        norm = _normalize_theme_record({"confidence": 0.5})
        assert norm["name"] == "Unknown"

    def test_missing_tickers_defaults_to_empty_list(self):
        norm = _normalize_theme_record({"name": "AI", "score": 0.5})
        assert norm["tickers"] == []

    def test_all_missing_fields_returns_safe_defaults(self):
        norm = _normalize_theme_record({})
        assert norm["score"] == pytest.approx(0.0)
        assert norm["persistence"] == pytest.approx(0.0)
        assert norm["acceleration"] == pytest.approx(0.0)
        assert norm["type"] == "classified"
        assert norm["name"] == "Unknown"

    def test_persistence_field_alias_respected(self):
        raw = {"name": "AI", "score": 0.8, "persistence": 0.55}
        norm = _normalize_theme_record(raw)
        assert norm["persistence"] == pytest.approx(0.55)

    def test_output_has_required_keys(self):
        norm = _normalize_theme_record({})
        for key in ("name", "type", "score", "persistence", "acceleration", "tickers"):
            assert key in norm

    def test_idempotent_on_already_normalized_record(self):
        raw = {"name": "AI", "type": "classified", "score": 0.9,
               "persistence": 0.7, "acceleration": 0.1, "tickers": ["NVDA"]}
        norm1 = _normalize_theme_record(raw)
        norm2 = _normalize_theme_record(norm1)
        assert norm1 == norm2


# ---------------------------------------------------------------------------
# TestMergeThemeSources — unit tests for _merge_theme_sources
# ---------------------------------------------------------------------------

class TestMergeThemeSources:
    def test_empty_both_returns_empty(self):
        assert _merge_theme_sources({}, {}) == {}

    def test_empty_engine_uses_discovery(self):
        discovery = _themes(_theme("AI", 0.80))
        result = _merge_theme_sources(discovery, {})
        themes = result["themes"]
        assert len(themes) == 1
        assert themes[0]["name"] == "AI"

    def test_empty_discovery_uses_engine(self):
        engine = _engine_themes(_engine_theme("AI Infrastructure", 0.95))
        result = _merge_theme_sources({}, engine)
        themes = result["themes"]
        assert len(themes) == 1
        assert themes[0]["name"] == "AI Infrastructure"
        assert themes[0]["score"] == pytest.approx(0.95)

    def test_deduplication_higher_score_wins(self):
        discovery = _themes(_theme("AI", 0.70))
        engine = _engine_themes(_engine_theme("AI", 0.90))
        result = _merge_theme_sources(discovery, engine)
        themes = result["themes"]
        ai_themes = [t for t in themes if t["name"] == "AI"]
        assert len(ai_themes) == 1
        assert ai_themes[0]["score"] == pytest.approx(0.90)

    def test_deduplication_lower_score_in_engine_does_not_win(self):
        discovery = _themes(_theme("AI", 0.90))
        engine = _engine_themes(_engine_theme("AI", 0.60))
        result = _merge_theme_sources(discovery, engine)
        ai_themes = [t for t in result["themes"] if t["name"] == "AI"]
        assert ai_themes[0]["score"] == pytest.approx(0.90)

    def test_non_overlapping_themes_all_included(self):
        discovery = _themes(_theme("Energy", 0.70))
        engine = _engine_themes(_engine_theme("AI Infrastructure", 0.95))
        result = _merge_theme_sources(discovery, engine)
        names = {t["name"] for t in result["themes"]}
        assert "Energy" in names
        assert "AI Infrastructure" in names

    def test_both_empty_themes_lists_returns_empty(self):
        assert _merge_theme_sources({"themes": []}, {"themes": []}) == {}

    def test_malformed_entry_skipped(self):
        engine = {"themes": ["not_a_dict", None, {"name": "AI", "confidence": 0.9}]}
        result = _merge_theme_sources({}, engine)
        assert len(result["themes"]) == 1
        assert result["themes"][0]["name"] == "AI"


# ---------------------------------------------------------------------------
# TestComputeTopThemeSchemas — regression tests for both artifact schemas
# ---------------------------------------------------------------------------

class TestComputeTopThemeSchemas:
    def test_discovery_schema_top_theme_populated(self):
        themes = _themes(_theme("AI", 0.80), _theme("Energy", 0.90))
        result = compute_top_theme(themes)
        assert result.get("name") == "Energy"
        assert result.get("score") == pytest.approx(0.90)

    def test_engine_schema_top_theme_not_blank(self):
        """Regression: confidence-based schema must populate top_theme (was returning {} before fix)."""
        themes = _engine_themes(
            _engine_theme("AI Infrastructure", confidence=0.95, persistence_7d=3),
            _engine_theme("Cybersecurity", confidence=0.88, persistence_7d=1),
        )
        result = compute_top_theme(themes)
        assert result.get("name") == "AI Infrastructure"
        assert result.get("score") == pytest.approx(0.95)

    def test_engine_schema_score_not_zero(self):
        themes = _engine_themes(_engine_theme("Defense", confidence=0.75))
        result = compute_top_theme(themes)
        assert result["score"] > 0.0

    def test_engine_schema_result_shape(self):
        themes = _engine_themes(_engine_theme())
        result = compute_top_theme(themes)
        for key in ("name", "type", "score", "persistence", "acceleration", "tickers"):
            assert key in result

    def test_missing_theme_files_returns_empty(self):
        result = compute_top_theme({})
        assert result == {}

    def test_malformed_theme_entry_does_not_crash(self):
        themes = {"themes": [None, "bad", 42, {"name": "AI", "confidence": 0.7}]}
        result = compute_top_theme(themes)
        assert result.get("name") == "AI"

    def test_build_summary_with_engine_themes_artifact(self, tmp_path):
        """End-to-end: write theme_signals.json, run generate, assert top_theme populated."""
        out_dir = tmp_path / "outputs" / "latest"
        out_dir.mkdir(parents=True)
        signals_data = {
            "generated_at": "2026-04-27T20:00:00",
            "run_date": "2026-04-27",
            "themes": [
                {"name": "AI Infrastructure", "confidence": 0.95,
                 "persistence_7d": 3, "catalog_match": "AI Infrastructure",
                 "tickers": ["NVDA", "AMD"]},
                {"name": "Defense", "confidence": 0.75,
                 "persistence_7d": 1, "catalog_match": "Defense",
                 "tickers": ["LMT", "RTX"]},
            ],
        }
        (out_dir / "theme_signals.json").write_text(
            json.dumps(signals_data), encoding="utf-8"
        )
        result = generate_system_decision_summary(root=tmp_path, write_files=False)
        assert result["top_theme"].get("name") == "AI Infrastructure", (
            f"top_theme should be 'AI Infrastructure', got: {result['top_theme']}"
        )
        assert result["top_theme"]["score"] == pytest.approx(0.95)

    def test_build_summary_with_only_discovery_artifact(self, tmp_path):
        """theme_opportunities.json (no theme_signals.json) still populates top_theme."""
        out_dir = tmp_path / "outputs" / "latest"
        out_dir.mkdir(parents=True)
        opp_data = {
            "generated_at": "2026-04-27T20:00:00",
            "themes": [
                {"name": "Semicap Equipment", "theme_type": "classified",
                 "score": 0.82, "persistence_score": 0.50, "acceleration_score": 0.10,
                 "tickers": ["AMAT", "LRCX"]},
            ],
        }
        (out_dir / "theme_opportunities.json").write_text(
            json.dumps(opp_data), encoding="utf-8"
        )
        result = generate_system_decision_summary(root=tmp_path, write_files=False)
        assert result["top_theme"].get("name") == "Semicap Equipment"

    def test_artifact_flags_include_theme_signals(self, tmp_path):
        """data_health.artifact_flags must include theme_signals key."""
        result = generate_system_decision_summary(root=tmp_path, write_files=False)
        flags = result["data_health"]["artifact_flags"]
        assert "theme_signals" in flags
        assert "theme_data_available" in flags

    def test_theme_data_available_true_when_engine_file_exists(self, tmp_path):
        out_dir = tmp_path / "outputs" / "latest"
        out_dir.mkdir(parents=True)
        signals_data = {"themes": [{"name": "AI", "confidence": 0.9, "tickers": []}]}
        (out_dir / "theme_signals.json").write_text(json.dumps(signals_data), encoding="utf-8")
        result = generate_system_decision_summary(root=tmp_path, write_files=False)
        flags = result["data_health"]["artifact_flags"]
        assert flags["theme_signals"] is True
        assert flags["theme_data_available"] is True
