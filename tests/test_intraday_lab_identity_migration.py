"""Session 2 identity-era, migration, provider-identity and tamper contracts.

These tests exist because of a real incident: on 2026-08-09 the raw and
canonical identity functions were extended (correctly), and the verifier — which
recomputed every object under the CURRENT function — reported five byte-perfect
immutable objects with the tampering reason. The corpus was intact; the question
had changed.

The contract frozen here is therefore two-sided, and BOTH sides are load-bearing:

    a legacy object must never be reported as tampered
    a legacy object must never be silently admitted to current research

Either failure alone destroys the value of the store — the first by making the
tamper signal meaningless, the second by letting research run on data that does
not satisfy today's point-in-time contract.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from portfolio_automation.intraday_lab import calendar as C
from portfolio_automation.intraday_lab import dataset as DS
from portfolio_automation.intraday_lab import features as F
from portfolio_automation.intraday_lab import foundation as FD
from portfolio_automation.intraday_lab import identity as ID
from portfolio_automation.intraday_lab import migration as MG
from portfolio_automation.intraday_lab import models as M
from portfolio_automation.intraday_lab import pipeline as PL
from portfolio_automation.intraday_lab import providers as PR
from portfolio_automation.intraday_lab import storage as ST

UTC = timezone.utc
NORMAL = date(2026, 8, 3)


# ── fixtures ───────────────────────────────────────────────────────────────
def _rows(session, n=None):
    out = []
    for i, ts in enumerate(session.expected_bar_starts[:n]):
        local = ts.astimezone(C.EXCHANGE_TZ).strftime("%Y-%m-%d %H:%M:%S")
        out.append({"date": local, "open": 100 + i * 0.01, "high": 100.5 + i * 0.01,
                    "low": 99.5 + i * 0.01, "close": 100.2 + i * 0.01,
                    "volume": 1000 + i})
    return out


def _provider(rows_by_symbol, **kw):
    return PR.FakeIntradayProvider(rows_by_symbol, **kw)


def _built(tmp_path, symbols=("SPY",)):
    session = C.resolve_session(NORMAL)
    return PL.build_historical_research_dataset(
        DS.DatasetRequest(symbols=symbols, start=NORMAL, end=NORMAL),
        _provider({s: _rows(session) for s in symbols}), root=str(tmp_path))


def _write_legacy_raw(tmp_path, rows, *, symbol="SPY", timeframe="5min",
                      provider="fmp", endpoint="/stable/historical-chart/5min"):
    """Write a raw object exactly as the PRE-era-registry code did.

    Deliberately hand-built rather than produced by current code: the whole
    point is to exercise objects this build can no longer mint.
    """
    man = {"symbol": symbol, "timeframe": timeframe}
    fp = ID._raw_v1(rows, man)
    ST.write_snapshot(ST.RAW, fp, {
        "payload.json": rows,
        "content_manifest.json": {
            "schema_version": "1", "provider": provider, "endpoint": endpoint,
            "symbol": symbol, "timeframe": timeframe, "row_count": len(rows),
            "raw_content_fingerprint": fp},
    }, root=str(tmp_path))
    return fp


def _write_legacy_canonical(tmp_path, bars_rows, *, timeframe="5min",
                            adjustment_state="split_adjusted"):
    man = {"timeframe": timeframe, "adjustment_state": adjustment_state}
    fp = ID._canonical_v2(bars_rows, man)
    ST.write_snapshot(ST.DATASETS, fp, {
        "canonical_bars.json": bars_rows,
        "content_manifest.json": {
            "schema_version": "1", "dataset_fingerprint": fp,
            "timeframe": timeframe, "adjustment_state": adjustment_state,
            "bar_count": len(bars_rows)},
    }, root=str(tmp_path))
    return fp


def _tamper(path, mutate):
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data, separators=(",", ":"), sort_keys=True))


# ═══════════════════════════════════════════════════════════════════════════
# §10  PIT IDENTITY — knowability must change current research identity
# ═══════════════════════════════════════════════════════════════════════════
def _pit_bar(delay_s, **kw):
    return M.IntradayBar(symbol="SPY", timeframe="5min",
                         bar_start_at=datetime(2026, 8, 3, 13, 30, tzinfo=UTC),
                         open=100, high=101, low=99, close=100.5, volume=1000,
                         adjustment_state="split_adjusted",
                         publication_delay=timedelta(seconds=delay_s), **kw)


def test_known_at_mutation_changes_current_identity():
    """Two datasets identical in OHLCV and bar_start_at, but B is publishable
    60s earlier. B confers a look-ahead advantage; they are NOT research
    equivalent. The v2 era gave them ONE identity."""
    a, b = _pit_bar(60), _pit_bar(0)
    assert a.known_at != b.known_at
    fa = DS.canonical_fingerprint([a], timeframe="5min", adjustment_state="split_adjusted")
    fb = DS.canonical_fingerprint([b], timeframe="5min", adjustment_state="split_adjusted")
    assert fa != fb

    # And prove the OLD era genuinely could not tell them apart — otherwise this
    # test would pass for the wrong reason and the era change looks unnecessary.
    rows_a = ST.bars_to_rows([a])
    rows_b = ST.bars_to_rows([b])
    man = {"timeframe": "5min", "adjustment_state": "split_adjusted"}
    assert ID._canonical_v2(rows_a, man) == ID._canonical_v2(rows_b, man)
    assert ID._canonical_v3(rows_a, man) != ID._canonical_v3(rows_b, man)


def test_bar_end_at_mutation_changes_current_identity():
    original = dict(M.TIMEFRAMES)
    try:
        M.TIMEFRAMES["15min"] = timedelta(minutes=15)
        a = _pit_bar(60)
        b = M.IntradayBar(symbol="SPY", timeframe="15min",
                          bar_start_at=a.bar_start_at, open=100, high=101, low=99,
                          close=100.5, volume=1000, adjustment_state="split_adjusted")
        assert a.bar_end_at != b.bar_end_at
        assert DS.canonical_fingerprint([a], timeframe="5min",
                                        adjustment_state="split_adjusted") != \
               DS.canonical_fingerprint([b], timeframe="15min",
                                        adjustment_state="split_adjusted")
    finally:
        M.TIMEFRAMES.clear()
        M.TIMEFRAMES.update(original)


def test_current_canonical_identity_protects_every_pit_field():
    """Every field the contract claims to protect must actually move the hash.

    A documented guarantee that no test exercises is how protection silently
    regresses when the field list is edited later.
    """
    man = {"timeframe": "5min", "adjustment_state": "split_adjusted"}
    base = ST.bars_to_rows([_pit_bar(60)])
    baseline = ID._canonical_v3(base, man)
    mutations = {
        "symbol": "AAPL", "bar_start_at": "2026-08-03T14:30:00+00:00",
        "bar_end_at": "2026-08-03T13:40:00+00:00",
        "known_at": "2026-08-03T13:99:00+00:00".replace("99", "40"),
        "open": 100.25, "high": 101.5, "low": 98.5, "close": 100.75, "volume": 999,
    }
    for field, value in mutations.items():
        rows = [dict(base[0], **{field: value})]
        assert ID._canonical_v3(rows, man) != baseline, f"{field} does not affect identity"
    # adjustment_state lives in the manifest half of the payload.
    assert ID._canonical_v3(base, {**man, "adjustment_state": "raw"}) != baseline


# ═══════════════════════════════════════════════════════════════════════════
# §23  LEGACY OBJECT SEMANTICS
# ═══════════════════════════════════════════════════════════════════════════
def test_intact_legacy_raw_verifies_and_is_not_called_tampered(tmp_path):
    rows = _rows(C.resolve_session(NORMAL), 5)
    fp = _write_legacy_raw(tmp_path, rows)
    v = ST.verify_raw_content(fp, root=str(tmp_path))
    assert v["verified"] is True
    assert v["state"] == ID.VERIFIED_LEGACY_MIGRATABLE
    assert v["identity_schema"] == ID.RAW_V1
    assert v["current_era"] is False          # sound evidence, not current
    assert v["reason"] is None                # and NOT accused of tampering


def test_intact_legacy_canonical_verifies_and_is_not_called_tampered(tmp_path):
    out = _built(tmp_path)
    bars = ST.read_snapshot(ST.DATASETS, out["dataset_fingerprint"],
                            "canonical_bars.json", root=str(tmp_path))
    fp = _write_legacy_canonical(tmp_path, bars)
    v = ST.verify_canonical_snapshot(fp, root=str(tmp_path))
    assert v["verified"] is True
    assert v["state"] == ID.VERIFIED_LEGACY_MIGRATABLE
    assert v["identity_schema"] == ID.CANONICAL_V2
    assert v["current_era"] is False
    assert v["reason"] is None


def test_modified_legacy_object_is_an_integrity_failure(tmp_path):
    """The tamper signal must survive era-awareness — that is the whole risk."""
    rows = _rows(C.resolve_session(NORMAL), 5)
    fp = _write_legacy_raw(tmp_path, rows)
    _tamper(ST.intraday_root(str(tmp_path)) / "raw" / "content" / fp / "payload.json",
            lambda d: d.__setitem__(0, {**d[0], "close": 4242.0}))
    v = ST.verify_raw_content(fp, root=str(tmp_path))
    assert v["verified"] is False
    assert v["state"] == ID.INTEGRITY_FAILURE
    assert v["current_era"] is False


def test_unknown_declared_identity_schema_fails_closed(tmp_path):
    """An era this build does not implement is unverifiable, not corrupt — and
    must never be silently downgraded to an era we do happen to have."""
    rows = _rows(C.resolve_session(NORMAL), 5)
    out = _built(tmp_path)
    fp = out["raw_content_fingerprints"][0]
    _tamper(ST.intraday_root(str(tmp_path)) / "raw" / "content" / fp / "content_manifest.json",
            lambda d: d.__setitem__("identity_schema", "intraday_raw_v99"))
    v = ST.verify_raw_content(fp, root=str(tmp_path))
    assert v["verified"] is False
    assert v["state"] == ID.UNSUPPORTED_IDENTITY_SCHEMA
    assert v["current_era"] is False
    assert "v99" in v["reason"]


def test_declared_era_is_not_downgraded_to_another_matching_era(tmp_path):
    """A v1-minted object that CLAIMS v2 must fail, even though v1 would verify.

    Probing past a declaration would let a forger choose whichever historical
    function validates their bytes.
    """
    rows = _rows(C.resolve_session(NORMAL), 5)
    fp = _write_legacy_raw(tmp_path, rows)
    _tamper(ST.intraday_root(str(tmp_path)) / "raw" / "content" / fp / "content_manifest.json",
            lambda d: d.__setitem__("identity_schema", ID.RAW_V2))
    v = ST.verify_raw_content(fp, root=str(tmp_path))
    assert v["verified"] is False
    assert v["state"] == ID.INTEGRITY_FAILURE


def test_ambiguous_identity_era_fails_closed(monkeypatch):
    """If two eras both reproduce an identity the object's meaning is not
    determined, so it must not be treated as verified under either."""
    rows = [{"a": 1}]
    man = {"symbol": "SPY", "timeframe": "5min", "provider": "fmp", "endpoint": "/e"}
    clash = ID.IdentityEra("clash_v0", ID._raw_v2, ("x",), "duplicate of v2")
    monkeypatch.setitem(ID._REGISTRY, "raw", ID.RAW_ERAS + (clash,))
    att = ID.attribute("raw", ID._raw_v2(rows, man), rows, man)
    assert att["state"] == ID.AMBIGUOUS_IDENTITY_SCHEMA
    assert set(att["probed"]) == {ID.RAW_V2, "clash_v0"}


def test_legacy_object_whose_bytes_cannot_express_current_era_is_archival(tmp_path):
    """A legacy object missing a field the CURRENT identity protects can never
    be migrated from its own bytes. That is archival, not corrupt, and it must
    not be silently treated as migratable."""
    out = _built(tmp_path)
    bars = ST.read_snapshot(ST.DATASETS, out["dataset_fingerprint"],
                            "canonical_bars.json", root=str(tmp_path))
    stripped = [{k: v for k, v in r.items() if k != "known_at"} for r in bars]
    fp = _write_legacy_canonical(tmp_path, stripped)
    v = ST.verify_canonical_snapshot(fp, root=str(tmp_path))
    assert v["verified"] is True
    assert v["state"] == ID.VERIFIED_LEGACY_ARCHIVAL
    assert v["current_era"] is False
    assert v["migration_required"] is False       # nothing to migrate TO
    assert MG.migrate_canonical_content(fp, root=str(tmp_path))["status"] == MG.NOT_MIGRATABLE


# ═══════════════════════════════════════════════════════════════════════════
# §11–14  MIGRATION
# ═══════════════════════════════════════════════════════════════════════════
def _legacy_graph(tmp_path):
    """Build a current graph, then rewrite its manifest to reference legacy-era
    objects — the exact shape the real corpus was found in."""
    out = _built(tmp_path)
    root = str(tmp_path)
    bars = ST.read_snapshot(ST.DATASETS, out["dataset_fingerprint"],
                            "canonical_bars.json", root=root)
    legacy_canon = _write_legacy_canonical(tmp_path, bars)

    legacy_raws = []
    for raw_fp in out["raw_content_fingerprints"]:
        payload = ST.read_snapshot(ST.RAW, raw_fp, "payload.json", root=root)
        man = ST.read_snapshot(ST.RAW, raw_fp, "content_manifest.json", root=root)
        legacy_raws.append(_write_legacy_raw(
            tmp_path, payload, symbol=man["symbol"], timeframe=man["timeframe"],
            provider=man["provider"], endpoint=man["endpoint"]))

    man = ST.read_snapshot(ST.DATASET_MANIFESTS, out["manifest_fingerprint"],
                           "dataset_manifest.json", root=root)
    req = ST.read_snapshot(ST.DATASET_MANIFESTS, out["manifest_fingerprint"],
                           "request_manifest.json", root=root)
    recon = ST.read_snapshot(ST.DATASET_MANIFESTS, out["manifest_fingerprint"],
                             "reconciliation.json", root=root)
    sessions = [[r["symbol"], r["market_date"], r["admission_status"]] for r in recon]
    legacy_mfp = DS.manifest_fingerprint_from_parts(
        content_fingerprint=legacy_canon, request=man["request"],
        calendar=DS._calendar_identity(), timeframe=man["timeframe"],
        adjustment_state=man["adjustment_state"], sessions=sessions)
    ST.write_snapshot(ST.DATASET_MANIFESTS, legacy_mfp, {
        "dataset_manifest.json": {**man, "dataset_fingerprint": legacy_canon,
                                  "manifest_fingerprint": legacy_mfp,
                                  "dataset_id": f"intraday-5min-{legacy_canon[:16]}",
                                  "fingerprint_schema": ID.CANONICAL_V2},
        "reconciliation.json": recon,
        "request_manifest.json": {k: v for k, v in
                                  {**req, "canonical_content_fingerprint": legacy_canon,
                                   "manifest_fingerprint": legacy_mfp,
                                   "raw_content_fingerprints": sorted(legacy_raws)}.items()
                                  if k != "calendar_identity"},
    }, root=root)
    return out, legacy_mfp, legacy_canon, legacy_raws


def test_legacy_graph_is_verified_but_not_research_ready(tmp_path):
    out, legacy_mfp, _, _ = _legacy_graph(tmp_path)
    prov = ST.verify_dataset_provenance(legacy_mfp, root=str(tmp_path))
    assert prov["verified"] is True             # integrity: sound
    assert prov["current_era"] is False         # eligibility: no
    assert FD._canonical_ready({"manifest_fingerprint": legacy_mfp,
                                "dataset_fingerprint": prov["canonical_content_fingerprint"]},
                               str(tmp_path)) is False


def test_migration_produces_the_same_identity_the_pipeline_would_mint(tmp_path):
    """Determinism proof: migrating legacy bytes must land exactly on the
    identity a fresh current-era build produces for the same data."""
    out, legacy_mfp, legacy_canon, legacy_raws = _legacy_graph(tmp_path)
    res = MG.migrate_dataset_manifest(legacy_mfp, root=str(tmp_path))
    assert res["status"] == MG.MIGRATED
    assert res["current_manifest_fingerprint"] == out["manifest_fingerprint"]
    assert res["canonical"]["current_identity"] == out["dataset_fingerprint"]
    assert sorted(r["current_identity"] for r in res["raw"]) == \
        sorted(out["raw_content_fingerprints"])


def test_migrated_graph_becomes_research_ready(tmp_path):
    out, legacy_mfp, _, _ = _legacy_graph(tmp_path)
    MG.migrate_dataset_manifest(legacy_mfp, root=str(tmp_path))
    prov = ST.verify_dataset_provenance(out["manifest_fingerprint"], root=str(tmp_path))
    assert prov["verified"] is True and prov["current_era"] is True
    assert FD._canonical_ready(out, str(tmp_path)) is True


def test_migration_never_mutates_the_legacy_object(tmp_path):
    out, legacy_mfp, legacy_canon, legacy_raws = _legacy_graph(tmp_path)
    root = ST.intraday_root(str(tmp_path))
    before = {p: p.read_bytes() for p in (root / "datasets" / "content" / legacy_canon).iterdir()}
    before.update({p: p.read_bytes()
                   for p in (root / "datasets" / "manifests" / legacy_mfp).iterdir()})
    for fp in legacy_raws:
        before.update({p: p.read_bytes() for p in (root / "raw" / "content" / fp).iterdir()})

    MG.migrate_dataset_manifest(legacy_mfp, root=str(tmp_path))

    for path, data in before.items():
        assert path.exists(), f"{path} was deleted by migration"
        assert path.read_bytes() == data, f"{path} was rewritten by migration"


def test_migration_is_idempotent(tmp_path):
    _, legacy_mfp, _, _ = _legacy_graph(tmp_path)
    first = MG.migrate_dataset_manifest(legacy_mfp, root=str(tmp_path))
    second = MG.migrate_dataset_manifest(legacy_mfp, root=str(tmp_path))
    # The second pass recognises completed work from lineage instead of
    # re-deriving it, and must never mint a different target.
    assert first["status"] == MG.MIGRATED
    assert second["status"] == MG.ALREADY_MIGRATED
    assert second["current_manifest_fingerprint"] == first["current_manifest_fingerprint"]
    assert second["lineage_id"] == first["lineage_id"]


def test_migration_lineage_is_persisted_and_complete(tmp_path):
    _, legacy_mfp, legacy_canon, _ = _legacy_graph(tmp_path)
    res = MG.migrate_dataset_manifest(legacy_mfp, root=str(tmp_path))
    body = ST.read_snapshot(ST.MIGRATIONS, res["lineage_id"],
                            "migration_lineage.json", root=str(tmp_path))
    assert body["legacy_identity"] == legacy_mfp
    assert body["current_identity"] == res["current_manifest_fingerprint"]
    assert body["legacy_canonical_identity"] == legacy_canon
    assert body["migration_version"] == MG.MIGRATION_VERSION
    assert body["legacy_object_retained"] is True
    assert body["legacy_eligibility"] == "ARCHIVAL_EVIDENCE_ONLY"
    assert body["content_equivalence"]["legacy_manifest_identity_replayed"] is True
    # WHEN it happened is an event fact, never inside the content-addressed body.
    assert "migrated_at" not in body


def test_reminted_features_keep_values_and_change_identity(tmp_path):
    """§14: numerically identical, but a NEW fingerprint, because feature
    identity binds to the source dataset. Relabelling would break that binding.
    """
    out, legacy_mfp, legacy_canon, _ = _legacy_graph(tmp_path)
    root = str(tmp_path)
    legacy_bars = ST.bars_from_rows(ST.read_snapshot(ST.DATASETS, legacy_canon,
                                                     "canonical_bars.json", root=root))
    legacy_vals = PL.features_from_bars(
        legacy_bars, dataset_id=f"intraday-5min-{legacy_canon[:16]}",
        fingerprint=legacy_canon, manifest_fingerprint=legacy_mfp)
    legacy_fp = F.feature_fingerprint(legacy_vals)

    res = MG.migrate_dataset_manifest(legacy_mfp, root=root)
    new_fp = res["features"]["feature_fingerprint"]
    new_rows = ST.read_snapshot(ST.FEATURES, new_fp, "features.json", root=root)

    def by_key(rows):
        return {(r["feature_id"], r["symbol"], r["event_at"]): r["value"] for r in rows}

    assert by_key([v.to_dict() for v in legacy_vals]) == by_key(new_rows)
    assert new_fp != legacy_fp
    assert new_rows[0]["source_dataset_fingerprint"] == out["dataset_fingerprint"]


def test_remint_refuses_when_calendar_meaning_cannot_be_reproduced(tmp_path, monkeypatch):
    """Migrating AFTER a calendar change must fail closed, not silently
    reinterpret archived research under new schedule semantics."""
    _, legacy_mfp, _, _ = _legacy_graph(tmp_path)
    base = C._schedule_digest()
    monkeypatch.setattr(C, "_schedule_digest",
                        lambda: {**base, "schedule_digest": "upgraded-schedule"})
    res = MG.migrate_dataset_manifest(legacy_mfp, root=str(tmp_path))
    assert res["status"] == MG.REFUSED
    assert "calendar" in res["reason"]


def test_active_corpus_separates_current_from_archival(tmp_path):
    out, legacy_mfp, _, _ = _legacy_graph(tmp_path)
    MG.migrate_dataset_manifest(legacy_mfp, root=str(tmp_path))
    ac = MG.active_corpus(root=str(tmp_path))
    active = {a["manifest_fingerprint"] for a in ac["active_manifests"]}
    archival = {a["manifest_fingerprint"] for a in ac["archival_manifests"]}
    assert out["manifest_fingerprint"] in active
    assert legacy_mfp in archival
    assert legacy_mfp not in active            # never silently reused
    assert ac["integrity_failures"] == []


# ═══════════════════════════════════════════════════════════════════════════
# §21–22  TAMPER CASCADES
# ═══════════════════════════════════════════════════════════════════════════
def test_raw_tampering_cascades_to_feature_readiness(tmp_path):
    out = _built(tmp_path)
    assert FD._canonical_ready(out, str(tmp_path)) is True
    raw_fp = out["raw_content_fingerprints"][0]
    _tamper(ST.intraday_root(str(tmp_path)) / "raw" / "content" / raw_fp / "payload.json",
            lambda d: d.__setitem__(0, {**d[0], "close": 4242.0}))
    assert ST.verify_raw_content(raw_fp, root=str(tmp_path))["verified"] is False
    assert ST.verify_dataset_provenance(out["manifest_fingerprint"],
                                        root=str(tmp_path))["verified"] is False
    assert FD._canonical_ready(out, str(tmp_path)) is False
    assert FD._feature_ready(out, str(tmp_path)) is False


def test_canonical_tampering_cascades_to_readiness(tmp_path):
    out = _built(tmp_path)
    _tamper(ST.intraday_root(str(tmp_path)) / "datasets" / "content"
            / out["dataset_fingerprint"] / "canonical_bars.json",
            lambda d: d.__setitem__(0, {**d[0], "known_at": "2030-01-01T00:00:00+00:00"}))
    v = ST.verify_canonical_snapshot(out["dataset_fingerprint"], root=str(tmp_path))
    assert v["verified"] is False and v["state"] == ID.INTEGRITY_FAILURE
    assert FD._canonical_ready(out, str(tmp_path)) is False


def test_feature_tampering_breaks_feature_readiness(tmp_path):
    out = _built(tmp_path)
    assert FD._feature_ready(out, str(tmp_path)) is True
    _tamper(ST.intraday_root(str(tmp_path)) / "features" / "content"
            / out["feature_fingerprint"] / "features.json",
            lambda d: d.__setitem__(0, {**d[0], "value": 99.0}))
    assert ST.verify_feature_snapshot(out["feature_fingerprint"],
                                      root=str(tmp_path))["verified"] is False
    assert FD._feature_ready(out, str(tmp_path)) is False


@pytest.mark.parametrize("field,value", [
    ("request_fingerprint", "tampered"),
    ("calendar_fingerprint", ""),
    ("canonical_content_fingerprint", "somethingelse"),
    ("requested_symbol_date_count", 99),
])
def test_manifest_tampering_cascades_to_readiness(tmp_path, field, value):
    out = _built(tmp_path)
    path = (ST.intraday_root(str(tmp_path)) / "datasets" / "manifests"
            / out["manifest_fingerprint"] / "request_manifest.json")
    _tamper(path, lambda d: d.__setitem__(field, value))
    assert ST.verify_dataset_provenance(out["manifest_fingerprint"],
                                        root=str(tmp_path))["verified"] is False
    assert FD._canonical_ready(out, str(tmp_path)) is False


def test_identity_schema_tampering_breaks_readiness(tmp_path):
    """The identity-schema field is itself tamper-relevant: relabelling an
    object's era must not let it slip through under a different verifier."""
    out = _built(tmp_path)
    _tamper(ST.intraday_root(str(tmp_path)) / "datasets" / "content"
            / out["dataset_fingerprint"] / "content_manifest.json",
            lambda d: d.__setitem__("identity_schema", ID.CANONICAL_V2))
    v = ST.verify_canonical_snapshot(out["dataset_fingerprint"], root=str(tmp_path))
    assert v["verified"] is False and v["state"] == ID.INTEGRITY_FAILURE
    assert FD._canonical_ready(out, str(tmp_path)) is False


