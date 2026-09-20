"""BrokerPortfolioSnapshot contract + Schwab evidence adapter + gateway admission."""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from portfolio_automation.brokers import broker_evidence as BE
from portfolio_automation.brokers import broker_models as bm
from portfolio_automation.brokers import schwab_evidence_adapter as AD
from portfolio_automation.evidence_gateway.admission import AdmissionReason
from portfolio_automation.evidence_gateway.admissibility import AdmissibilityReason
from portfolio_automation.northstar.evidence import EvidenceSnapshot

T0 = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)
FIX = Path("tests/fixtures/schwab")


def _normalized(now_iso: str = "2026-09-20T12:00:00+00:00") -> bm.BrokerSnapshot:
    raw = json.loads((FIX / "accounts_positions.json").read_text())
    nums = json.loads((FIX / "account_numbers.json").read_text())
    return bm.normalize_accounts(raw, nums, now_iso=now_iso)


def _bps(sync_id="sync-1", retrieved_at=T0, **kw) -> BE.BrokerPortfolioSnapshot:
    return BE.BrokerPortfolioSnapshot.from_normalized(
        _normalized(), sync_id=sync_id, retrieved_at=retrieved_at, source_commit="abc123", **kw)


# ------------------------------------------------------------- contract ----

def test_contract_is_versioned_and_kernel_identified():
    b = _bps()
    d = b.to_dict()
    assert d["contract_type"] == "broker_portfolio_snapshot" and d["schema_version"] == "1.0.0"
    assert d["source_id"].startswith("src_") and len(d["source_id"]) == 36
    assert d["source_label"] == "schwab_trader_api"
    assert d["broker_snapshot_id"].startswith("bps_")
    assert len(d["payload_hash"]) == 64
    assert d["sync_id"] == "sync-1" and d["retrieved_at"].endswith("Z")
    assert d["source_commit"] == "abc123" and d["normalizer_version"] == "1"
    assert d["endpoints"] == list(BE.ENDPOINTS)


def test_fixture_normalizes_into_masked_positions_and_balances():
    b = _bps()
    assert b.positions and all(p["symbol"] for p in b.positions)
    for a in b.accounts:
        assert a["account_id_masked"].startswith("…") and len(a["account_id_masked"]) <= 5
    assert b.totals["market_value"] > 0


def test_canonical_identity_ignores_run_specific_fields():
    a = _bps(sync_id="sync-A", retrieved_at=T0)
    b = _bps(sync_id="sync-B", retrieved_at=T0 + timedelta(hours=3))
    assert a.payload() == b.payload()
    assert a.payload_hash == b.payload_hash
    assert a.broker_snapshot_id == b.broker_snapshot_id      # same portfolio -> same identity
    assert a.to_dict()["sync_id"] != b.to_dict()["sync_id"]  # run identity still carried


def test_different_portfolio_facts_change_identity():
    snap = _normalized()
    snap.accounts[0].positions[0].quantity += 1
    changed = BE.BrokerPortfolioSnapshot.from_normalized(snap, sync_id="s", retrieved_at=T0)
    assert changed.payload_hash != _bps().payload_hash
    assert changed.broker_snapshot_id != _bps().broker_snapshot_id


def test_contract_has_no_secret_or_raw_account_fields():
    d = _bps().to_dict()
    blob = json.dumps(d).lower()
    for leak in ("access_token", "refresh_token", "client_secret", "authorization",
                 "password", "username"):
        assert leak not in blob, leak
    # every PLAIN account number the fixture knows must be absent (masked only)
    for entry in json.loads((FIX / "account_numbers.json").read_text()):
        assert str(entry["accountNumber"]) not in json.dumps(d), "raw account number leaked"
    assert '"accountnumber"' not in blob     # the raw-number KEY never appears either
    names = {f.name for f in dataclasses.fields(BE.BrokerPortfolioSnapshot)}
    assert not names & {"access_token", "refresh_token", "client_secret", "authorization_code"}


@pytest.mark.parametrize("bad", [
    {"account_id_masked": "123456789", "cash": 1.0},                  # raw account number
    {"account_id_masked": "acct-1234", "cash": 1.0},                  # not the mask form
])
def test_unmasked_account_reference_is_refused(bad):
    with pytest.raises(BE.BrokerEvidenceError):
        BE.BrokerPortfolioSnapshot(sync_id="s", retrieved_at=T0, accounts=(bad,),
                                   positions=(), totals={"market_value": 0.0, "cash": 1.0})


