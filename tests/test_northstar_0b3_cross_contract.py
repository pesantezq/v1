"""Northstar 0B.3 — cross-contract certification.

Builds the full milestone-3 graph end to end from a single evidence root and
proves the architecture-law separation invariants across ALL contract families at
once (each family also has its own dedicated suite). This is the graph-level gate:

    ResearchClaim -> ExperimentSpec -> ExperimentResult
    PredictionRecord -> CapitalProposal
    PredictionRecord/context -> ExitProposal
    prediction/proposal/action refs -> OutcomeRecord
    validated evidence/governance refs -> StrategyPassport

Invariants proven here:
  * every contract type + id prefix is distinct;
  * PredictionRecord cannot become CapitalProposal (no inheritance; reference only);
  * CapitalProposal cannot mutate PredictionRecord / execute capital action;
  * ExitProposal cannot execute an exit;
  * ExperimentResult cannot rewrite ExperimentSpec;
  * OutcomeRecord cannot rewrite earlier predictions/proposals (ids only; no collapse);
  * StrategyPassport cannot grant itself production/capital authority;
  * no contract smuggles authority through arbitrary payload keys;
  * identity / PIT / provenance behave consistently across all six (round-trip
    reproduces identity; provenance is never identity-bearing; PIT bounds present).
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone

import pytest

from portfolio_automation.northstar import (
    CapitalProposal, DataSourceDescriptor, EvidenceSnapshot, ExitProposal, ExperimentResult,
    ExperimentSpec, OutcomeRecord, PointInTime, PredictionRecord, PredictionTask, Provenance,
    ResearchClaim, ResearchTask, StrategyPassport, WorkerResult, canonical_dumps,
)
from portfolio_automation.northstar.pit import KNOWN_AT_SOURCE_REPORTED

UTC = timezone.utc
T0 = datetime(2026, 8, 5, 13, 30, tzinfo=UTC)
T1 = datetime(2026, 8, 9, 11, 0, tzinfo=UTC)
T2 = datetime(2026, 9, 6, 20, 0, tzinfo=UTC)


def _prov(**kw) -> Provenance:
    return Provenance(producer_id=kw.pop("producer_id", "system.graph"),
                      producer_type=kw.pop("producer_type", "system"),
                      recorded_at=kw.pop("recorded_at", T2), **kw)


def _snapshot(evidence_type="market_data.close"):
    src = DataSourceDescriptor(provider="fmp", dataset="quotes_daily", source_type="market_data")
    return EvidenceSnapshot(
        source_id=src.source_id, entity_id="AAPL", entity_type="symbol", evidence_type=evidence_type,
        pit=PointInTime(observed_at=T0, known_at=T0, known_at_basis=KNOWN_AT_SOURCE_REPORTED, retrieved_at=T1),
        provenance=_prov(producer_id="adapter.fmp", producer_type="source_adapter", source_id=src.source_id),
        payload={"close": 231.5})


def _graph():
    """Construct one instance of every milestone-3 contract, correctly wired."""
    # research root
    rt = ResearchTask(question="Does float turnover predict 20d excess return?", as_of=T1,
                      allowed_evidence_types=("market_data.close",), output_expectation="worker_result.findings",
                      provenance=_prov(producer_id="system.research"))
    wr = WorkerResult(research_task_id=rt.research_task_id, worker_id="worker.researcher",
                      provenance=_prov(producer_id="worker.researcher", producer_type="ai_worker", model_id="qwen2.5:7b"),
                      findings={"corr": 0.2}, confidence=0.6)
    rcl = ResearchClaim(claim="High float turnover increases 20d excess return",
                        testable_metric="return.excess_spy_20d", direction="increase",
                        provenance=_prov(producer_id="worker.researcher", producer_type="ai_worker"),
                        worker_result_ids=(wr.worker_result_id,))
    # experiment
    exs = ExperimentSpec(hypothesis_claim_id=rcl.claim_id, universe=("AAPL",), as_of=T1,
                         evaluation_windows=("20d",), metrics=("return.excess_spy_20d",),
                         success_gate="excess>0 at 20d", abandon_gate="excess<=0 at 20d",
                         provenance=_prov(producer_id="system.experiment"),
                         allowed_evidence_types=("market_data.close",))
    exr = ExperimentResult(experiment_spec_id=exs.experiment_spec_id, provenance=_prov(
        producer_id="system.experiment_runner", model_id="model.baseline", code_version="git:abc123"),
        windows_evaluated=("20d",), evidence_refs=(_snapshot().ref(),),
        observations={"excess_spy_20d": 0.006})
    # prediction -> capital / exit
    pt = PredictionTask(entity_ids=("AAPL",), as_of=T1, horizon_days=20, target="return.total",
                        allowed_evidence_types=("market_data.close",), provenance=_prov(producer_id="system.pred"))
    prd = PredictionRecord(task_id=pt.task_id, entity_id="AAPL", as_of=T1, horizon_days=20,
                           prediction_kind="point_estimate", prediction_value=0.031, uncertainty_kind="stdev",
                           uncertainty_value=0.045, model_id="model.baseline", model_version="0.1.0",
                           evidence_refs=(_snapshot().ref(),), provenance=_prov(producer_id="model.baseline"))
    cap = CapitalProposal(prediction_record_ids=(prd.prediction_id,), rationale="positive 20d estimate",
                          provenance=_prov(), proposed_sizing={"AAPL": {"target_weight": 0.03}})
    xit = ExitProposal(position_ref="portfolio.position.AAPL", proposed_action_kind="trim",
                       rationale="thesis weakened", provenance=_prov(),
                       prediction_record_ids=(prd.prediction_id,), proposed_terms={"trim_fraction": 0.5})
    # outcome (attributes prediction/allocation/exit + realized action)
    out = OutcomeRecord(resolution_as_of=T2, resolution_status="resolved", provenance=_prov(),
                        prediction_record_ids=(prd.prediction_id,), capital_proposal_ids=(cap.capital_proposal_id,),
                        exit_proposal_ids=(xit.exit_proposal_id,),
                        realized_action_refs=("action:broker-fill-2026-09-06",),
                        evidence_refs=(_snapshot("market_data.resolved_return").ref(),),
                        component_outcomes={"prediction": {"error": -0.004}, "allocation": {"contribution_bps": 12.0},
                                            "exit": {"timing_bps": 3.5}, "portfolio": {"period_return": 0.018}})
    # passport (evidence trail from experiment result + outcome + claim)
    spp = StrategyPassport(strategy_id="strategy.float_turnover_20d", lifecycle_stage="certified", as_of=T2,
                           status_rationale="passed certification gate", provenance=_prov(),
                           experiment_result_ids=(exr.experiment_result_id,),
                           outcome_record_ids=(out.outcome_record_id,), research_claim_ids=(rcl.claim_id,))
    return dict(rcl=rcl, exs=exs, exr=exr, prd=prd, cap=cap, xit=xit, out=out, spp=spp)


G = _graph()


def test_full_graph_wired_by_reference():
    # each downstream contract references its upstream by the exact upstream id
    assert G["exs"].hypothesis_claim_id == G["rcl"].claim_id
    assert G["exr"].experiment_spec_id == G["exs"].experiment_spec_id
    assert G["cap"].prediction_record_ids == (G["prd"].prediction_id,)
    assert G["xit"].prediction_record_ids == (G["prd"].prediction_id,)
    assert G["prd"].prediction_id in G["out"].prediction_record_ids
    assert G["cap"].capital_proposal_id in G["out"].capital_proposal_ids
    assert G["xit"].exit_proposal_id in G["out"].exit_proposal_ids
    assert G["exr"].experiment_result_id in G["spp"].experiment_result_ids
    assert G["out"].outcome_record_id in G["spp"].outcome_record_ids
    assert G["rcl"].claim_id in G["spp"].research_claim_ids


def test_all_types_and_prefixes_distinct():
    ids = {
        "rcl": G["rcl"].claim_id, "exs": G["exs"].experiment_spec_id,
        "exr": G["exr"].experiment_result_id, "prd": G["prd"].prediction_id,
        "cap": G["cap"].capital_proposal_id, "xit": G["xit"].exit_proposal_id,
        "out": G["out"].outcome_record_id, "spp": G["spp"].strategy_passport_id,
    }
    prefixes = {v.split("_", 1)[0] for v in ids.values()}
    assert prefixes == {"rcl", "exs", "exr", "prd", "cap", "xit", "out", "spp"}
    types = {G[k].contract_type for k in ("rcl", "exs", "exr", "prd", "cap", "xit", "out", "spp")}
    assert len(types) == 8


def test_prediction_cannot_become_capital_proposal():
    # reference, never inheritance
    assert not issubclass(CapitalProposal, PredictionRecord)
    assert not issubclass(PredictionRecord, CapitalProposal)
    assert PredictionRecord not in CapitalProposal.__mro__
    # the proposal holds only the prediction's ID (a str), not the record
    assert all(isinstance(p, str) for p in G["cap"].prediction_record_ids)


def test_capital_proposal_cannot_mutate_prediction_or_execute():
    with pytest.raises(dataclasses.FrozenInstanceError):
        G["cap"].rationale = "x"                     # type: ignore[misc]
    # no execution surface; execution/approval keys rejected from sizing
    with pytest.raises(ValueError):
        CapitalProposal(prediction_record_ids=(G["prd"].prediction_id,), rationale="r", provenance=_prov(),
                        proposed_sizing={"AAPL": {"execute": True}})


def test_exit_proposal_cannot_execute_an_exit():
    for bad in ({"sell_now": True}, {"liquidate": True}, {"broker_order": {"side": "sell"}}):
        with pytest.raises(ValueError):
            ExitProposal(position_ref="p", proposed_action_kind="exit", rationale="r", provenance=_prov(),
                         proposed_terms=bad)


def test_experiment_result_cannot_rewrite_spec():
    # the result holds NO preregistered spec fields — only a reference to the spec id
    result_fields = {f.name for f in dataclasses.fields(ExperimentResult)}
    spec_only = {"universe", "metrics", "success_gate", "abandon_gate", "evaluation_windows",
                 "hypothesis_claim_id", "allowed_evidence_types"}
    assert result_fields.isdisjoint(spec_only)
    assert G["exr"].experiment_spec_id == G["exs"].experiment_spec_id   # reference only


def test_outcome_cannot_rewrite_predictions_or_collapse():
    # holds only ids of the artifacts it attributes
    assert all(isinstance(x, str) for x in G["out"].prediction_record_ids + G["out"].capital_proposal_ids
               + G["out"].exit_proposal_ids)
    # cannot contain a prediction/proposal object field
    out_fields = {f.name for f in dataclasses.fields(OutcomeRecord)}
    assert "prediction_value" not in out_fields and "proposed_sizing" not in out_fields
    # attribution never collapses to a single generic score
    with pytest.raises(ValueError):
        OutcomeRecord(resolution_as_of=T2, resolution_status="resolved", provenance=_prov(),
                      prediction_record_ids=(G["prd"].prediction_id,),
                      component_outcomes={"strategy_success": 0.9})


def test_passport_cannot_grant_itself_authority():
    passport_fields = {f.name for f in dataclasses.fields(StrategyPassport)}
    assert passport_fields.isdisjoint({"production_enabled", "capital_eligible", "deploy",
                                       "may_receive_capital", "approved", "authorized"})
    # even a certified passport rejects an authority-claiming attribute
    with pytest.raises(ValueError):
        StrategyPassport(strategy_id="s", lifecycle_stage="certified", as_of=T2, status_rationale="r",
                         provenance=_prov(), research_claim_ids=(G["rcl"].claim_id,),
                         attributes={"capital_eligible": True})


def test_no_contract_smuggles_authority_through_payload():
    prd_id, rcl_id = G["prd"].prediction_id, G["rcl"].claim_id
    # ExperimentResult observations
    with pytest.raises(ValueError):
        ExperimentResult(experiment_spec_id=G["exs"].experiment_spec_id, provenance=_prov(
            producer_id="p", model_id="m", code_version="v"), windows_evaluated=("20d",),
            observations={"approve": True})
    # OutcomeRecord component
    with pytest.raises(ValueError):
        OutcomeRecord(resolution_as_of=T2, resolution_status="resolved", provenance=_prov(),
                      prediction_record_ids=(prd_id,), component_outcomes={"prediction": {"promote": 1}})
    # StrategyPassport attributes
    with pytest.raises(ValueError):
        StrategyPassport(strategy_id="s", lifecycle_stage="challenger", as_of=T2, status_rationale="r",
                         provenance=_prov(), research_claim_ids=(rcl_id,), attributes={"deploy": True})


def test_identity_round_trips_for_all_six_milestone3_contracts():
    cases = [
        (ExperimentSpec, G["exs"], "experiment_spec_id"),
        (ExperimentResult, G["exr"], "experiment_result_id"),
        (CapitalProposal, G["cap"], "capital_proposal_id"),
        (ExitProposal, G["xit"], "exit_proposal_id"),
        (OutcomeRecord, G["out"], "outcome_record_id"),
        (StrategyPassport, G["spp"], "strategy_passport_id"),
    ]
    for cls, obj, id_attr in cases:
        d = json.loads(canonical_dumps(obj.to_canonical_dict()))
        back = cls.from_dict(d)
        assert getattr(back, id_attr) == getattr(obj, id_attr)
        assert back.to_canonical_dict() == obj.to_canonical_dict()
        # tamper: a wrong recorded id must fail identity reproduction
        d[id_attr] = d[id_attr].split("_", 1)[0] + "_" + "0" * 32
        with pytest.raises(ValueError):
            cls.from_dict(d)


def test_provenance_never_identity_bearing_across_families():
    human = _prov(producer_id="human.reviewer", producer_type="human")
    assert G["cap"].capital_proposal_id == dataclasses.replace(
        G["cap"], provenance=human, proposed_sizing=G["cap"].proposed_sizing_copy()).capital_proposal_id
    assert G["xit"].exit_proposal_id == dataclasses.replace(
        G["xit"], provenance=human, proposed_terms=G["xit"].proposed_terms_copy()).exit_proposal_id
    assert G["spp"].strategy_passport_id == dataclasses.replace(
        G["spp"], provenance=human, attributes=G["spp"].attributes_copy()).strategy_passport_id


def test_pit_bounds_present_across_families():
    # every contract that defines a point-in-time carries an aware datetime
    assert G["exs"].as_of.tzinfo is not None
    assert G["prd"].as_of.tzinfo is not None
    assert G["out"].resolution_as_of.tzinfo is not None
    assert G["spp"].as_of.tzinfo is not None