def test_a_disappeared_reconciliation_record_fails_verification(tmp_path):
    out = _built(tmp_path)
    (ST.intraday_root(str(tmp_path)) / "datasets" / "manifests"
     / out["manifest_fingerprint"] / "reconciliation.json").write_text("[]")
    v = ST.verify_dataset_provenance(out["manifest_fingerprint"], root=str(tmp_path))
    assert v["verified"] is False and "disappeared" in v["reason"]


# ═══════════════════════════════════════════════════════════════════════════
# §16  FAILURE CAUSALITY — three distinct provider outcomes
# ═══════════════════════════════════════════════════════════════════════════
def test_provider_exception_is_not_missing_market_data(tmp_path):
    out = PL.build_historical_research_dataset(
        DS.DatasetRequest(symbols=("SPY",), start=NORMAL, end=NORMAL),
        _provider({}, raises={"SPY": RuntimeError("connection reset")}),
        root=str(tmp_path))
    acq = out["acquisitions"][0]
    assert acq["provider_status"] == "PROVIDER_ERROR"
    assert acq["error_code"] == "PROVIDER_EXCEPTION"
    assert {r["admission_status"] for r in out["rejections"]["rejections"]} == \
        {DS.REJECTED_PROVIDER_ERROR}


def test_empty_response_is_missing_bars(tmp_path):
    out = PL.build_historical_research_dataset(
        DS.DatasetRequest(symbols=("SPY",), start=NORMAL, end=NORMAL),
        _provider({"SPY": []}), root=str(tmp_path))
    assert out["acquisitions"][0]["provider_status"] == "NO_DATA"
    assert {r["admission_status"] for r in out["rejections"]["rejections"]} == \
        {DS.REJECTED_MISSING_BARS}


