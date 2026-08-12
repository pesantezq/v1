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
        provenance=kw.pop("provenance", _prov(producer_id="system.prediction_scheduler")),
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
        provenance=kw.pop("provenance", _prov(producer_id="system.rd_control_plane")),
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
        provenance=kw.pop("provenance", _prov(producer_id="worker.generic_researcher",
                                              producer_type="ai_worker")),
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

    # Milestone 3 underway: ExperimentSpec + ExperimentResult + CapitalProposal +
    # ExitProposal delivered; outcome/passport stay absent until built.
    for delivered in ("ExperimentSpec", "ExperimentResult", "CapitalProposal", "ExitProposal"):
        assert hasattr(ns, delivered)
    for absent in ("OutcomeRecord", "StrategyPassport"):
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


# ── 0B.2 hardening: PredictionTask deep immutability ───────────────────────


def test_task_params_caller_mutation_cannot_change_identity():
    params = {"benchmark": "SPY", "bands": [1, 2, 3]}
    t = _task(target_params=params)
    before = t.task_id
    params["benchmark"] = "TAMPERED"      # caller mutates original reference
    params["bands"].append(99)
    assert t.task_id == before
    assert t.target_params_copy() == {"benchmark": "SPY", "bands": [1, 2, 3]}
    # The mutable reference was dropped entirely (snapshot-payload discipline).
    assert t.target_params is None


def test_task_params_copy_is_fresh_each_access():
    t = _task(target_params={"benchmark": "SPY"})
    copy = t.target_params_copy()
    copy["benchmark"] = "TAMPERED"
    assert t.target_params_copy() == {"benchmark": "SPY"}
    assert _task(target_params=None).target_params_copy() is None


def test_task_params_identity_and_round_trip_preserved():
    a = _task(target_params={"benchmark": "SPY"})
    b = _task(target_params={"benchmark": "SPY"})
    assert a.task_id == b.task_id and a == b
    assert a.task_id != _task(target_params={"benchmark": "QQQ"}).task_id
    rt = PredictionTask.from_dict(json.loads(canonical_dumps(a.to_canonical_dict())))
    assert rt.task_id == a.task_id and rt == a


# ── 0B.2 hardening continuation: six bounded repairs ────────────────────────


def test_r2_sequence_values_frozen_and_list_tuple_equivalent():
    pred_list = _record(prediction_kind="quantiles", prediction_value=[0.01, 0.03, 0.05],
                        uncertainty_kind="quantile_band", uncertainty_value=[0.02, 0.02, 0.03])
    caller_pred, caller_unc = [0.01, 0.03, 0.05], [0.02, 0.02, 0.03]
    r = _record(prediction_kind="quantiles", prediction_value=caller_pred,
                uncertainty_kind="quantile_band", uncertainty_value=caller_unc)
    before = r.prediction_id
    caller_pred.append(9.9)      # mutate caller prediction list
    caller_unc[0] = 9.9          # mutate caller uncertainty list
    assert r.prediction_id == before
    assert r.prediction_value == (0.01, 0.03, 0.05)     # frozen tuple
    assert r.uncertainty_value == (0.02, 0.02, 0.03)
    # list == tuple semantics
    pred_tuple = _record(prediction_kind="quantiles", prediction_value=(0.01, 0.03, 0.05),
                         uncertainty_kind="quantile_band", uncertainty_value=(0.02, 0.02, 0.03))
    assert pred_list.prediction_id == pred_tuple.prediction_id == before
    assert pred_list == pred_tuple
    # round trip stable, still serialized as JSON arrays
    data = json.loads(canonical_dumps(r.to_canonical_dict()))
    assert data["prediction_value"] == [0.01, 0.03, 0.05]
    rt = PredictionRecord.from_dict(data)
    assert rt.prediction_id == before and rt == r


def test_r3_prediction_abstention_valid_shapes():
    a = _record(abstained=True, abstention_reason="insufficient defensible evidence",
                prediction_value=None, uncertainty_kind=None, uncertainty_value=None,
                evidence_refs=())
    assert a.abstained and a.prediction_value is None
    # evidence MAY also be retained when inspected
    b = _record(abstained=True, abstention_reason="conflicting sources",
                prediction_value=None, uncertainty_kind=None, uncertainty_value=None)
    assert b.evidence_refs
    assert a.prediction_id != b.prediction_id
    rt = PredictionRecord.from_dict(json.loads(canonical_dumps(a.to_canonical_dict())))
    assert rt == a and rt.prediction_id == a.prediction_id
    data = json.loads(canonical_dumps(a.to_canonical_dict()))
    data["abstention_reason"] = "tampered"
    with pytest.raises(ValueError, match="does not reproduce"):
        PredictionRecord.from_dict(data)


