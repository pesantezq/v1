"""Tests for portfolio_automation.daily_check_runner.

These exercise the deterministic triage paths the cron wrapper depends on.
Each test uses a tmp_path repo and writes synthetic outputs/latest/ JSONs
to drive the triage to a specific verdict.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from portfolio_automation import daily_check_runner as dcr


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _base_artifacts(root: Path, *, current_fp: str = "fp_test") -> None:
    """Write a minimally-healthy artifact set under outputs/latest/."""
    latest = root / "outputs" / "latest"
    _write(
        latest / "daily_run_status.json",
        {
            "overall_status": "ok",
            "stage_summary": {"total": 24, "ok": 17, "warn": 0, "failed": 0},
            "required_missing_count": 0,
        },
    )
    _write(
        latest / "risk_delta.json",
        {
            "overall_status": "ok",
            "concentration": {
                "top_position": {
                    "symbol": "QQQ",
                    "weight": 0.50,
                    "cap": 0.60,
                    "headroom": 0.10,
                }
            },
            "leverage": {"total_exposure": 0.15, "cap": 0.25},
        },
    )
    _write(
        latest / "retune_impact.json",
        {
            "current_fingerprint": current_fp,
            "outcome_attribution": {
                "pre_tracker_label": "pre_tracker_unknown",
                "by_fingerprint": {
                    current_fp: {
                        "resolved_1d": 5,
                        "hit_rate_1d": 0.6,
                    },
                    "pre_tracker_unknown": {
                        "resolved_1d": 100,
                        "hit_rate_1d": 0.55,
                    },
                },
            },
        },
    )
    _write(
        latest / "fmp_budget_status.json",
        {
            "budget": {"status": "ok", "count_today": 100, "budget": 250},
        },
    )
    _write(
        latest / "decisions_due_for_resolution.json",
        {"stuck_count": 0, "by_ticker": []},
    )
    # Gauge versions tail line — matches current_fp first_seen_at = now-3d.
    gauge_path = root / "data" / "gauge_versions.jsonl"
    gauge_path.parent.mkdir(parents=True, exist_ok=True)
    gauge_path.write_text(
        json.dumps(
            {
                "fingerprint": current_fp,
                "first_seen_at": "2026-05-19T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _now() -> datetime:
    return datetime(2026, 5, 22, 9, 15, tzinfo=timezone.utc)


def test_load_state_missing_returns_defaults(tmp_path: Path) -> None:
    state = dcr._load_state(tmp_path)
    assert state["last_fingerprint"] == ""
    assert state["thresholds_crossed"] == []


def test_newly_crossed_thresholds_skips_already_recorded() -> None:
    assert dcr._newly_crossed(44, []) == ["n_10", "n_30"]
    assert dcr._newly_crossed(44, ["n_10"]) == ["n_30"]
    assert dcr._newly_crossed(44, ["n_10", "n_30"]) == []
    assert dcr._newly_crossed(150, []) == ["n_10", "n_30", "n_50", "n_100"]


def test_green_when_everything_nominal(tmp_path: Path) -> None:
    _base_artifacts(tmp_path)
    result = dcr.run_daily_check(tmp_path, now=_now())
    assert result.verdict == "GREEN"
    assert "stages OK" in result.headline
    assert result.red_action is None


def test_red_when_budget_exhausted(tmp_path: Path) -> None:
    _base_artifacts(tmp_path)
    _write(
        tmp_path / "outputs" / "latest" / "fmp_budget_status.json",
        {"budget": {"status": "exhausted", "count_today": 250, "budget": 250}},
    )
    result = dcr.run_daily_check(tmp_path, now=_now())
    assert result.verdict == "RED"
    assert "FMP" in result.headline
    assert result.red_action is not None
    assert "fmp_daily_calls_budget" in result.red_action


def test_red_when_stuck_signals_present(tmp_path: Path) -> None:
    _base_artifacts(tmp_path)
    _write(
        tmp_path / "outputs" / "latest" / "decisions_due_for_resolution.json",
        {
            "stuck_count": 3,
            "by_ticker": [{"symbol": "NVDA", "count": 2}],
        },
    )
    result = dcr.run_daily_check(tmp_path, now=_now())
    assert result.verdict == "RED"
    assert "NVDA" in (result.red_action or "")


def test_red_when_concentration_breach(tmp_path: Path) -> None:
    _base_artifacts(tmp_path)
    _write(
        tmp_path / "outputs" / "latest" / "risk_delta.json",
        {
            "overall_status": "breach",
            "concentration": {
                "top_position": {
                    "symbol": "QQQ",
                    "weight": 0.70,
                    "cap": 0.60,
                    "headroom": -0.10,
                }
            },
            "leverage": {"total_exposure": 0.15, "cap": 0.25},
        },
    )
    result = dcr.run_daily_check(tmp_path, now=_now())
    assert result.verdict == "RED"
    assert "QQQ" in (result.red_action or "")


def test_red_when_retune_overperforming(tmp_path: Path) -> None:
    _base_artifacts(tmp_path)
    # Bump current_fp resolved_1d to 44 with strong hit_rate to trigger the
    # +10pp / n>=30 RED branch.
    _write(
        tmp_path / "outputs" / "latest" / "retune_impact.json",
        {
            "current_fingerprint": "fp_test",
            "outcome_attribution": {
                "pre_tracker_label": "pre_tracker_unknown",
                "by_fingerprint": {
                    "fp_test": {"resolved_1d": 44, "hit_rate_1d": 0.79},
                    "pre_tracker_unknown": {"resolved_1d": 352, "hit_rate_1d": 0.40},
                },
            },
        },
    )
    result = dcr.run_daily_check(tmp_path, now=_now())
    assert result.verdict == "RED"
    assert "Retune validated" in result.headline


def test_red_when_retune_underperforming(tmp_path: Path) -> None:
    _base_artifacts(tmp_path)
    _write(
        tmp_path / "outputs" / "latest" / "retune_impact.json",
        {
            "current_fingerprint": "fp_test",
            "outcome_attribution": {
                "pre_tracker_label": "pre_tracker_unknown",
                "by_fingerprint": {
                    "fp_test": {"resolved_1d": 40, "hit_rate_1d": 0.20},
                    "pre_tracker_unknown": {"resolved_1d": 352, "hit_rate_1d": 0.50},
                },
            },
        },
    )
    result = dcr.run_daily_check(tmp_path, now=_now())
    assert result.verdict == "RED"
    assert "underperforming" in (result.red_action or "")


def test_red_when_all_artifacts_missing(tmp_path: Path) -> None:
    # No outputs/latest written → degrade to "cron did not run".
    result = dcr.run_daily_check(tmp_path, now=_now())
    assert result.verdict == "RED"
    assert result.headline == "cron did not run today"


def test_amber_when_near_cap_only(tmp_path: Path) -> None:
    _base_artifacts(tmp_path)
    _write(
        tmp_path / "outputs" / "latest" / "fmp_budget_status.json",
        {"budget": {"status": "near_cap", "count_today": 230, "budget": 250}},
    )
    _write(
        tmp_path / "outputs" / "latest" / "risk_delta.json",
        {
            "overall_status": "near_cap",
            "concentration": {
                "top_position": {
                    "symbol": "QQQ",
                    "weight": 0.55,
                    "cap": 0.60,
                    "headroom": 0.05,
                }
            },
            "leverage": {"total_exposure": 0.15, "cap": 0.25},
        },
    )
    result = dcr.run_daily_check(tmp_path, now=_now())
    assert result.verdict == "AMBER"
    assert "near cap" in result.headline.lower() or "near" in result.headline.lower()


def test_state_written_with_newly_crossed_thresholds(tmp_path: Path) -> None:
    _base_artifacts(tmp_path)
    _write(
        tmp_path / "outputs" / "latest" / "retune_impact.json",
        {
            "current_fingerprint": "fp_test",
            "outcome_attribution": {
                "pre_tracker_label": "pre_tracker_unknown",
                "by_fingerprint": {
                    "fp_test": {"resolved_1d": 44, "hit_rate_1d": 0.50},
                    "pre_tracker_unknown": {
                        "resolved_1d": 100,
                        "hit_rate_1d": 0.48,
                    },
                },
            },
        },
    )
    dcr.run_daily_check(tmp_path, now=_now())
    state = json.loads((tmp_path / "data" / "daily_check_state.json").read_text())
    assert state["last_fingerprint"] == "fp_test"
    assert state["last_current_fp_resolved_1d"] == 44
    assert "n_10" in state["thresholds_crossed"]
    assert "n_30" in state["thresholds_crossed"]
    assert "n_50" not in state["thresholds_crossed"]


def test_fingerprint_change_resets_thresholds(tmp_path: Path) -> None:
    # Seed prior state with thresholds already crossed under an old fingerprint.
    state_path = tmp_path / "data" / "daily_check_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "last_run_at": "2026-05-21T09:15:00Z",
                "last_fingerprint": "old_fp",
                "last_current_fp_resolved_1d": 88,
                "last_pre_tracker_hit_rate_1d": 0.40,
                "thresholds_crossed": ["n_10", "n_30", "n_50"],
            }
        ),
        encoding="utf-8",
    )

    _base_artifacts(tmp_path, current_fp="new_fp")
    # Bump current resolved_1d so n_10 should re-cross under the new fp.
    _write(
        tmp_path / "outputs" / "latest" / "retune_impact.json",
        {
            "current_fingerprint": "new_fp",
            "outcome_attribution": {
                "pre_tracker_label": "pre_tracker_unknown",
                "by_fingerprint": {
                    "new_fp": {"resolved_1d": 12, "hit_rate_1d": 0.50},
                    "pre_tracker_unknown": {
                        "resolved_1d": 100,
                        "hit_rate_1d": 0.48,
                    },
                },
            },
        },
    )

    result = dcr.run_daily_check(tmp_path, now=_now())
    assert result.fingerprint_changed is True

    state = json.loads(state_path.read_text())
    # Old thresholds reset; only n_10 re-crossed under new fp.
    assert state["thresholds_crossed"] == ["n_10"]


def test_format_report_markdown_includes_heartbeat_line() -> None:
    result = dcr.DailyCheckResult(
        verdict="AMBER",
        headline="WARN — FMP near cap; others nominal",
        body_lines=["Attribution: x", "Risk: y"],
    )
    md = dcr.format_report_markdown(result, "2026-05-22")
    first_line = md.splitlines()[0]
    assert first_line.startswith("[AMBER] daily check 2026-05-22:")
    assert "Attribution: x" in md
    assert "Risk: y" in md


def test_write_report_creates_directory(tmp_path: Path) -> None:
    result = dcr.DailyCheckResult(
        verdict="GREEN", headline="x", body_lines=["No action required."]
    )
    path = dcr.write_report(tmp_path, result, "2026-05-22")
    assert path.exists()
    assert path.parent.name == "daily_checks"
    assert path.read_text().startswith("[GREEN] daily check 2026-05-22:")


def test_agent_dispatch_signals_includes_attribution_analyst_on_milestone() -> None:
    dispatch = dcr._agent_dispatch_signals(
        status={"overall_status": "ok", "required_missing_count": 0},
        due={"stuck_count": 0},
        newly_crossed=["n_30"],
        fingerprint_changed=False,
        delta_hit_rate_pp=2.0,
        current_fp_resolved_1d=30,
        current_fp_age_days=3,
    )
    assert "portfolio-attribution-analyst" in dispatch
    assert "portfolio-resolver-investigator" not in dispatch


def test_agent_dispatch_signals_includes_investigator_on_stuck() -> None:
    dispatch = dcr._agent_dispatch_signals(
        status={"overall_status": "ok", "required_missing_count": 0},
        due={"stuck_count": 2},
        newly_crossed=[],
        fingerprint_changed=False,
        delta_hit_rate_pp=None,
        current_fp_resolved_1d=10,
        current_fp_age_days=1,
    )
    assert "portfolio-resolver-investigator" in dispatch
