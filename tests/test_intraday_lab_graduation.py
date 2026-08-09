"""Session 2 graduation gate + pilot + Session 3 handoff contract.

The gate's job is to be WRONG-PROOF in one specific direction: it must never
report READY on evidence it does not have. The previous implementation
hardcoded LIMITED with a hand-written justification, which could not follow the
evidence in either direction — it would have kept saying LIMITED after the
blocker was fixed, and kept saying LIMITED for a stale reason if a different
blocker appeared.

So these tests care less about "does it say READY today" and more about "does
it stop saying READY the moment the evidence stops supporting it".
"""
from __future__ import annotations

from datetime import date, timezone

import pytest

from portfolio_automation.intraday_lab import calendar as C
from portfolio_automation.intraday_lab import dataset as DS
from portfolio_automation.intraday_lab import foundation as FD
from portfolio_automation.intraday_lab import migration as MG
from portfolio_automation.intraday_lab import pilot as PI
from portfolio_automation.intraday_lab import providers as PR
from portfolio_automation.intraday_lab import storage as ST

UTC = timezone.utc

authoritative = pytest.mark.skipif(
    C._calendar() is None, reason="exchange_calendars not installed")


def _freeze(tmp_path, windows=None):
    """Run a pilot AND make it durable evidence — the only thing graduation reads.

    Passing an in-memory pilot is no longer sufficient by design: a verdict that
    cannot survive a process exit is the defect this closure fixed.
    """
    out = PI.run_pilot(_WindowProvider(), root=str(tmp_path),
                       windows=windows or PI.PILOT_WINDOWS)
    C.persist_certified_schedule(root=str(tmp_path))
    fp = PI.persist_pilot(out, root=str(tmp_path))
    PI.set_graduation_evidence(fp, root=str(tmp_path))
    return out, fp


