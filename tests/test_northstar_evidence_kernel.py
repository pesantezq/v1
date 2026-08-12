"""Northstar Phase 0B milestone 1 — evidence kernel contract tests.

Covers the milestone's required areas: deterministic serialization/IDs,
immutability (incl. nested payloads), timezone discipline, PIT semantics with
no fabricated time, multi-source coexistence, revision, EvidenceRef
round-trips, feature provenance, source isolation, structural secret
rejection, serialization round-trips, contract versioning, and separation
invariants (no execution/approval/portfolio surface).
"""
from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime, timezone

import pytest

from portfolio_automation.northstar import (
    CanonicalizationError,
    DataSourceDescriptor,
    EvidenceRef,
    EvidenceSnapshot,
    FeatureRecord,
    PointInTime,
    Provenance,
    canonical_dumps,
    content_hash,
    deterministic_id,
)
from portfolio_automation.northstar.pit import (
    KNOWN_AT_DERIVED_CONSERVATIVE,
    KNOWN_AT_SOURCE_REPORTED,
    KNOWN_AT_UNKNOWN,
)

UTC = timezone.utc
T0 = datetime(2026, 8, 5, 13, 30, tzinfo=UTC)
T1 = datetime(2026, 8, 9, 11, 0, tzinfo=UTC)


def make_source(provider="sec_edgar", dataset="10q_fundamentals", **kw) -> DataSourceDescriptor:
    return DataSourceDescriptor(
        provider=provider, dataset=dataset, source_type=kw.pop("source_type", "fundamental"), **kw
    )


def make_provenance(**kw) -> Provenance:
    producer_type = kw.pop("producer_type", "source_adapter")
    if producer_type == "source_adapter":
        # source_adapter provenance requires a source identity (fail closed).
        kw.setdefault("source_id", make_source().source_id)
    return Provenance(
        producer_id=kw.pop("producer_id", "adapter.sec_edgar"),
        producer_type=producer_type,
        recorded_at=kw.pop("recorded_at", T1),
        **kw,
    )


def make_pit(**kw) -> PointInTime:
    defaults = dict(
        published_at=T0,
        known_at=T0,
        known_at_basis=KNOWN_AT_SOURCE_REPORTED,
        retrieved_at=T1,
        effective_period_start=date(2026, 4, 1),
        effective_period_end=date(2026, 6, 30),
        effective_period_label="2026-Q2",
    )
    defaults.update(kw)
    return PointInTime(**defaults)


def make_snapshot(**kw) -> EvidenceSnapshot:
    source_id = kw.pop("source_id", make_source().source_id)
    return EvidenceSnapshot(
        source_id=source_id,
        entity_id=kw.pop("entity_id", "AAPL"),
        entity_type=kw.pop("entity_type", "symbol"),
        evidence_type=kw.pop("evidence_type", "fundamental.revenue"),
        pit=kw.pop("pit", make_pit()),
        provenance=kw.pop("provenance", make_provenance(source_id=source_id)),
        payload=kw.pop("payload", {"revenue_usd": 94_100_000_000.0, "currency": "USD"}),
        **kw,
    )


# ── Deterministic serialization ────────────────────────────────────────────


def test_canonical_dumps_is_deterministic_and_key_ordered():
    a = canonical_dumps({"b": 1, "a": [1, 2, {"z": True, "y": None}]})
    b = canonical_dumps({"a": [1, 2, {"y": None, "z": True}], "b": 1})
    assert a == b
    assert a == '{"a":[1,2,{"y":null,"z":true}],"b":1}'


def test_same_object_serializes_identically():
    s1, s2 = make_snapshot(), make_snapshot()
    assert s1.to_json() == s2.to_json()


def test_canonical_rejects_uncanonical_types():
    for bad in ({1: "non-string key"}, {"x": {1, 2}}, {"x": b"bytes"}, {"x": object()},
                {"x": float("nan")}):
        with pytest.raises(CanonicalizationError):
            canonical_dumps(bad)


