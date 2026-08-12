"""Northstar Phase 0B milestone 3 — ExperimentSpec contract tests.

Covers: deterministic identity (exs_), preregistration-IS-identity (any change is
a new experiment), PIT as_of discipline, ResearchClaim cross-reference validation,
unordered-set semantics, round-trip + schema-version + tamper rejection, immutability,
and the structural invariants: an ExperimentSpec carries no result data and no
portfolio-action authority (ExperimentSpec != ExperimentResult; not an action).
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone

import pytest

from portfolio_automation.northstar import (
    Provenance, ResearchClaim, ResearchTask, WorkerResult, canonical_dumps,
)
from portfolio_automation.northstar.experiments import ExperimentSpec, SCHEMA_VERSION

UTC = timezone.utc
T0 = datetime(2026, 8, 5, 13, 30, tzinfo=UTC)
T1 = datetime(2026, 8, 9, 11, 0, tzinfo=UTC)


def _prov(**kw) -> Provenance:
    return Provenance(
        producer_id=kw.pop("producer_id", "system.experiment_author"),
        producer_type=kw.pop("producer_type", "system"),
        recorded_at=kw.pop("recorded_at", T1),
        **kw,
    )


def _claim_id() -> str:
    rt = ResearchTask(question="Does float turnover predict 20d excess return?", as_of=T1,
                      allowed_evidence_types=("market_data.close",),
                      output_expectation="worker_result.findings",
                      provenance=_prov(producer_id="system.research_scheduler"))
    wr = WorkerResult(research_task_id=rt.research_task_id, worker_id="worker.generic_researcher",
                      provenance=_prov(producer_id="worker.generic_researcher",
                                       producer_type="ai_worker", model_id="qwen2.5:7b"),
                      findings={"observation": "float turnover elevated"}, confidence=0.7)
    return ResearchClaim(claim="High float turnover increases 20d excess return",
                         testable_metric="return.excess_spy_20d", direction="increase",
                         provenance=_prov(producer_id="worker.generic_researcher", producer_type="ai_worker"),
                         worker_result_ids=(wr.worker_result_id,)).claim_id


RCL = _claim_id()


def _spec(**kw) -> ExperimentSpec:
    return ExperimentSpec(
        hypothesis_claim_id=kw.pop("hypothesis_claim_id", RCL),
        universe=kw.pop("universe", ("AAPL", "MSFT")),
        as_of=kw.pop("as_of", T1),
        evaluation_windows=kw.pop("evaluation_windows", ("20d", "60d")),
        metrics=kw.pop("metrics", ("return.excess_spy_20d",)),
        success_gate=kw.pop("success_gate", "excess_spy_20d>0 at p<0.05"),
        abandon_gate=kw.pop("abandon_gate", "excess_spy_20d<=0 or n<30"),
        provenance=kw.pop("provenance", _prov()),
        **kw,
    )


# --- identity ---------------------------------------------------------------
def test_valid_and_id_prefix():
    s = _spec()
    assert s.experiment_spec_id.startswith("exs_")
    assert s.contract_type == "experiment_spec"


def test_identity_is_deterministic_and_order_free():
    a = _spec(universe=("AAPL", "MSFT"), metrics=("return.excess_spy_20d",))
    b = _spec(universe=("MSFT", "AAPL"), metrics=("return.excess_spy_20d",))
    assert a.experiment_spec_id == b.experiment_spec_id


def test_provenance_and_notes_are_not_identity_bearing():
    a = _spec()
    b = _spec(provenance=_prov(producer_id="human.analyst", producer_type="human"), notes="anything")
    assert a.experiment_spec_id == b.experiment_spec_id


@pytest.mark.parametrize("change", [
    dict(universe=("AAPL",)),
    dict(as_of=T0),
    dict(evaluation_windows=("20d",)),
    dict(metrics=("return.total",)),
    dict(success_gate="different gate"),
    dict(abandon_gate="different abandon"),
    dict(allowed_evidence_types=("fundamental.revenue",)),
])
def test_preregistration_change_is_a_new_experiment(change):
    assert _spec().experiment_spec_id != _spec(**change).experiment_spec_id


def test_hypothesis_change_is_a_new_experiment():
    other = ExperimentSpec(hypothesis_claim_id=RCL, universe=("AAPL",), as_of=T1,
                           evaluation_windows=("20d",), metrics=("m",),
                           success_gate="g", abandon_gate="a", provenance=_prov())
    # a genuinely different claim id -> different experiment
    rt = ResearchTask(question="other question entirely", as_of=T1,
                      allowed_evidence_types=("news.headline",),
                      output_expectation="worker_result.findings", provenance=_prov())
    wr = WorkerResult(research_task_id=rt.research_task_id, worker_id="worker.generic_researcher",
                      provenance=_prov(producer_id="worker.generic_researcher", producer_type="ai_worker"),
                      findings={"x": 2}, confidence=0.5)
    other_rcl = ResearchClaim(claim="different", testable_metric="return.total", direction="decrease",
                              provenance=_prov(producer_id="worker.generic_researcher", producer_type="ai_worker"),
                              worker_result_ids=(wr.worker_result_id,)).claim_id
    assert _spec().experiment_spec_id != _spec(hypothesis_claim_id=other_rcl).experiment_spec_id


# --- validation -------------------------------------------------------------
@pytest.mark.parametrize("field_name", ["hypothesis_claim_id", "success_gate", "abandon_gate"])
def test_required_strings(field_name):
    with pytest.raises(ValueError):
        _spec(**{field_name: ""})


def test_hypothesis_must_be_a_research_claim_id():
    with pytest.raises(ValueError):
        _spec(hypothesis_claim_id="prd_not_a_claim")
    with pytest.raises(ValueError):
        _spec(hypothesis_claim_id="not-an-id")


def test_naive_as_of_rejected():
    with pytest.raises(ValueError):
        _spec(as_of=datetime(2026, 8, 9, 11, 0))   # no tzinfo — PIT discipline


@pytest.mark.parametrize("empty_field", ["universe", "evaluation_windows", "metrics"])
def test_empty_sets_rejected(empty_field):
    with pytest.raises(ValueError):
        _spec(**{empty_field: ()})


def test_wildcard_universe_sole_value():
    assert _spec(universe=("*",)).experiment_spec_id.startswith("exs_")
    with pytest.raises(ValueError):
        _spec(universe=("*", "AAPL"))


def test_provenance_required():
    with pytest.raises(ValueError):
        _spec(provenance=None)


def test_schema_version_gate():
    with pytest.raises(ValueError):
        _spec(schema_version="9.9.9")


# --- serialization ----------------------------------------------------------
def _serialized(s: ExperimentSpec) -> dict:
    # kernel convention: canonical bytes then reload (datetimes -> ISO strings).
    return json.loads(canonical_dumps(s.to_canonical_dict()))


def test_roundtrip_and_identity_reproduction():
    s = _spec()
    r = ExperimentSpec.from_dict(_serialized(s))
    assert r.experiment_spec_id == s.experiment_spec_id
    assert r.to_canonical_dict() == s.to_canonical_dict()


def test_tampered_id_rejected():
    d = _serialized(_spec())
    d["experiment_spec_id"] = "exs_deadbeef"
    with pytest.raises(ValueError):
        ExperimentSpec.from_dict(d)


def test_wrong_contract_type_rejected():
    d = _serialized(_spec())
    d["contract_type"] = "experiment_result"
    with pytest.raises(ValueError):
        ExperimentSpec.from_dict(d)


# --- structural invariants --------------------------------------------------
def test_frozen_immutable():
    s = _spec()
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.success_gate = "changed"          # type: ignore[misc]


def test_carries_no_result_or_action_fields():
    names = {f.name for f in dataclasses.fields(ExperimentSpec)}
    forbidden = {"result", "results", "observation", "observations", "verdict",
                 "metric_values", "outcome", "allocation", "action", "execute",
                 "capital", "trade", "approved", "certified"}
    assert names.isdisjoint(forbidden), f"ExperimentSpec must not carry result/action fields: {names & forbidden}"