def _rows(session):
    out = []
    for i, ts in enumerate(session.expected_bar_starts):
        out.append({"date": ts.astimezone(C.EXCHANGE_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                    "open": 100 + i * 0.01, "high": 100.5 + i * 0.01,
                    "low": 99.5 + i * 0.01, "close": 100.2 + i * 0.01,
                    "volume": 1000 + i})
    return out


class _WindowProvider:
    """Serves a correct full grid for any requested session in the window."""

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


# ═══════════════════════════════════════════════════════════════════════════
# Pilot
# ═══════════════════════════════════════════════════════════════════════════
@authoritative
def test_pilot_windows_span_the_regimes_they_claim_to():
    labels = {w.label for w in PI.PILOT_WINDOWS}
    assert len(PI.PILOT_WINDOWS) >= 6
    years = {w.start.year for w in PI.PILOT_WINDOWS}
    assert years >= {2017, 2020, 2022, 2024, 2025, 2026}
    assert all(w.rationale for w in PI.PILOT_WINDOWS), "every window must justify itself"
    assert len(labels) == len(PI.PILOT_WINDOWS), "labels must be unique"


@authoritative
def test_pilot_accounts_for_every_requested_session(tmp_path):
    out = PI.run_pilot(_WindowProvider(), root=str(tmp_path),
                       windows=PI.PILOT_WINDOWS[:3])
    assert out["totals"]["windows_failed"] == 0
    assert out["every_requested_session_accounted_for"] is True
    assert out["all_windows_provenance_verified"] is True
    assert out["all_windows_current_era"] is True
    for w in out["windows"]:
        assert w["sessions_reconciled"] == w["requested_symbol_dates"]


@authoritative
def test_pilot_reports_rejections_rather_than_hiding_them(tmp_path):
    """A window whose provider returns nothing must show up as rejections, not
    as a smaller-but-clean-looking result."""
    class _Empty(_WindowProvider):
        provider_id = "fake-empty"

        def fetch(self, symbol, start, end, timeframe):
            return [], None

    out = PI.run_pilot(_Empty(), root=str(tmp_path), windows=PI.PILOT_WINDOWS[:1])
    w = out["windows"][0]
    assert w["sessions_admitted"] == 0
    assert w["sessions_rejected"] > 0
    assert w["sessions_reconciled"] == w["requested_symbol_dates"]
    assert out["rejection_breakdown"]["REJECTED_MISSING_BARS"] > 0


@authoritative
def test_a_pipeline_error_is_recorded_not_raised(tmp_path):
    class _Broken(_WindowProvider):
        provider_id = "fake-broken"

        def endpoint_for(self, timeframe):
            raise RuntimeError("no endpoint")

        def fetch(self, symbol, start, end, timeframe):
            raise RuntimeError("boom")

    out = PI.run_pilot(_Broken(), root=str(tmp_path), windows=PI.PILOT_WINDOWS[:1])
    # The provider fails, but the pilot still produces a verdict per window.
    assert out["totals"]["windows"] == 1
    w = out["windows"][0]
    assert w["status"] in ("OK", "PIPELINE_ERROR")
    if w["status"] == "OK":
        assert w["sessions_rejected"] > 0        # recorded as provider failure


def test_pilot_never_enables_strategy_validation(tmp_path):
    out = PI.run_pilot(_WindowProvider(), root=str(tmp_path),
                       windows=PI.PILOT_WINDOWS[-1:])
    assert out["strategy_validation_allowed"] is False
    assert out["observe_only"] is True


# ═══════════════════════════════════════════════════════════════════════════
# Graduation gate
# ═══════════════════════════════════════════════════════════════════════════
def test_graduation_is_limited_without_a_pilot(tmp_path):
    """The core anti-pattern: never READY on evidence that is simply absent."""
    g = FD.session2_graduation(None, root=str(tmp_path))
    assert g["status"] == FD.DATASET_FEATURE_FOUNDATION_LIMITED
    assert "historical_pilot_ran" in g["blockers"]
    assert g["strategy_validation_allowed"] is False


@authoritative
def test_graduation_reaches_ready_on_complete_evidence(tmp_path):
    _freeze(tmp_path)
    g = FD.session2_graduation(root=str(tmp_path))
    assert g["blockers"] == [], g["blockers"]
    assert g["status"] == FD.DATASET_FEATURE_FOUNDATION_READY
    assert g["measured_passed"] == g["measured_total"]


@authoritative
def test_graduation_falls_back_to_limited_when_the_pilot_regresses(tmp_path):
    pilot, _ = _freeze(tmp_path)
    assert FD.session2_graduation(root=str(tmp_path))["status"] == \
        FD.DATASET_FEATURE_FOUNDATION_READY
    broken = {**pilot, "every_requested_session_accounted_for": False}
    g = FD.session2_graduation(broken, root=str(tmp_path))
    assert g["status"] == FD.DATASET_FEATURE_FOUNDATION_LIMITED
    assert "every_requested_session_accounted_for" in g["blockers"]


@authoritative
def test_graduation_fails_when_a_corpus_object_is_tampered(tmp_path):
    """Evidence-driven means the gate must react to the CORPUS, not just flags."""
    pilot, _ = _freeze(tmp_path)
    assert FD.session2_graduation(root=str(tmp_path))["blockers"] == []
    fp = pilot["windows"][0]["dataset_fingerprint"]
    path = (ST.intraday_root(str(tmp_path)) / "datasets" / "content" / fp
            / "canonical_bars.json")
    import json as _json
    rows = _json.loads(path.read_text())
    rows[0] = {**rows[0], "close": 4242.0}
    path.write_text(_json.dumps(rows, separators=(",", ":"), sort_keys=True))

    g = FD.session2_graduation(root=str(tmp_path))
    assert g["status"] == FD.DATASET_FEATURE_FOUNDATION_LIMITED
    assert "legacy_objects_verify_or_fail_honestly" in g["blockers"]


def test_graduation_never_self_certifies_tamper_detection(tmp_path):
    """A runtime status function must not claim invariants it did not run.

    These belong to the test suite, and the gate names the enforcing tests
    instead of asserting the outcome — otherwise READY would be a verdict
    derived from absent data, the exact failure this lab exists to prevent.
    """
    g = FD.session2_graduation(None, root=str(tmp_path))
    enforced = g["test_enforced_contracts"]
    assert "raw_tampering_breaks_readiness" in enforced
    assert "manifest_tampering_breaks_readiness" in enforced
    assert "canonical_tampering_breaks_readiness" in enforced
    assert "feature_tampering_breaks_readiness" in enforced
    # and none of them is counted as a measured check
    assert not (set(enforced) & set(g["measured_checks"]))
    for name in enforced.values():
        assert "tests/" in name or "test_" in name


def test_graduation_never_crashes_on_a_broken_probe(monkeypatch, tmp_path):
    """A gate that raises leaves the operator with no verdict at all."""
    monkeypatch.setattr(C, "coverage", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    g = FD.session2_graduation(None, root=str(tmp_path))
    assert g["status"] == FD.DATASET_FEATURE_FOUNDATION_LIMITED
    assert any("RuntimeError" in (v.get("reason") or "")
               for v in g["measured_checks"].values())


@authoritative
def test_session2_status_follows_the_computed_verdict(tmp_path):
    _freeze(tmp_path)
    st = FD.session2_status(root=str(tmp_path))
    assert st["architecture_status"] == st["graduation"]["status"]
    assert st["strategy_validation_allowed"] is False


# ═══════════════════════════════════════════════════════════════════════════
# Session 3 handoff contract
# ═══════════════════════════════════════════════════════════════════════════
def test_no_contract_is_published_while_session_2_is_limited(tmp_path):
    c = FD.session3_input_contract(None, root=str(tmp_path))
    assert c["session_3_gate"] == FD.SESSION_3_NO_GO
    assert c["contract"] is None          # a contract beside LIMITED reads as permission
    assert c["blockers"]


@authoritative
def test_contract_is_published_on_go_and_states_the_temporal_invariant(tmp_path):
    _freeze(tmp_path)
    c = FD.session3_input_contract(root=str(tmp_path))
    assert c["session_3_gate"] == FD.SESSION_3_GO
    body = c["contract"]
    t = body["temporal_contract"]
    assert "10:06" in t["invariant"] and "10:05" in t["invariant"]
    assert "retrieved_at" in t["known_at_rule"]
    assert set(t["fields_guaranteed"]) == {"bar_start_at", "bar_end_at", "known_at"}


@authoritative
def test_contract_restricts_session_3_to_current_era_objects(tmp_path):
    _freeze(tmp_path)
    c = FD.session3_input_contract(root=str(tmp_path))["contract"]
    ident = c["identity_contract"]
    corpus = MG.active_corpus(root=str(tmp_path))
    assert ident["active_manifests"] == [a["manifest_fingerprint"]
                                         for a in corpus["active_manifests"]]
    assert not (set(ident["active_manifests"]) & set(ident["archival_manifests"]))


@authoritative
def test_contract_discloses_the_halted_session_selection_bias(tmp_path):
    """The pilot proved halted sessions are rejected. A contract that omitted
    that would hand Session 3 a silently biased universe."""
    _freeze(tmp_path)
    c = FD.session3_input_contract(root=str(tmp_path))["contract"]
    note = c["admission_contract"]["known_exclusion"]
    assert "halt" in note.lower() and "bias" in note.lower()
    assert c["adjustment_contract"]["absolute_price_features"].startswith("BLOCKED")
    assert c["adjustment_contract"]["volume_features"].startswith("BLOCKED")


@authoritative
def test_graduating_the_data_does_not_graduate_strategies(tmp_path):
    """SESSION_3_GO is permission to START, never permission to validate."""
    _freeze(tmp_path)
    c = FD.session3_input_contract(root=str(tmp_path))
    assert c["session_3_gate"] == FD.SESSION_3_GO
    assert c["strategy_validation_allowed"] is False
    assert FD.session2_graduation(root=str(tmp_path))[
        "strategy_validation_allowed"] is False


# ═══════════════════════════════════════════════════════════════════════════
# FMP governance — the lab must not route around the budget governor, and the
# governor must not be able to disguise its own refusal as absent market data.
# ═══════════════════════════════════════════════════════════════════════════
def test_the_lab_run_mode_can_never_be_silently_skipped():
    """A governor skip returns [], which fetch_status reads as NO_DATA and the
    reconciler records as REJECTED_MISSING_BARS — OUR refusal written into
    immutable evidence as a market-data gap. The lab's run mode is chosen so
    that neither skip path is reachable."""
    from portfolio_automation.data_budget.scheduler import RunModeScheduler, DEFAULT_RUN_MODES

    mode = PI.INTRADAY_RESEARCH_RUN_MODE
    sched = RunModeScheduler(DEFAULT_RUN_MODES)
    # REGISTERED, not relying on the unknown-mode fallback: depending on the
    # default for absent keys is an accident, not a policy.
    assert mode in DEFAULT_RUN_MODES, "intraday_research must be explicitly registered"
    assert DEFAULT_RUN_MODES[mode]["call_budget"] > 0, "budget must be intentional"
    assert not DEFAULT_RUN_MODES[mode].get("cache_only")
    # Bandwidth-guard skip fires only for low priority — unreachable here.
    assert sched.priority(mode) != "low"
    assert sched.should_skip(mode, bandwidth_exhausted=True) is False
    # historical_replay WOULD have been skippable, proving the choice is
    # load-bearing rather than incidental.
    assert sched.should_skip("historical_replay", bandwidth_exhausted=True) is True

    # The run-budget skip IS reachable now that the budget is real, so the
    # pilot pre-flights: it refuses to start rather than let the governor skip
    # mid-run and have those skips recorded as absent market data.
    head = PI.budget_headroom(PI.DEFAULT_SYMBOLS, PI.PILOT_WINDOWS)
    assert head["fits"] is True
    assert head["planned_calls"] < head["call_budget"]
    assert sched.over_run_budget(mode, calls_so_far=head["planned_calls"]) is False


def test_pilot_refuses_to_start_when_it_would_exceed_its_budget():
    """A governor skip returns [] and becomes REJECTED_MISSING_BARS in immutable
    evidence. Refusing up front is the only way that stays impossible."""
    many = PI.PILOT_WINDOWS * 10
    head = PI.budget_headroom(PI.DEFAULT_SYMBOLS, many)
    assert head["fits"] is False
    with pytest.raises(RuntimeError, match="budget"):
        PI.run_pilot(_WindowProvider(), windows=many, root=".")


def test_the_lab_never_constructs_an_ungoverned_fmp_client():
    import ast
    import pathlib

    src = pathlib.Path("portfolio_automation/intraday_lab").rglob("*.py")
    for py in src:
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "fmp_client":
                names = {a.name for a in node.names}
                assert "FMPClient" not in names, f"{py} imports FMPClient directly"


def test_governed_provider_rejects_a_client_without_get_json():
    with pytest.raises(PR.ProviderError):
        PR.GovernedFMPIntradayProvider(object())