# ── Deterministic ID ───────────────────────────────────────────────────────


def test_same_semantic_inputs_same_id():
    assert make_snapshot().snapshot_id == make_snapshot().snapshot_id
    assert make_source().source_id == make_source().source_id


def test_changed_semantic_input_changes_id():
    base = make_snapshot()
    assert make_snapshot(entity_id="MSFT").snapshot_id != base.snapshot_id
    assert make_snapshot(payload={"revenue_usd": 1.0}).snapshot_id != base.snapshot_id
    assert make_snapshot(pit=make_pit(published_at=T1, known_at=T1)).snapshot_id != base.snapshot_id


def test_acquisition_metadata_excluded_from_identity():
    # A later re-retrieval of identical information reproduces the identical ID.
    later = datetime(2026, 9, 1, tzinfo=UTC)
    a = make_snapshot()
    b = make_snapshot(pit=make_pit(retrieved_at=later), provenance=make_provenance(recorded_at=later))
    assert a.snapshot_id == b.snapshot_id


def test_source_identity_ignores_characterization():
    a = make_source(cost_class="free", status="planned")
    b = make_source(cost_class="paid_optional", status="active")
    assert a.source_id == b.source_id  # same source, re-characterized


def test_deterministic_id_prefix_validated():
    with pytest.raises(CanonicalizationError):
        deterministic_id("bad prefix!", {"x": 1})


# ── Immutability ───────────────────────────────────────────────────────────


def test_frozen_fields_cannot_be_reassigned():
    snap = make_snapshot()
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.entity_id = "MSFT"  # type: ignore[misc]


def test_nested_payload_mutation_cannot_change_evidence():
    payload = {"revenue_usd": 94_100_000_000.0, "currency": "USD", "tags": ["q2"]}
    snap = make_snapshot(payload=payload)
    before_id, before_hash = snap.snapshot_id, snap.payload_hash
    # Mutating the caller's dict after construction changes nothing.
    payload["revenue_usd"] = 0.0
    payload["tags"].append("tampered")
    assert snap.snapshot_id == before_id and snap.payload_hash == before_hash
    # Mutating a returned copy changes nothing either.
    copy = snap.payload_copy()
    copy["revenue_usd"] = -1
    assert snap.payload_copy()["revenue_usd"] == 94_100_000_000.0
    assert snap.snapshot_id == before_id


# ── Timezone discipline ────────────────────────────────────────────────────


def test_naive_datetimes_rejected_everywhere():
    naive = datetime(2026, 8, 5, 13, 30)
    with pytest.raises(ValueError):
        make_pit(published_at=naive)
    with pytest.raises(ValueError):
        make_provenance(recorded_at=naive)
    with pytest.raises(CanonicalizationError):
        canonical_dumps({"t": naive})
    with pytest.raises(ValueError):
        FeatureRecord(
            feature_name="f", derivation_id="derivation.f", derivation_version="1",
            entity_id="AAPL", as_of=naive, value=1.0,
            inputs=(make_snapshot().ref(),), provenance=make_provenance(producer_type="derivation"),
        )


def test_timestamps_encode_as_utc_z():
    from datetime import timedelta

    est = datetime(2026, 8, 5, 9, 30, tzinfo=timezone(timedelta(hours=-4)))
    text = canonical_dumps({"t": est})
    assert '"2026-08-05T13:30:00.000000Z"' in text


# ── PIT semantics — no fabricated time ─────────────────────────────────────


def test_missing_timestamps_stay_explicitly_missing():
    pit = PointInTime(retrieved_at=T1)  # nothing else known
    d = pit.to_canonical_dict()
    assert d["observed_at"] is None and d["published_at"] is None and d["known_at"] is None
    assert d["known_at_basis"] == KNOWN_AT_UNKNOWN
    # Round-trip preserves explicit missingness.
    rt = PointInTime.from_dict(json.loads(canonical_dumps(d)))
    assert rt.known_at is None and rt.known_at_basis == KNOWN_AT_UNKNOWN


