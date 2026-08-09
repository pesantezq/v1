"""Session 2 graduation must survive losing every in-memory object.

THE DEFECT THIS FREEZES
=======================

Session 2 previously reported READY only while the caller still held the pilot
dictionary in memory. The artifact written to disk said
`DATASET_FEATURE_FOUNDATION_READY`, but a fresh process recomputed
`LIMITED` — the verdict did not survive a process exit, a session end, or a
reboot. A graduation gate that evaporates is worse than one that fails, because
the stale artifact keeps asserting the opposite.

So the contract frozen here is:

    the verdict is DERIVED from persisted immutable evidence, every time

which means it must also FALL when that evidence is tampered with. Both
directions are tested; a gate that only ever says READY proves nothing.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from datetime import date

import pytest

from portfolio_automation.intraday_lab import calendar as C
from portfolio_automation.intraday_lab import foundation as FD
from portfolio_automation.intraday_lab import pilot as PI
from portfolio_automation.intraday_lab import storage as ST

authoritative = pytest.mark.skipif(
    C._calendar() is None, reason="exchange_calendars not installed")

REPO = str(__import__("pathlib").Path(__file__).resolve().parent.parent)


def _rows(session):
    return [{"date": ts.astimezone(C.EXCHANGE_TZ).strftime("%Y-%m-%d %H:%M:%S"),
             "open": 100 + i * 0.01, "high": 100.5 + i * 0.01,
             "low": 99.5 + i * 0.01, "close": 100.2 + i * 0.01, "volume": 1000 + i}
            for i, ts in enumerate(session.expected_bar_starts)]


class _WindowProvider:
    provider_id = "fake-historical"

    def endpoint_for(self, timeframe):
        return "/fake/intraday/5min"

    def fetch(self, symbol, start, end, timeframe):
        rows = []
        for s in C.sessions_in_range(date.fromisoformat(start), date.fromisoformat(end)):
            if s.session_type in ("REGULAR", "EARLY_CLOSE"):
                rows.extend(_rows(s))
        return rows, None

    def provenance(self):
        return {"provider_id": self.provider_id, "governed": False}


def _freeze(tmp_path, windows=None):
    """Produce durable graduation evidence and point the gate at it."""
    # The FULL protocol window set: a smaller pilot is now correctly refused as
    # graduation evidence, which is the point of the protocol.
    out = PI.run_pilot(_WindowProvider(), root=str(tmp_path),
                       windows=windows or PI.PILOT_WINDOWS)
    C.persist_certified_schedule(root=str(tmp_path))
    fp = PI.persist_pilot(out, root=str(tmp_path))
    PI.set_graduation_evidence(fp, root=str(tmp_path))
    return out, fp


# ═══════════════════════════════════════════════════════════════════════════
# Durability
# ═══════════════════════════════════════════════════════════════════════════
@authoritative
def test_graduation_is_limited_before_evidence_is_frozen(tmp_path):
    """Running a pilot is not the same as COMMITTING it as evidence."""
    PI.run_pilot(_WindowProvider(), root=str(tmp_path), windows=PI.PILOT_WINDOWS[-1:])
    g = FD.session2_graduation(root=str(tmp_path))
    assert g["status"] == FD.DATASET_FEATURE_FOUNDATION_LIMITED
    assert "graduation_evidence_is_durable" in g["blockers"]
    assert FD.session3_input_contract(root=str(tmp_path))["session_3_gate"] == \
        FD.SESSION_3_NO_GO


@authoritative
def test_verdict_survives_discarding_every_in_memory_object(tmp_path):
    pilot, fp = _freeze(tmp_path)
    before = FD.session2_graduation(root=str(tmp_path))
    assert before["status"] == FD.DATASET_FEATURE_FOUNDATION_READY

    del pilot                                  # lose the in-memory evidence
    after = FD.session2_graduation(root=str(tmp_path))
    assert after["status"] == FD.DATASET_FEATURE_FOUNDATION_READY
    assert after["measured_passed"] == before["measured_passed"]
    assert FD.session3_input_contract(root=str(tmp_path))["session_3_gate"] == \
        FD.SESSION_3_GO


@authoritative
def test_verdict_is_recovered_in_a_genuinely_fresh_process(tmp_path):
    """A subprocess proves a real process boundary, not just a rebound name.

    Nothing in-memory crosses it — the child imports the modules from scratch
    and reads only what is on disk.
    """
    _freeze(tmp_path)
    script = textwrap.dedent("""
        import json, sys
        sys.path.insert(0, sys.argv[1])
        from portfolio_automation.intraday_lab import foundation as FD
        g = FD.session2_graduation(root=sys.argv[2])
        c = FD.session3_input_contract(root=sys.argv[2])
        print(json.dumps({"status": g["status"], "blockers": g["blockers"],
                          "measured": g["measured_passed"],
                          "gate": c["session_3_gate"],
                          "has_contract": c["contract"] is not None}))
    """)
    proc = subprocess.run([sys.executable, "-c", script, REPO, str(tmp_path)],
                          capture_output=True, text=True, timeout=300, cwd=REPO)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["status"] == FD.DATASET_FEATURE_FOUNDATION_READY, out
    assert out["blockers"] == []
    assert out["gate"] == FD.SESSION_3_GO
    assert out["has_contract"] is True


@authoritative
def test_evidence_is_located_by_explicit_pointer_not_by_mtime(tmp_path):
    """Selecting evidence by 'newest directory' would make the verdict depend on
    filesystem incidentals rather than an explicit, reviewable decision."""
    _, first = _freeze(tmp_path)
    # A second, NEWER, equally valid graduation pilot exists but is not pointed
    # at. Both satisfy the protocol, so recency is the only thing separating them.
    extra = PI.PILOT_WINDOWS + (
        PI.PilotWindow("extra-2019", date(2019, 5, 6), date(2019, 5, 8), "extra"),)
    second_out = PI.run_pilot(_WindowProvider(), root=str(tmp_path), windows=extra)
    second = PI.persist_pilot(second_out, root=str(tmp_path))
    assert second != first

    ev = PI.load_graduation_evidence(root=str(tmp_path))
    assert ev["pilot_fingerprint"] == first, "pointer must win over recency"

    PI.set_graduation_evidence(second, root=str(tmp_path))
    assert PI.load_graduation_evidence(root=str(tmp_path))["pilot_fingerprint"] == second


@authoritative
def test_pointer_refuses_to_name_unverifiable_evidence(tmp_path):
    _freeze(tmp_path)
    with pytest.raises(ValueError, match="unverifiable"):
        PI.set_graduation_evidence("0" * 32, root=str(tmp_path))


@authoritative
def test_missing_evidence_never_triggers_provider_calls(tmp_path):
    """A missing pointer is a governance fact to report, not a licence to spend
    provider budget re-manufacturing evidence."""
    ev = PI.load_graduation_evidence(root=str(tmp_path))
    assert ev["available"] is False and ev["pilot"] is None
    assert "pointer" in ev["reason"]
    g = FD.session2_graduation(root=str(tmp_path))
    assert g["status"] == FD.DATASET_FEATURE_FOUNDATION_LIMITED


# ═══════════════════════════════════════════════════════════════════════════
# Durable-evidence tampering — the gate must be BOUND to the bytes
# ═══════════════════════════════════════════════════════════════════════════
def _tamper(path, mutate):
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data, separators=(",", ":"), sort_keys=True))


@authoritative
def test_tampering_the_pilot_totals_is_caught_by_recomputation(tmp_path):
    """Stored totals are never trusted — they are recomputed from window rows."""
    _, fp = _freeze(tmp_path)
    path = ST.intraday_root(str(tmp_path)) / ST.PILOTS / fp / "pilot.json"
    _tamper(path, lambda d: d["totals"].__setitem__("sessions_admitted", 99999))
    v = PI.verify_historical_pilot(fp, root=str(tmp_path))
    assert v["verified"] is False
    assert "recompute" in v["reason"]
    assert FD.session2_graduation(root=str(tmp_path))["status"] == \
        FD.DATASET_FEATURE_FOUNDATION_LIMITED
    assert FD.session3_input_contract(root=str(tmp_path))["session_3_gate"] == \
        FD.SESSION_3_NO_GO


@authoritative
def test_tampering_a_pilot_window_breaks_its_identity(tmp_path):
    _, fp = _freeze(tmp_path)
    path = ST.intraday_root(str(tmp_path)) / ST.PILOTS / fp / "pilot.json"
    _tamper(path, lambda d: d["windows"][0].__setitem__("manifest_fingerprint", "x" * 32))
    v = PI.verify_historical_pilot(fp, root=str(tmp_path))
    assert v["verified"] is False
    assert FD.session2_graduation(root=str(tmp_path))["status"] == \
        FD.DATASET_FEATURE_FOUNDATION_LIMITED


@authoritative
def test_tampering_a_referenced_dataset_breaks_graduation(tmp_path):
    """The pilot object itself is untouched; a graph it NAMES is corrupted."""
    pilot, fp = _freeze(tmp_path)
    ds_fp = pilot["windows"][0]["dataset_fingerprint"]
    path = (ST.intraday_root(str(tmp_path)) / "datasets" / "content" / ds_fp
            / "canonical_bars.json")
    _tamper(path, lambda d: d.__setitem__(0, {**d[0], "close": 4242.0}))
    assert PI.verify_historical_pilot(fp, root=str(tmp_path))["verified"] is False
    assert FD.session2_graduation(root=str(tmp_path))["status"] == \
        FD.DATASET_FEATURE_FOUNDATION_LIMITED
    assert FD.session3_input_contract(root=str(tmp_path))["session_3_gate"] == \
        FD.SESSION_3_NO_GO


@authoritative
def test_tampering_the_archived_schedule_breaks_graduation(tmp_path):
    _freeze(tmp_path)
    digest = C.calendar_identity()["schedule_digest"]
    path = ST.intraday_root(str(tmp_path)) / ST.CALENDARS / digest / "schedule.json"
    _tamper(path, lambda d: d.__setitem__(0, {**d[0], "close_et": "13:00"}))
    assert C.verify_certified_schedule(digest, root=str(tmp_path))["verified"] is False
    g = FD.session2_graduation(root=str(tmp_path))
    assert g["status"] == FD.DATASET_FEATURE_FOUNDATION_LIMITED
    assert "certified_schedule_is_archived" in g["blockers"]


@authoritative
def test_tampering_a_dataset_manifest_breaks_identity_recomputation(tmp_path):
    pilot, fp = _freeze(tmp_path)
    mfp = pilot["windows"][0]["manifest_fingerprint"]
    path = (ST.intraday_root(str(tmp_path)) / "datasets" / "manifests" / mfp
            / "reconciliation.json")
    recs = json.loads(path.read_text())
    # Flip to a DIFFERENT valid accounting state — the manifest fingerprint is
    # computed over the session status matrix, so relabelling one outcome
    # changes what the dataset means without touching a single bar.
    flipped = ("REJECTED_MISSING_BARS" if recs[0]["admission_status"] == "ADMITTED"
               else "ADMITTED")
    recs[0] = {**recs[0], "admission_status": flipped}
    path.write_text(json.dumps(recs, separators=(",", ":"), sort_keys=True))
    v = ST.verify_dataset_provenance(mfp, root=str(tmp_path))
    assert v["verified"] is False
    assert FD.session2_graduation(root=str(tmp_path))["status"] == \
        FD.DATASET_FEATURE_FOUNDATION_LIMITED


# ═══════════════════════════════════════════════════════════════════════════
# Calendar fallback must not satisfy the completed Session 2 contract
# ═══════════════════════════════════════════════════════════════════════════
@authoritative
def test_calendar_fallback_cannot_graduate(tmp_path, monkeypatch):
    """The repo-native fallback is useful degradation, never authoritative."""
    _freeze(tmp_path)
    assert FD.session2_graduation(root=str(tmp_path))["status"] == \
        FD.DATASET_FEATURE_FOUNDATION_READY

    monkeypatch.setattr(C, "_calendar", lambda: None)
    C._DIGEST_CACHE.clear()
    try:
        assert C.calendar_provenance()["authoritative"] is False
        g = FD.session2_graduation(root=str(tmp_path))
        assert g["status"] == FD.DATASET_FEATURE_FOUNDATION_LIMITED
        assert "calendar_is_authoritative" in g["blockers"]
        assert FD.session3_input_contract(root=str(tmp_path))["session_3_gate"] == \
            FD.SESSION_3_NO_GO
    finally:
        C._DIGEST_CACHE.clear()


# ═══════════════════════════════════════════════════════════════════════════
# Archived schedule
# ═══════════════════════════════════════════════════════════════════════════
@authoritative
def test_archived_schedule_is_addressed_by_the_digest_manifests_reference(tmp_path):
    """No new identity is introduced: dataset manifests already point here."""
    digest = C.persist_certified_schedule(root=str(tmp_path))
    assert digest == C.calendar_identity()["schedule_digest"]
    v = C.verify_certified_schedule(digest, root=str(tmp_path))
    assert v["verified"] is True
    assert v["session_count"] > 2000
    rows = ST.read_snapshot(ST.CALENDARS, digest, "schedule.json", root=str(tmp_path))
    # The archive must be reconstructible, not merely a hash.
    sample = {r["market_date"]: r for r in rows}
    assert sample["2025-11-28"]["close_et"] == "13:00"
    assert sample["2025-11-28"]["session_type"] == "EARLY_CLOSE"
    assert sample["2026-08-03"]["close_et"] == "16:00"
    assert "2018-12-05" not in sample            # unscheduled closure, no session


@authoritative
def test_session_type_in_the_archive_cannot_contradict_its_close_time(tmp_path):
    digest = C.persist_certified_schedule(root=str(tmp_path))
    path = ST.intraday_root(str(tmp_path)) / ST.CALENDARS / digest / "schedule.json"
    rows = json.loads(path.read_text())
    idx = next(i for i, r in enumerate(rows) if r["session_type"] == "REGULAR")
    rows[idx] = {**rows[idx], "session_type": "EARLY_CLOSE"}
    path.write_text(json.dumps(rows, separators=(",", ":"), sort_keys=True))
    v = C.verify_certified_schedule(digest, root=str(tmp_path))
    assert v["verified"] is False
    assert "session_type" in v["reason"]


# ═══════════════════════════════════════════════════════════════════════════
# Governance
# ═══════════════════════════════════════════════════════════════════════════
@authoritative
def test_durable_graduation_never_enables_strategy_validation(tmp_path):
    _freeze(tmp_path)
    g = FD.session2_graduation(root=str(tmp_path))
    c = FD.session3_input_contract(root=str(tmp_path))
    assert g["status"] == FD.DATASET_FEATURE_FOUNDATION_READY
    assert c["session_3_gate"] == FD.SESSION_3_GO
    assert g["strategy_validation_allowed"] is False
    assert c["strategy_validation_allowed"] is False
    assert g["measured_checks"]["strategy_validation_never_enabled_in_source"]["pass"]


def test_the_source_probe_actually_detects_an_enabling_assignment(tmp_path, monkeypatch):
    """Proves the probe is not vacuous: it must FAIL on a module that enables it.

    The predecessor was `x is not None and True`, which could not fail and so
    measured nothing while inflating the passing count.
    """
    lab = __import__("pathlib").Path(FD.__file__).parent
    planted = lab / "_zz_probe_fixture.py"
    planted.write_text('strategy_validation_allowed = True\n', encoding="utf-8")
    try:
        g = FD.session2_graduation(root=str(tmp_path))
        assert g["measured_checks"][
            "strategy_validation_never_enabled_in_source"]["pass"] is False
    finally:
        planted.unlink()
    g2 = FD.session2_graduation(root=str(tmp_path))
    assert g2["measured_checks"][
        "strategy_validation_never_enabled_in_source"]["pass"] is True


# ═══════════════════════════════════════════════════════════════════════════
# FEATURE EVIDENCE — "DATASET_FEATURE_FOUNDATION_READY" must mean both halves
#
# The gate previously verified datasets and ignored features, so a pilot's
# feature snapshot could be DELETED outright and graduation stayed READY while
# still calling itself DATASET_FEATURE_FOUNDATION_READY.
# ═══════════════════════════════════════════════════════════════════════════
def _feature_dir(tmp_path, pilot, i=0):
    return (ST.intraday_root(str(tmp_path)) / "features" / "content"
            / pilot["windows"][i]["feature_fingerprint"])


@authoritative
def test_deleting_a_pilot_feature_snapshot_breaks_graduation(tmp_path):
    pilot, fp = _freeze(tmp_path)
    assert FD.session2_graduation(root=str(tmp_path))["status"] == \
        FD.DATASET_FEATURE_FOUNDATION_READY
    import shutil
    shutil.rmtree(_feature_dir(tmp_path, pilot))

    v = PI.verify_historical_pilot(fp, root=str(tmp_path))
    assert v["verified"] is False and "feature" in v["reason"]
    assert FD.session2_graduation(root=str(tmp_path))["status"] == \
        FD.DATASET_FEATURE_FOUNDATION_LIMITED
    assert FD.session3_input_contract(root=str(tmp_path))["session_3_gate"] == \
        FD.SESSION_3_NO_GO


@authoritative
def test_tampering_feature_content_breaks_graduation(tmp_path):
    pilot, fp = _freeze(tmp_path)
    _tamper(_feature_dir(tmp_path, pilot) / "features.json",
            lambda d: d.__setitem__(0, {**d[0], "value": 42.0}))
    assert PI.verify_historical_pilot(fp, root=str(tmp_path))["verified"] is False
    assert FD.session2_graduation(root=str(tmp_path))["status"] == \
        FD.DATASET_FEATURE_FOUNDATION_LIMITED


@authoritative
@pytest.mark.parametrize("field", ["source_dataset_fingerprint",
                                   "source_dataset_manifest_fingerprint"])
def test_feature_bound_to_the_wrong_source_breaks_graduation(tmp_path, field):
    """Feature identity binds to its source; a feature pointing elsewhere would
    let an experiment attribute results to data that did not produce them."""
    pilot, fp = _freeze(tmp_path)
    _tamper(_feature_dir(tmp_path, pilot) / "feature_content_manifest.json",
            lambda d: d.__setitem__(field, "z" * 32))
    v = PI.verify_historical_pilot(fp, root=str(tmp_path))
    assert v["verified"] is False
    assert FD.session2_graduation(root=str(tmp_path))["status"] == \
        FD.DATASET_FEATURE_FOUNDATION_LIMITED


@authoritative
def test_feature_observation_count_is_verified_not_trusted(tmp_path):
    """The pilot's stored count is a claim; the verified snapshot is evidence."""
    pilot, fp = _freeze(tmp_path)
    path = ST.intraday_root(str(tmp_path)) / ST.PILOTS / fp / "pilot.json"
    _tamper(path, lambda d: d["windows"][0].__setitem__("feature_observations", 1))
    v = PI.verify_historical_pilot(fp, root=str(tmp_path))
    assert v["verified"] is False


