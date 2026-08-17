"""Backend truth states and capability readiness.

The dashboard exists so a human can trust what it says about an autonomous
system. These tests are therefore about honesty, not coverage: a truthful
PENDING_BACKEND must beat a fabricated LIVE, and a truthful PARTIAL must beat an
unjustified READY.
"""
from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

from portfolio_automation.engineer_worker import ew0a_readmodels as rm
from portfolio_automation.engineer_worker.control_center_truth import (
    FRESHNESS_SECONDS, Capability, Readiness, TruthState, assess_readiness,
    classify,
)

REPO = Path(__file__).resolve().parents[1]
NOW = "2026-08-17T12:00:00+00:00"
FRESH = "2026-08-17T11:59:00+00:00"       # 1 minute old
OLD = "2026-08-16T00:00:00+00:00"         # ~36 hours old


# ── AC1: the five states are distinct, not aliases ─────────────────────────
def test_the_five_truth_states_are_distinct():
    values = [s.value for s in TruthState]
    assert sorted(values) == sorted(
        ["LIVE", "STALE", "PENDING_BACKEND", "UNAVAILABLE", "UNKNOWN"])
    assert len(set(values)) == 5


def test_each_state_is_reachable_by_a_different_evidence_shape():
    """If two states could only ever be produced by the same evidence, one of
    them would be decoration."""
    assert classify(producer_exists=True, value="x", recorded_at=FRESH, now=NOW,
                    threshold="heartbeat") is TruthState.LIVE
    assert classify(producer_exists=True, value="x", recorded_at=OLD, now=NOW,
                    threshold="heartbeat") is TruthState.STALE
    assert classify(producer_exists=False, value=None) is TruthState.PENDING_BACKEND
    assert classify(producer_exists=True, value=None) is TruthState.UNAVAILABLE
    assert classify(producer_exists=True, value="x", recorded_at=None,
                    now=NOW) is TruthState.UNKNOWN


# ── AC2: STALE is measured, never guessed ──────────────────────────────────
def test_missing_timestamp_is_unknown_not_stale():
    """The load-bearing distinction. Calling an untimestamped value STALE
    asserts an age nobody measured."""
    assert classify(producer_exists=True, value="present", recorded_at=None,
                    now=NOW) is TruthState.UNKNOWN


@pytest.mark.parametrize("bad", ["", "   ", "not-a-date", "2026-13-45T99:99:99Z",
                                 12345, None, "2026-08-17T12:00:00"])
def test_unusable_timestamps_are_unknown_not_stale(bad):
    """The naive (tz-less) case matters: comparing it to an aware reference
    would require inventing a zone, and that silently shifts the age."""
    assert classify(producer_exists=True, value="present", recorded_at=bad,
                    now=NOW) is TruthState.UNKNOWN


def test_missing_reference_time_is_unknown():
    assert classify(producer_exists=True, value="present", recorded_at=FRESH,
                    now=None) is TruthState.UNKNOWN


def test_stale_requires_value_timestamp_and_exceeded_threshold():
    assert classify(producer_exists=True, value="v", recorded_at=OLD, now=NOW,
                    threshold="heartbeat") is TruthState.STALE
    # same evidence, a threshold long enough to cover it -> LIVE
    assert classify(producer_exists=True, value="v", recorded_at=OLD, now=NOW,
                    threshold="verification") is TruthState.STALE  # 36h > 24h
    assert classify(producer_exists=True, value="v", recorded_at=FRESH, now=NOW,
                    threshold="verification") is TruthState.LIVE


def test_a_future_timestamp_is_unknown_not_live():
    """Evidence stamped after the evaluation time is unexplained, not fresh."""
    assert classify(producer_exists=True, value="v",
                    recorded_at="2026-08-18T12:00:00+00:00", now=NOW) is TruthState.UNKNOWN


def test_threshold_is_named_not_a_buried_literal():
    assert FRESHNESS_SECONDS["heartbeat"] < FRESHNESS_SECONDS["verification"]
    assert all(isinstance(v, int) and v > 0 for v in FRESHNESS_SECONDS.values())


# ── AC3: no fabrication ────────────────────────────────────────────────────
def test_a_missing_producer_is_pending_backend_not_unavailable():
    """Engineering incompleteness must not masquerade as an outage: they lead
    an operator to different actions."""
    assert classify(producer_exists=False, value=None) is TruthState.PENDING_BACKEND
    assert classify(producer_exists=False, value="ignored") is TruthState.PENDING_BACKEND


def test_an_existing_producer_with_no_value_is_unavailable():
    assert classify(producer_exists=True, value=None) is TruthState.UNAVAILABLE
    assert classify(producer_exists=True, value="") is TruthState.UNAVAILABLE
    assert classify(producer_exists=True,
                    value="PENDING_BACKEND") is TruthState.UNAVAILABLE


