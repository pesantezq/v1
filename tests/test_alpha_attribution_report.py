"""Tests for portfolio_automation/alpha_attribution_report.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portfolio_automation.alpha_attribution_report import (
    _MIN_N,
    _TARGET_SOURCES,
    _downside_stdev,
    _stdev,
    build_plan,
    compute_source_metrics,
    run_alpha_attribution_report,
)


# ---------------------------------------------------------------------------
# _stdev / _downside_stdev
# ---------------------------------------------------------------------------


def test_stdev_zero_for_constant():
    vals = [0.01] * 10
    assert _stdev(vals, 0.01) == 0.0


def test_stdev_matches_sample_formula():
    vals = [0.01, 0.02, 0.03]
    mean = sum(vals) / len(vals)
    sd = _stdev(vals, mean)
    assert sd == pytest.approx(0.01, abs=1e-4)


def test_downside_stdev_only_uses_negatives():
    vals = [0.01, -0.02, 0.03, -0.04]
    dsd = _downside_stdev(vals, target=0.0)
    assert dsd > 0


def test_downside_stdev_zero_when_no_losses():
    assert _downside_stdev([0.01, 0.02, 0.03]) == 0.0


# ---------------------------------------------------------------------------
# compute_source_metrics
# ---------------------------------------------------------------------------


def _row(*, correct: bool, ret: float) -> dict:
    return {
        "resolved": True,
        "direction_correct": correct,
        "return_pct": ret,
    }


def test_metrics_insufficient_below_min_n():
    rows = [_row(correct=True, ret=0.02)] * 10
    m = compute_source_metrics(rows)
    assert m["status"] == "insufficient_data"
    assert m["min_required"] == _MIN_N


def test_metrics_ok_with_enough_data():
    rows = ([_row(correct=True, ret=0.02)] * 15
            + [_row(correct=False, ret=-0.01)] * 10)
    m = compute_source_metrics(rows)
    assert m["status"] == "ok"
    assert m["n_returns"] == 25
    assert m["sharpe_proxy"] is not None
    assert m["mean_return_pct"] > 0
    assert m["return_stdev_pct"] > 0


def test_metrics_handle_constant_returns_no_sharpe():
    # 25 identical returns → stdev=0 → sharpe undefined
    rows = [_row(correct=True, ret=0.01)] * 25
    m = compute_source_metrics(rows)
    assert m["status"] == "ok"
    assert m["return_stdev_pct"] == 0.0
    assert m["sharpe_proxy"] is None


# ---------------------------------------------------------------------------
# build_plan
# ---------------------------------------------------------------------------


def test_plan_envelope_observe_only():
    plan = build_plan(rows_by_source={}, notes=[])
    assert plan["observe_only"] is True
    assert plan["schema_version"] == "1"
    assert list(plan["by_source"].keys()) == list(_TARGET_SOURCES)
    assert plan["min_n_required"] == _MIN_N


def test_plan_identifies_best_sharpe_source():
    high_sharpe = ([_row(correct=True, ret=0.03)] * 18
                   + [_row(correct=False, ret=-0.01)] * 7)
    low_sharpe = ([_row(correct=True, ret=0.01)] * 18
                  + [_row(correct=False, ret=-0.02)] * 7)
    plan = build_plan(
        rows_by_source={
            "structural": high_sharpe,
            "market": low_sharpe,
        },
        notes=[],
    )
    assert plan["best_sharpe_source"] == "structural"
    assert plan["worst_sharpe_source"] == "market"


# ---------------------------------------------------------------------------
# run_alpha_attribution_report — integration
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )


def test_run_no_data(tmp_path):
    plan = run_alpha_attribution_report(tmp_path, base_dir=tmp_path / "outputs")
    assert plan["observe_only"] is True
    for src in _TARGET_SOURCES:
        assert plan["by_source"][src]["status"] == "insufficient_data"
    out_json = tmp_path / "outputs" / "latest" / "alpha_attribution_report.json"
    assert out_json.exists()


def test_run_with_watchlist_data(tmp_path):
    rows = ([{"source": "watchlist", "resolved": True,
              "direction_correct": True, "return_pct": 0.02}] * 18
            + [{"source": "watchlist", "resolved": True,
                "direction_correct": False, "return_pct": -0.01}] * 7)
    _write_jsonl(tmp_path / "outputs" / "policy" / "decision_outcomes.jsonl",
                 rows)
    plan = run_alpha_attribution_report(tmp_path, base_dir=tmp_path / "outputs")
    assert plan["by_source"]["watchlist"]["status"] == "ok"
    assert plan["by_source"]["structural"]["status"] == "insufficient_data"


def test_run_observe_only_hardcoded(tmp_path):
    run_alpha_attribution_report(tmp_path, base_dir=tmp_path / "outputs")
    payload = json.loads(
        (tmp_path / "outputs" / "latest" / "alpha_attribution_report.json")
        .read_text("utf-8")
    )
    assert payload["observe_only"] is True
    assert "observational" in payload["advisory_disclaimer"].lower()