@authoritative
def test_verified_pilot_reports_per_window_feature_evidence(tmp_path):
    _, fp = _freeze(tmp_path)
    v = PI.verify_historical_pilot(fp, root=str(tmp_path))
    assert v["all_windows_features_verified"] is True
    for w in v["window_verification"]:
        assert w["dataset_provenance_verified"] and w["dataset_current_era"]
        assert w["feature_verified"]
        assert w["feature_dataset_binding_verified"]
        assert w["feature_manifest_binding_verified"]
        assert w["feature_observation_count_verified"]
        assert w["acquisition_events"] > 0


# ═══════════════════════════════════════════════════════════════════════════
# GRADUATION PROTOCOL — a valid pilot is not automatically graduation evidence
# ═══════════════════════════════════════════════════════════════════════════
@authoritative
def test_an_underpowered_pilot_is_valid_but_cannot_graduate(tmp_path):
    """One normal 2026 week is a sound research object and a terrible standard.
    Integrity and sufficiency are reported separately so neither lies."""
    small = PI.run_pilot(_WindowProvider(), root=str(tmp_path),
                         windows=PI.PILOT_WINDOWS[-1:])
    C.persist_certified_schedule(root=str(tmp_path))
    fp = PI.persist_pilot(small, root=str(tmp_path))

    v = PI.verify_historical_pilot(fp, root=str(tmp_path))
    assert v["pilot_integrity_valid"] is True          # not corrupt
    assert v["graduation_protocol_satisfied"] is False  # not sufficient
    assert any("missing required window" in f
               for f in v["graduation_protocol"]["failures"])

    with pytest.raises(ValueError, match="does NOT satisfy"):
        PI.set_graduation_evidence(fp, root=str(tmp_path))
    assert FD.session2_graduation(root=str(tmp_path))["status"] == \
        FD.DATASET_FEATURE_FOUNDATION_LIMITED


