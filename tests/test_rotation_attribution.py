"""
Tests for rotation attribution observe-only layer.

Covers:
  A. Rotation event serialization
  B. Margin band assignment
  C. Strategy grouping
  D. Sparse history handling
  E. Recommendation generation
  F. Observe-only guarantee
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from rotation_event_logger import (
    RotationEventRecord,
    append_rotation_events,
    load_rotation_events,
)
from profit_attribution.rotation_attribution import (
    MIN_EVENTS_FOR_RECOMMENDATION,
    SMALL_SAMPLE,
    MarginBandSummary,
    RotationAttributionSummary,
    StrategyRotationSummary,
    assign_margin_band,
    build_rotation_memo,
    evaluate_rotation_attribution,
    write_rotation_reports,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _exit_dict(
    symbol: str = "AAPL",
    strategy_type: str = "momentum",
    incumbent_score: float = 60.0,
    challenger_score: float = 75.0,
    required_margin: float = 12.0,
    rotation_triggered: bool = True,
    challenger_symbol: str | None = None,
    challenger_events: list | None = None,
) -> dict[str, Any]:
    """Build a minimal ExitSuggestion.to_dict()-style dict for testing."""
    actual_margin = round(challenger_score - incumbent_score, 2)
    return {
        "symbol": symbol,
        "action": "SELL" if rotation_triggered else "HOLD",
        "strategy_type": strategy_type,
        "reasons": [],
        "triggers": ["opportunity_rotation"] if rotation_triggered else [],
        "rotation_detail": {
            "incumbent_score": incumbent_score,
            "challenger_score": challenger_score,
            "actual_margin": actual_margin,
            "required_margin": required_margin,
            "rotation_triggered": rotation_triggered,
            "score_basis": "composite_0_to_100",
        },
        "challenger_symbol": challenger_symbol,
        "challenger_events": challenger_events or [],
    }


def _event_dict(
    symbol: str = "AAPL",
    strategy_type: str = "momentum",
    actual_margin: float = 15.0,
    required_margin: float = 12.0,
    rotation_triggered: bool = True,
    challenger_is_breakout: bool = False,
    forward_return_5d: float | None = None,
    outcome_resolved: bool = False,
    run_id: str = "test_run",
) -> dict[str, Any]:
    """Build a raw rotation event dict as stored in JSONL."""
    return {
        "event_id": f"{symbol}_{run_id}",
        "timestamp": "2026-04-17T10:00:00",
        "run_id": run_id,
        "symbol": symbol,
        "strategy_type": strategy_type,
        "incumbent_score": 60.0,
        "challenger_score": round(60.0 + actual_margin, 2),
        "actual_margin": actual_margin,
        "required_margin": required_margin,
        "rotation_triggered": rotation_triggered,
        "score_basis": "composite_0_to_100",
        "challenger_symbol": None,
        "challenger_is_breakout": challenger_is_breakout,
        "degraded_mode": False,
        "drawdown_regime": "normal",
        "forward_return_5d": forward_return_5d,
        "outcome_resolved": outcome_resolved,
    }


def _write_events_to_tmp(events: list[dict]) -> Path:
    """Write events to a temp JSONL file and return the path."""
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
    )
    for e in events:
        tmp.write(json.dumps(e) + "\n")
    tmp.close()
    return Path(tmp.name)


def _enough_events(
    n: int = MIN_EVENTS_FOR_RECOMMENDATION,
    triggered_frac: float = 0.5,
    near_frac: float = 0.2,
    strategy: str = "momentum",
) -> list[dict]:
    """Build a list of n events for recommendation tests."""
    events = []
    for i in range(n):
        triggered = i < int(n * triggered_frac)
        if triggered:
            near = i < int(n * triggered_frac * near_frac)
            actual = 12.5 if near else 20.0   # near_threshold gap=0.5; moderate gap=8
        else:
            actual = 5.0   # below threshold: 5-12 < 0
        events.append(_event_dict(
            symbol=f"SYM{i}",
            strategy_type=strategy,
            actual_margin=actual,
            required_margin=12.0,
            rotation_triggered=triggered,
            run_id=f"run_{i}",
        ))
    return events


# ---------------------------------------------------------------------------
# A. Rotation event serialization
# ---------------------------------------------------------------------------

class TestRotationEventSerialization:

    def test_to_dict_has_all_required_keys(self):
        record = RotationEventRecord(
            event_id="AAPL_run1",
            timestamp="2026-04-17T10:00:00",
            run_id="run1",
            symbol="AAPL",
            strategy_type="momentum",
            incumbent_score=60.0,
            challenger_score=75.0,
            actual_margin=15.0,
            required_margin=12.0,
            rotation_triggered=True,
            score_basis="composite_0_to_100",
            challenger_symbol=None,
            challenger_is_breakout=False,
            degraded_mode=False,
            drawdown_regime="normal",
        )
        d = record.to_dict()
        for key in (
            "event_id", "timestamp", "run_id", "symbol", "strategy_type",
            "incumbent_score", "challenger_score", "actual_margin",
            "required_margin", "rotation_triggered", "score_basis",
            "challenger_symbol", "challenger_is_breakout",
            "degraded_mode", "drawdown_regime",
            "forward_return_5d", "outcome_resolved",
        ):
            assert key in d, f"missing key: {key}"

    def test_to_dict_values_match(self):
        record = RotationEventRecord(
            event_id="MSFT_run2",
            timestamp="2026-04-17T11:00:00",
            run_id="run2",
            symbol="MSFT",
            strategy_type="compounder",
            incumbent_score=80.0,
            challenger_score=92.0,
            actual_margin=12.0,
            required_margin=25.0,
            rotation_triggered=False,
            score_basis="composite_0_to_100",
            challenger_symbol="NVDA",
            challenger_is_breakout=True,
            degraded_mode=True,
            drawdown_regime="modest_dip",
        )
        d = record.to_dict()
        assert d["incumbent_score"] == 80.0
        assert d["challenger_score"] == 92.0
        assert d["actual_margin"] == 12.0
        assert d["required_margin"] == 25.0
        assert d["rotation_triggered"] is False
        assert d["challenger_symbol"] == "NVDA"
        assert d["challenger_is_breakout"] is True
        assert d["outcome_resolved"] is False
        assert d["forward_return_5d"] is None

    def test_append_dry_run_returns_count_not_zero(self):
        results = [
            _exit_dict("AAPL", rotation_triggered=True),
            _exit_dict("GOOGL", rotation_triggered=False),
        ]
        count = append_rotation_events(
            results,
            run_id="test_run",
            dry_run=True,
        )
        assert count == 2

    def test_append_skips_empty_rotation_detail(self):
        results = [
            {"symbol": "HOLD", "strategy_type": "momentum", "rotation_detail": {}},
            _exit_dict("AAPL"),
        ]
        count = append_rotation_events(results, run_id="run1", dry_run=True)
        assert count == 1

    def test_append_empty_list_returns_zero(self):
        assert append_rotation_events([], run_id="run1", dry_run=True) == 0

    def test_append_writes_to_file_and_loads_back(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "rotation_events.jsonl"
            results = [_exit_dict("AAPL"), _exit_dict("MSFT", rotation_triggered=False)]
            count = append_rotation_events(
                results, run_id="r1", history_path=path
            )
            assert count == 2
            loaded = load_rotation_events(path)
            assert len(loaded) == 2
            symbols = {e["symbol"] for e in loaded}
            assert "AAPL" in symbols
            assert "MSFT" in symbols

    def test_challenger_is_breakout_detected_from_events(self):
        result = _exit_dict(
            "NVDA",
            challenger_events=["BREAKOUT_PROXY", "STRONG_MOVE_UP"],
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "rot.jsonl"
            append_rotation_events([result], run_id="r1", history_path=path)
            loaded = load_rotation_events(path)
        assert loaded[0]["challenger_is_breakout"] is True

    def test_challenger_not_breakout_when_no_breakout_event(self):
        result = _exit_dict("TSLA", challenger_events=["STRONG_MOVE_UP"])
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "rot.jsonl"
            append_rotation_events([result], run_id="r1", history_path=path)
            loaded = load_rotation_events(path)
        assert loaded[0]["challenger_is_breakout"] is False


# ---------------------------------------------------------------------------
# B. Margin band assignment
# ---------------------------------------------------------------------------

class TestMarginBandAssignment:

    def test_below_threshold_when_actual_below_required(self):
        # actual=10, required=12 → gap=-2 → below_threshold
        assert assign_margin_band(10.0, 12.0) == "below_threshold"

    def test_at_required_exactly_is_near_threshold(self):
        # gap=0 → near_threshold
        assert assign_margin_band(12.0, 12.0) == "near_threshold"

    def test_near_threshold_within_4pts(self):
        # gap=3 → near_threshold
        assert assign_margin_band(15.0, 12.0) == "near_threshold"

    def test_near_threshold_upper_boundary_exclusive(self):
        # gap=3.99 → still near_threshold
        assert assign_margin_band(15.99, 12.0) == "near_threshold"

    def test_moderate_at_4pts_above(self):
        # gap=4.0 → moderate
        assert assign_margin_band(16.0, 12.0) == "moderate"

    def test_moderate_within_band(self):
        # gap=7 → moderate
        assert assign_margin_band(19.0, 12.0) == "moderate"

    def test_moderate_upper_boundary_exclusive(self):
        # gap=9.99 → moderate
        assert assign_margin_band(21.99, 12.0) == "moderate"

    def test_strong_at_10pts_above(self):
        # gap=10 → strong
        assert assign_margin_band(22.0, 12.0) == "strong"

    def test_strong_well_above(self):
        # gap=25 → strong
        assert assign_margin_band(37.0, 12.0) == "strong"

    def test_compounder_threshold_25_below(self):
        # actual=30, required=25 → gap=5 → moderate
        assert assign_margin_band(30.0, 25.0) == "moderate"

    def test_compounder_threshold_25_near(self):
        # actual=26, required=25 → gap=1 → near_threshold
        assert assign_margin_band(26.0, 25.0) == "near_threshold"


# ---------------------------------------------------------------------------
# C. Strategy grouping
# ---------------------------------------------------------------------------

class TestStrategyGrouping:

    def test_momentum_and_compounder_aggregate_separately(self):
        events = [
            _event_dict("AAPL", strategy_type="momentum", actual_margin=15.0),
            _event_dict("GOOGL", strategy_type="momentum", actual_margin=8.0,
                        rotation_triggered=False),
            _event_dict("MSFT", strategy_type="compounder", actual_margin=30.0),
        ]
        path = _write_events_to_tmp(events)
        try:
            summary = evaluate_rotation_attribution(path)
        finally:
            path.unlink(missing_ok=True)

        labels = {s.label for s in summary.by_strategy_type}
        assert "momentum" in labels
        assert "compounder" in labels

    def test_strategy_counts_are_correct(self):
        events = [
            _event_dict(f"M{i}", strategy_type="momentum") for i in range(3)
        ] + [
            _event_dict(f"C{i}", strategy_type="compounder") for i in range(2)
        ]
        path = _write_events_to_tmp(events)
        try:
            summary = evaluate_rotation_attribution(path)
        finally:
            path.unlink(missing_ok=True)

        m = next(s for s in summary.by_strategy_type if s.label == "momentum")
        c = next(s for s in summary.by_strategy_type if s.label == "compounder")
        assert m.total_events == 3
        assert c.total_events == 2

    def test_trigger_rate_correct_per_strategy(self):
        # 3 momentum events: 2 triggered
        events = [
            _event_dict("M1", strategy_type="momentum", rotation_triggered=True,
                        actual_margin=15.0),
            _event_dict("M2", strategy_type="momentum", rotation_triggered=True,
                        actual_margin=15.0),
            _event_dict("M3", strategy_type="momentum", rotation_triggered=False,
                        actual_margin=5.0),
        ]
        path = _write_events_to_tmp(events)
        try:
            summary = evaluate_rotation_attribution(path)
        finally:
            path.unlink(missing_ok=True)

        m = next(s for s in summary.by_strategy_type if s.label == "momentum")
        assert m.triggered_count == 2
        assert m.trigger_rate == pytest.approx(2 / 3, abs=0.001)

    def test_near_threshold_count_only_in_triggered(self):
        events = [
            # Triggered, near_threshold (gap = 12.5 - 12 = 0.5)
            _event_dict("A", actual_margin=12.5, required_margin=12.0,
                        rotation_triggered=True),
            # Triggered, moderate (gap = 18 - 12 = 6)
            _event_dict("B", actual_margin=18.0, required_margin=12.0,
                        rotation_triggered=True),
            # Not triggered → should not count toward near_threshold
            _event_dict("C", actual_margin=12.2, required_margin=12.0,
                        rotation_triggered=False),
        ]
        path = _write_events_to_tmp(events)
        try:
            summary = evaluate_rotation_attribution(path)
        finally:
            path.unlink(missing_ok=True)

        m = next(s for s in summary.by_strategy_type if s.label == "momentum")
        assert m.near_threshold_count == 1

    def test_challenger_type_groups_breakout_separately(self):
        events = [
            _event_dict("A", challenger_is_breakout=True,  rotation_triggered=True,
                        actual_margin=15.0),
            _event_dict("B", challenger_is_breakout=False, rotation_triggered=True,
                        actual_margin=15.0),
            _event_dict("C", challenger_is_breakout=False, rotation_triggered=False,
                        actual_margin=5.0),
        ]
        path = _write_events_to_tmp(events)
        try:
            summary = evaluate_rotation_attribution(path)
        finally:
            path.unlink(missing_ok=True)

        bo = next(c for c in summary.by_challenger_type if c.label == "breakout")
        nb = next(c for c in summary.by_challenger_type if c.label == "non_breakout")
        assert bo.total_events == 1
        assert nb.total_events == 2

    def test_margin_band_totals_sum_to_total_events(self):
        events = _enough_events(n=20, triggered_frac=0.6)
        path = _write_events_to_tmp(events)
        try:
            summary = evaluate_rotation_attribution(path)
        finally:
            path.unlink(missing_ok=True)

        band_total = sum(b.total_events for b in summary.by_margin_band)
        assert band_total == summary.total_events


# ---------------------------------------------------------------------------
# D. Sparse history handling
# ---------------------------------------------------------------------------

class TestSparseHistory:

    def test_zero_records_returns_valid_summary(self):
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tmp:
            path = Path(tmp.name)
        try:
            summary = evaluate_rotation_attribution(path)
        finally:
            path.unlink(missing_ok=True)

        assert isinstance(summary, RotationAttributionSummary)
        assert summary.total_events == 0
        assert summary.total_triggered == 0
        assert summary.trigger_rate is None
        assert summary.observe_only is True

    def test_missing_file_returns_valid_summary(self):
        path = Path("nonexistent_rotation_events_xyz.jsonl")
        summary = evaluate_rotation_attribution(path)
        assert summary.total_events == 0
        assert summary.observe_only is True

    def test_one_record_does_not_crash(self):
        events = [_event_dict("SOLO", rotation_triggered=True, actual_margin=15.0)]
        path = _write_events_to_tmp(events)
        try:
            summary = evaluate_rotation_attribution(path)
        finally:
            path.unlink(missing_ok=True)

        assert summary.total_events == 1
        assert summary.total_triggered == 1

    def test_one_record_small_sample_flagged(self):
        events = [_event_dict("SOLO", rotation_triggered=True, actual_margin=15.0)]
        path = _write_events_to_tmp(events)
        try:
            summary = evaluate_rotation_attribution(path)
        finally:
            path.unlink(missing_ok=True)

        # All groups with 1 event should have small_sample=True
        for b in summary.by_margin_band:
            if b.total_events > 0:
                assert b.small_sample is True

    def test_below_min_events_produces_no_recommendation(self):
        events = [_event_dict(f"S{i}") for i in range(MIN_EVENTS_FOR_RECOMMENDATION - 1)]
        path = _write_events_to_tmp(events)
        try:
            summary = evaluate_rotation_attribution(path)
        finally:
            path.unlink(missing_ok=True)

        assert "insufficient" in summary.recommendation.lower()
        assert "no recommendation" in summary.recommendation.lower()

    def test_empty_events_list_in_data_quality_note(self):
        summary = evaluate_rotation_attribution(Path("does_not_exist_xyz.jsonl"))
        assert summary.data_quality_notes
        assert any("no rotation" in n.lower() for n in summary.data_quality_notes)

    def test_margin_bands_always_present_even_with_no_data(self):
        summary = evaluate_rotation_attribution(Path("does_not_exist_xyz.jsonl"))
        assert len(summary.by_margin_band) == 4
        band_labels = {b.band_label for b in summary.by_margin_band}
        assert band_labels == {"below_threshold", "near_threshold", "moderate", "strong"}

    def test_challenger_type_groups_always_present_even_with_no_data(self):
        summary = evaluate_rotation_attribution(Path("does_not_exist_xyz.jsonl"))
        assert len(summary.by_challenger_type) == 2
        labels = {c.label for c in summary.by_challenger_type}
        assert labels == {"breakout", "non_breakout"}


# ---------------------------------------------------------------------------
# E. Recommendation generation
# ---------------------------------------------------------------------------

class TestRecommendationGeneration:

    def test_no_recommendation_below_min_events(self):
        events = [_event_dict(f"S{i}", run_id=f"r{i}") for i in range(5)]
        path = _write_events_to_tmp(events)
        try:
            summary = evaluate_rotation_attribution(path)
        finally:
            path.unlink(missing_ok=True)

        assert "no recommendation" in summary.recommendation.lower()
        assert "insufficient" in summary.recommendation.lower()

    def test_no_triggered_events_recommendation(self):
        events = [
            _event_dict(f"S{i}", rotation_triggered=False, actual_margin=5.0, run_id=f"r{i}")
            for i in range(MIN_EVENTS_FOR_RECOMMENDATION)
        ]
        path = _write_events_to_tmp(events)
        try:
            summary = evaluate_rotation_attribution(path)
        finally:
            path.unlink(missing_ok=True)

        assert "no rotations have triggered" in summary.recommendation.lower()

    def test_mostly_near_threshold_triggers_review_flag(self):
        # 10 events: 8 triggered, 7 of those near-threshold (7/8 = 87.5% > 50%)
        events = []
        for i in range(8):
            near = i < 7
            actual = 12.3 if near else 22.0   # near: gap=0.3; far: gap=10
            events.append(_event_dict(f"S{i}", actual_margin=actual, rotation_triggered=True,
                                      run_id=f"r{i}"))
        events += [
            _event_dict(f"S{8+i}", actual_margin=5.0, rotation_triggered=False,
                        run_id=f"r{8+i}")
            for i in range(2)
        ]
        path = _write_events_to_tmp(events)
        try:
            summary = evaluate_rotation_attribution(path)
        finally:
            path.unlink(missing_ok=True)

        assert "near-threshold" in summary.recommendation.lower()
        assert "threshold" in summary.recommendation.lower()

    def test_balanced_distribution_healthy_message(self):
        # 10 events: 5 triggered, all with large margins (strong band)
        events = [
            _event_dict(f"S{i}", actual_margin=25.0, rotation_triggered=(i < 5),
                        run_id=f"r{i}")
            for i in range(MIN_EVENTS_FOR_RECOMMENDATION)
        ]
        path = _write_events_to_tmp(events)
        try:
            summary = evaluate_rotation_attribution(path)
        finally:
            path.unlink(missing_ok=True)

        assert "balanced" in summary.recommendation.lower()

    def test_high_momentum_churn_vs_compounder_flagged(self):
        # 5 momentum: all triggered; 5 compounder: none triggered
        events = []
        for i in range(5):
            events.append(_event_dict(
                f"M{i}", strategy_type="momentum", actual_margin=15.0,
                rotation_triggered=True, run_id=f"m{i}",
            ))
        for i in range(5):
            events.append(_event_dict(
                f"C{i}", strategy_type="compounder", actual_margin=5.0,
                rotation_triggered=False, run_id=f"c{i}",
            ))
        path = _write_events_to_tmp(events)
        try:
            summary = evaluate_rotation_attribution(path)
        finally:
            path.unlink(missing_ok=True)

        assert "churn" in summary.recommendation.lower() or \
               "higher" in summary.recommendation.lower() or \
               "momentum" in summary.recommendation.lower()

    def test_near_threshold_win_rate_below_overall_triggers_flag(self):
        # 10 events with forward returns:
        # near-threshold (gap<4): 4 events, 1 win = 25% win rate
        # strong (gap>10): 6 events, 5 wins = 83% win rate → overall ~60%
        events = []
        # near-threshold triggered: gap=1, win=False (3 events) + win=True (1 event)
        for i in range(3):
            events.append(_event_dict(
                f"N{i}", actual_margin=13.0, required_margin=12.0,
                rotation_triggered=True, forward_return_5d=-0.02,
                outcome_resolved=True, run_id=f"n{i}",
            ))
        events.append(_event_dict(
            "N3", actual_margin=13.0, required_margin=12.0,
            rotation_triggered=True, forward_return_5d=0.03,
            outcome_resolved=True, run_id="n3",
        ))
        # strong (gap=15): 5 wins + 1 loss
        for i in range(5):
            events.append(_event_dict(
                f"S{i}", actual_margin=27.0, required_margin=12.0,
                rotation_triggered=True, forward_return_5d=0.05,
                outcome_resolved=True, run_id=f"s{i}",
            ))
        events.append(_event_dict(
            "S5", actual_margin=27.0, required_margin=12.0,
            rotation_triggered=True, forward_return_5d=-0.01,
            outcome_resolved=True, run_id="s5",
        ))
        path = _write_events_to_tmp(events)
        try:
            summary = evaluate_rotation_attribution(path)
        finally:
            path.unlink(missing_ok=True)

        # near-threshold: 1/4 = 25%, overall: 6/10 = 60% → gap > 10pp → flag
        assert "near-threshold" in summary.recommendation.lower() or \
               "threshold" in summary.recommendation.lower()


# ---------------------------------------------------------------------------
# F. Observe-only guarantee
# ---------------------------------------------------------------------------

class TestObserveOnlyGuarantee:

    def test_observe_only_flag_always_true(self):
        summary = evaluate_rotation_attribution(Path("does_not_exist.jsonl"))
        assert summary.observe_only is True

    def test_observe_only_true_with_data(self):
        events = _enough_events(n=MIN_EVENTS_FOR_RECOMMENDATION)
        path = _write_events_to_tmp(events)
        try:
            summary = evaluate_rotation_attribution(path)
        finally:
            path.unlink(missing_ok=True)

        assert summary.observe_only is True

    def test_to_dict_includes_observe_only_true(self):
        events = _enough_events(n=MIN_EVENTS_FOR_RECOMMENDATION)
        path = _write_events_to_tmp(events)
        try:
            summary = evaluate_rotation_attribution(path)
        finally:
            path.unlink(missing_ok=True)

        d = summary.to_dict()
        assert d["observe_only"] is True

    def test_evaluate_does_not_mutate_input(self):
        events = _enough_events(n=MIN_EVENTS_FOR_RECOMMENDATION)
        original_len = len(events)
        original_first = dict(events[0])

        path = _write_events_to_tmp(events)
        try:
            evaluate_rotation_attribution(path)
        finally:
            path.unlink(missing_ok=True)

        # Original list unchanged
        assert len(events) == original_len
        assert events[0] == original_first

    def test_summary_to_dict_is_json_serializable(self):
        events = _enough_events(n=MIN_EVENTS_FOR_RECOMMENDATION)
        path = _write_events_to_tmp(events)
        try:
            summary = evaluate_rotation_attribution(path)
        finally:
            path.unlink(missing_ok=True)

        d = summary.to_dict()
        serialized = json.dumps(d)    # must not raise
        reparsed = json.loads(serialized)
        assert reparsed["observe_only"] is True
        assert reparsed["total_events"] == summary.total_events

    def test_write_reports_dry_run_returns_true(self):
        summary = evaluate_rotation_attribution(Path("does_not_exist.jsonl"))
        result = write_rotation_reports(summary, dry_run=True)
        assert result is True

    def test_build_memo_empty_summary(self):
        summary = evaluate_rotation_attribution(Path("does_not_exist.jsonl"))
        memo = build_rotation_memo(summary)
        assert "[Rotation Attribution]" in memo
        assert "no rotation events" in memo.lower()

    def test_build_memo_with_data(self):
        events = _enough_events(n=MIN_EVENTS_FOR_RECOMMENDATION, triggered_frac=0.6)
        path = _write_events_to_tmp(events)
        try:
            summary = evaluate_rotation_attribution(path)
        finally:
            path.unlink(missing_ok=True)

        memo = build_rotation_memo(summary)
        assert "[Rotation Attribution]" in memo
        assert "/" in memo  # "X/Y evaluations triggered"

    def test_rotation_attribution_does_not_import_exit_engine(self):
        """rotation_attribution must not touch exit engine or live thresholds."""
        import importlib
        mod = importlib.import_module("profit_attribution.rotation_attribution")
        source_file = Path(mod.__file__).read_text(encoding="utf-8")
        # Must not import from exit_engine or DEFAULT_THRESHOLDS
        assert "exit_engine" not in source_file
        assert "DEFAULT_THRESHOLDS" not in source_file
        assert "replacement_gap_momentum" not in source_file

    def test_strategy_rotation_summary_to_dict_complete(self):
        s = StrategyRotationSummary(
            label="momentum",
            dimension="strategy_type",
            total_events=5,
            triggered_count=3,
            near_threshold_count=1,
            with_outcome=2,
            win_count=1,
            returns_5d=[0.02, -0.01],
            margins=[13.0, 14.0, 15.0],
        )
        d = s.to_dict()
        assert d["label"] == "momentum"
        assert d["trigger_rate"] == pytest.approx(3 / 5, abs=0.001)
        assert d["near_threshold_pct"] == pytest.approx(1 / 3, abs=0.001)
        assert d["win_rate"] == pytest.approx(0.5, abs=0.001)
        assert d["avg_actual_margin"] == pytest.approx(14.0, abs=0.001)

    def test_margin_band_summary_to_dict_complete(self):
        b = MarginBandSummary(
            band_label="near_threshold",
            band_range="+0 to +4 pts above required",
            total_events=4,
            triggered_count=4,
            with_outcome=2,
            win_count=1,
            returns_5d=[0.03, -0.01],
        )
        d = b.to_dict()
        assert d["band_label"] == "near_threshold"
        assert d["trigger_rate"] == pytest.approx(1.0, abs=0.001)
        assert d["win_rate"] == pytest.approx(0.5, abs=0.001)
        assert d["avg_return_5d"] == pytest.approx(0.01, abs=0.0001)


if __name__ == "__main__":
    import pytest as _pytest
    _pytest.main([__file__, "-v"])