def test_normalization_failure_has_its_own_causal_state(tmp_path):
    """The provider ANSWERED; we could not interpret it. Reporting missing
    market data would hide a provider schema change entirely."""
    bad = [{"date": "not-a-timestamp", "open": 1, "high": 2, "low": 0.5,
            "close": 1.5, "volume": 10}]
    out = PL.build_historical_research_dataset(
        DS.DatasetRequest(symbols=("SPY",), start=NORMAL, end=NORMAL),
        _provider({"SPY": bad}), root=str(tmp_path))
    acq = out["acquisitions"][0]
    assert acq["provider_status"] == "OK"
    assert acq["normalization_status"].startswith("FAILED")
    assert acq["raw_payload_hash"]                       # raw evidence preserved
    statuses = {r["admission_status"] for r in out["rejections"]["rejections"]}
    assert statuses == {DS.REJECTED_NORMALIZATION_ERROR}
    assert DS.REJECTED_MISSING_BARS not in statuses
    assert DS.REJECTED_PROVIDER_ERROR not in statuses


def test_the_three_failure_causes_are_mutually_distinct(tmp_path):
    """Pinned as a table so a future refactor cannot quietly merge two causes."""
    session = C.resolve_session(NORMAL)
    cases = {
        "exception": (_provider({}, raises={"SPY": RuntimeError("boom")}),
                      DS.REJECTED_PROVIDER_ERROR),
        "empty": (_provider({"SPY": []}), DS.REJECTED_MISSING_BARS),
        "unparseable": (_provider({"SPY": [{"date": "nope", "open": 1, "high": 2,
                                            "low": 0.5, "close": 1.5, "volume": 1}]}),
                        DS.REJECTED_NORMALIZATION_ERROR),
        "ok": (_provider({"SPY": _rows(session)}), DS.ADMITTED),
    }
    seen = {}
    for name, (prov, expected) in cases.items():
        out = PL.build_historical_research_dataset(
            DS.DatasetRequest(symbols=("SPY",), start=NORMAL, end=NORMAL), prov,
            root=str(tmp_path / name))
        statuses = {r["admission_status"] for r in
                    (out["rejections"]["rejections"] or
                     [{"admission_status": DS.ADMITTED}])}
        assert statuses == {expected}, f"{name}: got {statuses}"
        seen[name] = expected
    assert len(set(seen.values())) == 4       # four causes, four distinct states