@authoritative
def test_wrong_symbols_cannot_graduate(tmp_path):
    out = PI.run_pilot(_WindowProvider(), root=str(tmp_path), symbols=("SPY",))
    C.persist_certified_schedule(root=str(tmp_path))
    fp = PI.persist_pilot(out, root=str(tmp_path))
    v = PI.verify_historical_pilot(fp, root=str(tmp_path))
    assert v["graduation_protocol_satisfied"] is False
    assert any("AAPL" in f for f in v["graduation_protocol"]["failures"])
    with pytest.raises(ValueError):
        PI.set_graduation_evidence(fp, root=str(tmp_path))


@authoritative
def test_a_shifted_required_window_cannot_graduate(tmp_path):
    """Right label, wrong dates — the regime the window exists to exercise."""
    from dataclasses import replace
    shifted = tuple(
        replace(w, start=date(2026, 8, 10), end=date(2026, 8, 14))
        if w.label == "2026-normal" else w
        for w in PI.PILOT_WINDOWS)
    out = PI.run_pilot(_WindowProvider(), root=str(tmp_path), windows=shifted)
    C.persist_certified_schedule(root=str(tmp_path))
    fp = PI.persist_pilot(out, root=str(tmp_path))
    v = PI.verify_historical_pilot(fp, root=str(tmp_path))
    assert v["graduation_protocol_satisfied"] is False
    assert any("2026-normal" in f for f in v["graduation_protocol"]["failures"])