def test_known_at_requires_documented_basis():
    with pytest.raises(ValueError):
        PointInTime(known_at=T0)  # basis defaults to unknown → contradiction
    with pytest.raises(ValueError):
        PointInTime(known_at_basis=KNOWN_AT_SOURCE_REPORTED)  # basis without value


def test_conservative_derivation_is_explicit_and_recorded():
    pit = PointInTime(retrieved_at=T1)
    derived = pit.with_conservative_known_at()
    assert derived.known_at == T1
    assert derived.known_at_basis == KNOWN_AT_DERIVED_CONSERVATIVE
    assert pit.known_at is None  # original untouched
    with pytest.raises(ValueError):
        derived.with_conservative_known_at()  # never overwrite
    with pytest.raises(ValueError):
        PointInTime().with_conservative_known_at()  # nothing to derive from


def test_effective_period_ordering_enforced():
    with pytest.raises(ValueError):
        make_pit(effective_period_start=date(2026, 6, 30), effective_period_end=date(2026, 4, 1))


# ── Multi-source coexistence ───────────────────────────────────────────────


def test_two_providers_same_metric_period_coexist_distinctly():
    sec = make_source(provider="sec_edgar")
    fmp = make_source(provider="fmp", dataset="income_statement")
    a = make_snapshot(source_id=sec.source_id, payload={"revenue_usd": 94_100_000_000.0})
    b = make_snapshot(source_id=fmp.source_id, payload={"revenue_usd": 94_000_000_000.0})
    assert a.snapshot_id != b.snapshot_id
    assert a.entity_id == b.entity_id and a.evidence_type == b.evidence_type
    # No uniqueness on entity+metric+period: both are valid simultaneously —
    # Phase 0C may later emit DATA_CONFLICT from exactly this pair.


# ── Revision ───────────────────────────────────────────────────────────────


def test_revision_creates_new_identity_and_preserves_original():
    original = make_snapshot()
    original_id, original_payload = original.snapshot_id, original.payload_copy()
    revised = original.revise(
        new_payload={"revenue_usd": 94_500_000_000.0, "currency": "USD"},
        pit=make_pit(published_at=T1, known_at=T1),
        provenance=make_provenance(recorded_at=datetime(2026, 9, 1, tzinfo=UTC)),
    )
    assert revised.snapshot_id != original_id
    assert revised.supersedes_snapshot_id == original_id
    assert original.payload_copy() == original_payload  # untouched
    assert original.supersedes_snapshot_id is None


# ── EvidenceRef ────────────────────────────────────────────────────────────


def test_evidence_ref_round_trip_resolves_identity():
    snap = make_snapshot()
    ref = snap.ref()
    rt = EvidenceRef.from_dict(json.loads(canonical_dumps(ref.to_canonical_dict())))
    assert rt == ref
    assert rt.matches(snap)
    assert not rt.matches(make_snapshot(entity_id="MSFT"))


def test_evidence_ref_rejects_non_evidence_ids():
    with pytest.raises(ValueError):
        EvidenceRef(snapshot_id="ftr_deadbeef", source_id="s", entity_id="e",
                    evidence_type="t", payload_hash="h")


# ── FeatureRecord ──────────────────────────────────────────────────────────


def _feature(**kw) -> FeatureRecord:
    derivation_id = kw.pop("derivation_id", "derivation.revenue_yoy")
    derivation_version = kw.pop("derivation_version", "1.0.0")
    return FeatureRecord(
        feature_name=kw.pop("feature_name", "revenue_yoy"),
        derivation_id=derivation_id,
        derivation_version=derivation_version,
        entity_id=kw.pop("entity_id", "AAPL"),
        as_of=kw.pop("as_of", T1),
        value=kw.pop("value", 0.061),
        inputs=kw.pop("inputs", (make_snapshot().ref(),)),
        provenance=kw.pop("provenance", make_provenance(
            producer_id=derivation_id, producer_type="derivation",
            transformation_id=f"{derivation_id}@{derivation_version}")),
        **kw,
    )