# ═══════════════════════════════════════════════════════════════════════════
# §17  GOVERNED PROVIDER IDENTITY
# ═══════════════════════════════════════════════════════════════════════════
class _FakeClient:
    def __init__(self, body=None):
        self.body, self.calls = body, []

    def get_json(self, path, params=None, *, ttl_seconds=None, base_url=None):
        self.calls.append((path, params, ttl_seconds))
        return self.body


def test_governed_provider_takes_its_endpoint_from_the_registry():
    client = _FakeClient(body=[{"date": "2026-08-03 09:30:00"}])
    p = PR.GovernedFMPIntradayProvider(client)
    assert p.endpoint_for("5min") == "/stable/historical-chart/5min"
    p.fetch("SPY", "2026-08-03", "2026-08-03", "5min")
    assert client.calls[0][0] == "/stable/historical-chart/5min"
    assert client.calls[0][1] == {"symbol": "SPY", "from": "2026-08-03",
                                  "to": "2026-08-03"}


def test_governed_provider_refuses_an_unregistered_timeframe():
    """1min returned HTTP 402 on this account. Composing the path by string
    interpolation would happily produce an endpoint we are not entitled to."""
    p = PR.GovernedFMPIntradayProvider(_FakeClient())
    with pytest.raises(PR.UnsupportedTimeframe):
        p.endpoint_for("1min")


