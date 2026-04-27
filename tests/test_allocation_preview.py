from __future__ import annotations

import json
import copy
from pathlib import Path

import pytest

from watchlist_scanner.allocation_preview import (
    MULTIPLIER_GOOD,
    MULTIPLIER_NEUTRAL,
    MULTIPLIER_POOR,
    MULTIPLIER_STRONG,
    RANK_GOOD,
    RANK_NEUTRAL,
    RANK_STRONG,
    _build_reason,
    _rank_multiplier,
    _sector_from_signal,
    build_allocation_preview,
    generate_allocation_preview_report,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _signal(
    *,
    ticker: str = "NVDA",
    filter_allowed: bool = True,
    confidence_score: float = 0.80,
    final_rank_score: float = 0.70,
    portfolio_fit_label: str = "good",
    sector: str | None = None,
    fundamentals_sector: str | None = "TECHNOLOGY",
    themes: list[str] | None = None,
) -> dict:
    sig: dict = {
        "ticker": ticker,
        "filter_allowed": filter_allowed,
        "confidence_score": confidence_score,
        "final_rank_score": final_rank_score,
        "portfolio_fit_label": portfolio_fit_label,
        "themes": themes or ["AI", "Cloud"],
    }
    if fundamentals_sector is not None:
        sig["fundamentals"] = {"sector": fundamentals_sector}
    if sector is not None:
        sig["sector"] = sector
    return sig


def _snapshot(**overrides) -> dict:
    base: dict = {
        "config": {
            "baseline_position_pct": 0.02,
            "max_ticker_allocation": 0.08,
            "max_sector_allocation": 0.20,
            "max_total_allocation": 0.40,
        },
        "allocation_by_sector": {
            "TECHNOLOGY": 0.05,
            "CONSUMER CYCLICAL": 0.03,
        },
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# TestRankMultiplier
# ---------------------------------------------------------------------------

class TestRankMultiplier:
    def test_strong_returns_strong_multiplier(self):
        m, label = _rank_multiplier(RANK_STRONG)
        assert m == MULTIPLIER_STRONG
        assert label == "strong"

    def test_strong_just_above_threshold(self):
        m, label = _rank_multiplier(RANK_STRONG + 0.01)
        assert m == MULTIPLIER_STRONG

    def test_good_lower_bound(self):
        m, label = _rank_multiplier(RANK_GOOD)
        assert m == MULTIPLIER_GOOD
        assert label == "good"

    def test_good_just_below_strong(self):
        m, label = _rank_multiplier(RANK_STRONG - 0.01)
        assert m == MULTIPLIER_GOOD

    def test_neutral_lower_bound(self):
        m, label = _rank_multiplier(RANK_NEUTRAL)
        assert m == MULTIPLIER_NEUTRAL
        assert label == "neutral"

    def test_neutral_just_below_good(self):
        m, label = _rank_multiplier(RANK_GOOD - 0.01)
        assert m == MULTIPLIER_NEUTRAL

    def test_poor_below_neutral(self):
        m, label = _rank_multiplier(RANK_NEUTRAL - 0.01)
        assert m == MULTIPLIER_POOR
        assert label == "poor"

    def test_poor_at_zero(self):
        m, label = _rank_multiplier(0.0)
        assert m == MULTIPLIER_POOR

    def test_four_distinct_multipliers(self):
        values = {MULTIPLIER_STRONG, MULTIPLIER_GOOD, MULTIPLIER_NEUTRAL, MULTIPLIER_POOR}
        assert len(values) == 4


# ---------------------------------------------------------------------------
# TestSectorFromSignal
# ---------------------------------------------------------------------------

class TestSectorFromSignal:
    def test_reads_from_fundamentals(self):
        sig = {"fundamentals": {"sector": "technology"}}
        assert _sector_from_signal(sig) == "TECHNOLOGY"

    def test_falls_back_to_flat_sector(self):
        sig = {"sector": "Health Care"}
        assert _sector_from_signal(sig) == "HEALTH CARE"

    def test_fundamentals_takes_priority(self):
        sig = {"fundamentals": {"sector": "Energy"}, "sector": "Other"}
        assert _sector_from_signal(sig) == "ENERGY"

    def test_empty_signal_returns_unknown(self):
        assert _sector_from_signal({}) == "UNKNOWN"

    def test_none_sector_returns_unknown(self):
        sig = {"fundamentals": {"sector": None}}
        assert _sector_from_signal(sig) == "UNKNOWN"


# ---------------------------------------------------------------------------
# TestBuildAllocationPreviewFiltering
# ---------------------------------------------------------------------------

class TestBuildAllocationPreviewFiltering:
    def test_empty_signals_returns_empty_opportunities(self):
        result = build_allocation_preview([], _snapshot())
        assert result["opportunities"] == []

    def test_filter_allowed_false_excluded(self):
        sigs = [_signal(filter_allowed=False)]
        result = build_allocation_preview(sigs, _snapshot())
        assert result["opportunities"] == []

    def test_confidence_below_threshold_excluded(self):
        sigs = [_signal(confidence_score=0.49, final_rank_score=0.80)]
        result = build_allocation_preview(sigs, _snapshot(), confidence_threshold=0.50)
        assert result["opportunities"] == []

    def test_confidence_at_threshold_included(self):
        sigs = [_signal(confidence_score=0.50)]
        result = build_allocation_preview(sigs, _snapshot(), confidence_threshold=0.50)
        assert len(result["opportunities"]) == 1

    def test_filter_allowed_true_included(self):
        sigs = [_signal(filter_allowed=True, confidence_score=0.75)]
        result = build_allocation_preview(sigs, _snapshot())
        assert len(result["opportunities"]) == 1

    def test_mixed_signals_only_eligible_included(self):
        sigs = [
            _signal(ticker="A", filter_allowed=True, confidence_score=0.80),
            _signal(ticker="B", filter_allowed=False, confidence_score=0.80),
            _signal(ticker="C", filter_allowed=True, confidence_score=0.30),
        ]
        result = build_allocation_preview(sigs, _snapshot(), confidence_threshold=0.50)
        tickers = {o["ticker"] for o in result["opportunities"]}
        assert tickers == {"A"}


# ---------------------------------------------------------------------------
# TestBuildAllocationPreviewSizing
# ---------------------------------------------------------------------------

class TestBuildAllocationPreviewSizing:
    def test_strong_rank_applies_strong_multiplier(self):
        sigs = [_signal(final_rank_score=0.80)]
        snap = _snapshot()
        baseline = snap["config"]["baseline_position_pct"]
        result = build_allocation_preview(sigs, snap)
        opp = result["opportunities"][0]
        assert opp["rank_multiplier"] == MULTIPLIER_STRONG
        assert opp["baseline_size"] == pytest.approx(baseline)
        assert opp["preview_size"] == pytest.approx(round(baseline * MULTIPLIER_STRONG, 4))

    def test_good_rank_applies_good_multiplier(self):
        sigs = [_signal(final_rank_score=0.60)]
        snap = _snapshot()
        baseline = snap["config"]["baseline_position_pct"]
        result = build_allocation_preview(sigs, snap)
        opp = result["opportunities"][0]
        assert opp["rank_multiplier"] == MULTIPLIER_GOOD
        assert opp["preview_size"] == pytest.approx(round(baseline * MULTIPLIER_GOOD, 4))

    def test_neutral_rank_no_multiplier_change(self):
        sigs = [_signal(final_rank_score=0.40)]
        snap = _snapshot()
        baseline = snap["config"]["baseline_position_pct"]
        result = build_allocation_preview(sigs, snap)
        opp = result["opportunities"][0]
        assert opp["rank_multiplier"] == MULTIPLIER_NEUTRAL
        assert opp["preview_size"] == pytest.approx(baseline)

    def test_poor_rank_reduces_size(self):
        sigs = [_signal(final_rank_score=0.20)]
        snap = _snapshot()
        baseline = snap["config"]["baseline_position_pct"]
        result = build_allocation_preview(sigs, snap)
        opp = result["opportunities"][0]
        assert opp["rank_multiplier"] == MULTIPLIER_POOR
        assert opp["preview_size"] == pytest.approx(round(baseline * MULTIPLIER_POOR, 4))

    def test_sorted_by_final_rank_score_descending(self):
        sigs = [
            _signal(ticker="A", final_rank_score=0.50),
            _signal(ticker="C", final_rank_score=0.90),
            _signal(ticker="B", final_rank_score=0.70),
        ]
        result = build_allocation_preview(sigs, _snapshot())
        tickers = [o["ticker"] for o in result["opportunities"]]
        scores = [o["final_rank_score"] for o in result["opportunities"]]
        assert tickers == ["C", "B", "A"]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# TestCapsEnforced
# ---------------------------------------------------------------------------

class TestCapsEnforced:
    def test_max_ticker_cap_applied(self):
        snap = _snapshot()
        snap["config"]["max_ticker_allocation"] = 0.01  # very tight cap
        sigs = [_signal(final_rank_score=0.80)]  # would give 0.02 * 1.25 = 0.025
        result = build_allocation_preview(sigs, snap)
        opp = result["opportunities"][0]
        assert opp["preview_size"] <= 0.01
        assert "max_position_cap" in opp["capped_by"]

    def test_sector_cap_applied(self):
        snap = _snapshot()
        # existing TECHNOLOGY exposure = 0.19, cap = 0.20 → headroom = 0.01
        snap["allocation_by_sector"] = {"TECHNOLOGY": 0.19}
        snap["config"]["max_sector_allocation"] = 0.20
        snap["config"]["baseline_position_pct"] = 0.02
        sigs = [_signal(final_rank_score=0.80, fundamentals_sector="TECHNOLOGY")]
        result = build_allocation_preview(sigs, snap)
        opp = result["opportunities"][0]
        assert opp["preview_size"] <= 0.01 + 1e-9
        assert "sector_cap" in opp["capped_by"]

    def test_total_allocation_cap_applied(self):
        snap = _snapshot()
        snap["config"]["max_total_allocation"] = 0.01  # almost no room
        snap["config"]["baseline_position_pct"] = 0.02
        sigs = [
            _signal(ticker="A", final_rank_score=0.80),
            _signal(ticker="B", final_rank_score=0.70),
        ]
        result = build_allocation_preview(sigs, snap)
        total = sum(o["preview_size"] for o in result["opportunities"])
        assert total <= 0.01 + 1e-9

    def test_total_cap_capped_by_label_present(self):
        snap = _snapshot()
        snap["config"]["max_total_allocation"] = 0.01
        snap["config"]["baseline_position_pct"] = 0.02
        sigs = [
            _signal(ticker="A", final_rank_score=0.80),
            _signal(ticker="B", final_rank_score=0.70),
        ]
        result = build_allocation_preview(sigs, snap)
        capped_opps = [o for o in result["opportunities"] if "total_cap" in o["capped_by"]]
        assert len(capped_opps) >= 1

    def test_sector_headroom_cumulative_across_candidates(self):
        # Two tech stocks; together they should not exceed sector cap
        snap = _snapshot()
        snap["allocation_by_sector"] = {"TECHNOLOGY": 0.15}
        snap["config"]["max_sector_allocation"] = 0.20  # headroom = 0.05
        snap["config"]["baseline_position_pct"] = 0.04  # 0.04 * 1.25 = 0.05 each
        sigs = [
            _signal(ticker="A", final_rank_score=0.80, fundamentals_sector="TECHNOLOGY"),
            _signal(ticker="B", final_rank_score=0.79, fundamentals_sector="TECHNOLOGY"),
        ]
        result = build_allocation_preview(sigs, snap)
        tech_total = sum(o["preview_size"] for o in result["opportunities"])
        assert tech_total <= 0.05 + 1e-9

    def test_no_cap_when_within_limits(self):
        snap = _snapshot()
        sigs = [_signal(final_rank_score=0.60)]  # 0.02 * 1.10 = 0.022, well within caps
        result = build_allocation_preview(sigs, snap)
        opp = result["opportunities"][0]
        assert opp["capped_by"] == []


# ---------------------------------------------------------------------------
# TestOutputShape
# ---------------------------------------------------------------------------

class TestOutputShape:
    def test_observe_only_flag(self):
        result = build_allocation_preview([], _snapshot())
        assert result["observe_only"] is True

    def test_not_applied_flag(self):
        result = build_allocation_preview([], _snapshot())
        assert result["not_applied"] is True

    def test_required_top_level_keys(self):
        result = build_allocation_preview([], _snapshot())
        required = {
            "generated_at", "observe_only", "not_applied",
            "confidence_threshold", "candidate_count",
            "total_baseline_pct", "total_preview_pct", "opportunities",
        }
        assert required.issubset(result.keys())

    def test_opportunity_required_keys(self):
        sigs = [_signal(final_rank_score=0.70)]
        result = build_allocation_preview(sigs, _snapshot())
        opp = result["opportunities"][0]
        required = {
            "ticker", "final_rank_score", "rank_label", "rank_multiplier",
            "baseline_size", "preview_size", "capped_by",
            "sector", "confidence_score", "portfolio_fit_label", "reason",
        }
        assert required.issubset(opp.keys())

    def test_output_is_json_serializable(self):
        sigs = [_signal(final_rank_score=0.65)]
        result = build_allocation_preview(sigs, _snapshot())
        parsed = json.loads(json.dumps(result))
        assert parsed["observe_only"] is True

    def test_candidate_count_matches_opportunities(self):
        sigs = [_signal(ticker=f"T{i}", final_rank_score=0.6 - i * 0.05) for i in range(3)]
        result = build_allocation_preview(sigs, _snapshot())
        assert result["candidate_count"] == len(result["opportunities"])

    def test_total_preview_pct_matches_sum(self):
        sigs = [_signal(ticker="A", final_rank_score=0.70), _signal(ticker="B", final_rank_score=0.60)]
        result = build_allocation_preview(sigs, _snapshot())
        expected = round(sum(o["preview_size"] for o in result["opportunities"]), 4)
        assert result["total_preview_pct"] == pytest.approx(expected)

    def test_reason_field_is_non_empty_string(self):
        sigs = [_signal(final_rank_score=0.70)]
        result = build_allocation_preview(sigs, _snapshot())
        assert isinstance(result["opportunities"][0]["reason"], str)
        assert len(result["opportunities"][0]["reason"]) > 0


# ---------------------------------------------------------------------------
# TestNoMutation
# ---------------------------------------------------------------------------

class TestNoMutation:
    def test_portfolio_snapshot_not_mutated(self):
        snap = _snapshot()
        snap_copy = copy.deepcopy(snap)
        sigs = [
            _signal(ticker="A", final_rank_score=0.80, fundamentals_sector="TECHNOLOGY"),
            _signal(ticker="B", final_rank_score=0.70, fundamentals_sector="TECHNOLOGY"),
        ]
        build_allocation_preview(sigs, snap)
        assert snap == snap_copy

    def test_signals_not_mutated(self):
        sig = _signal(final_rank_score=0.70)
        sig_copy = copy.deepcopy(sig)
        build_allocation_preview([sig], _snapshot())
        assert sig == sig_copy

    def test_allocation_by_sector_original_unchanged(self):
        snap = _snapshot()
        original_sector = dict(snap["allocation_by_sector"])
        sigs = [_signal(final_rank_score=0.70, fundamentals_sector="TECHNOLOGY")]
        build_allocation_preview(sigs, snap)
        assert snap["allocation_by_sector"] == original_sector


# ---------------------------------------------------------------------------
# TestGenerateReport (file I/O)
# ---------------------------------------------------------------------------

class TestGenerateReport:
    def test_missing_signals_produces_empty_output(self, tmp_path):
        # No signals file — should still produce valid output
        (tmp_path / "outputs" / "portfolio").mkdir(parents=True)
        snap = _snapshot()
        (tmp_path / "outputs" / "portfolio" / "portfolio_snapshot.json").write_text(
            json.dumps(snap), encoding="utf-8"
        )
        result = generate_allocation_preview_report(
            root=tmp_path, output_dir=tmp_path / "outputs" / "performance"
        )
        assert result["opportunities"] == []
        assert (tmp_path / "outputs" / "performance" / "allocation_policy_preview.json").exists()

    def test_report_writes_valid_json(self, tmp_path):
        _setup_tmp(tmp_path, signals=[_signal(final_rank_score=0.70)])
        generate_allocation_preview_report(
            root=tmp_path, output_dir=tmp_path / "outputs" / "performance"
        )
        out = json.loads(
            (tmp_path / "outputs" / "performance" / "allocation_policy_preview.json").read_text()
        )
        assert out["observe_only"] is True

    def test_report_includes_opportunities(self, tmp_path):
        _setup_tmp(tmp_path, signals=[_signal(final_rank_score=0.70)])
        result = generate_allocation_preview_report(
            root=tmp_path, output_dir=tmp_path / "outputs" / "performance"
        )
        assert len(result["opportunities"]) == 1

    def test_signals_from_results_key(self, tmp_path):
        # watchlist_signals.json wraps signals under a "results" key
        _setup_tmp(tmp_path, signals=[_signal(ticker="NVDA", final_rank_score=0.80)])
        result = generate_allocation_preview_report(
            root=tmp_path, output_dir=tmp_path / "outputs" / "performance"
        )
        assert result["opportunities"][0]["ticker"] == "NVDA"


# ---------------------------------------------------------------------------
# Helper for file-based tests
# ---------------------------------------------------------------------------

def _setup_tmp(tmp_path: Path, signals: list[dict]) -> None:
    """Write minimal watchlist_signals.json and portfolio_snapshot.json."""
    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    portfolio = tmp_path / "outputs" / "portfolio"
    portfolio.mkdir(parents=True, exist_ok=True)

    scan_result = {"results": signals, "alerts": []}
    (latest / "watchlist_signals.json").write_text(
        json.dumps(scan_result), encoding="utf-8"
    )
    (portfolio / "portfolio_snapshot.json").write_text(
        json.dumps(_snapshot()), encoding="utf-8"
    )
