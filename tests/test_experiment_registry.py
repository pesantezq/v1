"""Phase 8 — experiment registry + research-integrity controls (observe-only).

Durable, auditable record of every research experiment: hypothesis -> commit ->
discovery/calibration/validation/OOS windows -> result -> promotion state.
Failures are retained; in-sample discovery cannot masquerade as OOS validation.

TDD: written before portfolio_automation/experiment_registry.py existed.
"""
from __future__ import annotations

import json
from pathlib import Path

import portfolio_automation.experiment_registry as er


_NOW = "2026-06-30T09:00:00+00:00"


def _exp(**over):
    base = dict(
        experiment_id="exp_001", hypothesis="crowd flock confirms momentum",
        rationale="retail attention precedes continuation", owner="research",
        source_commit="abc1234", data_sources=["signal_outcomes.csv"],
        pit_policy="point_in_time", discovery_window=["2024-01", "2024-06"],
        calibration_window=["2024-07", "2024-09"], validation_window=["2024-10", "2024-12"],
        oos_window=["2025-01", "2025-03"], benchmark="SPY",
        cost_assumptions={"bps": 5}, variants_tested=3, now=_NOW,
    )
    base.update(over)
    return er.new_experiment(**base)


# ---------------------------------------------------------------------------
# Schema + statuses
# ---------------------------------------------------------------------------

_REQUIRED = {"experiment_id", "hypothesis", "rationale", "owner", "source_commit",
             "data_sources", "pit_policy", "discovery_window", "calibration_window",
             "validation_window", "oos_window", "benchmark", "cost_assumptions",
             "variants_tested", "selected_variant", "status", "result",
             "failure_conditions", "promotion_state", "created_at", "updated_at"}


def test_new_experiment_has_all_required_fields():
    e = _exp()
    assert _REQUIRED.issubset(set(e)), _REQUIRED - set(e)
    assert e["status"] == "proposed"
    assert e["variants_tested"] == 3


def test_all_statuses_declared():
    for s in ("proposed", "running", "inconclusive", "rejected", "validated",
              "promoted", "degraded", "retired", "superseded"):
        assert s in er.EXPERIMENT_STATUSES


# ---------------------------------------------------------------------------
# Research-integrity controls
# ---------------------------------------------------------------------------


def test_discovery_and_validation_must_be_disjoint():
    # overlapping discovery + validation windows -> integrity warning, not validatable
    e = _exp(discovery_window=["2024-01", "2024-12"], validation_window=["2024-06", "2025-01"])
    chk = er.validate_research_controls(e)
    assert chk["discovery_validation_disjoint"] is False
    assert "discovery_validation_overlap" in chk["warnings"]


def test_disjoint_windows_pass():
    chk = er.validate_research_controls(_exp())
    assert chk["discovery_validation_disjoint"] is True


def test_pit_unsupported_dataset_marks_degraded():
    e = _exp(pit_policy="not_point_in_time")
    chk = er.validate_research_controls(e)
    assert chk["pit_supported"] is False
    assert chk["recommended_status"] == "degraded"


def test_multiple_testing_disclosed():
    chk = er.validate_research_controls(_exp(variants_tested=20))
    assert chk["multiple_testing_risk"] is True  # many variants -> disclose


def test_insufficient_sample_flagged():
    chk = er.validate_research_controls(_exp(), oos_sample_size=5)
    assert chk["sample_sufficient"] is False


# ---------------------------------------------------------------------------
# Persistence: durable, retains failures, idempotent
# ---------------------------------------------------------------------------


def test_register_then_update_retains_history(tmp_path):
    er.register_experiment(tmp_path, _exp())
    # reject it
    er.update_experiment(tmp_path, "exp_001", status="rejected",
                         result={"reason": "no edge OOS"}, now=_NOW)
    reg = er.read_registry(tmp_path)
    e = next(x for x in reg if x["experiment_id"] == "exp_001")
    assert e["status"] == "rejected"
    # failed experiment is RETAINED (not deleted)
    assert e["result"]["reason"] == "no edge OOS"
    assert len(reg) == 1


def test_register_is_idempotent_on_same_id(tmp_path):
    er.register_experiment(tmp_path, _exp())
    er.register_experiment(tmp_path, _exp())  # same id -> no duplicate
    assert len(er.read_registry(tmp_path)) == 1


def test_supersession_links(tmp_path):
    er.register_experiment(tmp_path, _exp())
    er.register_experiment(tmp_path, _exp(experiment_id="exp_002", supersedes="exp_001"))
    er.update_experiment(tmp_path, "exp_001", status="superseded", now=_NOW)
    reg = {e["experiment_id"]: e for e in er.read_registry(tmp_path)}
    assert reg["exp_001"]["status"] == "superseded"
    assert reg["exp_002"]["supersedes"] == "exp_001"