def test_fabricated_telemetry_cannot_reach_live_without_a_producer():
    """NEGATIVE CONTROL. Inventing a plausible heartbeat and a plausible
    timestamp must not buy a LIVE state while no producer exists."""
    fabricated = classify(producer_exists=False, value="ALIVE",
                          recorded_at=FRESH, now=NOW, threshold="heartbeat")
    assert fabricated is TruthState.PENDING_BACKEND
    assert fabricated is not TruthState.LIVE


def test_the_real_dashboard_invents_no_heartbeat_or_latency():
    d = rm.build_dashboard(REPO, now=NOW)
    assert d["worker"]["operational_state"] == rm.PENDING_BACKEND
    assert d["supervisor"]["measured_latency_ms"] == rm.PENDING_BACKEND
    assert d["supervisor"]["verification_queue"] == rm.PENDING_BACKEND
    states = {c["capability"]: c["state"] for c in d["backend_truth"]["capabilities"]}
    assert states["worker_activity"] == "PENDING_BACKEND"


# ── AC6: paired guard controls ─────────────────────────────────────────────
def test_marking_everything_live_is_rejected():
    """NEGATIVE CONTROL. An implementation that declares every capability LIVE
    regardless of evidence would produce READY here. The real dashboard must
    not, because a producer genuinely does not exist."""
    all_live = [Capability(n, TruthState.LIVE, required=True)
                for n in ("controller_state", "worker_authority", "worker_activity")]
    assert assess_readiness(all_live).readiness is Readiness.READY

    real = rm.build_dashboard(REPO, now=NOW)["backend_truth"]
    assert real["readiness"] != Readiness.READY.value, (
        "the repository has capabilities with no producer; READY would be a lie")


def test_marking_everything_pending_is_rejected():
    """NEGATIVE CONTROL, the other extreme. Labelling everything pending is the
    lazy-safe answer; it must not be what the real dashboard produces, or the
    taxonomy would carry no information."""
    all_pending = [Capability(n, TruthState.PENDING_BACKEND, required=True)
                   for n in ("controller_state", "worker_authority")]
    assert assess_readiness(all_pending).readiness is Readiness.UNAVAILABLE

    real = rm.build_dashboard(REPO, now=NOW)["backend_truth"]
    assert real["readiness"] != Readiness.UNAVAILABLE.value
    live = [c for c in real["capabilities"] if c["state"] == "LIVE"]
    assert live, "some capabilities ARE live; reporting none would be false too"
    assert any(c["state"] == "PENDING_BACKEND" for c in real["capabilities"])


# ── AC4: readiness is capability-based, not a percentage ───────────────────
def test_many_live_secondary_fields_do_not_buy_ready():
    """The specific failure this replaces: cosmetic LIVE fields hiding the
    absence of a critical capability."""
    caps = [Capability("controller_state", TruthState.LIVE, required=True),
            Capability("worker_authority", TruthState.LIVE, required=True),
            Capability("worker_activity", TruthState.PENDING_BACKEND, required=True)]
    caps += [Capability(f"cosmetic_{i}", TruthState.LIVE, required=False)
             for i in range(20)]

    result = assess_readiness(caps)
    assert result.readiness is Readiness.PARTIAL
    assert result.counts["LIVE"] == 22, "the LIVE majority is real..."
    assert any("worker_activity" in r for r in result.reasons), "...and irrelevant"


def test_only_secondary_gaps_yield_mostly_live():
    caps = [Capability("controller_state", TruthState.LIVE, required=True),
            Capability("worker_authority", TruthState.LIVE, required=True),
            Capability("component_health", TruthState.PENDING_BACKEND, required=False)]
    assert assess_readiness(caps).readiness is Readiness.MOSTLY_LIVE


def test_a_broken_oversight_floor_is_unavailable_not_partial():
    """If authority itself cannot be established, no amount of other evidence
    makes the dashboard trustworthy."""
    caps = [Capability("controller_state", TruthState.LIVE, required=True),
            Capability("worker_authority", TruthState.UNAVAILABLE, required=True)]
    assert assess_readiness(caps).readiness is Readiness.UNAVAILABLE


def test_a_required_stale_capability_degrades_readiness():
    caps = [Capability("controller_state", TruthState.LIVE, required=True),
            Capability("worker_authority", TruthState.LIVE, required=True),
            Capability("supervisor_state", TruthState.STALE, required=True)]
    assert assess_readiness(caps).readiness is Readiness.PARTIAL


