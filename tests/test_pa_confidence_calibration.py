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
    MIN_RESOLVED_ROWS,
    OUTCOMES_JSONL_RELATIVE_PATH,
    _confidence_bucket,
    _group_stats,
    build_calibration,
    compute_confidence_buckets,
    compute_decision_analysis,
    compute_validation_analysis,
    compute_overall,
    generate_insights,
    render_calibration_md,
    run_calibration,
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