@authoritative
def test_extra_windows_are_allowed_required_subset_of_observed(tmp_path):
    """Documented policy: more adversarial evidence must never be a regression."""
    extra = PI.PILOT_WINDOWS + (
        PI.PilotWindow("extra-2019", date(2019, 5, 6), date(2019, 5, 8), "extra"),)
    out = PI.run_pilot(_WindowProvider(), root=str(tmp_path), windows=extra)
    C.persist_certified_schedule(root=str(tmp_path))
    fp = PI.persist_pilot(out, root=str(tmp_path))
    v = PI.verify_historical_pilot(fp, root=str(tmp_path))
    assert v["graduation_protocol_satisfied"] is True
    assert v["graduation_protocol"]["extra_window_policy"] == \
        "required_subset_of_observed"
    PI.set_graduation_evidence(fp, root=str(tmp_path))       # must be accepted


def test_protocol_is_part_of_pilot_identity():
    """Same results under a different protocol must not share an identity, or a
    generic pilot could be relabelled as graduation evidence after the fact."""
    base = {"symbols": ["AAPL", "SPY"], "windows": [], "calendar_identity": {},
            "graduation_protocol_id": PI.GRADUATION_PROTOCOL_ID,
            "strategy_validation_allowed": False}
    other = {**base, "graduation_protocol_id": "SOMETHING_ELSE"}
    assert PI.pilot_fingerprint(base) != PI.pilot_fingerprint(other)