def test_r3_prediction_abstention_invalid_shapes():
    with pytest.raises(ValueError, match="abstention_reason"):
        _record(abstained=True, prediction_value=None, uncertainty_kind=None,
                uncertainty_value=None)
    for leak in ({"prediction_value": 0.0}, {"uncertainty_kind": "stdev"},
                 {"uncertainty_value": 0.01}):
        kwargs = dict(abstained=True, abstention_reason="r", prediction_value=None,
                      uncertainty_kind=None, uncertainty_value=None)
        kwargs.update(leak)
        with pytest.raises(ValueError, match="must be None when abstained"):
            _record(**kwargs)
    with pytest.raises(ValueError, match="requires abstained"):
        _record(abstention_reason="reason without abstaining")
    with pytest.raises(ValueError):
        _record(prediction_value=None)   # normal prediction still needs a value
    with pytest.raises(ValueError):
        _record(uncertainty_kind=None)
    # abstained/abstention_reason participate in identity
    a = _record(abstained=True, abstention_reason="reason A", prediction_value=None,
                uncertainty_kind=None, uncertainty_value=None)
    b = _record(abstained=True, abstention_reason="reason B", prediction_value=None,
                uncertainty_kind=None, uncertainty_value=None)
    assert a.prediction_id != b.prediction_id


def test_r4_worker_producer_consistency():
    ok_ai = _wresult()   # ai_worker, matching ids
    assert ok_ai.provenance.producer_type == "ai_worker"
    ok_sys = _wresult(worker_id="tool.screener", provenance=_prov(
        producer_id="tool.screener", producer_type="system"))
    assert ok_sys.provenance.producer_type == "system"
    with pytest.raises(ValueError, match="must equal"):
        _wresult(provenance=_prov(producer_id="someone.else", producer_type="ai_worker"))
    for bad_type, extra in (("source_adapter", {"source_id": "src_" + "0" * 32}),
                            ("derivation", {}), ("human", {})):
        with pytest.raises(ValueError, match="producer_type"):
            _wresult(provenance=_prov(producer_id="worker.generic_researcher",
                                      producer_type=bad_type, **extra))


def test_r5_worker_model_identity():
    base = _wresult()
    modeled = _wresult(provenance=_prov(producer_id="worker.generic_researcher",
                                        producer_type="ai_worker", model_id="llm.local@1"))
    other_model = _wresult(provenance=_prov(producer_id="worker.generic_researcher",
                                            producer_type="ai_worker", model_id="llm.local@2"))
    assert modeled.worker_result_id != other_model.worker_result_id
    assert base.worker_result_id != modeled.worker_result_id
    # recorded_at stays non-identity-bearing
    later = _wresult(provenance=_prov(producer_id="worker.generic_researcher",
                                      producer_type="ai_worker",
                                      recorded_at=datetime(2026, 9, 1, tzinfo=UTC)))
    assert later.worker_result_id == base.worker_result_id
    # code_version deliberately EXCLUDED from identity (documented rationale:
    # reproduction/attribution is the ExperimentSpec path's job; worker deploys
    # must not fragment result identity)
    versioned = _wresult(provenance=_prov(producer_id="worker.generic_researcher",
                                          producer_type="ai_worker", code_version="abc123"))
    assert versioned.worker_result_id == base.worker_result_id


def test_r6_provenance_required_and_non_identity():
    for ctor, kwargs in ((PredictionTask, dict(entity_ids=("AAPL",), as_of=T1, horizon_days=5,
                                               target="return.total",
                                               allowed_evidence_types=("market_data.close",),
                                               provenance=None)),):
        with pytest.raises((ValueError, TypeError)):
            ctor(**kwargs)
    with pytest.raises(ValueError):
        _rtask(provenance=None)
    with pytest.raises(ValueError):
        _claim(provenance=None)
    # recorded_at-only change must not change semantic ids
    later = datetime(2026, 9, 1, tzinfo=UTC)
    assert _task().task_id == _task(provenance=_prov(
        producer_id="system.prediction_scheduler", recorded_at=later)).task_id
    assert _rtask().research_task_id == _rtask(provenance=_prov(
        producer_id="system.rd_control_plane", recorded_at=later)).research_task_id
    wid = _wresult().worker_result_id
    assert _claim(worker_result_ids=(wid,)).claim_id == _claim(
        worker_result_ids=(wid,),
        provenance=_prov(producer_id="worker.generic_researcher",
                         producer_type="ai_worker", recorded_at=later)).claim_id
    # provenance survives round trips
    for obj, cls in ((_task(), PredictionTask), (_rtask(), ResearchTask), (_claim(), ResearchClaim)):
        rt = cls.from_dict(json.loads(canonical_dumps(obj.to_canonical_dict())))
        assert rt.provenance == obj.provenance


