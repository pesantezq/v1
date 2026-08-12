"""Northstar 0B.3 — CapitalProposal contract tests.

Covers deterministic cap_ identity, PredictionRecord cross-reference (referenced by
id, never contained/mutated), advisory sizing snapshot immutability, the hard
capital invariants (proposal != approval != execution != action — execution/approval
keys rejected), round-trip + tamper, and structural absence of approval/action fields.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone

import pytest

from portfolio_automation.northstar import (
    DataSourceDescriptor, EvidenceSnapshot, PointInTime, PredictionRecord, PredictionTask,
    Provenance, canonical_dumps,
)
from portfolio_automation.northstar.pit import KNOWN_AT_SOURCE_REPORTED
from portfolio_automation.northstar.decisions import CapitalProposal

UTC = timezone.utc
T0 = datetime(2026, 8, 5, 13, 30, tzinfo=UTC)
T1 = datetime(2026, 8, 9, 11, 0, tzinfo=UTC)


def _prov(**kw) -> Provenance:
    return Provenance(producer_id=kw.pop("producer_id", "system.capital_engine_v2_shadow"),
                      producer_type=kw.pop("producer_type", "system"),
                      recorded_at=kw.pop("recorded_at", T1), **kw)


def _snapshot():
    src = DataSourceDescriptor(provider="fmp", dataset="quotes_daily", source_type="market_data")
    return EvidenceSnapshot(
        source_id=src.source_id, entity_id="AAPL", entity_type="symbol",
        evidence_type="market_data.close",
        pit=PointInTime(observed_at=T0, known_at=T0, known_at_basis=KNOWN_AT_SOURCE_REPORTED, retrieved_at=T1),
        provenance=_prov(producer_id="adapter.fmp_quotes", producer_type="source_adapter", source_id=src.source_id),
        payload={"close": 231.5, "currency": "USD"})


def _prd_id() -> str:
    task = PredictionTask(entity_ids=("AAPL",), as_of=T1, horizon_days=20, target="return.total",
                          allowed_evidence_types=("market_data.close",),
                          provenance=_prov(producer_id="system.prediction_scheduler"))
    return PredictionRecord(
        task_id=task.task_id, entity_id="AAPL", as_of=T1, horizon_days=20,
        prediction_kind="point_estimate", prediction_value=0.031, uncertainty_kind="stdev",
        uncertainty_value=0.045, model_id="model.shadow_baseline", model_version="0.1.0",
        evidence_refs=(_snapshot().ref(),), provenance=_prov(producer_id="model.shadow_baseline")).prediction_id


PRD = _prd_id()


def _cap(**kw) -> CapitalProposal:
    return CapitalProposal(
        prediction_record_ids=kw.pop("prediction_record_ids", (PRD,)),
        rationale=kw.pop("rationale", "positive 20d excess-return estimate with acceptable uncertainty"),
        provenance=kw.pop("provenance", _prov()),
        proposed_sizing=kw.pop("proposed_sizing", {"AAPL": {"target_weight": 0.03, "max_position_pct": 0.05}}),
        **kw)


def test_valid_and_prefix():
    c = _cap()
    assert c.capital_proposal_id.startswith("cap_") and c.contract_type == "capital_proposal"


def test_references_prediction_by_valid_id_not_object():
    with pytest.raises(ValueError):
        _cap(prediction_record_ids=("rcl_wrong_family",))
    with pytest.raises(ValueError):
        _cap(prediction_record_ids=())                       # a proposal must rely on >=1 prediction
    assert all(isinstance(p, str) for p in _cap().prediction_record_ids)  # referenced, never contained


def test_rationale_and_sizing_required():
    with pytest.raises(ValueError):
        _cap(rationale="")
    with pytest.raises(ValueError):
        _cap(proposed_sizing={})


def test_execution_and_approval_keys_rejected_but_allocation_allowed():
    # allocation content is the proposal's purpose -> allowed
    assert _cap(proposed_sizing={"AAPL": {"target_weight": 0.02, "allocation_pct": 2.0}}).capital_proposal_id
    for bad in ({"AAPL": {"execute": True}}, {"approve": 1}, {"AAPL": {"trade": "buy"}},
                {"order": {"side": "buy"}}, {"AAPL": {"broker": "schwab"}}, {"certified": True}):
        with pytest.raises(ValueError):
            _cap(proposed_sizing=bad)


def test_sizing_frozen():
    payload = {"AAPL": {"target_weight": 0.03}}
    c = _cap(proposed_sizing=payload)
    payload["AAPL"]["target_weight"] = 0.99          # mutate caller dict
    assert c.proposed_sizing_copy()["AAPL"]["target_weight"] == 0.03


def test_identity_deterministic_order_free():
    a = _cap(proposed_sizing={"AAPL": {"target_weight": 0.03}, "MSFT": {"target_weight": 0.02}})
    b = _cap(proposed_sizing={"MSFT": {"target_weight": 0.02}, "AAPL": {"target_weight": 0.03}})
    assert a.capital_proposal_id == b.capital_proposal_id


@pytest.mark.parametrize("change", [
    dict(rationale="different thesis"),
    dict(proposed_sizing={"AAPL": {"target_weight": 0.10}}),
])
def test_identity_changes(change):
    assert _cap().capital_proposal_id != _cap(**change).capital_proposal_id


def test_provenance_not_identity_bearing():
    assert _cap().capital_proposal_id == _cap(provenance=_prov(producer_id="human.pm", producer_type="human")).capital_proposal_id


def test_roundtrip_and_tamper():
    c = _cap()
    d = json.loads(canonical_dumps(c.to_canonical_dict()))
    back = CapitalProposal.from_dict(d)
    assert back.capital_proposal_id == c.capital_proposal_id
    assert back.to_canonical_dict() == c.to_canonical_dict()
    d["capital_proposal_id"] = "cap_deadbeef"
    with pytest.raises(ValueError):
        CapitalProposal.from_dict(d)


def test_wrong_contract_type_rejected():
    d = json.loads(canonical_dumps(_cap().to_canonical_dict()))
    d["contract_type"] = "experiment_spec"
    with pytest.raises(ValueError):
        CapitalProposal.from_dict(d)


def test_frozen_and_no_approval_or_action_fields():
    with pytest.raises(dataclasses.FrozenInstanceError):
        _cap().rationale = "x"   # type: ignore[misc]
    names = {f.name for f in dataclasses.fields(CapitalProposal)}
    forbidden = {"approved", "approval", "certified", "execute", "execution", "order",
                 "trade", "fill", "broker", "authorized", "promoted", "production_ready"}
    assert names.isdisjoint(forbidden), f"CapitalProposal must carry no approval/action fields: {names & forbidden}"
