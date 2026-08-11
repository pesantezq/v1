"""Northstar Phase 0B milestone 2 — prediction + research contract tests.

Covers: deterministic identity, immutability (incl. frozen findings),
mandatory uncertainty/evidence, unordered-set semantics, cross-contract
reference validation, round-trips + schema versioning + tamper rejection,
and the separation invariants: prediction ≠ action, WorkerResult ≠ truth,
ResearchClaim ≠ certified alpha.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone

import pytest

from portfolio_automation.northstar import (
    EvidenceSnapshot,
    PointInTime,
    PredictionRecord,
    PredictionTask,
    Provenance,
    ResearchClaim,
    ResearchTask,
    WorkerResult,
    canonical_dumps,
)
from portfolio_automation.northstar.pit import KNOWN_AT_SOURCE_REPORTED

UTC = timezone.utc
T0 = datetime(2026, 8, 5, 13, 30, tzinfo=UTC)
T1 = datetime(2026, 8, 9, 11, 0, tzinfo=UTC)


def _prov(**kw) -> Provenance:
    return Provenance(
        producer_id=kw.pop("producer_id", "system.prediction_engine_v2_shadow"),
        producer_type=kw.pop("producer_type", "system"),
        recorded_at=kw.pop("recorded_at", T1),
        **kw,
    )


def _snapshot(entity="AAPL", payload=None) -> EvidenceSnapshot:
    from portfolio_automation.northstar import DataSourceDescriptor

    src = DataSourceDescriptor(provider="fmp", dataset="quotes_daily", source_type="market_data")
    return EvidenceSnapshot(
        source_id=src.source_id,
        entity_id=entity,
        entity_type="symbol",
        evidence_type="market_data.close",
        pit=PointInTime(observed_at=T0, known_at=T0, known_at_basis=KNOWN_AT_SOURCE_REPORTED,
                        retrieved_at=T1),
        provenance=_prov(producer_id="adapter.fmp_quotes", producer_type="source_adapter",
                         source_id=src.source_id),
        payload=payload or {"close": 231.5, "currency": "USD"},
    )


def _task(**kw) -> PredictionTask:
    return PredictionTask(
        entity_ids=kw.pop("entity_ids", ("AAPL", "MSFT")),
        as_of=kw.pop("as_of", T1),
        horizon_days=kw.pop("horizon_days", 20),
        target=kw.pop("target", "return.total"),
        allowed_evidence_types=kw.pop("allowed_evidence_types", ("market_data.close", "fundamental.revenue")),
        **kw,
    )


def _record(**kw) -> PredictionRecord:
    return PredictionRecord(
        task_id=kw.pop("task_id", _task().task_id),
        entity_id=kw.pop("entity_id", "AAPL"),
        as_of=kw.pop("as_of", T1),
        horizon_days=kw.pop("horizon_days", 20),
        prediction_kind=kw.pop("prediction_kind", "point_estimate"),
        prediction_value=kw.pop("prediction_value", 0.031),
        uncertainty_kind=kw.pop("uncertainty_kind", "stdev"),
        uncertainty_value=kw.pop("uncertainty_value", 0.045),
        model_id=kw.pop("model_id", "model.shadow_baseline"),
        model_version=kw.pop("model_version", "0.1.0"),
        evidence_refs=kw.pop("evidence_refs", (_snapshot().ref(),)),
        provenance=kw.pop("provenance", _prov()),
        **kw,
    )


def _rtask(**kw) -> ResearchTask:
    return ResearchTask(
        question=kw.pop("question", "Does rising short interest precede negative 20d returns in small caps?"),
        as_of=kw.pop("as_of", T1),
        allowed_evidence_types=kw.pop("allowed_evidence_types", ("short_interest.ratio", "market_data.close")),
        output_expectation=kw.pop("output_expectation", "worker_result.findings"),
        **kw,
    )


def _wresult(**kw) -> WorkerResult:
    return WorkerResult(
        research_task_id=kw.pop("research_task_id", _rtask().research_task_id),
        worker_id=kw.pop("worker_id", "worker.generic_researcher"),
        provenance=kw.pop("provenance", _prov(producer_id="worker.generic_researcher",
                                              producer_type="ai_worker")),
        evidence_refs=kw.pop("evidence_refs", (_snapshot().ref(),)),
        confidence=kw.pop("confidence", 0.6),
        findings=kw.pop("findings", {"observation": "weak negative association", "n": 42}),
        **kw,
    )


def _claim(**kw) -> ResearchClaim:
    return ResearchClaim(
        claim=kw.pop("claim", "High short-interest ratio predicts lower 20d total return"),
        testable_metric=kw.pop("testable_metric", "return.total_20d"),
        direction=kw.pop("direction", "decrease"),
        worker_result_ids=kw.pop("worker_result_ids", (_wresult().worker_result_id,)),
        **kw,
    )


# ── PredictionTask ─────────────────────────────────────────────────────────


def test_task_identity_deterministic_and_universe_unordered():
    a = _task(entity_ids=("AAPL", "MSFT"))
    b = _task(entity_ids=("MSFT", "AAPL"))
    assert a.task_id == b.task_id
    assert _task(horizon_days=21).task_id != a.task_id
    with pytest.raises(ValueError):
        _task(entity_ids=("AAPL", "AAPL"))


def test_task_notes_not_identity_bearing():
    assert _task(notes="a").task_id == _task(notes="b").task_id


def test_task_carries_no_results_and_no_action_surface():
    names = {f.name for f in dataclasses.fields(PredictionTask)}
    assert names.isdisjoint({"prediction_value", "result", "outcome", "allocation", "weight"})
    with pytest.raises(ValueError):
        _task(target_params={"allocation": 0.5})


def test_task_requires_scope_and_positive_horizon():
    with pytest.raises(ValueError):
        _task(allowed_evidence_types=())
    with pytest.raises(ValueError):
        _task(horizon_days=0)
    with pytest.raises(ValueError):
        _task(as_of=datetime(2026, 8, 9, 11, 0))  # naive


def test_task_round_trip():
    t = _task(target_params={"benchmark": "SPY"}, notes="context")
    rt = PredictionTask.from_dict(json.loads(canonical_dumps(t.to_canonical_dict())))
    assert rt.task_id == t.task_id and rt == t


# ── PredictionRecord ───────────────────────────────────────────────────────


def test_record_identity_deterministic_and_refs_unordered():
    r1, r2 = _snapshot("AAPL").ref(), _snapshot("MSFT").ref()
    a = _record(evidence_refs=(r1, r2))
    b = _record(evidence_refs=(r2, r1))
    assert a.prediction_id == b.prediction_id
    assert _record(prediction_value=0.02).prediction_id != a.prediction_id


def test_record_requires_uncertainty():
    with pytest.raises(ValueError, match="no implied certainty"):
        _record(uncertainty_value=None)


def test_record_requires_evidence():
    with pytest.raises(ValueError, match="cannot state its evidence"):
        _record(evidence_refs=())


def test_record_validates_reference_ids():
    with pytest.raises(ValueError):
        _record(task_id="rtk_" + "0" * 32)  # wrong prefix
    with pytest.raises(ValueError):
        _record(feature_ids=("evs_" + "0" * 32,))  # feature ids must be ftr_


def test_record_model_provenance_consistency():
    good = _record(provenance=_prov(model_id="model.shadow_baseline@0.1.0"))
    assert good.model_id == "model.shadow_baseline"
    with pytest.raises(ValueError, match="contradicts"):
        _record(provenance=_prov(model_id="model.other@9.9.9"))


def test_record_has_no_action_surface_and_no_mutable_resolution():
    names = {f.name for f in dataclasses.fields(PredictionRecord)}
    forbidden = {"allocation", "weight", "size", "order", "execute", "trade",
                 "approve", "position", "resolved", "outcome", "resolution"}
    assert names.isdisjoint(forbidden)
    rec = _record()
    with pytest.raises(dataclasses.FrozenInstanceError):
        rec.prediction_value = 1.0  # type: ignore[misc]


def test_record_round_trip_and_tamper_rejection():
    rec = _record(feature_ids=("ftr_" + "a" * 32,), prediction_value=[0.01, 0.03, 0.05],
                  prediction_kind="quantiles")
    data = json.loads(canonical_dumps(rec.to_canonical_dict()))
    rt = PredictionRecord.from_dict(data)
    assert rt.prediction_id == rec.prediction_id and rt == rec
    data["prediction_value"] = [0.9, 0.95, 0.99]
    with pytest.raises(ValueError, match="does not reproduce"):
        PredictionRecord.from_dict(data)
    bad = json.loads(canonical_dumps(rec.to_canonical_dict()))
    bad["schema_version"] = "2.0.0"
    with pytest.raises(ValueError, match="unsupported schema_version"):
        PredictionRecord.from_dict(bad)


# ── ResearchTask ───────────────────────────────────────────────────────────


def test_research_task_identity_and_validation():
    assert _rtask().research_task_id == _rtask().research_task_id
    assert _rtask(question="other?").research_task_id != _rtask().research_task_id
    assert _rtask(notes="x").research_task_id == _rtask(notes="y").research_task_id
    with pytest.raises(ValueError):
        _rtask(effort_class="unbounded")
    with pytest.raises(ValueError):
        _rtask(allowed_evidence_types=())
    rt = ResearchTask.from_dict(json.loads(canonical_dumps(_rtask().to_canonical_dict())))
    assert rt == _rtask()


# ── WorkerResult ───────────────────────────────────────────────────────────


def test_worker_result_never_production_truth():
    for bad in ({"approved": True}, {"summary": {"certification": "passed"}},
                {"steps": [{"promote": "yes"}]}, {"production_ready": 1}):
        with pytest.raises(ValueError, match="never\\s+production truth|never"):
            _wresult(findings=bad)
    names = {f.name for f in dataclasses.fields(WorkerResult)}
    assert names.isdisjoint({"approved", "certified", "authority", "production"})


def test_worker_result_confidence_mandatory_unless_abstained():
    with pytest.raises(ValueError, match="confidence is required"):
        _wresult(confidence=None)
    with pytest.raises(ValueError):
        _wresult(confidence=1.5)


def test_worker_abstention_is_first_class():
    a = _wresult(abstained=True, abstention_reason="insufficient evidence in scope",
                 confidence=None, findings=None)
    assert a.abstained and a.findings_copy() == {}
    with pytest.raises(ValueError):
        _wresult(abstained=True, abstention_reason=None, confidence=None, findings=None)
    with pytest.raises(ValueError):
        _wresult(abstained=True, abstention_reason="r", confidence=0.5, findings=None)


def test_worker_findings_frozen_and_identity_stable():
    findings = {"observation": "x", "details": {"n": 42}}
    w = _wresult(findings=findings)
    before = w.worker_result_id
    findings["details"]["n"] = 0  # caller mutation
    assert w.worker_result_id == before
    assert w.findings_copy()["details"]["n"] == 42
    copy = w.findings_copy()
    copy["observation"] = "tampered"
    assert w.findings_copy()["observation"] == "x"


def test_worker_result_round_trip_including_abstention():
    for w in (_wresult(), _wresult(abstained=True, abstention_reason="out of scope",
                                   confidence=None, findings=None, evidence_refs=())):
        rt = WorkerResult.from_dict(json.loads(canonical_dumps(w.to_canonical_dict())))
        assert rt.worker_result_id == w.worker_result_id and rt == w


# ── ResearchClaim ──────────────────────────────────────────────────────────


def test_claim_falsifiability_is_structural():
    with pytest.raises(ValueError):
        _claim(testable_metric="")
    with pytest.raises(ValueError):
        _claim(direction="up_probably")
    with pytest.raises(ValueError, match="cite at least one source"):
        _claim(worker_result_ids=(), evidence_refs=())


def test_claim_is_not_certified_alpha():
    names = {f.name for f in dataclasses.fields(ResearchClaim)}
    assert names.isdisjoint({"certified", "certification", "status", "approved",
                             "alpha", "promoted", "influence"})


def test_claim_identity_and_round_trip():
    wid = _wresult().worker_result_id
    a = _claim(worker_result_ids=(wid,))
    b = _claim(worker_result_ids=(wid,))
    assert a.claim_id == b.claim_id
    assert _claim(direction="conditional").claim_id != a.claim_id
    rt = ResearchClaim.from_dict(json.loads(canonical_dumps(a.to_canonical_dict())))
    assert rt.claim_id == a.claim_id and rt == a
    with pytest.raises(ValueError):
        _claim(worker_result_ids=("prd_" + "0" * 32,))  # wrong prefix


# ── Cross-family separation ────────────────────────────────────────────────


def test_prediction_and_capital_remain_separate_families():
    import portfolio_automation.northstar as ns

    # Milestone 2 ships predictions/research; capital/exit/outcome/passport
    # and experiment families stay absent until milestone 3.
    for absent in ("CapitalProposal", "ExitProposal", "OutcomeRecord",
                   "StrategyPassport", "ExperimentSpec", "ExperimentResult"):
        assert not hasattr(ns, absent)
    # And PredictionRecord is not a base class of anything here — reference,
    # not inheritance, is the only permitted relationship.
    assert PredictionRecord.__subclasses__() == []


def test_contract_types_distinct_and_prefixed():
    objs = {
        "prediction_task": _task().task_id,
        "prediction_record": _record().prediction_id,
        "research_task": _rtask().research_task_id,
        "worker_result": _wresult().worker_result_id,
        "research_claim": _claim().claim_id,
    }
    prefixes = {v.split("_", 1)[0] for v in objs.values()}
    assert prefixes == {"ptk", "prd", "rtk", "wkr", "rcl"}