# ═══════════════════════════════════════════════════════════════════════════
# STATUS CONSISTENCY
# ═══════════════════════════════════════════════════════════════════════════
@authoritative
def test_status_never_reports_ready_with_false_readiness_fields(tmp_path):
    """The exact contradiction that existed: READY beside feature_dataset_ready
    = False, because the fields read a caller argument that was absent."""
    _freeze(tmp_path)
    st = FD.session2_status(root=str(tmp_path))
    assert st["architecture_status"] == FD.DATASET_FEATURE_FOUNDATION_READY
    assert st["canonical_dataset_ready"] is True
    assert st["feature_dataset_ready"] is True
    assert st["graduation_evidence_ready"] is True
    assert st["graduation_protocol_satisfied"] is True


@authoritative
def test_status_and_graduation_agree_in_a_fresh_process(tmp_path):
    _freeze(tmp_path)
    script = textwrap.dedent("""
        import json, sys
        sys.path.insert(0, sys.argv[1])
        from portfolio_automation.intraday_lab import foundation as FD
        st = FD.session2_status(root=sys.argv[2])
        g = FD.session2_graduation(root=sys.argv[2])
        c = FD.session3_input_contract(root=sys.argv[2])
        print(json.dumps({"status": st["architecture_status"],
                          "canonical": st["canonical_dataset_ready"],
                          "features": st["feature_dataset_ready"],
                          "grad": g["status"], "gate": c["session_3_gate"]}))
    """)
    proc = subprocess.run([sys.executable, "-c", script, REPO, str(tmp_path)],
                          capture_output=True, text=True, timeout=300, cwd=REPO)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["status"] == out["grad"] == FD.DATASET_FEATURE_FOUNDATION_READY
    assert out["canonical"] is True and out["features"] is True
    assert out["gate"] == FD.SESSION_3_GO


