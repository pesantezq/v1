"""Session 3.0 foundation hardening — durable evidence + derivation proof.

TWO DEFECTS THIS FREEZES
========================

**A. Graduation was caller-authoritative.** `session3_0_status(audit_dict)`
returned `SESSION_3_0_POLICY_READY` / `SESSION_3_1_GO` with zero blockers from a
fabricated dictionary — even with the rendered population JSON deleted. A
rendered report is a convenience; it is not authority.

**B. Derived-view verification proved nothing about derivation.** A content hash
proves an object has not changed since it was minted. It does not prove it was
minted correctly. Eight of eight self-consistent-but-wrong views passed the old
verifier: faked `known_at`, faked close, faked `bar_end_at`, faked dataset
fingerprint, faked calendar identity, emptied `explained_missing`, downgraded
classification, and an unrelated raw object swapped in.

    tampering detection  !=  derivation correctness

Both are now recomputed from the exact persisted Session 2 evidence.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from datetime import date, datetime, timedelta

import pytest

from portfolio_automation.intraday_lab import calendar as C
from portfolio_automation.intraday_lab import irregular_sessions as IR
from portfolio_automation.intraday_lab import pipeline as PL
from portfolio_automation.intraday_lab import population_audit as PA
from portfolio_automation.intraday_lab import storage as ST
from portfolio_automation.intraday_lab.dataset import (
    DatasetRequest, _calendar_identity, build_canonical_dataset,
)

REPO = str(__import__("pathlib").Path(__file__).resolve().parent.parent)
HALT_DATE = date(2020, 3, 9)

authoritative = pytest.mark.skipif(
    C._calendar() is None, reason="exchange_calendars not installed")


def _rows(session):
    return [{"date": ts.astimezone(C.EXCHANGE_TZ).strftime("%Y-%m-%d %H:%M:%S"),
             "open": 100 + i * 0.01, "high": 100.5 + i * 0.01,
             "low": 99.5 + i * 0.01, "close": 100.2 + i * 0.01, "volume": 1000 + i}
            for i, ts in enumerate(session.expected_bar_starts)]


class _Provider:
    """Serves a full grid, but DROPS the two fully-halted bars on the halt date."""

    provider_id = "fake-historical"

    def endpoint_for(self, timeframe):
        return "/fake/intraday/5min"

    def fetch(self, symbol, start, end, timeframe):
        out = []
        for s in C.sessions_in_range(date.fromisoformat(start), date.fromisoformat(end)):
            if s.session_type not in ("REGULAR", "EARLY_CLOSE"):
                continue
            rows = _rows(s)
            ev = IR.mwcb_event_for(s.market_date)
            if ev:
                hs, rs = ev.window_utc()
                keep = []
                for r, ts in zip(rows, s.expected_bar_starts):
                    if IR.bar_fully_inside_halt(ts, ts + timedelta(minutes=5), hs, rs):
                        continue                    # the halt: bars never printed
                    keep.append(r)
                rows = keep
            out.extend(rows)
        return out, None

    def provenance(self):
        return {"provider_id": self.provider_id, "governed": False}


def _build_halt_view(tmp_path, symbol="SPY"):
    """A real, honestly-derived halt view over persisted Session 2 evidence."""
    root = str(tmp_path)
    req = DatasetRequest(symbols=(symbol,), start=HALT_DATE, end=date(2020, 3, 13))
    out = PL.build_historical_research_dataset(req, _Provider(), root=root)
    acq = PL.acquire(req, _Provider(), root=root)
    ds = build_canonical_dataset(
        acq["bars_by_date"], request=req,
        provider_failures=acq["provider_failures"],
        normalization_failures=acq["normalization_failures"])
    rec = next(r for r in ds.reconciliations if r.market_date == HALT_DATE)
    cl = IR.classify_session(
        symbol=symbol, market_date=HALT_DATE, timeframe=rec.timeframe,
        session2_state=rec.admission_status,
        missing_timestamps=rec.missing_timestamps,
        unexpected_timestamps=rec.unexpected_timestamps,
        session_type=C.SESSION_REGULAR)
    bars = sorted(acq["bars_by_date"].get((symbol, HALT_DATE), []),
                  key=lambda b: b.bar_start_at)
    mfp = out["manifest_fingerprint"]
    payload = IR.irregular_view_payload(
        classification=cl, source_manifest_fingerprint=mfp,
        source_dataset_fingerprint=out["dataset_fingerprint"],
        raw_content_fingerprints=IR.expected_raw_lineage(mfp, symbol, "5min", root=root),
        calendar_identity=_calendar_identity(), bars=bars)
    return payload, IR.persist_irregular_view(payload, root=root), root


# ═══════════════════════════════════════════════════════════════════════════
# DEFECT B — derivation must be RECOMPUTED, not asserted
# ═══════════════════════════════════════════════════════════════════════════
@authoritative
def test_an_honestly_derived_view_verifies(tmp_path):
    payload, fp, root = _build_halt_view(tmp_path)
    v = IR.verify_irregular_view(fp, root=root)
    assert v["verified"] is True, v.get("reason")
    assert v["classification"] == IR.VALID_MARKET_WIDE_HALT_SESSION
    assert v["derivation_recomputed"] is True
    assert len(payload["explained_missing"]) == 2


@authoritative
@pytest.mark.parametrize("label,mutate", [
    ("known_at moved earlier",
     lambda d: d["observed_bars"][0].__setitem__("known_at", "2020-03-09T14:31:00+00:00")),
    ("close changed",
     lambda d: d["observed_bars"][0].__setitem__("close", 4242.0)),
    ("bar_end_at moved",
     lambda d: d["observed_bars"][0].__setitem__("bar_end_at", "2020-03-09T14:40:00+00:00")),
    ("adjustment_state changed",
     lambda d: d["observed_bars"][0].__setitem__("adjustment_state", "unadjusted")),
    ("source_dataset_fingerprint faked",
     lambda d: d.__setitem__("source_dataset_fingerprint", "f" * 32)),
    ("calendar_identity faked",
     lambda d: d.__setitem__("calendar_identity", {"exchange": "XNYS", "backend": "made-up"})),
    ("explained_missing emptied",
     lambda d: d.__setitem__("explained_missing", [])),
    ("session2_state downgraded",
     lambda d: d.__setitem__("session2_state", "ADMITTED")),
    ("classification faked",
     lambda d: d.__setitem__("classification", IR.VALID_CONTINUOUS_SESSION)),
])
def test_an_incorrectly_derived_view_is_rejected(tmp_path, label, mutate):
    """Each case mints a NEW, internally self-consistent object whose content
    hash is perfectly valid. Only recomputation can catch these."""
    payload, _, root = _build_halt_view(tmp_path)
    bad = json.loads(json.dumps(payload))
    mutate(bad)
    fp_bad = IR.persist_irregular_view(bad, root=root)
    assert ST.content_hash(bad) == fp_bad            # self-consistent by construction
    v = IR.verify_irregular_view(fp_bad, root=root)
    assert v["verified"] is False, f"{label} was accepted"


@authoritative
def test_an_unrelated_but_valid_raw_object_cannot_bless_a_view(tmp_path):
    """A 2026 SPY payload must not validate a March 2020 view merely because
    both hashes verify. Lineage comes from the MANIFEST, never from the view."""
    payload, _, root = _build_halt_view(tmp_path)
    other = PL.build_historical_research_dataset(
        DatasetRequest(symbols=("SPY",), start=date(2026, 8, 3), end=date(2026, 8, 7)),
        _Provider(), root=root)
    foreign = other["raw_content_fingerprints"]
    assert foreign and foreign != payload["raw_content_fingerprints"]
    assert ST.verify_raw_content(foreign[0], root=root)["verified"] is True

    bad = {**json.loads(json.dumps(payload)), "raw_content_fingerprints": foreign}
    fp_bad = IR.persist_irregular_view(bad, root=root)
    v = IR.verify_irregular_view(fp_bad, root=root)
    assert v["verified"] is False
    assert "lineage" in v["reason"]


@authoritative
def test_verification_uses_only_persisted_evidence_and_never_refetches(tmp_path, monkeypatch):
    payload, fp, root = _build_halt_view(tmp_path)

    def _explode(*a, **k):
        raise AssertionError("verification must not touch the provider")

    monkeypatch.setattr(PL, "acquire", _explode)
    from portfolio_automation.intraday_lab import pilot as PI
    monkeypatch.setattr(PI, "governed_fmp_provider", _explode)
    assert IR.verify_irregular_view(fp, root=root)["verified"] is True


def test_view_identity_schema_bumped_and_history_preserved():
    """The verification CONTRACT changed, so the identity schema changes with
    it — following the Session 2 identity-era precedent."""
    assert IR.IRREGULAR_VIEW_IDENTITY_SCHEMA == "intraday_irregular_session_v2"
    assert "intraday_irregular_session_v1" in IR.IRREGULAR_VIEW_SCHEMA_HISTORY


@authoritative
def test_a_v1_schema_view_is_archival_not_silently_current(tmp_path):
    payload, _, root = _build_halt_view(tmp_path)
    legacy = {**json.loads(json.dumps(payload)),
              "identity_schema": "intraday_irregular_session_v1"}
    fp = IR.persist_irregular_view(legacy, root=root)
    v = IR.verify_irregular_view(fp, root=root)
    assert v["verified"] is False
    assert v.get("archival") is True
    assert "archival" in v["reason"]


# ═══════════════════════════════════════════════════════════════════════════
# DEFECT A — a caller dictionary is never authority
# ═══════════════════════════════════════════════════════════════════════════
_FABRICATED = {
    "accounting_exact": True,
    "counts": {IR.VALID_MARKET_WIDE_HALT_SESSION: 8, IR.REJECTED_SOURCE_ERROR: 0},
    "comparison": {"n_continuous": 999, "n_halt": 8},
    "exact_mwcb_prevalence": {"registry_complete_for_window": True},
}


def test_a_fabricated_caller_audit_cannot_grant_graduation(tmp_path):
    """The exact pre-fix bypass: a perfect-looking dict, no durable evidence."""
    st = PA.session3_0_status(_FABRICATED, root=str(tmp_path))
    assert st["status"] == PA.SESSION_3_0_LIMITED
    assert st["session_3_1_gate"] == PA.SESSION_3_1_NO_GO
    assert "population_evidence_available" in st["blockers"]


def test_a_missing_pointer_is_not_graduation(tmp_path):
    ev = PA.load_session3_graduation_evidence(root=str(tmp_path))
    assert ev["available"] is False and ev["audit"] is None
    assert "pointer" in ev["reason"]


def _freeze_session2(root: str):
    """Session 3.0 graduation REQUIRES Session 2 graduation, so a Session 3 test
    root must establish it first — the dependency is the point, not an obstacle."""
    from portfolio_automation.intraday_lab import pilot as PI

    C.persist_certified_schedule(root=root)
    out = PI.run_pilot(_Provider(), root=root)
    fp = PI.persist_pilot(out, root=root)
    PI.set_graduation_evidence(fp, root=root)
    return fp


def _freeze_population(tmp_path, *, with_session2=True, chunks=None):
    root = str(tmp_path)
    if with_session2:
        _freeze_session2(root)
    else:
        C.persist_certified_schedule(root=root)
    chunks = chunks if chunks is not None else (
        [(2026, date(2026, 8, 3), date(2026, 8, 7))] + PA.mwcb_windows())
    audit = PA.run_population_audit(_Provider(), root=root, chunks=chunks)
    fp = PA.persist_population_audit(audit, root=root)
    PA.set_session3_graduation_evidence(fp, root=root)
    return audit, fp, root


@authoritative
def test_durable_evidence_grants_graduation(tmp_path):
    audit, fp, root = _freeze_population(tmp_path)
    assert PA.verify_population_audit(fp, root=root)["verified"] is True
    st = PA.session3_0_status(root=root)
    assert st["status"] == PA.SESSION_3_0_POLICY_READY, st["blockers"]
    assert st["session_3_1_gate"] == PA.SESSION_3_1_GO
    assert st["population_fingerprint"] == fp
    assert st["measured_passed"] == st["measured_total"]


@authoritative
def test_a_fabricated_audit_cannot_override_real_evidence(tmp_path):
    """Even WITH valid durable evidence, the caller argument must not be able to
    change the verdict — it is display data, not authority."""
    _freeze_population(tmp_path)
    with_arg = PA.session3_0_status(_FABRICATED, root=str(tmp_path))
    without = PA.session3_0_status(root=str(tmp_path))
    assert with_arg["measured_checks"] == without["measured_checks"]
    assert with_arg["population_fingerprint"] == without["population_fingerprint"]


@authoritative
def test_a_corrupt_population_audit_is_not_graduation(tmp_path):
    _, fp, root = _freeze_population(tmp_path)
    path = ST.intraday_root(root) / ST.SESSION3_POPULATION / fp / "population_audit.json"
    data = json.loads(path.read_text())
    data["counts"][IR.VALID_CONTINUOUS_SESSION] = 99999
    path.write_text(json.dumps(data, separators=(",", ":"), sort_keys=True))
    assert PA.verify_population_audit(fp, root=root)["verified"] is False
    st = PA.session3_0_status(root=root)
    assert st["status"] == PA.SESSION_3_0_LIMITED
    assert st["session_3_1_gate"] == PA.SESSION_3_1_NO_GO


@authoritative
def test_valid_but_insufficient_evidence_is_authentic_yet_not_graduation(tmp_path):
    """Integrity and sufficiency stay separate: a real audit with no halt
    sessions is authentic evidence that cannot graduate Session 3.0."""
    root = str(tmp_path)
    _freeze_session2(root)
    audit = PA.run_population_audit(
        _Provider(), root=root,
        chunks=[(2026, date(2026, 8, 3), date(2026, 8, 7))])   # no MWCB window
    fp = PA.persist_population_audit(audit, root=root)
    assert PA.verify_population_audit(fp, root=root)["verified"] is True   # authentic

    with pytest.raises(ValueError, match="insufficient|unverifiable"):
        PA.set_session3_graduation_evidence(fp, root=root)

    # Hand-written pointer must not confer what the setter refused.
    path = ST.intraday_root(root) / ST.SESSION3_GRADUATION_POINTER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": "1", "population_fingerprint": fp}))
    ev = PA.load_session3_graduation_evidence(root=root)
    assert ev["available"] is False
    assert ev["integrity_valid"] is True          # NOT called corrupt
    assert "insufficient" in ev["reason"]
    st = PA.session3_0_status(root=root)
    assert st["session_3_1_gate"] == PA.SESSION_3_1_NO_GO


@authoritative
def test_graduation_survives_a_fresh_process(tmp_path):
    _freeze_population(tmp_path)
    script = textwrap.dedent("""
        import json, sys
        sys.path.insert(0, sys.argv[1])
        from portfolio_automation.intraday_lab import population_audit as PA
        st = PA.session3_0_status(root=sys.argv[2])
        print(json.dumps({"status": st["status"], "gate": st["session_3_1_gate"],
                          "blockers": st["blockers"]}))
    """)
    proc = subprocess.run([sys.executable, "-c", script, REPO, str(tmp_path)],
                          capture_output=True, text=True, timeout=300, cwd=REPO)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["status"] == PA.SESSION_3_0_POLICY_READY, out
    assert out["gate"] == PA.SESSION_3_1_GO and out["blockers"] == []


@authoritative
def test_a_repointed_pointer_fails_in_a_fresh_process(tmp_path):
    _freeze_population(tmp_path)
    path = ST.intraday_root(str(tmp_path)) / ST.SESSION3_GRADUATION_POINTER
    path.write_text(json.dumps({"schema_version": "1",
                                "population_fingerprint": "0" * 32}))
    script = textwrap.dedent("""
        import json, sys
        sys.path.insert(0, sys.argv[1])
        from portfolio_automation.intraday_lab import population_audit as PA
        st = PA.session3_0_status(root=sys.argv[2])
        print(json.dumps({"status": st["status"], "gate": st["session_3_1_gate"]}))
    """)
    proc = subprocess.run([sys.executable, "-c", script, REPO, str(tmp_path)],
                          capture_output=True, text=True, timeout=300, cwd=REPO)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["status"] == PA.SESSION_3_0_LIMITED
    assert out["gate"] == PA.SESSION_3_1_NO_GO


@authoritative
def test_rendered_reports_are_not_authority(tmp_path):
    """Deleting the human-readable JSON must not change the verdict either way."""
    _freeze_population(tmp_path)
    before = PA.session3_0_status(root=str(tmp_path))["status"]
    rendered = (ST.intraday_root(str(tmp_path)) / "session3"
                / "irregular_session_population.json")
    if rendered.exists():
        rendered.unlink()
    assert PA.session3_0_status(root=str(tmp_path))["status"] == before


# ═══════════════════════════════════════════════════════════════════════════
# §20-23 halt-boundary semantics
# ═══════════════════════════════════════════════════════════════════════════
@authoritative
def test_real_march_2020_boundary_bars_are_identified_with_tradable_time():
    session = C.resolve_session(HALT_DATE)
    boundary = IR.halt_boundary_bars(HALT_DATE, session.expected_bar_starts)
    et = {datetime.fromisoformat(k).astimezone(C.EXCHANGE_TZ).strftime("%H:%M"): v
          for k, v in boundary.items()}
    assert set(et) == {"09:30", "09:45"}
    assert et["09:30"] == pytest.approx(253.0)     # halt begins 09:34:13
    assert et["09:45"] == pytest.approx(47.0)      # reopen 09:49:13
    # Fully-halted bars are ABSENT, not boundary bars.
    assert "09:35" not in et and "09:40" not in et


@authoritative
def test_the_one_second_boundary_bar_is_what_decides_the_policy():
    """2020-03-16 09:30-09:35 carries ONE second of tradable time. Its close is
    a real price; its high-low is a single print wearing a 5-minute label."""
    d = date(2020, 3, 16)
    boundary = IR.halt_boundary_bars(d, C.resolve_session(d).expected_bar_starts)
    secs = sorted(boundary.values())
    assert secs[0] == pytest.approx(1.0)
    assert IR.HALT_BOUNDARY_FEATURE_POLICY["close_endpoint"]["status"] == IR.ALLOWED
    assert IR.HALT_BOUNDARY_FEATURE_POLICY["normalized_range"]["status"] == IR.BLOCKED


def test_halt_boundary_policy_is_explicit_for_every_primitive():
    policy = IR.halt_boundary_policy()
    required = {"close_endpoint", "close_to_close_return", "n_bar_displacement",
                "within_segment_realized_volatility", "normalized_range",
                "intra_bar_open_to_close", "opening_range_construction"}
    assert required <= set(policy["features"])
    for name, entry in policy["features"].items():
        assert entry["status"] in (IR.ALLOWED, IR.BLOCKED)
        assert entry["why"]
    assert "FEATURE_UNAVAILABLE" in policy["opening_window_rule"]
    assert "no strategy performance" in policy["basis"]


def test_a_non_halt_date_has_no_boundary_bars():
    d = date(2020, 3, 17)                       # volatile, but no circuit breaker
    assert IR.halt_boundary_bars(d, C.resolve_session(d).expected_bar_starts) == {}


# ═══════════════════════════════════════════════════════════════════════════
# Governance
# ═══════════════════════════════════════════════════════════════════════════
@authoritative
def test_session3_foundation_never_enables_strategy_validation(tmp_path):
    _freeze_population(tmp_path)
    st = PA.session3_0_status(root=str(tmp_path))
    assert st["strategy_validation_allowed"] is False
    assert IR.policy_provenance()["strategy_validation_allowed"] is False
    assert IR.halt_boundary_policy()["observe_only"] is True
