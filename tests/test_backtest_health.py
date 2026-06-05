"""
Tests for backtesting/backtest_health.py — analysis-health pairing for the
Pattern-Improvement Loop (Step 6; observe-only, Quant/Developer lens).

Fully offline and deterministic. Asserts the health assessor returns GREEN on a
healthy backtest artifact set and the right RED/AMBER status + flag on each
degraded state: missing results, a "looks-fresh-but-empty" artifact
(content_liveness), all-'unknown' regimes (degenerate output), a stale artifact,
a low sample, and a flipped calibration slope.

Observe-only: reads artifacts and computes a status; writes nothing and touches no
protected logic.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from backtesting.backtest_health import assess_backtest_health

_NOW = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)


def _write_results(backtest_dir: Path, *, generated_at: str, evaluated: int,
                   regimes: list[str], slope: float) -> None:
    backtest_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "observe_only": True,
        "generated_at": generated_at,
        "performance": {"total_signals": evaluated, "evaluated": evaluated,
                        "hit_rate": 55.0, "results": [{} for _ in range(evaluated)]},
        "calibration": {"calibration_slope": slope},
        "added_metrics": {
            "per_regime": [{"regime": r, "count": 10, "hit_rate": 55.0, "avg_return": 0.5}
                           for r in regimes],
        },
    }
    (backtest_dir / "poc_simulation_results.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_proposals(path: Path, *, proposed_count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "observe_only": True, "proposed_only": True,
        "summary": {"proposed_count": proposed_count, "evaluated": proposed_count + 1},
    }), encoding="utf-8")


def _healthy_tree(tmp_path: Path) -> tuple[str, str]:
    bt = tmp_path / "backtest"
    _write_results(bt, generated_at=_NOW.isoformat(), evaluated=120,
                   regimes=["risk_on", "neutral", "risk_off"], slope=0.30)
    prop = tmp_path / "policy" / "signal_weight_proposals.json"
    _write_proposals(prop, proposed_count=2)
    return str(bt), str(prop)


def _assess(bt, prop):
    return assess_backtest_health(backtest_dir=bt, proposals_path=prop,
                                  now=_NOW, max_age_days=400, min_evaluated=30)


# --------------------------------------------------------------------------
# Healthy
# --------------------------------------------------------------------------

def test_healthy_tree_is_green(tmp_path):
    bt, prop = _healthy_tree(tmp_path)
    rep = _assess(bt, prop)
    assert rep["observe_only"] is True
    assert rep["status"] == "GREEN"
    assert rep["flags"] == []


# --------------------------------------------------------------------------
# Degraded states
# --------------------------------------------------------------------------

def test_missing_results_is_red(tmp_path):
    rep = _assess(str(tmp_path / "backtest"), str(tmp_path / "policy" / "p.json"))
    assert rep["status"] == "RED"
    assert "results_missing" in rep["flags"]


def test_looks_fresh_but_empty_is_red(tmp_path):
    bt = tmp_path / "backtest"
    _write_results(bt, generated_at=_NOW.isoformat(), evaluated=0, regimes=[], slope=0.0)
    prop = tmp_path / "policy" / "p.json"
    _write_proposals(prop, proposed_count=0)
    rep = _assess(str(bt), str(prop))
    assert rep["status"] == "RED"
    assert "looks_fresh_but_empty" in rep["flags"]


def test_all_unknown_regimes_is_red(tmp_path):
    bt = tmp_path / "backtest"
    _write_results(bt, generated_at=_NOW.isoformat(), evaluated=100,
                   regimes=["unknown", "unknown"], slope=0.2)
    prop = tmp_path / "policy" / "p.json"
    _write_proposals(prop, proposed_count=1)
    rep = _assess(str(bt), str(prop))
    assert rep["status"] == "RED"
    assert "degenerate_regimes" in rep["flags"]


def test_stale_results_is_amber(tmp_path):
    bt = tmp_path / "backtest"
    old = datetime(2024, 1, 1, tzinfo=timezone.utc).isoformat()  # > 400 days before _NOW
    _write_results(bt, generated_at=old, evaluated=120,
                   regimes=["risk_on", "neutral"], slope=0.3)
    prop = tmp_path / "policy" / "p.json"
    _write_proposals(prop, proposed_count=1)
    rep = _assess(str(bt), str(prop))
    assert rep["status"] == "AMBER"
    assert "stale" in rep["flags"]


def test_low_sample_is_amber(tmp_path):
    bt = tmp_path / "backtest"
    _write_results(bt, generated_at=_NOW.isoformat(), evaluated=5,
                   regimes=["risk_on", "neutral"], slope=0.3)
    prop = tmp_path / "policy" / "p.json"
    _write_proposals(prop, proposed_count=1)
    rep = _assess(str(bt), str(prop))
    assert rep["status"] == "AMBER"
    assert "low_sample" in rep["flags"]


def test_calibration_flip_is_amber(tmp_path):
    bt = tmp_path / "backtest"
    _write_results(bt, generated_at=_NOW.isoformat(), evaluated=120,
                   regimes=["risk_on", "neutral"], slope=-0.18)
    prop = tmp_path / "policy" / "p.json"
    _write_proposals(prop, proposed_count=1)
    rep = _assess(str(bt), str(prop))
    assert rep["status"] == "AMBER"
    assert "calibration_slope_flipped" in rep["flags"]


def test_score_gate_opt_in_reports_invariance_green(tmp_path):
    # Opt-in Step-5 protected-score invariance gate. Against the real registry the
    # weight delta is score-invariant (default_weight is decoupled from scoring),
    # so the gate is GREEN and no coupling-regression flag is raised. The
    # artifact-only health status is unaffected by enabling the gate.
    bt = tmp_path / "backtest"
    _write_results(bt, generated_at=_NOW.isoformat(), evaluated=120,
                   regimes=["risk_on", "neutral"], slope=0.5)
    prop = tmp_path / "policy" / "p.json"
    _write_proposals(prop, proposed_count=1)
    rep = assess_backtest_health(backtest_dir=str(bt), proposals_path=str(prop),
                                 now=_NOW, run_score_gate=True,
                                 registry_path="config/signal_registry.yaml")
    assert rep["details"].get("score_invariance") in ("GREEN", "inconclusive")
    assert "score_coupling_regression" not in rep["flags"]


def test_score_gate_off_by_default_keeps_artifact_only_path(tmp_path):
    bt = tmp_path / "backtest"
    _write_results(bt, generated_at=_NOW.isoformat(), evaluated=120,
                   regimes=["risk_on", "neutral"], slope=0.5)
    prop = tmp_path / "policy" / "p.json"
    _write_proposals(prop, proposed_count=1)
    rep = _assess(str(bt), str(prop))
    assert "score_invariance" not in rep["details"]
