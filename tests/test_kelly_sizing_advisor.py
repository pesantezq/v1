"""Tests for portfolio_automation/kelly_sizing_advisor.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portfolio_automation.kelly_sizing_advisor import (
    _KELLY_HARD_CAP,
    _MIN_RESOLVED_FOR_KELLY,
    _TARGET_DECISIONS,
    build_plan,
    evaluate_decision_group,
    kelly_fraction,
    run_kelly_sizing_advisor,
)


# ---------------------------------------------------------------------------
# kelly_fraction
# ---------------------------------------------------------------------------


def test_kelly_zero_when_avg_loss_zero():
    assert kelly_fraction(hit_rate=0.7, avg_win_pct=0.05, avg_loss_pct=0.0) == 0.0


def test_kelly_zero_when_negative_edge():
    # 30% hit rate × 4% win vs 3% loss → negative edge
    assert kelly_fraction(hit_rate=0.30, avg_win_pct=0.04, avg_loss_pct=0.03) == 0.0


def test_kelly_positive_when_positive_edge():
    # 60% hit rate × 5% win vs 3% loss → positive edge
    # b = 5/3 ≈ 1.667; full Kelly = (1.667*0.6 - 0.4) / 1.667 = 0.36
    # half-Kelly = 0.18; below 25% cap
    f = kelly_fraction(hit_rate=0.60, avg_win_pct=0.05, avg_loss_pct=0.03)
    assert 0.15 < f < 0.20


def test_kelly_clamped_at_hard_cap():
    # Heavily favourable inputs → full Kelly > 50% → half-Kelly hits cap
    f = kelly_fraction(hit_rate=0.95, avg_win_pct=0.20, avg_loss_pct=0.02)
    assert f == _KELLY_HARD_CAP


def test_kelly_full_vs_half():
    full = kelly_fraction(hit_rate=0.6, avg_win_pct=0.05,
                          avg_loss_pct=0.03, half_kelly=False)
    half = kelly_fraction(hit_rate=0.6, avg_win_pct=0.05, avg_loss_pct=0.03)
    assert full > half


# ---------------------------------------------------------------------------
# evaluate_decision_group
# ---------------------------------------------------------------------------


def test_eval_insufficient_data_below_threshold():
    rows = [{"resolved": True, "direction_correct": True, "return_pct": 0.03}] * 5
    r = evaluate_decision_group(rows, "BUY")
    assert r["status"] == "insufficient_data"
    assert r["kelly_fraction_suggested"] is None


def _resolved_row(*, correct: bool, ret: float) -> dict:
    return {
        "resolved": True,
        "direction_correct": correct,
        "return_pct": ret,
    }


def test_eval_sufficient_data_positive_edge():
    rows = [_resolved_row(correct=True, ret=0.04) for _ in range(15)]
    rows += [_resolved_row(correct=False, ret=-0.02) for _ in range(10)]
    # n_judgeable = 25 ≥ 20; hit_rate=60%, avg_win=4%, avg_loss=2%
    r = evaluate_decision_group(rows, "BUY")
    assert r["status"] == "ok"
    assert r["hit_rate"] == pytest.approx(0.6, abs=1e-6)
    assert r["kelly_fraction_suggested"] > 0


def test_eval_insufficient_when_no_losses():
    rows = [_resolved_row(correct=True, ret=0.04) for _ in range(25)]
    r = evaluate_decision_group(rows, "BUY")
    assert r["status"] == "insufficient_data"
    assert "no positive or no negative" in r.get("reason", "")


# ---------------------------------------------------------------------------
# build_plan
# ---------------------------------------------------------------------------


def test_plan_has_one_row_per_target_decision():
    plan = build_plan(rows_by_decision={}, notes=[])
    decisions = [r["decision"] for r in plan["by_decision"]]
    assert decisions == list(_TARGET_DECISIONS)


def test_plan_observe_only_hardcoded():
    plan = build_plan(rows_by_decision={}, notes=[])
    assert plan["observe_only"] is True
    assert plan["schema_version"] == "1"
    assert plan["half_kelly"] is True
    assert plan["hard_cap"] == _KELLY_HARD_CAP


# ---------------------------------------------------------------------------
# run_kelly_sizing_advisor — integration
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )


def test_run_no_data_returns_insufficient(tmp_path):
    plan = run_kelly_sizing_advisor(tmp_path, base_dir=tmp_path / "outputs")
    assert plan["observe_only"] is True
    for row in plan["by_decision"]:
        assert row["status"] == "insufficient_data"
    out_json = tmp_path / "outputs" / "latest" / "kelly_sizing_advisor.json"
    assert out_json.exists()


def test_run_with_buy_data(tmp_path):
    outcomes = (
        [{"decision": "BUY", "resolved": True, "direction_correct": True,
          "return_pct": 0.05}] * 15
        + [{"decision": "BUY", "resolved": True, "direction_correct": False,
            "return_pct": -0.03}] * 10
    )
    _write_jsonl(tmp_path / "outputs" / "policy" / "decision_outcomes.jsonl",
                 outcomes)
    plan = run_kelly_sizing_advisor(tmp_path, base_dir=tmp_path / "outputs")
    buy_row = next(r for r in plan["by_decision"] if r["decision"] == "BUY")
    assert buy_row["status"] == "ok"
    assert buy_row["kelly_fraction_suggested"] > 0
    # Other decisions still insufficient
    sell_row = next(r for r in plan["by_decision"] if r["decision"] == "SELL")
    assert sell_row["status"] == "insufficient_data"


def test_run_observe_only_artifact(tmp_path):
    run_kelly_sizing_advisor(tmp_path, base_dir=tmp_path / "outputs")
    payload = json.loads(
        (tmp_path / "outputs" / "latest" / "kelly_sizing_advisor.json")
        .read_text("utf-8")
    )
    assert payload["observe_only"] is True
    assert "advisory" in payload["advisory_disclaimer"].lower()
