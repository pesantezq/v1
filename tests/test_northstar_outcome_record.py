"""Northstar 0B.3 — OutcomeRecord contract tests.

Covers deterministic out_ identity, PIT resolution_as_of, resolved/unresolved
semantics, reference-only linkage to prd_/cap_/xit_ (never contained, so no
retrospective rewriting), the ATTRIBUTION-SEPARATION crux (component_outcomes
keyed strictly by dimension — a blended 'strategy success' score is rejected;
prediction/allocation/exit/portfolio quality stay independently retrievable),
evidence-not-permission (authority keys rejected), snapshot immutability, and
round-trip + tamper.
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
from portfolio_automation.northstar.decisions import CapitalProposal, ExitProposal
from portfolio_automation.northstar.outcomes import OutcomeRecord, OUTCOME_DIMENSIONS

UTC = timezone.utc
T0 = datetime(2026, 8, 5, 13, 30, tzinfo=UTC)
T1 = datetime(2026, 8, 9, 11, 0, tzinfo=UTC)
T2 = datetime(2026, 9, 6, 20, 0, tzinfo=UTC)   # resolution time (later)


def _prov(**kw) -> Provenance:
    return Provenance(producer_id=kw.pop("producer_id", "system.attribution_engine_v1"),
                      producer_type=kw.pop("producer_type", "system"),
                      recorded_at=kw.pop("recorded_at", T2), **kw)


def _snapshot(evidence_type="market_data.close"):
    src = DataSourceDescriptor(provider="fmp", dataset="quotes_daily", source_type="market_data")
    return EvidenceSnapshot(
        source_id=src.source_id, entity_id="AAPL", entity_type="symbol",
        evidence_type=evidence_type,
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


def _cap_id(prd) -> str:
    return CapitalProposal(prediction_record_ids=(prd,), rationale="positive 20d estimate",
                           provenance=_prov(), proposed_sizing={"AAPL": {"target_weight": 0.03}}).capital_proposal_id


def _xit_id(prd) -> str:
    return ExitProposal(position_ref="portfolio.position.AAPL", proposed_action_kind="trim",
                        rationale="thesis weakened", provenance=_prov(),
                        prediction_record_ids=(prd,), proposed_terms={"trim_fraction": 0.5}).exit_proposal_id


PRD = _prd_id()
CAP = _cap_id(PRD)
XIT = _xit_id(PRD)

# Distinct per-dimension measurements — NOT one blended score.
COMPONENTS = {
    "prediction": {"directional_hit": True, "error": -0.004},
    "allocation": {"realized_contribution_bps": 12.0},
    "exit": {"timing_vs_hold_bps": 3.5},
    "portfolio": {"period_return": 0.018},
    "reference": {"benchmark": "SPY", "excess_return": 0.006},
}


def _out(**kw) -> OutcomeRecord:
    return OutcomeRecord(
        resolution_as_of=kw.pop("resolution_as_of", T2),
        resolution_status=kw.pop("resolution_status", "resolved"),
        provenance=kw.pop("provenance", _prov()),
        prediction_record_ids=kw.pop("prediction_record_ids", (PRD,)),
        capital_proposal_ids=kw.pop("capital_proposal_ids", (CAP,)),
        exit_proposal_ids=kw.pop("exit_proposal_ids", (XIT,)),
        evidence_refs=kw.pop("evidence_refs", (_snapshot("market_data.resolved_return").ref(),)),
        component_outcomes=kw.pop("component_outcomes", dict(COMPONENTS)),
        **kw)


def test_valid_and_prefix():
    o = _out()
    assert o.outcome_record_id.startswith("out_") and o.contract_type == "outcome_record"


def test_references_prior_artifacts_by_valid_id_only():
    assert all(isinstance(p, str) for p in _out().prediction_record_ids)     # referenced, never contained
    with pytest.raises(ValueError):
        _out(prediction_record_ids=("cap_wrong_family",))                    # prd_ only
    with pytest.raises(ValueError):
        _out(capital_proposal_ids=("prd_wrong_family",))                     # cap_ only
    with pytest.raises(ValueError):
        _out(exit_proposal_ids=("cap_wrong_family",))                        # xit_ only


def test_must_attribute_to_something():
    # evidence alone attributes nothing
    with pytest.raises(ValueError):
        _out(prediction_record_ids=(), capital_proposal_ids=(), exit_proposal_ids=(),
             realized_action_refs=())
    # a realized action alone is enough attribution
    assert _out(prediction_record_ids=(), capital_proposal_ids=(), exit_proposal_ids=(),
                realized_action_refs=("action:broker-fill-2026-09-06",)).outcome_record_id


def test_attribution_is_never_collapsed_into_one_score():
    # the crux: a blended generic score is structurally rejected...
    for collapse in ({"strategy_success": 0.9}, {"overall_score": 1}, {"blended_score": 0.5},
                     {"success": True}, {"prediction": {}, "aggregate": 0.7}):
        with pytest.raises(ValueError):
            _out(component_outcomes=collapse)
    # ...while the distinct dimensions coexist and stay independently retrievable
    o = _out()
    assert o.component("prediction") == COMPONENTS["prediction"]
    assert o.component("allocation") == COMPONENTS["allocation"]
    assert o.component("exit") == COMPONENTS["exit"]
    assert o.component("portfolio") == COMPONENTS["portfolio"]
    assert set(o.component_outcomes_copy()) <= OUTCOME_DIMENSIONS


def test_evidence_not_permission():
    for bad in ({"prediction": {"approve": True}}, {"portfolio": {"promote": 1}},
                {"allocation": {"execute": "now"}}, {"exit": {"certified": True}}):
        with pytest.raises(ValueError):
            _out(component_outcomes=bad)
    names = {f.name for f in dataclasses.fields(OutcomeRecord)}
    forbidden = {"approved", "approval", "certified", "execute", "execution", "order",
                 "trade", "promoted", "authorized", "production_ready"}
    assert names.isdisjoint(forbidden)


def test_resolved_unresolved_semantics():
    # unresolved cannot carry measurements
    with pytest.raises(ValueError):
        _out(resolution_status="unresolved", component_outcomes=dict(COMPONENTS))
    assert _out(resolution_status="unresolved", component_outcomes=None).outcome_record_id
    # resolved / partially_resolved must carry measurements
    with pytest.raises(ValueError):
        _out(resolution_status="resolved", component_outcomes=None)
    assert _out(resolution_status="partially_resolved",
                component_outcomes={"prediction": {"error": -0.004}}).outcome_record_id
    with pytest.raises(ValueError):
        _out(resolution_status="bogus")


def test_component_outcomes_frozen():
    payload = {"prediction": {"error": -0.004}}
    o = _out(component_outcomes=payload)
    payload["prediction"]["error"] = 9.9                     # mutate caller dict
    assert o.component_outcomes_copy()["prediction"]["error"] == -0.004


def test_identity_deterministic_order_free():
    a = _out(component_outcomes={"prediction": {"a": 1}, "allocation": {"b": 2}})
    b = _out(component_outcomes={"allocation": {"b": 2}, "prediction": {"a": 1}})
    assert a.outcome_record_id == b.outcome_record_id


@pytest.mark.parametrize("change", [
    dict(resolution_status="partially_resolved", component_outcomes={"prediction": {"error": -0.004}}),
    dict(resolution_as_of=datetime(2026, 9, 7, 20, 0, tzinfo=UTC)),
    dict(component_outcomes={"prediction": {"error": 0.9}}),
    dict(exit_proposal_ids=()),
])
def test_identity_changes(change):
    assert _out().outcome_record_id != _out(**change).outcome_record_id


def test_provenance_not_identity_bearing():
    assert _out().outcome_record_id == _out(provenance=_prov(producer_id="human.analyst", producer_type="human")).outcome_record_id


def test_roundtrip_and_tamper():
    o = _out()
    d = json.loads(canonical_dumps(o.to_canonical_dict()))
    back = OutcomeRecord.from_dict(d)
    assert back.outcome_record_id == o.outcome_record_id
    assert back.to_canonical_dict() == o.to_canonical_dict()
    d["outcome_record_id"] = "out_deadbeef"
    with pytest.raises(ValueError):
        OutcomeRecord.from_dict(d)
    d2 = json.loads(canonical_dumps(o.to_canonical_dict()))
    d2["contract_type"] = "capital_proposal"
    with pytest.raises(ValueError):
        OutcomeRecord.from_dict(d2)


def test_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        _out().resolution_status = "x"   # type: ignore[misc]