@authoritative
def test_status_readiness_falls_with_the_evidence(tmp_path):
    pilot, fp = _freeze(tmp_path)
    import shutil
    shutil.rmtree(_feature_dir(tmp_path, pilot))
    st = FD.session2_status(root=str(tmp_path))
    assert st["architecture_status"] == FD.DATASET_FEATURE_FOUNDATION_LIMITED
    assert st["feature_dataset_ready"] is False
    assert st["canonical_dataset_ready"] is False


# ═══════════════════════════════════════════════════════════════════════════
# EVENT IDENTITY — the verifier claimed recomputation it never performed
# ═══════════════════════════════════════════════════════════════════════════
@authoritative
def test_acquisition_event_identity_actually_recomputes(tmp_path):
    _freeze(tmp_path)
    base = ST.intraday_root(str(tmp_path)) / ST.RAW_EVENTS
    ids = [d.name for d in base.iterdir() if d.is_dir()]
    assert ids
    for eid in ids:
        v = ST.verify_acquisition_event(eid, root=str(tmp_path))
        assert v["verified"] is True and v["recomputed"] == eid


@authoritative
@pytest.mark.parametrize("field,value", [
    ("symbol", "TAMPERED"),
    ("retrieved_at", "2001-01-01T00:00:00+00:00"),
    ("provider_status", "OK_BUT_NOT_REALLY"),
    ("raw_payload_hash", "q" * 32),
    ("request_fingerprint", "q" * 32),
])
def test_tampering_an_identity_field_breaks_the_acquisition_event(tmp_path, field, value):
    _freeze(tmp_path)
    base = ST.intraday_root(str(tmp_path)) / ST.RAW_EVENTS
    eid = sorted(d.name for d in base.iterdir() if d.is_dir())[0]
    _tamper(base / eid / "acquisition_event.json", lambda d: d.__setitem__(field, value))
    v = ST.verify_acquisition_event(eid, root=str(tmp_path))
    assert v["verified"] is False
    assert "recompute" in v["reason"] or "raw" in v["reason"]


@authoritative
def test_tampering_a_non_identity_disclosure_does_not_break_the_event(tmp_path):
    """`row_count` is disclosure, not identity — the contract is explicit about
    which fields define a retrieval, and widening it silently would make an
    idempotent refetch look like corruption."""
    _freeze(tmp_path)
    base = ST.intraday_root(str(tmp_path)) / ST.RAW_EVENTS
    eid = sorted(d.name for d in base.iterdir() if d.is_dir())[0]
    _tamper(base / eid / "acquisition_event.json",
            lambda d: d.__setitem__("error_message_safe", "annotated later"))
    assert ST.verify_acquisition_event(eid, root=str(tmp_path))["verified"] is True


@authoritative
def test_build_event_identity_actually_recomputes(tmp_path):
    _freeze(tmp_path)
    base = ST.intraday_root(str(tmp_path)) / ST.DATASET_EVENTS
    ids = [d.name for d in base.iterdir() if d.is_dir()]
    assert ids
    for bid in ids:
        assert ST.verify_build_event(bid, root=str(tmp_path))["verified"] is True