def test_secret_material_in_any_field_is_refused():
    with pytest.raises(BE.BrokerEvidenceError):
        BE.BrokerPortfolioSnapshot(
            sync_id="s", retrieved_at=T0, accounts=(),
            positions=({"symbol": "QQQ", "quantity": 1.0, "asset_type": "Bearer abc.def.ghi"},),
            totals={"market_value": 0.0, "cash": 0.0})
    with pytest.raises(BE.BrokerEvidenceError):
        BE.BrokerPortfolioSnapshot(
            sync_id="s", retrieved_at=T0, accounts=(),
            positions=({"symbol": "QQQ", "quantity": 1.0, "access_token": "x"},),
            totals={"market_value": 0.0, "cash": 0.0})


def test_naive_retrieved_at_is_refused():
    with pytest.raises(BE.BrokerEvidenceError):
        _bps(retrieved_at=datetime(2026, 9, 20, 12, 0, 0))


def test_non_finite_quantity_is_refused():
    with pytest.raises(BE.BrokerEvidenceError):
        BE.BrokerPortfolioSnapshot(
            sync_id="s", retrieved_at=T0, accounts=(),
            positions=({"symbol": "QQQ", "quantity": float("nan")},),
            totals={"market_value": 0.0, "cash": 0.0})


def test_descriptor_carries_no_auth_surface_and_honest_capabilities():
    d = BE.data_source_descriptor()
    assert d.provider == "schwab" and d.status == "active"
    assert d.pit_capability == "none" and d.historical_capability == "none"
    assert d.rights_class == "private_research"
    assert not {"url", "endpoint", "api_key", "token"} & {f.name for f in dataclasses.fields(d)}


# ------------------------------------------------------------- adapter -----

def test_adapter_builds_canonical_snapshot_with_possession_pit():
    evs = AD.to_evidence_snapshot(_bps())
    assert isinstance(evs, EvidenceSnapshot)
    assert evs.evidence_type == "broker.portfolio_snapshot" and evs.entity_type == "other"
    assert evs.pit.retrieved_at == T0 and evs.pit.known_at == T0
    assert evs.pit.known_at_basis == "derived_conservative"
    assert evs.pit.observed_at is None                      # not fabricated
    assert evs.provenance.producer_type == "source_adapter"
    assert evs.provenance.source_id == evs.source_id
    assert evs.provenance.code_version == "abc123"
    assert "sync=sync-1" in evs.provenance.transformation_id
    assert evs.payload_copy() == _bps().payload()


def test_adapter_is_deterministic_for_equivalent_inputs():
    a = AD.to_evidence_snapshot(_bps(sync_id="A", retrieved_at=T0))
    b = AD.to_evidence_snapshot(_bps(sync_id="B", retrieved_at=T0))
    assert a.payload_hash == b.payload_hash and a.snapshot_id == b.snapshot_id
    c = AD.to_evidence_snapshot(_bps(sync_id="C", retrieved_at=T0 + timedelta(minutes=1)))
    assert c.payload_hash == a.payload_hash            # same facts
    assert c.snapshot_id != a.snapshot_id              # different possession instant (PIT)


def test_adapter_output_carries_no_secrets():
    blob = AD.to_evidence_snapshot(_bps()).to_json().lower()
    for leak in ("access_token", "refresh_token", "client_secret", "bearer", "123456789"):
        assert leak not in blob


# ------------------------------------------------------------- admission ---

def test_valid_broker_snapshot_is_admitted():
    evs = AD.to_evidence_snapshot(_bps())
    d = AD.admit_broker_snapshot(evs, as_of=T0 + timedelta(seconds=1))
    assert d.admitted and d.reason is AdmissionReason.ADMITTED and d.snapshot_id == evs.snapshot_id


def test_future_dated_snapshot_is_refused_as_lookahead():
    evs = AD.to_evidence_snapshot(_bps(retrieved_at=T0 + timedelta(hours=1)))
    d = AD.admit_broker_snapshot(evs, as_of=T0)
    assert not d.admitted and d.reason is AdmissionReason.PIT_REFUSED
    assert d.pit_reason is AdmissibilityReason.KNOWN_AT_AFTER_AS_OF


