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
    out = PI.run_pilot(_WindowProvider(), root=str(tmp_path),
                       windows=windows or PI.PILOT_WINDOWS[-2:])
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
    _, first = _freeze(tmp_path, windows=PI.PILOT_WINDOWS[-1:])
    # A second, NEWER pilot object exists but is not pointed at.
    second_out = PI.run_pilot(_WindowProvider(), root=str(tmp_path),
                              windows=PI.PILOT_WINDOWS[-2:])
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
