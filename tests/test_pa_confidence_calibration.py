"""Tests for portfolio_automation/confidence_calibration.py

Distinct from tests/test_confidence_calibration.py which covers the
profit_attribution.confidence_calibration module.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from portfolio_automation.confidence_calibration import (
    CALIBRATION_JSON_RELATIVE_PATH,
    CALIBRATION_MD_RELATIVE_PATH,
    CONF_LOW_MAX,
    CONF_MED_MAX,
    CONFIDENCE_BUCKETS_5,
    LATEST_CALIBRATION_JSON_RELATIVE_PATH,
    LATEST_CALIBRATION_MD_RELATIVE_PATH,
    MIN_RESOLVED_ROWS,
    OUTCOMES_JSONL_RELATIVE_PATH,
    _OVERCONFIDENT_GAP,
    _UNDERCONFIDENT_GAP,
    _MIN_SIGNAL_RESOLVED,
    CalibrationBucket,
    ConfidenceCalibrationSummary,
    SignalCalibrationResult,
    _compute_bucket_5,
    _compute_calibration_buckets_5,
    _compute_signal_result,
    _extract_dq_context,
    _normalize_confidence,
    _summary_to_dict,
    _confidence_bucket,
    _group_stats,
    build_calibration,
    compute_confidence_buckets,
    compute_decision_analysis,
    compute_validation_analysis,
    compute_overall,
    evaluate_confidence_calibration,
    generate_insights,
    load_data_quality_report,
    load_decision_outcomes,
    render_calibration_md,
    run_calibration,
    write_confidence_calibration_report,
)
from gui_operator_data import load_confidence_calibration

# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _resolved_row(
    decision: str = "BUY",
    confidence: float | None = 0.80,
    validation_status: str = "aligned",
    return_pct: float = 0.05,
    direction_correct: bool | None = True,
    strategy: str = "momentum",
) -> dict[str, Any]:
    return {
        "decision": decision,
        "confidence": confidence,
        "validation_status": validation_status,
        "return_pct": return_pct,
        "direction_correct": direction_correct,
        "strategy": strategy,
        "resolved": True,
    }


def _unresolved_row() -> dict[str, Any]:
    return {
        "decision": "BUY",
        "confidence": 0.90,
        "validation_status": "aligned",
        "return_pct": None,
        "direction_correct": None,
        "resolved": False,
    }


def _make_rows(n: int, **kwargs) -> list[dict[str, Any]]:
    return [_resolved_row(**kwargs) for _ in range(n)]


def _write_jsonl(tmp: Path, rows: list[dict[str, Any]]) -> Path:
    path = tmp.joinpath(*OUTCOMES_JSONL_RELATIVE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )
    return path


def _sufficient_dataset() -> list[dict[str, Any]]:
    """Build a diverse dataset of exactly MIN_RESOLVED_ROWS resolved rows."""
    rows: list[dict[str, Any]] = []
    rows += _make_rows(5, confidence=0.20, validation_status="caution",
                       return_pct=-0.03, direction_correct=False, decision="SELL")
    rows += _make_rows(8, confidence=0.55, validation_status="caution",
                       return_pct=0.01, direction_correct=True, decision="BUY")
    rows += _make_rows(7, confidence=0.85, validation_status="aligned",
                       return_pct=0.06, direction_correct=True, decision="BUY")
    shortfall = MIN_RESOLVED_ROWS - len(rows)
    if shortfall > 0:
        rows += _make_rows(shortfall, confidence=0.50, validation_status="caution",
                           return_pct=0.02, direction_correct=True, decision="WAIT")
    return rows[:MIN_RESOLVED_ROWS]


# ---------------------------------------------------------------------------
# Class 1: Bucket assignment
# ---------------------------------------------------------------------------


class TestConfidenceBucketAssignment:
    def test_low_bucket_at_zero(self):
        assert _confidence_bucket(0.0) == "low"

    def test_low_bucket_just_below_threshold(self):
        assert _confidence_bucket(CONF_LOW_MAX - 0.001) == "low"

    def test_medium_bucket_at_low_max(self):
        assert _confidence_bucket(CONF_LOW_MAX) == "medium"

    def test_medium_bucket_midpoint(self):
        assert _confidence_bucket(0.55) == "medium"

    def test_medium_bucket_just_below_high(self):
        assert _confidence_bucket(CONF_MED_MAX - 0.001) == "medium"

    def test_high_bucket_at_med_max(self):
        assert _confidence_bucket(CONF_MED_MAX) == "high"

    def test_high_bucket_near_one(self):
        assert _confidence_bucket(0.99) == "high"

    def test_high_bucket_exactly_one(self):
        assert _confidence_bucket(1.0) == "high"

    def test_none_returns_unknown(self):
        assert _confidence_bucket(None) == "unknown"

    def test_negative_treated_as_low(self):
        assert _confidence_bucket(-0.1) == "low"


# ---------------------------------------------------------------------------
# Class 2: Group statistics math
# ---------------------------------------------------------------------------


class TestGroupStats:
    def test_all_correct_hit_rate_one(self):
        rows = _make_rows(5, direction_correct=True)
        stats = _group_stats(rows)
        assert stats["hit_rate"] == 1.0
        assert stats["count"] == 5

    def test_all_incorrect_hit_rate_zero(self):
        rows = _make_rows(4, direction_correct=False)
        stats = _group_stats(rows)
        assert stats["hit_rate"] == 0.0

    def test_mixed_hit_rate(self):
        rows = [
            _resolved_row(direction_correct=True),
            _resolved_row(direction_correct=True),
            _resolved_row(direction_correct=False),
        ]
        stats = _group_stats(rows)
        assert abs(stats["hit_rate"] - 2 / 3) < 1e-9

    def test_neutral_direction_excluded_from_hit_rate(self):
        rows = [
            _resolved_row(direction_correct=True),
            _resolved_row(direction_correct=None),   # HOLD — neutral
        ]
        stats = _group_stats(rows)
        assert stats["hit_rate"] == 1.0  # denominator = 1

    def test_all_neutral_hit_rate_is_none(self):
        rows = _make_rows(3, direction_correct=None)
        stats = _group_stats(rows)
        assert stats["hit_rate"] is None

    def test_avg_return_correct(self):
        rows = [
            _resolved_row(return_pct=0.10),
            _resolved_row(return_pct=0.20),
        ]
        stats = _group_stats(rows)
        assert abs(stats["avg_return"] - 0.15) < 1e-9

    def test_none_return_excluded_from_avg(self):
        rows = [
            _resolved_row(return_pct=0.10),
            {"direction_correct": True, "return_pct": None, "resolved": True},
        ]
        stats = _group_stats(rows)
        assert abs(stats["avg_return"] - 0.10) < 1e-9

    def test_empty_rows(self):
        stats = _group_stats([])
        assert stats["count"] == 0
        assert stats["hit_rate"] is None
        assert stats["avg_return"] is None


# ---------------------------------------------------------------------------
# Class 3: Confidence bucket grouping
# ---------------------------------------------------------------------------


class TestComputeConfidenceBuckets:
    def test_all_bucket_keys_always_present(self):
        result = compute_confidence_buckets(_make_rows(5, confidence=0.80))
        for k in ("low", "medium", "high", "unknown"):
            assert k in result

    def test_rows_land_in_correct_buckets(self):
        rows = (
            _make_rows(3, confidence=0.20) +
            _make_rows(4, confidence=0.55) +
            _make_rows(5, confidence=0.85)
        )
        result = compute_confidence_buckets(rows)
        assert result["low"]["count"] == 3
        assert result["medium"]["count"] == 4
        assert result["high"]["count"] == 5

    def test_none_confidence_goes_to_unknown(self):
        rows = _make_rows(2, confidence=None)
        result = compute_confidence_buckets(rows)
        assert result["unknown"]["count"] == 2
        assert result["low"]["count"] == 0

    def test_empty_input_all_zero(self):
        result = compute_confidence_buckets([])
        for k in ("low", "medium", "high", "unknown"):
            assert result[k]["count"] == 0
            assert result[k]["hit_rate"] is None


# ---------------------------------------------------------------------------
# Class 4: Validation analysis
# ---------------------------------------------------------------------------


class TestComputeValidationAnalysis:
    def test_canonical_keys_always_present(self):
        rows = _make_rows(3, validation_status="aligned")
        result = compute_validation_analysis(rows)
        for k in ("aligned", "caution", "contradiction", "insufficient_context"):
            assert k in result

    def test_groups_by_validation_status(self):
        rows = (
            _make_rows(4, validation_status="aligned") +
            _make_rows(6, validation_status="caution")
        )
        result = compute_validation_analysis(rows)
        assert result["aligned"]["count"] == 4
        assert result["caution"]["count"] == 6

    def test_empty_canonical_keys_have_zero_count(self):
        rows = _make_rows(2, validation_status="aligned")
        result = compute_validation_analysis(rows)
        assert result["caution"]["count"] == 0
        assert result["caution"]["hit_rate"] is None

    def test_non_canonical_status_captured(self):
        rows = _make_rows(2, validation_status="custom_status")
        result = compute_validation_analysis(rows)
        assert "custom_status" in result
        assert result["custom_status"]["count"] == 2


# ---------------------------------------------------------------------------
# Class 5: Decision analysis
# ---------------------------------------------------------------------------


class TestComputeDecisionAnalysis:
    def test_groups_by_decision_type(self):
        rows = (
            _make_rows(3, decision="BUY") +
            _make_rows(2, decision="SELL") +
            _make_rows(4, decision="WAIT")
        )
        result = compute_decision_analysis(rows)
        assert result["BUY"]["count"] == 3
        assert result["SELL"]["count"] == 2
        assert result["WAIT"]["count"] == 4

    def test_empty_input_returns_empty_dict(self):
        assert compute_decision_analysis([]) == {}


# ---------------------------------------------------------------------------
# Class 6: Overall metrics
# ---------------------------------------------------------------------------


class TestComputeOverall:
    def test_total_resolved_count(self):
        overall = compute_overall(_make_rows(10))
        assert overall["total_resolved"] == 10

    def test_hit_rate_calculation(self):
        rows = _make_rows(7, direction_correct=True) + _make_rows(3, direction_correct=False)
        overall = compute_overall(rows)
        assert abs(overall["overall_hit_rate"] - 0.70) < 1e-9

    def test_avg_return_calculation(self):
        rows = _make_rows(5, return_pct=0.10) + _make_rows(5, return_pct=0.20)
        overall = compute_overall(rows)
        assert abs(overall["overall_avg_return"] - 0.15) < 1e-9

    def test_empty_returns_nones(self):
        overall = compute_overall([])
        assert overall["total_resolved"] == 0
        assert overall["overall_hit_rate"] is None
        assert overall["overall_avg_return"] is None


# ---------------------------------------------------------------------------
# Class 7: Insight generation
# ---------------------------------------------------------------------------


def _conf_buckets(low_hr=0.30, high_hr=0.80, low_ret=-0.01, high_ret=0.05):
    return {
        "low": {"count": 5, "hit_rate": low_hr, "avg_return": low_ret},
        "medium": {"count": 5, "hit_rate": 0.55, "avg_return": 0.01},
        "high": {"count": 5, "hit_rate": high_hr, "avg_return": high_ret},
        "unknown": {"count": 0, "hit_rate": None, "avg_return": None},
    }


def _val_analysis(aligned_hr=0.75, caution_hr=0.40):
    return {
        "aligned": {"count": 5, "hit_rate": aligned_hr, "avg_return": 0.03},
        "caution": {"count": 10, "hit_rate": caution_hr, "avg_return": 0.01},
        "contradiction": {"count": 1, "hit_rate": 0.20, "avg_return": -0.02},
        "insufficient_context": {"count": 2, "hit_rate": None, "avg_return": None},
    }


def _overall(hit_rate=0.62, total=50):
    return {"overall_hit_rate": hit_rate, "overall_avg_return": 0.02, "total_resolved": total}


class TestInsightGeneration:
    def test_calibrated_insight_when_high_outperforms_low(self):
        insights = generate_insights(_conf_buckets(low_hr=0.30, high_hr=0.80),
                                     _val_analysis(), {}, _overall())
        assert any("calibrated" in i.lower() for i in insights)

    def test_inverted_insight_when_low_outperforms_high(self):
        insights = generate_insights(_conf_buckets(low_hr=0.80, high_hr=0.30),
                                     _val_analysis(), {}, _overall())
        text = " ".join(insights).lower()
        assert "inverted" in text or "low-confidence" in text

    def test_predictive_insight_when_aligned_beats_caution(self):
        insights = generate_insights(_conf_buckets(),
                                     _val_analysis(aligned_hr=0.80, caution_hr=0.50),
                                     {}, _overall())
        assert any("predictive" in i.lower() for i in insights)

    def test_poor_caution_insight_when_caution_hits_below_40pct(self):
        insights = generate_insights(
            _conf_buckets(low_hr=0.50, high_hr=0.52),   # similar — no calibration insight
            _val_analysis(aligned_hr=0.52, caution_hr=0.30),   # caution poor
            {}, _overall(),
        )
        text = " ".join(insights).lower()
        assert "caution" in text or "depriorit" in text

    def test_small_dataset_insight_fires_below_50_rows(self):
        insights = generate_insights(_conf_buckets(), _val_analysis(), {}, _overall(total=25))
        text = " ".join(insights)
        assert "25" in text or "small" in text.lower() or "dataset" in text.lower()

    def test_no_error_when_hit_rates_none(self):
        empty = {k: {"count": 0, "hit_rate": None, "avg_return": None}
                 for k in ("low", "medium", "high", "unknown")}
        empty_val = {k: {"count": 0, "hit_rate": None, "avg_return": None}
                     for k in ("aligned", "caution")}
        insights = generate_insights(empty, empty_val, {},
                                     {"overall_hit_rate": None, "overall_avg_return": None,
                                      "total_resolved": 0})
        assert isinstance(insights, list)

    def test_insights_capped_at_five(self):
        insights = generate_insights(_conf_buckets(), _val_analysis(), {}, _overall())
        assert len(insights) <= 5


# ---------------------------------------------------------------------------
# Class 8: build_calibration payload schema
# ---------------------------------------------------------------------------


class TestBuildCalibration:
    def test_required_keys_present(self):
        payload = build_calibration(_sufficient_dataset())
        required = {
            "generated_at", "observe_only", "total_resolved",
            "overall_hit_rate", "overall_avg_return",
            "confidence_buckets", "validation_analysis",
            "decision_analysis", "insights",
        }
        assert required.issubset(set(payload.keys()))

    def test_observe_only_always_true(self):
        assert build_calibration(_sufficient_dataset())["observe_only"] is True

    def test_total_resolved_matches_row_count(self):
        rows = _make_rows(MIN_RESOLVED_ROWS, confidence=0.75)
        assert build_calibration(rows)["total_resolved"] == MIN_RESOLVED_ROWS

    def test_overall_hit_rate_in_range(self):
        hr = build_calibration(_sufficient_dataset()).get("overall_hit_rate")
        if hr is not None:
            assert 0.0 <= hr <= 1.0

    def test_confidence_buckets_have_required_keys(self):
        payload = build_calibration(_sufficient_dataset())
        for key in ("low", "medium", "high"):
            bucket = payload["confidence_buckets"].get(key, {})
            assert "count" in bucket
            assert "hit_rate" in bucket
            assert "avg_return" in bucket

    def test_validation_analysis_has_canonical_keys(self):
        payload = build_calibration(_sufficient_dataset())
        for key in ("aligned", "caution"):
            assert key in payload["validation_analysis"]

    def test_insights_is_list(self):
        assert isinstance(build_calibration(_sufficient_dataset())["insights"], list)

    def test_generated_at_is_iso_string(self):
        ts = build_calibration(_sufficient_dataset())["generated_at"]
        assert isinstance(ts, str) and "T" in ts


# ---------------------------------------------------------------------------
# Class 9: Markdown rendering
# ---------------------------------------------------------------------------


class TestMarkdownRendering:
    def _payload(self) -> dict[str, Any]:
        p = build_calibration(_sufficient_dataset())
        p["available"] = True
        p["insufficient_data"] = False
        return p

    def test_contains_observe_only_disclaimer(self):
        md = render_calibration_md(self._payload())
        assert "Observe-only" in md or "observe-only" in md.lower()

    def test_contains_all_section_headers(self):
        md = render_calibration_md(self._payload())
        assert "## Overall" in md
        assert "Confidence Bucket" in md
        assert "Validation Status" in md
        assert "Decision Type" in md

    def test_insufficient_data_payload_renders_message(self):
        payload = {
            "generated_at": "2026-04-29T09:00:00",
            "observe_only": True,
            "available": False,
            "insufficient_data": True,
            "total_resolved": 5,
            "min_required": MIN_RESOLVED_ROWS,
            "overall_hit_rate": None,
            "overall_avg_return": None,
            "confidence_buckets": {},
            "validation_analysis": {},
            "decision_analysis": {},
            "insights": [],
        }
        md = render_calibration_md(payload)
        assert "Insufficient" in md or "insufficient" in md

    def test_ends_with_newline(self):
        assert render_calibration_md(self._payload()).endswith("\n")

    def test_insights_section_when_insights_present(self):
        payload = self._payload()
        if payload.get("insights"):
            md = render_calibration_md(payload)
            assert "## Key Insights" in md


# ---------------------------------------------------------------------------
# Class 10: run_calibration I/O integration
# ---------------------------------------------------------------------------


class TestRunCalibration:
    def test_insufficient_data_below_threshold(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _write_jsonl(tmp, _make_rows(5))
            payload, _ = run_calibration(tmp, write_files=False)
            assert payload["available"] is False
            assert payload["insufficient_data"] is True

    def test_insufficient_summary_line(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _write_jsonl(tmp, _make_rows(3))
            payload, _ = run_calibration(tmp, write_files=False)
            assert "Insufficient" in payload["summary_line"] or "3" in payload["summary_line"]

    def test_missing_jsonl_available_false(self):
        with tempfile.TemporaryDirectory() as td:
            payload, _ = run_calibration(Path(td), write_files=False)
            assert payload["available"] is False
            assert payload["total_resolved"] == 0

    def test_sufficient_data_available_true(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _write_jsonl(tmp, _sufficient_dataset())
            payload, _ = run_calibration(tmp, write_files=False)
            assert payload["available"] is True
            assert payload["insufficient_data"] is False

    def test_writes_json_and_md(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _write_jsonl(tmp, _sufficient_dataset())
            run_calibration(tmp)
            assert tmp.joinpath(*CALIBRATION_JSON_RELATIVE_PATH).exists()
            assert tmp.joinpath(*CALIBRATION_MD_RELATIVE_PATH).exists()

    def test_skips_write_when_flag_false(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _write_jsonl(tmp, _sufficient_dataset())
            run_calibration(tmp, write_files=False)
            assert not tmp.joinpath(*CALIBRATION_JSON_RELATIVE_PATH).exists()

    def test_unresolved_rows_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rows = _sufficient_dataset() + [_unresolved_row() for _ in range(10)]
            _write_jsonl(tmp, rows)
            payload, _ = run_calibration(tmp, write_files=False)
            assert payload["total_resolved"] == MIN_RESOLVED_ROWS

    def test_custom_min_resolved(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _write_jsonl(tmp, _make_rows(5))
            payload, _ = run_calibration(tmp, write_files=False, min_resolved=3)
            assert payload["available"] is True

    def test_json_written_is_valid(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _write_jsonl(tmp, _sufficient_dataset())
            run_calibration(tmp)
            data = json.loads(tmp.joinpath(*CALIBRATION_JSON_RELATIVE_PATH).read_text())
            assert isinstance(data, dict)
            assert "confidence_buckets" in data

    def test_non_fatal_on_nonexistent_root(self):
        payload, md = run_calibration(Path("/nonexistent/path"), write_files=False)
        assert payload["available"] is False
        assert isinstance(md, str)

    def test_malformed_jsonl_lines_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            path = tmp.joinpath(*OUTCOMES_JSONL_RELATIVE_PATH)
            path.parent.mkdir(parents=True, exist_ok=True)
            good = "\n".join(json.dumps(r) for r in _make_rows(MIN_RESOLVED_ROWS))
            path.write_text(good + "\nBAD LINE\n{broken", encoding="utf-8")
            payload, _ = run_calibration(tmp, write_files=False)
            assert payload["available"] is True
            assert payload["total_resolved"] == MIN_RESOLVED_ROWS

    def test_summary_line_contains_resolved_count(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _write_jsonl(tmp, _sufficient_dataset())
            payload, _ = run_calibration(tmp, write_files=False)
            assert str(MIN_RESOLVED_ROWS) in payload["summary_line"]


# ---------------------------------------------------------------------------
# Class 11: GUI data layer — load_confidence_calibration
# ---------------------------------------------------------------------------


class TestGuiDataLayer:
    def test_returns_empty_when_file_missing(self):
        with tempfile.TemporaryDirectory() as td:
            result = load_confidence_calibration(Path(td))
            assert result["available"] is False
            assert "not available" in result["summary_line"]

    def test_returns_empty_on_malformed_json(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            path = tmp / "outputs" / "policy"
            path.mkdir(parents=True, exist_ok=True)
            (path / "confidence_calibration.json").write_text("{invalid}", encoding="utf-8")
            result = load_confidence_calibration(tmp)
            assert result["available"] is False
            assert "could not be read" in result["summary_line"]

    def test_returns_empty_on_non_dict_json(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            path = tmp / "outputs" / "policy"
            path.mkdir(parents=True, exist_ok=True)
            (path / "confidence_calibration.json").write_text("[]", encoding="utf-8")
            result = load_confidence_calibration(tmp)
            assert result["available"] is False

    def test_loads_valid_calibration_file(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _write_jsonl(tmp, _sufficient_dataset())
            run_calibration(tmp)
            result = load_confidence_calibration(tmp)
            assert result["available"] is True
            assert result["total_resolved"] == MIN_RESOLVED_ROWS
            assert "confidence_buckets" in result

    def test_defaults_available_true_when_key_absent(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            path = tmp / "outputs" / "policy"
            path.mkdir(parents=True, exist_ok=True)
            (path / "confidence_calibration.json").write_text(
                json.dumps({"total_resolved": 30}), encoding="utf-8"
            )
            assert load_confidence_calibration(tmp)["available"] is True

    def test_summary_line_defaults_with_total(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            path = tmp / "outputs" / "policy"
            path.mkdir(parents=True, exist_ok=True)
            (path / "confidence_calibration.json").write_text(
                json.dumps({"available": True, "total_resolved": 42}), encoding="utf-8"
            )
            result = load_confidence_calibration(tmp)
            assert "42" in result["summary_line"]

    def test_insufficient_data_flag_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _write_jsonl(tmp, _make_rows(5))
            run_calibration(tmp)
            result = load_confidence_calibration(tmp)
            assert result["insufficient_data"] is True


# ---------------------------------------------------------------------------
# Class 12: _normalize_confidence
# ---------------------------------------------------------------------------


class TestNormalizeConfidence:
    def test_value_below_one_unchanged(self):
        assert _normalize_confidence(0.75) == pytest.approx(0.75)

    def test_value_at_one_unchanged(self):
        assert _normalize_confidence(1.0) == pytest.approx(1.0)

    def test_value_above_one_divided_by_100(self):
        assert _normalize_confidence(75.0) == pytest.approx(0.75)

    def test_zero_unchanged(self):
        assert _normalize_confidence(0.0) == pytest.approx(0.0)

    def test_100_becomes_one(self):
        assert _normalize_confidence(100.0) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Class 13: 5-bucket assignment
# ---------------------------------------------------------------------------


class TestBucket5Assignment:
    def test_very_low_at_zero(self):
        assert _compute_bucket_5(0.0) == "very_low"

    def test_very_low_just_below_0_25(self):
        assert _compute_bucket_5(0.249) == "very_low"

    def test_low_at_0_25(self):
        assert _compute_bucket_5(0.25) == "low"

    def test_low_just_below_0_50(self):
        assert _compute_bucket_5(0.499) == "low"

    def test_medium_at_0_50(self):
        assert _compute_bucket_5(0.50) == "medium"

    def test_medium_just_below_0_70(self):
        assert _compute_bucket_5(0.699) == "medium"

    def test_high_at_0_70(self):
        assert _compute_bucket_5(0.70) == "high"

    def test_high_just_below_0_85(self):
        assert _compute_bucket_5(0.849) == "high"

    def test_very_high_at_0_85(self):
        assert _compute_bucket_5(0.85) == "very_high"

    def test_very_high_at_one(self):
        assert _compute_bucket_5(1.0) == "very_high"

    def test_none_returns_unknown(self):
        assert _compute_bucket_5(None) == "unknown"


# ---------------------------------------------------------------------------
# Class 14: load_decision_outcomes / load_data_quality_report
# ---------------------------------------------------------------------------


class TestLoaders:
    def test_load_decision_outcomes_empty_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            result = load_decision_outcomes(Path(td))
            assert result == []

    def test_load_decision_outcomes_returns_all_rows(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            rows = _make_rows(10) + [_unresolved_row()]
            _write_jsonl(tmp, rows)
            result = load_decision_outcomes(tmp)
            assert len(result) == 11

    def test_load_data_quality_report_empty_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            assert load_data_quality_report(Path(td)) == {}

    def test_load_data_quality_report_returns_dict(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            path = tmp / "outputs" / "latest"
            path.mkdir(parents=True, exist_ok=True)
            (path / "data_quality_report.json").write_text(
                json.dumps({"issues": [], "degraded_mode": False}), encoding="utf-8"
            )
            result = load_data_quality_report(tmp)
            assert isinstance(result, dict)
            assert "issues" in result

    def test_load_data_quality_report_empty_on_malformed_json(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            path = tmp / "outputs" / "latest"
            path.mkdir(parents=True, exist_ok=True)
            (path / "data_quality_report.json").write_text("{bad}", encoding="utf-8")
            assert load_data_quality_report(tmp) == {}

    def test_load_data_quality_report_empty_on_non_dict(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            path = tmp / "outputs" / "latest"
            path.mkdir(parents=True, exist_ok=True)
            (path / "data_quality_report.json").write_text("[]", encoding="utf-8")
            assert load_data_quality_report(tmp) == {}


# ---------------------------------------------------------------------------
# Class 15: evaluate_confidence_calibration
# ---------------------------------------------------------------------------


def _resolved_row_with_source(
    source: str = "MOMENTUM_SIGNAL",
    confidence: float = 0.80,
    direction_correct: bool | None = True,
) -> dict[str, Any]:
    return {
        "decision": "BUY",
        "confidence": confidence,
        "validation_status": "aligned",
        "return_pct": 0.05,
        "direction_correct": direction_correct,
        "source": source,
        "resolved": True,
    }


def _make_rows_with_source(n: int, source: str, **kwargs) -> list[dict[str, Any]]:
    return [_resolved_row_with_source(source=source, **kwargs) for _ in range(n)]


class TestEvaluateConfidenceCalibration:
    def test_insufficient_data_returns_available_true(self):
        summary = evaluate_confidence_calibration([])
        assert summary.available is True
        assert summary.insufficient_data is True
        assert summary.observe_only is True

    def test_insufficient_data_below_min(self):
        rows = [_resolved_row() for _ in range(5)]
        summary = evaluate_confidence_calibration(rows)
        assert summary.insufficient_data is True
        assert "5" in summary.summary_line

    def test_sufficient_data_available_not_insufficient(self):
        rows = _sufficient_dataset()
        summary = evaluate_confidence_calibration(rows)
        assert summary.available is True
        assert summary.insufficient_data is False

    def test_observe_only_always_true(self):
        summary = evaluate_confidence_calibration(_sufficient_dataset())
        assert summary.observe_only is True

    def test_total_resolved_count(self):
        rows = _sufficient_dataset()
        summary = evaluate_confidence_calibration(rows)
        assert summary.total_resolved == len(rows)

    def test_overall_hit_rate_computed(self):
        rows = _make_rows(20, direction_correct=True)
        summary = evaluate_confidence_calibration(rows)
        assert summary.overall_hit_rate == pytest.approx(1.0)

    def test_overall_calibration_gap_computed(self):
        rows = _make_rows(20, confidence=0.90, direction_correct=True)
        summary = evaluate_confidence_calibration(rows)
        # avg_conf=0.90, hit_rate=1.0 → gap = 0.90 - 1.0 = -0.10
        assert summary.overall_calibration_gap is not None
        assert abs(summary.overall_calibration_gap - (-0.10)) < 0.01

    def test_five_buckets_returned(self):
        summary = evaluate_confidence_calibration(_sufficient_dataset())
        assert len(summary.buckets_5) == 5

    def test_bucket_labels_match_constants(self):
        summary = evaluate_confidence_calibration(_sufficient_dataset())
        labels = [b.label for b in summary.buckets_5]
        for label, _, _ in CONFIDENCE_BUCKETS_5:
            assert label in labels

    def test_overconfident_signal_result(self):
        from unittest.mock import MagicMock
        mock_registry = MagicMock()
        mock_registry.validate_signal_id.return_value = True
        mock_registry.is_discovery_only.return_value = False

        rows = _make_rows_with_source(20, source="SIG_A", confidence=0.95, direction_correct=False)
        summary = evaluate_confidence_calibration(
            rows, registry=mock_registry, min_resolved=10, min_signal_resolved=5
        )
        sig = next((s for s in summary.signal_results if s.signal_id == "SIG_A"), None)
        assert sig is not None
        assert sig.overconfident is True
        assert sig.suggested_review is True

    def test_underconfident_signal_result(self):
        from unittest.mock import MagicMock
        mock_registry = MagicMock()
        mock_registry.validate_signal_id.return_value = True
        mock_registry.is_discovery_only.return_value = False

        rows = _make_rows_with_source(20, source="SIG_B", confidence=0.10, direction_correct=True)
        summary = evaluate_confidence_calibration(
            rows, registry=mock_registry, min_resolved=10, min_signal_resolved=5
        )
        sig = next((s for s in summary.signal_results if s.signal_id == "SIG_B"), None)
        assert sig is not None
        assert sig.underconfident is True
        assert sig.suggested_review is True

    def test_discovery_only_signal_no_suggested_review(self):
        from unittest.mock import MagicMock
        mock_registry = MagicMock()
        mock_registry.validate_signal_id.return_value = True
        mock_registry.is_discovery_only.return_value = True

        rows = _make_rows_with_source(20, source="DISCOVERY_SIG", confidence=0.95, direction_correct=False)
        summary = evaluate_confidence_calibration(
            rows, registry=mock_registry, min_resolved=10, min_signal_resolved=5
        )
        sig = next((s for s in summary.signal_results if s.signal_id == "DISCOVERY_SIG"), None)
        assert sig is not None
        assert sig.discovery_only is True
        assert sig.suggested_review is False

    def test_unknown_signal_treated_as_discovery_only(self):
        rows = _make_rows_with_source(20, source="UNKNOWN_SIG", confidence=0.95, direction_correct=False)
        summary = evaluate_confidence_calibration(
            rows, registry=None, min_resolved=10, min_signal_resolved=5
        )
        sig = next((s for s in summary.signal_results if s.signal_id == "UNKNOWN_SIG"), None)
        assert sig is not None
        assert sig.discovery_only is True
        assert sig.suggested_review is False

    def test_signals_below_min_resolved_excluded(self):
        rows = _make_rows_with_source(3, source="RARE_SIG")
        extra = _make_rows(17)
        summary = evaluate_confidence_calibration(
            rows + extra, min_resolved=15, min_signal_resolved=5
        )
        assert not any(s.signal_id == "RARE_SIG" for s in summary.signal_results)

    def test_dq_warnings_included_from_report(self):
        dq = {"issues": [{"severity": "warning", "message": "Stale price detected"}]}
        summary = evaluate_confidence_calibration(
            _sufficient_dataset(), dq_report=dq
        )
        assert any("Stale" in w for w in summary.dq_warnings)

    def test_dq_warnings_empty_without_report(self):
        summary = evaluate_confidence_calibration(_sufficient_dataset())
        assert summary.dq_warnings == []

    def test_summary_line_contains_count(self):
        rows = _sufficient_dataset()
        summary = evaluate_confidence_calibration(rows)
        assert str(len(rows)) in summary.summary_line

    def test_custom_min_resolved(self):
        rows = _make_rows(5)
        summary = evaluate_confidence_calibration(rows, min_resolved=3)
        assert summary.insufficient_data is False


# ---------------------------------------------------------------------------
# Class 16: _extract_dq_context
# ---------------------------------------------------------------------------


class TestExtractDqContext:
    def test_empty_report_returns_empty(self):
        assert _extract_dq_context({}) == []

    def test_critical_issue_included(self):
        dq = {"issues": [{"severity": "critical", "message": "Missing price"}]}
        result = _extract_dq_context(dq)
        assert any("CRITICAL" in w for w in result)

    def test_warning_issue_included(self):
        dq = {"issues": [{"severity": "warning", "message": "Stale data"}]}
        result = _extract_dq_context(dq)
        assert any("WARNING" in w for w in result)

    def test_info_issue_excluded(self):
        dq = {"issues": [{"severity": "info", "message": "Just info"}]}
        result = _extract_dq_context(dq)
        assert result == []

    def test_degraded_mode_adds_warning(self):
        dq = {"degraded_mode": True, "issues": []}
        result = _extract_dq_context(dq)
        assert any("degraded" in w.lower() for w in result)

    def test_capped_at_10(self):
        issues = [{"severity": "warning", "message": f"issue {i}"} for i in range(15)]
        result = _extract_dq_context({"issues": issues})
        assert len(result) <= 10


# ---------------------------------------------------------------------------
# Class 17: _summary_to_dict schema
# ---------------------------------------------------------------------------


class TestSummaryToDict:
    def _make_summary(self) -> ConfidenceCalibrationSummary:
        rows = _sufficient_dataset()
        return evaluate_confidence_calibration(rows)

    def test_required_keys_present(self):
        d = _summary_to_dict(self._make_summary())
        required = {
            "generated_at", "observe_only", "available", "insufficient_data",
            "total_resolved", "min_required", "overall_hit_rate",
            "overall_average_confidence", "overall_calibration_gap",
            "buckets_5", "signal_results", "dq_warnings", "summary_line",
        }
        assert required.issubset(set(d.keys()))

    def test_observe_only_true(self):
        assert _summary_to_dict(self._make_summary())["observe_only"] is True

    def test_buckets_5_is_list(self):
        assert isinstance(_summary_to_dict(self._make_summary())["buckets_5"], list)

    def test_signal_results_is_list(self):
        assert isinstance(_summary_to_dict(self._make_summary())["signal_results"], list)

    def test_bucket_has_required_keys(self):
        d = _summary_to_dict(self._make_summary())
        if d["buckets_5"]:
            b = d["buckets_5"][0]
            assert "label" in b and "count" in b and "hit_rate" in b

    def test_serialisable_to_json(self):
        d = _summary_to_dict(self._make_summary())
        assert json.dumps(d)  # must not raise


# ---------------------------------------------------------------------------
# Class 18: write_confidence_calibration_report — LATEST artifacts
# ---------------------------------------------------------------------------


class TestLatestArtifacts:
    def test_writes_json_to_latest(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _write_jsonl(tmp, _sufficient_dataset())
            write_confidence_calibration_report(tmp)
            assert tmp.joinpath(*LATEST_CALIBRATION_JSON_RELATIVE_PATH).exists()

    def test_writes_md_to_latest(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _write_jsonl(tmp, _sufficient_dataset())
            write_confidence_calibration_report(tmp)
            assert tmp.joinpath(*LATEST_CALIBRATION_MD_RELATIVE_PATH).exists()

    def test_json_artifact_is_valid(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _write_jsonl(tmp, _sufficient_dataset())
            write_confidence_calibration_report(tmp)
            data = json.loads(
                tmp.joinpath(*LATEST_CALIBRATION_JSON_RELATIVE_PATH).read_text()
            )
            assert isinstance(data, dict)
            assert "buckets_5" in data
            assert "observe_only" in data

    def test_json_observe_only_true(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _write_jsonl(tmp, _sufficient_dataset())
            write_confidence_calibration_report(tmp)
            data = json.loads(
                tmp.joinpath(*LATEST_CALIBRATION_JSON_RELATIVE_PATH).read_text()
            )
            assert data["observe_only"] is True

    def test_md_contains_enhanced_header(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _write_jsonl(tmp, _sufficient_dataset())
            write_confidence_calibration_report(tmp)
            md = tmp.joinpath(*LATEST_CALIBRATION_MD_RELATIVE_PATH).read_text()
            assert "Enhanced" in md or "Calibration" in md

    def test_md_ends_with_newline(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _write_jsonl(tmp, _sufficient_dataset())
            write_confidence_calibration_report(tmp)
            md = tmp.joinpath(*LATEST_CALIBRATION_MD_RELATIVE_PATH).read_text()
            assert md.endswith("\n")

    def test_returns_summary_object(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _write_jsonl(tmp, _sufficient_dataset())
            result = write_confidence_calibration_report(tmp)
            assert isinstance(result, ConfidenceCalibrationSummary)

    def test_insufficient_data_still_writes(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _write_jsonl(tmp, _make_rows(3))
            write_confidence_calibration_report(tmp)
            assert tmp.joinpath(*LATEST_CALIBRATION_JSON_RELATIVE_PATH).exists()

    def test_missing_outcomes_still_writes(self):
        with tempfile.TemporaryDirectory() as td:
            summary = write_confidence_calibration_report(Path(td))
            assert summary.insufficient_data is True


# ---------------------------------------------------------------------------
# Class 19: run_calibration also writes to LATEST
# ---------------------------------------------------------------------------


class TestRunCalibrationLatestWrite:
    def test_run_calibration_writes_latest_json(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _write_jsonl(tmp, _sufficient_dataset())
            run_calibration(tmp)
            assert tmp.joinpath(*LATEST_CALIBRATION_JSON_RELATIVE_PATH).exists()

    def test_run_calibration_writes_latest_md(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _write_jsonl(tmp, _sufficient_dataset())
            run_calibration(tmp)
            assert tmp.joinpath(*LATEST_CALIBRATION_MD_RELATIVE_PATH).exists()

    def test_run_calibration_still_writes_policy(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _write_jsonl(tmp, _sufficient_dataset())
            run_calibration(tmp)
            assert tmp.joinpath(*CALIBRATION_JSON_RELATIVE_PATH).exists()
            assert tmp.joinpath(*CALIBRATION_MD_RELATIVE_PATH).exists()

    def test_run_calibration_write_false_skips_latest(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _write_jsonl(tmp, _sufficient_dataset())
            run_calibration(tmp, write_files=False)
            assert not tmp.joinpath(*LATEST_CALIBRATION_JSON_RELATIVE_PATH).exists()


# ---------------------------------------------------------------------------
# Class 20: buckets_5 always-5 contract
# ---------------------------------------------------------------------------


_EXPECTED_BUCKET_LABELS = {"very_low", "low", "medium", "high", "very_high"}


class TestBuckets5AlwaysPresent:
    def test_insufficient_data_returns_5_buckets(self):
        rows = _make_rows(3)
        summary = evaluate_confidence_calibration(rows)
        assert summary.insufficient_data is True
        assert len(summary.buckets_5) == 5

    def test_empty_outcomes_returns_5_buckets(self):
        summary = evaluate_confidence_calibration([])
        assert len(summary.buckets_5) == 5

    def test_all_5_labels_present_when_insufficient(self):
        summary = evaluate_confidence_calibration(_make_rows(1))
        labels = {b.label for b in summary.buckets_5}
        assert labels == _EXPECTED_BUCKET_LABELS

    def test_one_resolved_returns_5_buckets_and_correct_summary(self):
        rows = [_resolved_row(confidence=0.80, direction_correct=True)]
        summary = evaluate_confidence_calibration(rows)
        assert summary.insufficient_data is True
        assert summary.total_resolved == 1
        assert "1" in summary.summary_line
        assert len(summary.buckets_5) == 5

    def test_one_resolved_lands_in_correct_bucket(self):
        rows = [_resolved_row(confidence=0.80, direction_correct=True)]
        summary = evaluate_confidence_calibration(rows)
        high = next(b for b in summary.buckets_5 if b.label == "high")
        assert high.count == 1
        other_counts = [b.count for b in summary.buckets_5 if b.label != "high"]
        assert all(c == 0 for c in other_counts)

    def test_zero_to_one_confidence_buckets_correctly(self):
        rows = [
            _resolved_row(confidence=0.10),   # very_low
            _resolved_row(confidence=0.35),   # low
            _resolved_row(confidence=0.60),   # medium
            _resolved_row(confidence=0.75),   # high
            _resolved_row(confidence=0.90),   # very_high
        ]
        summary = evaluate_confidence_calibration(rows, min_resolved=1)
        by_label = {b.label: b for b in summary.buckets_5}
        assert by_label["very_low"].count == 1
        assert by_label["low"].count == 1
        assert by_label["medium"].count == 1
        assert by_label["high"].count == 1
        assert by_label["very_high"].count == 1

    def test_zero_to_100_confidence_normalised_and_bucketed(self):
        rows = [
            _resolved_row(confidence=80.0),   # normalised → 0.80 → high
        ]
        summary = evaluate_confidence_calibration(rows, min_resolved=1)
        high = next(b for b in summary.buckets_5 if b.label == "high")
        assert high.count == 1

    def test_confidence_1_0_in_very_high(self):
        rows = [_resolved_row(confidence=1.0)]
        summary = evaluate_confidence_calibration(rows, min_resolved=1)
        very_high = next(b for b in summary.buckets_5 if b.label == "very_high")
        assert very_high.count == 1

    def test_sufficient_data_still_returns_5_buckets(self):
        summary = evaluate_confidence_calibration(_sufficient_dataset())
        assert summary.insufficient_data is False
        assert len(summary.buckets_5) == 5

    def test_sufficient_data_all_5_labels_present(self):
        summary = evaluate_confidence_calibration(_sufficient_dataset())
        labels = {b.label for b in summary.buckets_5}
        assert labels == _EXPECTED_BUCKET_LABELS

    def test_json_artifact_has_5_buckets_when_insufficient(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _write_jsonl(tmp, _make_rows(3))
            write_confidence_calibration_report(tmp)
            data = json.loads(
                tmp.joinpath(*LATEST_CALIBRATION_JSON_RELATIVE_PATH).read_text()
            )
            assert data["insufficient_data"] is True
            assert len(data["buckets_5"]) == 5

    def test_json_artifact_has_5_buckets_when_no_outcomes(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            write_confidence_calibration_report(tmp)
            data = json.loads(
                tmp.joinpath(*LATEST_CALIBRATION_JSON_RELATIVE_PATH).read_text()
            )
            assert len(data["buckets_5"]) == 5

    def test_empty_buckets_have_zero_count(self):
        summary = evaluate_confidence_calibration([])
        assert all(b.count == 0 for b in summary.buckets_5)

    def test_empty_buckets_hit_rate_is_none(self):
        summary = evaluate_confidence_calibration([])
        assert all(b.hit_rate is None for b in summary.buckets_5)

    def test_insufficient_data_other_fields_unchanged(self):
        rows = _make_rows(3)
        summary = evaluate_confidence_calibration(rows)
        assert summary.available is True
        assert summary.observe_only is True
        assert summary.total_resolved == 3
        assert summary.overall_hit_rate is None
        assert summary.overall_calibration_gap is None
        assert summary.signal_results == []
