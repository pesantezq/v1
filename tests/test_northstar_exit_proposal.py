"""Northstar 0B.3 — ExitProposal contract tests.

Covers deterministic xit_ identity, the descriptive action-kind enum
(continue/trim/exit/replace — a proposal LABEL, never an order), position_ref
attribution, optional PredictionRecord cross-reference (referenced by id, never
contained), advisory terms snapshot immutability, the hard exit invariant
(ExitProposal != execution — order keys sell/buy/liquidate/close/broker rejected),
round-trip + tamper, and structural absence of approval/action fields.
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
from portfolio_automation.northstar.decisions import ExitProposal, EXIT_ACTION_KINDS

UTC = timezone.utc
T0 = datetime(2026, 8, 5, 13, 30, tzinfo=UTC)
T1 = datetime(2026, 8, 9, 11, 0, tzinfo=UTC)


def _prov(**kw) -> Provenance:
    return Provenance(producer_id=kw.pop("producer_id", "system.exit_engine_v1_shadow"),
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
        prediction_kind="point_estimate", prediction_value=-0.02, uncertainty_kind="stdev",
        uncertainty_value=0.05, model_id="model.shadow_baseline", model_version="0.1.0",
        evidence_refs=(_snapshot().ref(),), provenance=_prov(producer_id="model.shadow_baseline")).prediction_id


PRD = _prd_id()


def _xit(**kw) -> ExitProposal:
    return ExitProposal(
        position_ref=kw.pop("position_ref", "portfolio.position.AAPL"),
        proposed_action_kind=kw.pop("proposed_action_kind", "trim"),
        rationale=kw.pop("rationale", "thesis weakened: 20d estimate turned negative with acceptable uncertainty"),
        provenance=kw.pop("provenance", _prov()),
        prediction_record_ids=kw.pop("prediction_record_ids", (PRD,)),
        proposed_terms=kw.pop("proposed_terms", {"trim_fraction": 0.5, "reason_code": "thesis_weakened"}),
        **kw)


def test_valid_and_prefix():
    x = _xit()
    assert x.exit_proposal_id.startswith("xit_") and x.contract_type == "exit_proposal"


def test_action_kind_enum_enforced():
    assert EXIT_ACTION_KINDS == {"continue", "trim", "exit", "replace"}
    for good in EXIT_ACTION_KINDS:
        assert _xit(proposed_action_kind=good).exit_proposal_id
    for bad in ("sell_now", "execute", "liquidate", "", "EXIT", "hold"):
        with pytest.raises(ValueError):
            _xit(proposed_action_kind=bad)


def test_position_ref_and_rationale_required():
    with pytest.raises(ValueError):
        _xit(position_ref="")
    with pytest.raises(ValueError):
        _xit(rationale="")


def test_predictions_optional_but_validated_when_present():
    # an exit can be risk-driven with no referenced prediction
    assert _xit(prediction_record_ids=()).exit_proposal_id
    with pytest.raises(ValueError):
        _xit(prediction_record_ids=("rcl_wrong_family",))     # must be prd_ ids
    assert all(isinstance(p, str) for p in _xit().prediction_record_ids)  # referenced, never contained


def test_terms_optional_defaults_empty():
    x = _xit(proposed_terms=None)
    assert x.proposed_terms_copy() == {}
    assert x.exit_proposal_id                                  # a "continue" with no terms is valid


def test_naming_an_exit_is_allowed_but_order_keys_rejected():
    # describing the proposal is fine; carrying an order is not
    assert _xit(proposed_action_kind="exit", proposed_terms={"target_exit_fraction": 1.0}).exit_proposal_id
    for bad in ({"sell_now": True}, {"AAPL": {"sell": 100}}, {"liquidate": True},
                {"broker_order": {"side": "sell"}}, {"close_position": True},
                {"approve": 1}, {"execute": True}, {"AAPL": {"buy": 10}}):
        with pytest.raises(ValueError):
            _xit(proposed_terms=bad)


def test_terms_frozen():
    payload = {"trim_fraction": 0.5}
    x = _xit(proposed_terms=payload)
    payload["trim_fraction"] = 0.99                           # mutate caller dict
    assert x.proposed_terms_copy()["trim_fraction"] == 0.5


def test_identity_deterministic_order_free():
    a = _xit(prediction_record_ids=(PRD,), proposed_terms={"a": 1, "b": 2})
    b = _xit(prediction_record_ids=(PRD,), proposed_terms={"b": 2, "a": 1})
    assert a.exit_proposal_id == b.exit_proposal_id


@pytest.mark.parametrize("change", [
    dict(position_ref="portfolio.position.MSFT"),
    dict(proposed_action_kind="exit"),
    dict(rationale="different thesis"),
    dict(proposed_terms={"trim_fraction": 0.9}),
    dict(prediction_record_ids=()),
])
def test_identity_changes(change):
    assert _xit().exit_proposal_id != _xit(**change).exit_proposal_id


def test_provenance_not_identity_bearing():
    assert _xit().exit_proposal_id == _xit(provenance=_prov(producer_id="human.pm", producer_type="human")).exit_proposal_id


def test_roundtrip_and_tamper():
    x = _xit()
    d = json.loads(canonical_dumps(x.to_canonical_dict()))
    back = ExitProposal.from_dict(d)
    assert back.exit_proposal_id == x.exit_proposal_id
    assert back.to_canonical_dict() == x.to_canonical_dict()
    d["exit_proposal_id"] = "xit_deadbeef"
    with pytest.raises(ValueError):
        ExitProposal.from_dict(d)


def test_wrong_contract_type_rejected():
    d = json.loads(canonical_dumps(_xit().to_canonical_dict()))
    d["contract_type"] = "capital_proposal"
    with pytest.raises(ValueError):
        ExitProposal.from_dict(d)


def test_frozen_and_no_approval_or_action_fields():
    with pytest.raises(dataclasses.FrozenInstanceError):
        _xit().rationale = "x"   # type: ignore[misc]
    names = {f.name for f in dataclasses.fields(ExitProposal)}
    forbidden = {"approved", "approval", "certified", "execute", "execution", "order",
                 "trade", "fill", "broker", "authorized", "promoted", "production_ready",
                 "sell", "sell_now", "buy", "liquidate", "close_position", "authorization"}
    assert names.isdisjoint(forbidden), f"ExitProposal must carry no approval/action fields: {names & forbidden}"
