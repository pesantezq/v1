"""Northstar 0B.3 — StrategyPassport contract tests (highest governance risk).

Covers deterministic spp_ identity, PIT as_of, the lifecycle-stage enum, the
evidence trail (referenced by exr_/out_/rcl_ id + EvidenceRef, never contained),
evidence-trail completeness, append-style versioning (a status change is a NEW
passport superseding the prior by id — never a mutation), and the GOVERNANCE
BOUNDARY: a passport grants NO production/deployment/capital authority — even a
'certified' passport carries no authority field and rejects authority-claiming
attributes; the entitlement mapping is left to an E4/human decision elsewhere.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone

import pytest

from portfolio_automation.northstar import (
    DataSourceDescriptor, EvidenceSnapshot, PointInTime, Provenance,
    ResearchClaim, ResearchTask, WorkerResult, canonical_dumps,
)
from portfolio_automation.northstar.pit import KNOWN_AT_SOURCE_REPORTED
from portfolio_automation.northstar.passport import StrategyPassport, LIFECYCLE_STAGES

UTC = timezone.utc
T0 = datetime(2026, 8, 5, 13, 30, tzinfo=UTC)
T1 = datetime(2026, 8, 9, 11, 0, tzinfo=UTC)
T2 = datetime(2026, 9, 6, 20, 0, tzinfo=UTC)


def _prov(**kw) -> Provenance:
    return Provenance(producer_id=kw.pop("producer_id", "system.strategy_governance"),
                      producer_type=kw.pop("producer_type", "system"),
                      recorded_at=kw.pop("recorded_at", T2), **kw)


def _rcl_id() -> str:
    rt = ResearchTask(question="Does float turnover predict 20d excess return?", as_of=T1,
                      allowed_evidence_types=("market_data.close",),
                      output_expectation="worker_result.findings",
                      provenance=_prov(producer_id="system.research_scheduler"))
    wr = WorkerResult(research_task_id=rt.research_task_id, worker_id="worker.generic_researcher",
                      provenance=_prov(producer_id="worker.generic_researcher",
                                       producer_type="ai_worker", model_id="qwen2.5:7b"),
                      findings={"corr": 0.2}, confidence=0.6)
    return ResearchClaim(claim="High float turnover increases 20d excess return",
                         testable_metric="return.excess_spy_20d", direction="increase",
                         provenance=_prov(producer_id="worker.generic_researcher", producer_type="ai_worker"),
                         worker_result_ids=(wr.worker_result_id,)).claim_id


def _evidence_ref():
    src = DataSourceDescriptor(provider="fmp", dataset="quotes_daily", source_type="market_data")
    return EvidenceSnapshot(
        source_id=src.source_id, entity_id="AAPL", entity_type="symbol", evidence_type="market_data.close",
        pit=PointInTime(observed_at=T0, known_at=T0, known_at_basis=KNOWN_AT_SOURCE_REPORTED, retrieved_at=T1),
        provenance=_prov(producer_id="adapter.fmp_quotes", producer_type="source_adapter", source_id=src.source_id),
        payload={"close": 231.5}).ref()


RCL = _rcl_id()


def _spp(**kw) -> StrategyPassport:
    return StrategyPassport(
        strategy_id=kw.pop("strategy_id", "strategy.float_turnover_20d"),
        lifecycle_stage=kw.pop("lifecycle_stage", "challenger"),
        as_of=kw.pop("as_of", T2),
        status_rationale=kw.pop("status_rationale", "passed preregistered challenger gate on out-of-sample window"),
        provenance=kw.pop("provenance", _prov()),
        research_claim_ids=kw.pop("research_claim_ids", (RCL,)),
        **kw)


def test_valid_and_prefix():
    p = _spp()
    assert p.strategy_passport_id.startswith("spp_") and p.contract_type == "strategy_passport"


def test_lifecycle_stage_enum_enforced():
    assert LIFECYCLE_STAGES == {"candidate", "challenger", "certified", "retained",
                                "reduced", "suspended", "retired"}
    for good in LIFECYCLE_STAGES:
        assert _spp(lifecycle_stage=good).strategy_passport_id
    for bad in ("production", "deployed", "approved", "", "CERTIFIED", "live"):
        with pytest.raises(ValueError):
            _spp(lifecycle_stage=bad)


def test_required_identity_fields():
    with pytest.raises(ValueError):
        _spp(strategy_id="")
    with pytest.raises(ValueError):
        _spp(status_rationale="")


def test_evidence_trail_referenced_by_id_and_required():
    # evidence referenced by id only, validated by family
    assert all(isinstance(c, str) for c in _spp().research_claim_ids)
    with pytest.raises(ValueError):
        _spp(research_claim_ids=("prd_wrong_family",))
    with pytest.raises(ValueError):
        _spp(experiment_result_ids=("out_wrong_family",))
    with pytest.raises(ValueError):
        _spp(outcome_record_ids=("exr_wrong_family",))
    # a passport must cite SOME evidence trail
    with pytest.raises(ValueError):
        _spp(research_claim_ids=(), experiment_result_ids=(), outcome_record_ids=(), evidence_refs=())
    # any single evidence kind suffices (incl. raw evidence ref)
    assert _spp(research_claim_ids=(), evidence_refs=(_evidence_ref(),)).strategy_passport_id


def test_grants_no_production_or_capital_authority():
    # no authority fields on the contract at all
    names = {f.name for f in dataclasses.fields(StrategyPassport)}
    forbidden = {"production_enabled", "production_ready", "deploy", "deployed", "capital_allocated",
                 "capital_eligible", "may_receive_capital", "approved", "authorized", "promoted",
                 "eligibility", "execute"}
    assert names.isdisjoint(forbidden), f"passport must carry no authority fields: {names & forbidden}"
    # authority-claiming attributes are rejected...
    for bad in ({"production_enabled": True}, {"capital_eligible": True}, {"deploy": True},
                {"may_receive_capital": True}, {"promote": 1}, {"approve": True}, {"allocation": 1.0}):
        with pytest.raises(ValueError):
            _spp(attributes=bad)
    # ...even for a CERTIFIED passport (certified records status, not entitlement)
    certified = _spp(lifecycle_stage="certified", attributes={"name": "Float Turnover 20d", "horizon": "20d"})
    assert certified.strategy_passport_id
    with pytest.raises(ValueError):
        _spp(lifecycle_stage="certified", attributes={"capital_eligible": True})


def test_append_style_versioning_not_mutation():
    v1 = _spp(lifecycle_stage="challenger", status_rationale="entered challenger")
    # a status change is a NEW passport superseding the prior by id — never a mutation
    with pytest.raises(dataclasses.FrozenInstanceError):
        v1.lifecycle_stage = "certified"   # type: ignore[misc]
    v2 = _spp(lifecycle_stage="certified", status_rationale="passed certification gate",
              supersedes_passport_id=v1.strategy_passport_id)
    assert v2.strategy_passport_id != v1.strategy_passport_id
    assert v2.supersedes_passport_id == v1.strategy_passport_id
    with pytest.raises(ValueError):
        _spp(supersedes_passport_id="prd_not_a_passport")


def test_attributes_frozen():
    payload = {"name": "s1"}
    p = _spp(attributes=payload)
    payload["name"] = "tampered"
    assert p.attributes_copy()["name"] == "s1"


@pytest.mark.parametrize("change", [
    dict(lifecycle_stage="certified"),
    dict(strategy_id="strategy.other"),
    dict(as_of=datetime(2026, 9, 7, 20, 0, tzinfo=UTC)),
    dict(status_rationale="different reason"),
    dict(supersedes_passport_id="spp_" + "a" * 32),
    dict(attributes={"name": "s2"}),
])
def test_identity_changes(change):
    assert _spp().strategy_passport_id != _spp(**change).strategy_passport_id


def test_provenance_not_identity_bearing():
    assert _spp().strategy_passport_id == _spp(provenance=_prov(producer_id="human.governor", producer_type="human")).strategy_passport_id


def test_roundtrip_and_tamper():
    p = _spp(lifecycle_stage="certified", attributes={"name": "s1"})
    d = json.loads(canonical_dumps(p.to_canonical_dict()))
    back = StrategyPassport.from_dict(d)
    assert back.strategy_passport_id == p.strategy_passport_id
    assert back.to_canonical_dict() == p.to_canonical_dict()
    d["strategy_passport_id"] = "spp_deadbeef"
    with pytest.raises(ValueError):
        StrategyPassport.from_dict(d)
    d2 = json.loads(canonical_dumps(p.to_canonical_dict()))
    d2["contract_type"] = "outcome_record"
    with pytest.raises(ValueError):
        StrategyPassport.from_dict(d2)