def test_governed_provider_will_not_wrap_an_ungoverned_client():
    with pytest.raises(PR.ProviderError):
        PR.GovernedFMPIntradayProvider(object())


def test_budget_refusal_is_distinct_from_absent_market_data(tmp_path):
    """OUR refusal to call must never be recorded as the market having no data."""
    p = PR.GovernedFMPIntradayProvider(_FakeClient(body=None))
    with pytest.raises(PR.ProviderBudgetRefusal):
        p.fetch("SPY", "2026-08-03", "2026-08-03", "5min")

    out = PL.build_historical_research_dataset(
        DS.DatasetRequest(symbols=("SPY",), start=NORMAL, end=NORMAL), p,
        root=str(tmp_path))
    acq = out["acquisitions"][0]
    assert acq["error_code"] == "PROVIDER_BUDGET_REFUSED"
    assert acq["provider_status"] == "PROVIDER_ERROR"
    assert acq["provider_status"] != "NO_DATA"


def test_raw_identity_records_the_real_provider_not_an_assumed_one(tmp_path):
    """The original defect: evidence stamped provider='fmp' around an arbitrary
    callable. Provider is part of raw identity, so a wrong claim mis-addresses
    the immutable object."""
    session = C.resolve_session(NORMAL)
    out = PL.build_historical_research_dataset(
        DS.DatasetRequest(symbols=("SPY",), start=NORMAL, end=NORMAL),
        _provider({"SPY": _rows(session)}, provider_id="not-fmp",
                  endpoint="/elsewhere"),
        root=str(tmp_path))
    man = ST.read_snapshot(ST.RAW, out["raw_content_fingerprints"][0],
                           "content_manifest.json", root=str(tmp_path))
    assert man["provider"] == "not-fmp"
    assert man["endpoint"] == "/elsewhere"
    assert out["provider_provenance"]["provider_id"] == "not-fmp"