def test_r7_strict_containers_and_wildcard():
    # raw string must raise, not explode into characters
    with pytest.raises(ValueError, match="list or tuple"):
        _task(entity_ids="IBM")
    with pytest.raises(ValueError, match="list or tuple"):
        _task(allowed_evidence_types=b"bytes")
    with pytest.raises(ValueError, match="list or tuple"):
        _rtask(scope_entities={"AAPL"})                    # set rejected
    with pytest.raises(ValueError, match="list or tuple"):
        _claim(scope_entities=(e for e in ("AAPL",)))      # generator rejected
    with pytest.raises(ValueError, match="list or tuple"):
        _record(evidence_refs={_snapshot().ref()})         # set of refs rejected
    with pytest.raises(ValueError):
        _task(entity_ids=("AAPL", "AAPL"))                 # duplicates still rejected
    # list accepted, tuple accepted, both normalize identically
    a = _task(entity_ids=["MSFT", "AAPL"])
    b = _task(entity_ids=("AAPL", "MSFT"))
    assert a == b and a.task_id == b.task_id and a.entity_ids == ("AAPL", "MSFT")
    # wildcard: sole value valid, mixture invalid, undefined elsewhere
    w = _task(allowed_evidence_types=("*",))
    assert w.allowed_evidence_types == ("*",)
    assert _rtask(allowed_evidence_types=["*"]).allowed_evidence_types == ("*",)
    with pytest.raises(ValueError, match="sole"):
        _task(allowed_evidence_types=("*", "market_data.close"))
    with pytest.raises(ValueError, match="wildcard"):
        _task(entity_ids=("*",))                           # no wildcard semantics on universes


# ── 0B.2 final hardening: strict containers hold on the from_dict path ──────


def test_r7f_from_dict_rejects_string_collections():
    # A tampered serialized document must be rejected, never exploded into
    # characters by tuple() pre-coercion before constructor validation.
    task_data = json.loads(canonical_dumps(_task().to_canonical_dict()))
    task_data["entity_ids"] = "IBM"
    with pytest.raises(ValueError, match="list or tuple"):
        PredictionTask.from_dict(task_data)
    task_data2 = json.loads(canonical_dumps(_task().to_canonical_dict()))
    task_data2["allowed_evidence_types"] = "market_data.close"
    with pytest.raises(ValueError, match="list or tuple"):
        PredictionTask.from_dict(task_data2)

    rtask_data = json.loads(canonical_dumps(_rtask().to_canonical_dict()))
    rtask_data["scope_entities"] = "AAPL"
    with pytest.raises(ValueError, match="list or tuple"):
        ResearchTask.from_dict(rtask_data)

    claim_data = json.loads(canonical_dumps(_claim().to_canonical_dict()))
    claim_data["worker_result_ids"] = "wkr_" + "0" * 32
    with pytest.raises(ValueError, match="list or tuple"):
        ResearchClaim.from_dict(claim_data)


def test_r7f_from_dict_rejects_malformed_ref_collections():
    rec_data = json.loads(canonical_dumps(_record().to_canonical_dict()))
    rec_data["evidence_refs"] = "evs_stringy"
    with pytest.raises(ValueError, match="list or tuple"):
        PredictionRecord.from_dict(rec_data)
    rec_data2 = json.loads(canonical_dumps(_record().to_canonical_dict()))
    rec_data2["evidence_refs"] = ["not-a-mapping"]
    with pytest.raises(ValueError, match="serialized mapping"):
        PredictionRecord.from_dict(rec_data2)

    w_data = json.loads(canonical_dumps(_wresult().to_canonical_dict()))
    w_data["evidence_refs"] = {"snapshot_id": "evs_x"}   # mapping, not a list of mappings
    with pytest.raises(ValueError, match="list or tuple"):
        WorkerResult.from_dict(w_data)

    c_data = json.loads(canonical_dumps(_claim(evidence_refs=(_snapshot().ref(),),
                                               worker_result_ids=()).to_canonical_dict()))
    c_data["evidence_refs"] = [42]
    with pytest.raises(ValueError, match="serialized mapping"):
        ResearchClaim.from_dict(c_data)


def test_r7f_round_trips_still_hold_for_all_five():
    for obj, cls in (
        (_task(target_params={"benchmark": "SPY"}), PredictionTask),
        (_record(prediction_kind="quantiles", prediction_value=[0.01, 0.05],
                 uncertainty_kind="quantile_band", uncertainty_value=[0.01, 0.02]), PredictionRecord),
        (_rtask(scope_entities=("AAPL",)), ResearchTask),
        (_wresult(), WorkerResult),
        (_claim(), ResearchClaim),
    ):
        rt = cls.from_dict(json.loads(canonical_dumps(obj.to_canonical_dict())))
        assert rt == obj
