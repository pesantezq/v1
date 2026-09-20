"""Immutable-success + latest-attempt model: a failed sync never erases or
empties the last admitted broker truth."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from portfolio_automation.brokers import broker_evidence as BE
from portfolio_automation.brokers import broker_evidence_store as ES
from portfolio_automation.brokers import broker_models as bm
from portfolio_automation.brokers import schwab_evidence_adapter as AD

T0 = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)
FIX = Path("tests/fixtures/schwab")


def _admitted_pair(retrieved_at=T0, sync_id="sync-1"):
    raw = json.loads((FIX / "accounts_positions.json").read_text())
    nums = json.loads((FIX / "account_numbers.json").read_text())
    snap = bm.normalize_accounts(raw, nums, now_iso=retrieved_at.isoformat())
    bps = BE.BrokerPortfolioSnapshot.from_normalized(snap, sync_id=sync_id, retrieved_at=retrieved_at)
    evs = AD.to_evidence_snapshot(bps)
    return evs, AD.admit_broker_snapshot(evs, as_of=retrieved_at + timedelta(seconds=1))


def test_admitted_success_is_current_and_archived_write_once(tmp_path):
    evs, dec = _admitted_pair()
    ES.record_admitted(tmp_path, snapshot=evs, decision=dec, sync_id="sync-1", now=T0)
    ES.record_attempt(tmp_path, sync_id="sync-1", outcome=ES.SyncOutcome.ADMITTED,
                      auth_state="OK", admission=dec, snapshot_id=evs.snapshot_id, now=T0)
    st = ES.read_state(tmp_path, now=T0 + timedelta(minutes=5))
    assert st.truth_status is ES.TruthStatus.CURRENT and not st.reauth_required
    assert st.positions() and st.admitted_age_s == 300.0 and st.integrity_error is None
    arch = tmp_path / "outputs/archive/broker_evidence/2026-09-20" / f"{evs.snapshot_id}.json"
    assert arch.exists()
    before = arch.read_bytes()
    ES.record_admitted(tmp_path, snapshot=evs, decision=dec, sync_id="sync-2", now=T0 + timedelta(hours=1))
    assert arch.read_bytes() == before                    # write-once: never rewritten


def test_failed_attempt_preserves_last_known_good_and_marks_it_stale(tmp_path):
    evs, dec = _admitted_pair()
    ES.record_admitted(tmp_path, snapshot=evs, decision=dec, sync_id="sync-1", now=T0)
    ES.record_attempt(tmp_path, sync_id="sync-1", outcome=ES.SyncOutcome.ADMITTED,
                      admission=dec, snapshot_id=evs.snapshot_id, now=T0)
    admitted_before = (tmp_path / "outputs/latest" / ES.LATEST_ADMITTED_FILE).read_bytes()
    rec = ES.record_attempt(tmp_path, sync_id="sync-2", outcome=ES.SyncOutcome.ACQUISITION_FAILED,
                            auth_state="OK", error="GET /accounts -> 503 access_token=LEAK", now=T0 + timedelta(days=1))
    assert (tmp_path / "outputs/latest" / ES.LATEST_ADMITTED_FILE).read_bytes() == admitted_before
    assert "LEAK" not in json.dumps(rec) and rec["implies_empty_portfolio"] is False
    st = ES.read_state(tmp_path, now=T0 + timedelta(days=1))
    assert st.truth_status is ES.TruthStatus.STALE_LAST_KNOWN_GOOD
    assert st.positions() is not None and len(st.positions()) > 0     # LKG intact, not emptied
    assert st.latest_attempt_outcome == "ACQUISITION_FAILED"


def test_failed_sync_with_no_history_is_unknown_not_empty(tmp_path):
    ES.record_attempt(tmp_path, sync_id="s", outcome=ES.SyncOutcome.ACQUISITION_FAILED, error="boom")
    st = ES.read_state(tmp_path)
    assert st.truth_status is ES.TruthStatus.NO_TRUTH
    assert st.positions() is None                          # unknown, NOT []
    assert st.to_dict()["admitted_snapshot_id"] is None


def test_reauth_required_is_distinguished_from_failed_acquisition(tmp_path):
    evs, dec = _admitted_pair()
    ES.record_admitted(tmp_path, snapshot=evs, decision=dec, sync_id="s1", now=T0)
    ES.record_attempt(tmp_path, sync_id="s2", outcome=ES.SyncOutcome.REAUTH_REQUIRED, auth_state="REAUTH_REQUIRED")
    st = ES.read_state(tmp_path)
    assert st.reauth_required is True
    assert st.truth_status is ES.TruthStatus.STALE_LAST_KNOWN_GOOD and st.positions()


def test_refused_evidence_is_never_recorded_as_truth(tmp_path):
    evs = _admitted_pair(retrieved_at=T0 + timedelta(hours=1))[0]
    refused = AD.admit_broker_snapshot(evs, as_of=T0)
    with pytest.raises(ValueError):
        ES.record_admitted(tmp_path, snapshot=evs, decision=refused, sync_id="s")
    ES.record_attempt(tmp_path, sync_id="s", outcome=ES.SyncOutcome.EVIDENCE_REFUSED, admission=refused)
    st = ES.read_state(tmp_path)
    assert st.truth_status is ES.TruthStatus.NO_TRUTH and st.positions() is None
    assert st.latest_attempt["admission"]["reason"] == "PIT_REFUSED"
    assert st.latest_attempt["snapshot_id"] is None


def test_tampered_admitted_record_is_reported_not_trusted(tmp_path):
    evs, dec = _admitted_pair()
    ES.record_admitted(tmp_path, snapshot=evs, decision=dec, sync_id="s", now=T0)
    p = tmp_path / "outputs/latest" / ES.LATEST_ADMITTED_FILE
    data = json.loads(p.read_text())
    data["evidence"]["payload"]["positions"][0]["quantity"] = 1e9
    p.write_text(json.dumps(data))
    st = ES.read_state(tmp_path)
    assert st.truth_status is ES.TruthStatus.NO_TRUTH and st.positions() is None
    assert st.integrity_error and "mismatch" in st.integrity_error


def test_admitted_record_with_mismatched_decision_is_rejected_on_read(tmp_path):
    evs, dec = _admitted_pair()
    ES.record_admitted(tmp_path, snapshot=evs, decision=dec, sync_id="s", now=T0)
    p = tmp_path / "outputs/latest" / ES.LATEST_ADMITTED_FILE
    data = json.loads(p.read_text())
    data["admission"]["admitted"] = False
    p.write_text(json.dumps(data))
    assert ES.read_state(tmp_path).truth_status is ES.TruthStatus.NO_TRUTH


def test_records_carry_no_secrets_and_use_governed_namespace(tmp_path):
    evs, dec = _admitted_pair()
    ES.record_admitted(tmp_path, snapshot=evs, decision=dec, sync_id="s", now=T0)
    ES.record_attempt(tmp_path, sync_id="s", outcome=ES.SyncOutcome.ADMITTED, admission=dec, snapshot_id=evs.snapshot_id)
    latest = tmp_path / "outputs" / "latest"
    blob = "".join(p.read_text() for p in latest.glob("broker_evidence_*.json")).lower()
    for leak in ("access_token", "refresh_token", "client_secret", "bearer ", "123456789"):
        assert leak not in blob
    assert (latest / ES.LATEST_ATTEMPT_FILE).exists() and (latest / ES.LATEST_ADMITTED_FILE).exists()
    for p in latest.glob("broker_evidence_*.json"):
        assert json.loads(p.read_text())["observe_only"] is True