def test_a_bare_callable_is_never_assumed_to_be_fmp(tmp_path):
    session = C.resolve_session(NORMAL)
    rows = _rows(session)
    out = PL.build_historical_research_dataset(
        DS.DatasetRequest(symbols=("SPY",), start=NORMAL, end=NORMAL),
        lambda s, a, b: (rows, None), root=str(tmp_path))
    man = ST.read_snapshot(ST.RAW, out["raw_content_fingerprints"][0],
                           "content_manifest.json", root=str(tmp_path))
    assert man["provider"] == "callable:unspecified"
    assert man["provider"] != "fmp"


def test_different_source_semantics_never_share_a_raw_identity():
    """Same observations, different source => different raw identity, so the
    stored content_manifest can never contradict the id it lives under."""
    rows = [{"date": "2026-08-03 09:30:00", "open": 1, "high": 2, "low": 0.5,
             "close": 1.5, "volume": 10}]
    a = ST.raw_payload_hash(rows, symbol="SPY", timeframe="5min",
                            provider="fmp", endpoint="/stable/historical-chart/5min")
    b = ST.raw_payload_hash(rows, symbol="SPY", timeframe="5min",
                            provider="other", endpoint="/stable/historical-chart/5min")
    c = ST.raw_payload_hash(rows, symbol="SPY", timeframe="5min",
                            provider="fmp", endpoint="/v3/other")
    assert len({a, b, c}) == 3