@authoritative
def test_tampering_a_build_event_identity_field_is_detected(tmp_path):
    _freeze(tmp_path)
    base = ST.intraday_root(str(tmp_path)) / ST.DATASET_EVENTS
    bid = sorted(d.name for d in base.iterdir() if d.is_dir())[0]
    _tamper(base / bid / "build_event.json",
            lambda d: d.__setitem__("acquisition_event_ids", []))
    v = ST.verify_build_event(bid, root=str(tmp_path))
    assert v["verified"] is False and "recompute" in v["reason"]


@authoritative
def test_a_current_manifest_without_acquisition_evidence_cannot_graduate(tmp_path):
    """Vacuous truth was passing: 'no build event found' returned verified=True.
    A current graduation manifest claims governed real-data acquisition."""
    pilot, fp = _freeze(tmp_path)
    mfp = pilot["windows"][0]["manifest_fingerprint"]
    base = ST.intraday_root(str(tmp_path)) / ST.DATASET_EVENTS
    import shutil
    removed = 0
    for d in list(base.iterdir()):
        ev = ST.read_snapshot(ST.DATASET_EVENTS, d.name, "build_event.json",
                              root=str(tmp_path))
        if ev and ev.get("manifest_fingerprint") == mfp:
            shutil.rmtree(d)
            removed += 1
    assert removed

    strict = ST.verify_manifest_acquisitions(mfp, root=str(tmp_path),
                                             require_evidence=True)
    assert strict["verified"] is False and "build event" in strict["reason"]
    # Legacy archival objects keep the historical contract.
    lenient = ST.verify_manifest_acquisitions(mfp, root=str(tmp_path),
                                              require_evidence=False)
    assert lenient["verified"] is True

    assert PI.verify_historical_pilot(fp, root=str(tmp_path))["verified"] is False
    assert FD.session2_graduation(root=str(tmp_path))["status"] == \
        FD.DATASET_FEATURE_FOUNDATION_LIMITED


# ═══════════════════════════════════════════════════════════════════════════
# POINTER SEMANTICS — a pointer is SELECTION, not AUTHORITY
#
# `graduation/pointer.json` is deliberately mutable, so the gate must never
# assume it was written by the approved setter. set_graduation_evidence()
# refused an insufficient pilot; the READ path did not, and a hand-written
# pointer to a one-window pilot produced READY / SESSION_3_GO. Every
# dereference now re-enforces the same admission contract.
# ═══════════════════════════════════════════════════════════════════════════
def _write_pointer_directly(tmp_path, fingerprint):
    """Bypass set_graduation_evidence entirely — that is the whole point."""
    path = ST.intraday_root(str(tmp_path)) / ST.GRADUATION_POINTER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": "1",
                                "pilot_fingerprint": fingerprint}))


@authoritative
def test_a_hand_written_pointer_cannot_bypass_the_protocol(tmp_path):
    _freeze(tmp_path)                                   # full, valid evidence
    assert FD.session2_graduation(root=str(tmp_path))["status"] == \
        FD.DATASET_FEATURE_FOUNDATION_READY

    small = PI.run_pilot(_WindowProvider(), root=str(tmp_path),
                         windows=PI.PILOT_WINDOWS[-1:])
    small_fp = PI.persist_pilot(small, root=str(tmp_path))
    v = PI.verify_historical_pilot(small_fp, root=str(tmp_path))
    assert v["pilot_integrity_valid"] is True           # authentic research object
    assert v["graduation_protocol_satisfied"] is False  # but not sufficient

    _write_pointer_directly(tmp_path, small_fp)

    ev = PI.load_graduation_evidence(root=str(tmp_path))
    assert ev["available"] is False
    assert ev["pilot_integrity_valid"] is True          # NOT called corrupt
    assert ev["graduation_protocol_satisfied"] is False
    assert "graduation protocol" in ev["reason"]

    g = FD.session2_graduation(root=str(tmp_path))
    st = FD.session2_status(root=str(tmp_path))
    c = FD.session3_input_contract(root=str(tmp_path))
    assert g["status"] == FD.DATASET_FEATURE_FOUNDATION_LIMITED
    assert "graduation_protocol_satisfied" in g["blockers"]
    assert st["architecture_status"] == FD.DATASET_FEATURE_FOUNDATION_LIMITED
    assert st["graduation_evidence_ready"] is False
    assert st["graduation_protocol_satisfied"] is False
    assert st["canonical_dataset_ready"] is False
    assert st["feature_dataset_ready"] is False
    assert c["session_3_gate"] == FD.SESSION_3_NO_GO
    assert c["contract"] is None