def test_feature_records_inputs_and_derivation_version():
    f = _feature()
    assert len(f.inputs) == 1 and f.inputs[0].snapshot_id.startswith("evs_")
    assert f.derivation_version == "1.0.0"
    rt = FeatureRecord.from_dict(json.loads(canonical_dumps(f.to_canonical_dict())))
    assert rt.feature_id == f.feature_id and rt == f


def test_feature_requires_evidence_inputs_unless_missing():
    with pytest.raises(ValueError):
        _feature(inputs=())
    missing = _feature(inputs=(), value=None, quality="missing")
    assert missing.quality == "missing" and missing.value is None


def test_feature_is_not_evidence():
    f = _feature()
    assert f.contract_type == "feature_record"
    assert f.feature_id.startswith("ftr_")
    assert not f.feature_id.startswith("evs_")
    # Changing the derivation version changes identity (reproducibility).
    assert _feature(derivation_version="1.1.0").feature_id != f.feature_id


def test_feature_value_kinds_validated():
    assert _feature(value=[1.0, 2.0, 3.0]).feature_id != _feature(value=[1.0, 2.0]).feature_id
    for bad in ({"a": 1}, [1.0] * 33, [], ["x"], float("inf")):
        with pytest.raises(ValueError):
            _feature(value=bad)


# ── Source isolation + secrets ─────────────────────────────────────────────


def test_canonical_contracts_carry_no_vendor_response_structures():
    # The kernel modules must not reference vendor-specific response schemas.
    import inspect
    from portfolio_automation.northstar import canonical, evidence, features, pit, provenance, sources

    for mod in (canonical, pit, provenance, evidence, features):
        src = inspect.getsource(mod).lower()
        for vendor_marker in ("historical-price", "quote_short", "edgar_full", "finra_"):
            assert vendor_marker not in src, f"{mod.__name__} leaks vendor schema {vendor_marker}"
    # sources.py may NAME providers as examples but defines no vendor fields.
    field_names = {f.name for f in dataclasses.fields(DataSourceDescriptor)}
    assert field_names.isdisjoint({"url", "endpoint", "api_key", "token", "auth", "password"})


def test_descriptor_rejects_credential_material():
    for bad_note in ("api_key=abc123", "Bearer xyz", "token=deadbeef", "sk-abcdefghijkl"):
        with pytest.raises(ValueError):
            make_source(notes=bad_note)
    with pytest.raises(ValueError):
        make_source(dataset="quotes?apikey=SECRET")


# ── Round trip + versioning ────────────────────────────────────────────────


def test_snapshot_round_trip_semantic_equality():
    snap = make_snapshot()
    rt = EvidenceSnapshot.from_json(snap.to_json())
    assert rt == snap and rt.snapshot_id == snap.snapshot_id
    assert rt.pit == snap.pit and rt.provenance == snap.provenance


def test_schema_version_survives_and_is_validated():
    snap = make_snapshot()
    data = json.loads(snap.to_json())
    assert data["schema_version"] == "1.0.0"
    del data["schema_version"]
    with pytest.raises(ValueError):
        EvidenceSnapshot.from_dict(data)
    src_data = make_source().to_canonical_dict()
    assert src_data["schema_version"] == "1.0.0"


def test_tampered_serialized_identity_is_rejected():
    data = json.loads(make_snapshot().to_json())
    data["payload"]["revenue_usd"] = 1.0  # payload no longer matches hashes
    with pytest.raises(ValueError):
        EvidenceSnapshot.from_dict(data)


def test_wrong_contract_type_rejected():
    data = json.loads(make_snapshot().to_json())
    data["contract_type"] = "capital_proposal"
    with pytest.raises(ValueError):
        EvidenceSnapshot.from_dict(data)


# ── Hardening: strict schema-version handling ──────────────────────────────