def test_counts_are_diagnostics_and_do_not_decide_readiness():
    """Same LIVE count, opposite verdicts — proving the decision is not
    arithmetic."""
    a = assess_readiness([
        Capability("controller_state", TruthState.LIVE, required=True),
        Capability("worker_authority", TruthState.LIVE, required=True),
        Capability("secondary", TruthState.PENDING_BACKEND, required=False)])
    b = assess_readiness([
        Capability("controller_state", TruthState.LIVE, required=True),
        Capability("worker_authority", TruthState.LIVE, required=True),
        Capability("critical", TruthState.PENDING_BACKEND, required=True)])
    assert a.counts["LIVE"] == b.counts["LIVE"] == 2
    assert a.readiness is Readiness.MOSTLY_LIVE
    assert b.readiness is Readiness.PARTIAL


# ── AC7: determinism ───────────────────────────────────────────────────────
def test_identical_evidence_and_reference_time_give_identical_output():
    first = rm.build_dashboard(REPO, now=NOW)["backend_truth"]
    second = rm.build_dashboard(REPO, now=NOW)["backend_truth"]
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_readiness_moves_only_because_the_reference_time_moved():
    """Freshness is evaluated against the injected time, not the wall clock."""
    early = classify(producer_exists=True, value="v", recorded_at=FRESH,
                     now="2026-08-17T12:01:00+00:00", threshold="heartbeat")
    later = classify(producer_exists=True, value="v", recorded_at=FRESH,
                     now="2026-08-18T12:00:00+00:00", threshold="heartbeat")
    assert early is TruthState.LIVE and later is TruthState.STALE


def test_the_truth_module_reads_no_wall_clock():
    src = Path(inspect.getfile(
        __import__("portfolio_automation.engineer_worker.control_center_truth",
                   fromlist=["x"]))).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            assert name not in {"now", "utcnow", "today", "time"}, (
                f"freshness must come from the injected reference time, not {name}()")


# ── AC5: the non-authoritative boundary is preserved ───────────────────────
def test_the_truth_module_imports_no_mutator():
    from portfolio_automation.engineer_worker import control_center_truth as t
    for forbidden in ("set_authority_level", "write_runtime_policy", "certify_attempt",
                      "run_mission", "run_task", "admit_engineer_task"):
        assert not hasattr(t, forbidden)


def test_the_truth_module_performs_no_io():
    """A projection helper that opened files could become a second source of
    truth by reading things the controller never projected."""
    from portfolio_automation.engineer_worker import control_center_truth as t
    tree = ast.parse(Path(inspect.getfile(t)).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            assert name not in {"open", "read_text", "write_text", "mkdir", "unlink"}


def test_the_dashboard_still_carries_no_secrets_with_the_new_section():
    blob = json.dumps(rm.build_dashboard(REPO, now=NOW))
    for leak in ("sk-", "Bearer", "Authorization", ".ew0a_openai_key", "api_key"):
        assert leak not in blob


# ── the real, evidence-derived answer ──────────────────────────────────────
def test_the_interface_doc_matches_what_the_code_actually_emits():
    """AC8 drift guard. A doc that drifts from the code is worse than no doc:
    it is confidently wrong about a system built for oversight."""
    doc = (REPO / "docs" / "WORKER_CONTROL_CENTER_INTERFACE.md").read_text(
        encoding="utf-8")
    bt = rm.build_dashboard(REPO, now=NOW)["backend_truth"]

    for state in TruthState:
        assert state.value in doc, f"{state.value} undocumented"
    for readiness in Readiness:
        assert readiness.value in doc, f"{readiness.value} undocumented"
    # the CURRENT classification must be the one the code derives
    assert f"Current classification — `{bt['readiness']}`" in doc

    pending = {c["capability"] for c in bt["capabilities"]
               if c["state"] == "PENDING_BACKEND"}
    for name in pending:
        assert name in doc, f"remaining PENDING_BACKEND capability {name} undocumented"
    for name, seconds in FRESHNESS_SECONDS.items():
        assert f"{name} {seconds}s" in doc, f"threshold {name} undocumented"


def test_the_repository_readiness_is_derived_and_not_hardcoded():
    """Whatever the answer is, it must come from evidence. Today a required
    capability has no producer, so READY is unavailable to us."""
    bt = rm.build_dashboard(REPO, now=NOW)["backend_truth"]
    assert bt["readiness"] in {r.value for r in Readiness}
    assert bt["readiness"] == Readiness.PARTIAL.value
    pending = {c["capability"] for c in bt["capabilities"]
               if c["state"] == "PENDING_BACKEND"}
    assert "worker_activity" in pending
    assert bt["reasons"], "a non-READY verdict must say why"