@authoritative
def test_a_hand_written_pointer_to_full_evidence_still_graduates(tmp_path):
    """The enforcement must gate on SUFFICIENCY, not on provenance of the write."""
    _, full_fp = _freeze(tmp_path)
    _write_pointer_directly(tmp_path, full_fp)
    ev = PI.load_graduation_evidence(root=str(tmp_path))
    assert ev["available"] is True
    assert ev["graduation_protocol_satisfied"] is True
    assert FD.session2_graduation(root=str(tmp_path))["status"] == \
        FD.DATASET_FEATURE_FOUNDATION_READY
    assert FD.session3_input_contract(root=str(tmp_path))["session_3_gate"] == \
        FD.SESSION_3_GO


@authoritative
def test_the_gate_measures_protocol_itself_not_only_via_the_loader(tmp_path):
    """Defence in depth: the central gate states its own admission condition,
    so a future loader regression cannot silently widen what graduates."""
    _freeze(tmp_path)
    g = FD.session2_graduation(root=str(tmp_path))
    assert g["measured_checks"]["graduation_protocol_satisfied"]["pass"] is True
    assert PI.GRADUATION_PROTOCOL_ID in \
        g["measured_checks"]["graduation_protocol_satisfied"]["note"]


@authoritative
def test_pointer_bypass_survives_a_fresh_process(tmp_path):
    _freeze(tmp_path)
    small_fp = PI.persist_pilot(
        PI.run_pilot(_WindowProvider(), root=str(tmp_path),
                     windows=PI.PILOT_WINDOWS[-1:]), root=str(tmp_path))
    _write_pointer_directly(tmp_path, small_fp)
    script = textwrap.dedent("""
        import json, sys
        sys.path.insert(0, sys.argv[1])
        from portfolio_automation.intraday_lab import foundation as FD
        g = FD.session2_graduation(root=sys.argv[2])
        c = FD.session3_input_contract(root=sys.argv[2])
        print(json.dumps({"status": g["status"], "gate": c["session_3_gate"],
                          "contract": c["contract"] is not None}))
    """)
    proc = subprocess.run([sys.executable, "-c", script, REPO, str(tmp_path)],
                          capture_output=True, text=True, timeout=300, cwd=REPO)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["status"] == FD.DATASET_FEATURE_FOUNDATION_LIMITED
    assert out["gate"] == FD.SESSION_3_NO_GO and out["contract"] is False


# ═══════════════════════════════════════════════════════════════════════════
# REQUIRED TIMEFRAME MUST BE PROVEN, NOT ASSUMED
#
# The check read `provider_provenance.timeframe` and accepted None. The
# governed FMP provider does not emit that key at all, so it read None on every
# real window and passed vacuously — a protocol claim proving nothing.
# ═══════════════════════════════════════════════════════════════════════════
@authoritative
def test_timeframe_is_proven_from_the_verified_request_manifest(tmp_path):
    pilot, fp = _freeze(tmp_path)
    for w in pilot["windows"]:
        assert PI._verified_window_timeframe(w, root=str(tmp_path)) == "5min"
        # The optional acquisition metadata is NOT the authority.
        assert (w.get("provider_provenance") or {}).get("timeframe") is None
    assert PI.verify_historical_pilot(fp, root=str(tmp_path))[
        "graduation_protocol_satisfied"] is True


@authoritative
def test_a_missing_request_timeframe_fails_the_protocol(tmp_path):
    """Absence must fail. Treating a missing timeframe as equivalent to 5min is
    how a protocol claim becomes decorative."""
    pilot, fp = _freeze(tmp_path)
    mfp = pilot["windows"][0]["manifest_fingerprint"]
    _tamper(ST.intraday_root(str(tmp_path)) / "datasets" / "manifests" / mfp
            / "request_manifest.json", lambda d: d.pop("timeframe", None))
    p = PI.check_graduation_protocol(
        ST.read_snapshot(ST.PILOTS, fp, "pilot.json", root=str(tmp_path)),
        root=str(tmp_path))
    assert p["satisfied"] is False
    assert any("no verifiable request timeframe" in f for f in p["failures"])


@authoritative
def test_a_wrong_request_timeframe_fails_the_protocol(tmp_path):
    pilot, fp = _freeze(tmp_path)
    mfp = pilot["windows"][0]["manifest_fingerprint"]
    _tamper(ST.intraday_root(str(tmp_path)) / "datasets" / "manifests" / mfp
            / "request_manifest.json", lambda d: d.__setitem__("timeframe", "15min"))
    p = PI.check_graduation_protocol(
        ST.read_snapshot(ST.PILOTS, fp, "pilot.json", root=str(tmp_path)),
        root=str(tmp_path))
    assert p["satisfied"] is False
    assert any("15min" in f for f in p["failures"])
    # And the whole gate follows, because manifest tampering also breaks
    # provenance — the protocol failure is defence in depth, not the only guard.
    assert FD.session2_graduation(root=str(tmp_path))["status"] == \
        FD.DATASET_FEATURE_FOUNDATION_LIMITED