def _persisted_contract_cases():
    """(label, valid serialized dict, deserializer) for every persisted contract."""
    snap = make_snapshot()
    return [
        ("data_source_descriptor", make_source().to_canonical_dict(),
         DataSourceDescriptor.from_dict),
        ("evidence_snapshot", json.loads(snap.to_json()), EvidenceSnapshot.from_dict),
        ("evidence_ref", snap.ref().to_canonical_dict(), EvidenceRef.from_dict),
        ("feature_record",
         json.loads(canonical_dumps(_feature().to_canonical_dict())),
         FeatureRecord.from_dict),
    ]


def test_schema_version_fail_closed_on_every_persisted_contract():
    for label, valid, from_dict in _persisted_contract_cases():
        # supported version -> accept
        from_dict(json.loads(canonical_dumps(valid)))
        # missing -> reject
        data = dict(valid)
        del data["schema_version"]
        with pytest.raises(ValueError, match="schema_version"):
            from_dict(data)
        # empty / non-string / unknown / future -> reject
        for bad in ("", None, 1, "0.9.0", "2.0.0", "9.9.9"):
            data = dict(valid, schema_version=bad)
            with pytest.raises(ValueError, match="schema_version"):
                from_dict(data)


def test_constructors_pin_the_supported_schema_version():
    with pytest.raises(ValueError, match="schema_version"):
        make_source(schema_version="0.9.0")
    with pytest.raises(ValueError, match="schema_version"):
        make_snapshot(schema_version="2.0.0")
    with pytest.raises(ValueError, match="schema_version"):
        _feature(schema_version="")
    good = make_snapshot()
    with pytest.raises(ValueError, match="schema_version"):
        EvidenceRef(snapshot_id=good.snapshot_id, source_id=good.source_id,
                    entity_id="AAPL", evidence_type="fundamental.revenue",
                    payload_hash=good.payload_hash, schema_version="9.9.9")


# ── Hardening: schema era participates in deterministic identity ──────────


def test_schema_era_is_the_major_version_and_fails_closed():
    from portfolio_automation.northstar.canonical import schema_era

    assert schema_era("1.0.0") == 1
    assert schema_era("12.3.4") == 12
    for bad in ("", "1.0", "v1.0.0", "1.0.0-beta", "01.0.0", "1.0.0.0"):
        with pytest.raises(CanonicalizationError):
            schema_era(bad)


def test_schema_era_participates_in_every_deterministic_identity():
    # The era (major version) is in each identity payload; bumping it mints a
    # new identity while all semantic fields stay identical.
    for obj, prop, prefix in (
        (make_source(), "source_id", "src"),
        (make_snapshot(), "snapshot_id", "evs"),
        (_feature(), "feature_id", "ftr"),
    ):
        payload = obj._identity_payload()
        assert payload["schema_era"] == 1
        assert getattr(obj, prop) == deterministic_id(prefix, payload)
        assert deterministic_id(prefix, dict(payload, schema_era=2)) != getattr(obj, prop)


# ── Hardening: identifier / hash format integrity ──────────────────────────


def test_evidence_ref_validates_identifier_formats():
    good = make_snapshot().ref()
    hex32, hex64 = "a" * 32, "b" * 64

    def build(**over):
        base = dict(snapshot_id=good.snapshot_id, source_id=good.source_id,
                    entity_id="AAPL", evidence_type="fundamental.revenue",
                    payload_hash=good.payload_hash)
        base.update(over)
        return EvidenceRef(**base)

    build()  # well-formed baseline
    for bad_snapshot in (f"evs_{'a'*31}", f"evs_{'A'*32}", f"evs_{'a'*33}",
                         f"src_{hex32}", "evs_", hex32):
        with pytest.raises(ValueError):
            build(snapshot_id=bad_snapshot)
    for bad_source in (f"evs_{hex32}", "src_short", f"src_{'F'*32}"):
        with pytest.raises(ValueError):
            build(source_id=bad_source)
    for bad_hash in ("b" * 63, "b" * 65, "B" * 64, f"sha256:{'b'*57}"):
        with pytest.raises(ValueError):
            build(payload_hash=bad_hash)


