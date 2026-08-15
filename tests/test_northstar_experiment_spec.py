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
from portfolio_automation.northstar.experiments import (
    ExperimentResult, ExperimentSpec, SCHEMA_VERSION,
)

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


# =========================== ExperimentResult ==============================
def _rprov(**kw) -> Provenance:
    return Provenance(
        producer_id=kw.pop("producer_id", "stratlab.walkforward"),
        producer_type=kw.pop("producer_type", "system"),
        recorded_at=kw.pop("recorded_at", T1),
        model_id=kw.pop("model_id", "stratlab.walkforward@1"),
        code_version=kw.pop("code_version", "abc1234"),
        **kw,
    )


def _result(**kw) -> ExperimentResult:
    return ExperimentResult(
        experiment_spec_id=kw.pop("experiment_spec_id", _spec().experiment_spec_id),
        provenance=kw.pop("provenance", _rprov()),
        windows_evaluated=kw.pop("windows_evaluated", ("20d", "60d")),
        observations=kw.pop("observations", {"return.excess_spy_20d": {"mean": 0.012, "n": 42, "p": 0.03}}),
        **kw,
    )


def test_result_valid_and_prefix():
    r = _result()
    assert r.experiment_result_id.startswith("exr_")
    assert r.contract_type == "experiment_result"


def test_result_references_spec_by_valid_id():
    with pytest.raises(ValueError):
        _result(experiment_spec_id="rcl_not_a_spec")
    with pytest.raises(ValueError):
        _result(experiment_spec_id="nope")


def test_result_identity_order_free_and_deterministic():
    a = _result(windows_evaluated=("20d", "60d"))
    b = _result(windows_evaluated=("60d", "20d"))
    assert a.experiment_result_id == b.experiment_result_id


@pytest.mark.parametrize("change", [
    dict(windows_evaluated=("20d",)),
    dict(observations={"return.excess_spy_20d": {"mean": 0.02, "n": 42, "p": 0.03}}),
    dict(partial=True, partial_reason="only 20d ran"),
    dict(provenance=_rprov(model_id="other.model@2")),
    dict(provenance=_rprov(code_version="def5678")),
])
def test_result_identity_changes(change):
    assert _result().experiment_result_id != _result(**change).experiment_result_id


def test_result_provenance_producer_not_identity_bearing():
    # producer_id / recorded_at are attribution; model_id + code_version ARE identity.
    a = _result()
    b = _result(provenance=_rprov(producer_id="other.runner", recorded_at=T0))
    assert a.experiment_result_id == b.experiment_result_id


def test_result_observations_required_and_frozen():
    with pytest.raises(ValueError):
        _result(observations={})
    payload = {"return.excess_spy_20d": {"mean": 0.01, "n": 30, "p": 0.04}}
    r = _result(observations=payload)
    payload["return.excess_spy_20d"]["mean"] = 999   # mutate caller dict
    assert r.observations_copy()["return.excess_spy_20d"]["mean"] == 0.01   # unchanged


def test_result_rejects_authority_observation_keys():
    for bad in ({"certified": True}, {"metrics": {"approve": 1}}, {"allocate": 100}):
        with pytest.raises(ValueError):
            _result(observations=bad)


def test_result_partial_discipline():
    with pytest.raises(ValueError):
        _result(partial=True)                        # partial without reason
    with pytest.raises(ValueError):
        _result(partial=False, partial_reason="x")   # reason without partial
    assert _result(partial=True, partial_reason="provider outage; only 20d ran").partial


def test_result_roundtrip_and_tamper():
    r = _result()
    payload = json.loads(canonical_dumps(r.to_canonical_dict()))
    back = ExperimentResult.from_dict(payload)
    assert back.experiment_result_id == r.experiment_result_id
    assert back.to_canonical_dict() == r.to_canonical_dict()
    payload["experiment_result_id"] = "exr_deadbeef"
    with pytest.raises(ValueError):
        ExperimentResult.from_dict(payload)


def test_result_wrong_contract_type_rejected():
    d = json.loads(canonical_dumps(_result().to_canonical_dict()))
    d["contract_type"] = "experiment_spec"
    with pytest.raises(ValueError):
        ExperimentResult.from_dict(d)


def test_result_cannot_rewrite_spec_no_prereg_fields():
    # structural: an ExperimentResult holds NO preregistration/spec fields — it
    # only references the spec by id, so it can never edit preregistered criteria.
    names = {f.name for f in dataclasses.fields(ExperimentResult)}
    prereg = {"universe", "metrics", "success_gate", "abandon_gate",
              "evaluation_windows", "allowed_evidence_types", "hypothesis_claim_id"}
    assert names.isdisjoint(prereg), f"ExperimentResult must not carry spec fields: {names & prereg}"


def test_result_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        _result().partial = True   # type: ignore[misc]