def test_payload_tampering_is_refused_on_hash_mismatch():
    evs = AD.to_evidence_snapshot(_bps())
    tampered = evs.to_canonical_dict()
    tampered["payload"]["positions"][0]["quantity"] = 999999.0
    with pytest.raises(ValueError):                    # from_dict refuses non-reproducing identity
        EvidenceSnapshot.from_dict(tampered)
    # and a snapshot altered after construction (bypassing the constructor) is
    # caught by the gateway's genuine re-hash
    object.__setattr__(evs, "payload_canonical", evs.payload_canonical.replace('"read_only":true', '"read_only":false'))
    d = AD.admit_broker_snapshot(evs, as_of=T0 + timedelta(seconds=1))
    assert not d.admitted and d.reason is AdmissionReason.PAYLOAD_HASH_MISMATCH


def test_hash_mismatch_in_serialized_form_is_refused():
    evs = AD.to_evidence_snapshot(_bps())
    data = evs.to_canonical_dict()
    data["payload_hash"] = "0" * 64
    with pytest.raises(ValueError):
        EvidenceSnapshot.from_dict(data)


def test_provenance_source_mismatch_is_refused():
    evs = AD.to_evidence_snapshot(_bps())
    other = "src_" + "f" * 32
    object.__setattr__(evs.provenance, "source_id", other)
    d = AD.admit_broker_snapshot(evs, as_of=T0 + timedelta(seconds=1))
    assert not d.admitted and d.reason is AdmissionReason.PROVENANCE_SOURCE_MISMATCH


def test_malformed_evidence_is_refused_not_raised():
    d = AD.admit_broker_snapshot({"not": "evidence"}, as_of=T0)  # type: ignore[arg-type]
    assert not d.admitted and d.reason is AdmissionReason.NOT_AN_EVIDENCE_SNAPSHOT


def test_wrong_reference_is_refused():
    evs = AD.to_evidence_snapshot(_bps())
    other = AD.to_evidence_snapshot(_bps(retrieved_at=T0 - timedelta(days=1))).ref()
    d = AD.admit_broker_snapshot(evs, as_of=T0 + timedelta(seconds=1), ref=other)
    assert not d.admitted and d.reason is AdmissionReason.REF_DOES_NOT_MATCH_SNAPSHOT


# ------------------------------------------------------------- projections -

def test_projections_match_legacy_shapes_and_name_their_evidence():
    evs = AD.to_evidence_snapshot(_bps())
    d = AD.admit_broker_snapshot(evs, as_of=T0 + timedelta(seconds=1))
    snap = AD.project_snapshot_dict(evs, d)
    pos = AD.project_positions_dict(evs, d)
    legacy_snap = bm.snapshot_dict(_normalized("2026-09-20T12:00:00.000000Z"))
    legacy_pos = bm.positions_dict(_normalized("2026-09-20T12:00:00.000000Z"))
    assert set(legacy_snap) <= set(snap) and set(legacy_pos) <= set(pos)
    assert snap["totals"] == legacy_snap["totals"]
    assert {a["account_id_masked"] for a in snap["accounts"]} == {a["account_id_masked"] for a in legacy_snap["accounts"]}
    assert sorted(p["symbol"] for p in pos["positions"]) == sorted(p["symbol"] for p in legacy_pos["positions"])
    assert snap["observe_only"] is True and pos["observe_only"] is True and pos["source"] == "schwab"
    assert snap["evidence_snapshot_id"] == evs.snapshot_id == pos["evidence_snapshot_id"]


def test_refused_evidence_cannot_be_projected_into_truth():
    evs = AD.to_evidence_snapshot(_bps(retrieved_at=T0 + timedelta(hours=1)))
    refused = AD.admit_broker_snapshot(evs, as_of=T0)
    with pytest.raises(AD.EvidenceNotAdmitted):
        AD.project_snapshot_dict(evs, refused)
    with pytest.raises(AD.EvidenceNotAdmitted):
        AD.project_positions_dict(evs, refused)


def test_projection_refuses_a_decision_for_a_different_snapshot():
    a = AD.to_evidence_snapshot(_bps(retrieved_at=T0))
    b = AD.to_evidence_snapshot(_bps(retrieved_at=T0 - timedelta(days=1)))
    d_b = AD.admit_broker_snapshot(b, as_of=T0)
    with pytest.raises(AD.EvidenceNotAdmitted):
        AD.project_positions_dict(a, d_b)