def test_snapshot_validates_source_and_supersedes_formats():
    with pytest.raises(ValueError):
        make_snapshot(source_id="not_a_source_id")
    with pytest.raises(ValueError):
        make_snapshot(supersedes_snapshot_id="evs_NOTHEX")
    ok = make_snapshot(supersedes_snapshot_id=f"evs_{'a'*32}")
    assert ok.supersedes_snapshot_id == f"evs_{'a'*32}"


# ── Hardening: provenance consistency ──────────────────────────────────────


def test_source_adapter_provenance_requires_source_id():
    with pytest.raises(ValueError, match="source_id"):
        Provenance(producer_id="adapter.x", producer_type="source_adapter",
                   recorded_at=T1)
    # Non-adapter producers may legitimately omit source_id.
    Provenance(producer_id="system.replayer", producer_type="system", recorded_at=T1)


def test_provenance_source_id_format_validated_when_present():
    with pytest.raises(ValueError):
        make_provenance(source_id="fmp")  # not a src_<32hex> identity


def test_snapshot_rejects_contradicting_provenance_source():
    other = make_source(provider="fmp", dataset="income_statement")
    with pytest.raises(ValueError, match="contradicts"):
        make_snapshot(provenance=make_provenance(source_id=other.source_id))
    # Matching source_id is required content for adapters and passes.
    assert make_snapshot().provenance.source_id == make_snapshot().source_id


def test_feature_provenance_transformation_must_match_derivation():
    with pytest.raises(ValueError, match="transformation_id"):
        _feature(provenance=make_provenance(
            producer_id="derivation.revenue_yoy", producer_type="derivation",
            transformation_id="derivation.other@1.0.0"))
    with pytest.raises(ValueError, match="transformation_id"):
        _feature(provenance=make_provenance(
            producer_id="derivation.revenue_yoy", producer_type="derivation",
            transformation_id="derivation.revenue_yoy@9.9.9"))
    # transformation_id is optional; when absent nothing can contradict.
    ok = _feature(provenance=make_provenance(
        producer_id="derivation.revenue_yoy", producer_type="derivation"))
    assert ok.provenance.transformation_id is None


# ── Hardening: feature inputs are an unordered dependency set ──────────────


def test_feature_input_order_is_deliberately_identity_free():
    a = make_snapshot(entity_id="AAPL").ref()
    b = make_snapshot(entity_id="MSFT").ref()
    forward = _feature(inputs=(a, b))
    reversed_ = _feature(inputs=(b, a))
    assert forward.feature_id == reversed_.feature_id  # dependency SET semantics


def test_feature_rejects_duplicate_inputs():
    a = make_snapshot().ref()
    with pytest.raises(ValueError, match="duplicate"):
        _feature(inputs=(a, a))


# ── Separation invariants ──────────────────────────────────────────────────


def test_no_execution_or_approval_surface_on_evidence_contracts():
    forbidden = {
        "execute", "order", "buy", "sell", "trade", "approve", "promote",
        "allocate", "broker", "position",
    }
    for cls in (DataSourceDescriptor, EvidenceSnapshot, EvidenceRef, FeatureRecord,
                PointInTime, Provenance):
        names = {f.name for f in dataclasses.fields(cls)}
        methods = {m for m in dir(cls) if not m.startswith("_")}
        for word in forbidden:
            assert not any(word in n.lower() for n in names), (cls.__name__, word)
            assert not any(m.lower().startswith(word) for m in methods), (cls.__name__, word)


def test_milestone3_contracts_not_yet_present():
    # Milestone discipline: milestone 3 (decision/outcome/passport) is now
    # underway — ExperimentSpec is delivered + verified; the remaining
    # milestone-3 families stay unimplemented until built.
    import portfolio_automation.northstar as ns

    assert hasattr(ns, "ExperimentSpec")     # milestone 3 — delivered
    assert hasattr(ns, "ExperimentResult")   # milestone 3 — delivered
    for later in ("CapitalProposal", "ExitProposal", "StrategyPassport", "OutcomeRecord"):
        assert not hasattr(ns, later)