# ═══════════════════════════════════════════════════════════════════════════
# GOVERNANCE
# ═══════════════════════════════════════════════════════════════════════════
def test_strategy_validation_stays_false_through_migration(tmp_path):
    _, legacy_mfp, _, _ = _legacy_graph(tmp_path)
    out = MG.migrate_dataset_manifest(legacy_mfp, root=str(tmp_path))
    assert out["status"] == MG.MIGRATED
    fresh = _built(tmp_path / "fresh")
    assert fresh["strategy_validation_allowed"] is False
    assert MG.migrate_corpus(root=str(tmp_path))["observe_only"] is True
    assert MG.active_corpus(root=str(tmp_path))["observe_only"] is True


def test_migration_writes_only_into_the_historical_namespace(tmp_path):
    _, legacy_mfp, _, _ = _legacy_graph(tmp_path)
    MG.migrate_dataset_manifest(legacy_mfp, root=str(tmp_path))
    written = {p.relative_to(tmp_path).parts[0] for p in tmp_path.rglob("*") if p.is_file()}
    assert written == {"outputs"}
    assert all(p.match("outputs/backtest/intraday/*") or
               "outputs/backtest/intraday" in str(p)
               for p in tmp_path.rglob("*.json"))


def test_already_migrated_is_reported_from_lineage_after_a_calendar_change(tmp_path,
                                                                          monkeypatch):
    """Migration is only reproducible while its calendar is. Once the calendar
    advances, an ALREADY-migrated manifest must report that from lineage rather
    than leaving a permanent, misleading refusal in the report."""
    _, legacy_mfp, _, _ = _legacy_graph(tmp_path)
    first = MG.migrate_dataset_manifest(legacy_mfp, root=str(tmp_path))
    assert first["status"] == MG.MIGRATED

    base = C._schedule_digest()
    monkeypatch.setattr(C, "_schedule_digest",
                        lambda: {**base, "schedule_digest": "upgraded-schedule"})
    again = MG.migrate_dataset_manifest(legacy_mfp, root=str(tmp_path))
    assert again["status"] == MG.ALREADY_MIGRATED
    assert again["current_manifest_fingerprint"] == first["current_manifest_fingerprint"]
    assert again["lineage_id"] == first["lineage_id"]


def test_lineage_is_a_claim_that_is_re_verified_not_trusted(tmp_path, monkeypatch):
    """A lineage record naming a corrupted target must not shortcut to success."""
    out, legacy_mfp, _, _ = _legacy_graph(tmp_path)
    MG.migrate_dataset_manifest(legacy_mfp, root=str(tmp_path))
    _tamper(ST.intraday_root(str(tmp_path)) / "datasets" / "content"
            / out["dataset_fingerprint"] / "canonical_bars.json",
            lambda d: d.__setitem__(0, {**d[0], "close": 1234.5}))
    base = C._schedule_digest()
    monkeypatch.setattr(C, "_schedule_digest",
                        lambda: {**base, "schedule_digest": "upgraded-schedule"})
    res = MG.migrate_dataset_manifest(legacy_mfp, root=str(tmp_path))
    assert res["status"] != MG.ALREADY_MIGRATED
