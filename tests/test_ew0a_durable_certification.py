"""The operating loop cannot reach the reviewer except through durable evidence.

These are the tests that stop a future refactor from quietly restoring the
ephemeral ``supervisor(packet)`` call. Without them the whole crash-resilience
suite can pass while the autonomous path bypasses every guarantee in it -- which
is precisely the state a senior review found this work in.
"""
from __future__ import annotations

import json

import pytest

from portfolio_automation.engineer_worker.durable_certification import (
    CertificationUnavailable, ReviewContext, binding_envelope, dispatch_durably,
)
from portfolio_automation.engineer_worker.ew0a import (
    AttemptEvidence, EngineeringTaskV0, EngineeringVerificationV0, Executor,
    FailureClass, RiskClass, VerificationVerdict, certify_attempt,
)
from portfolio_automation.engineer_worker.ew0a_authority import EngineerAuthorityLevel as Lvl
from portfolio_automation.engineer_worker.ew0a_loop import RuntimePolicy, run_mission, run_task
from portfolio_automation.engineer_worker.gpt_supervisor import (
    SupervisorDecision, SupervisorVerdict,
)
from portfolio_automation.engineer_worker.review_journal import (
    LifecycleKind, read_events_strict,
)

POLICY = RuntimePolicy(mission_id="m1")


def _task(**kw):
    base = dict(task_id="t1", title="t", goal="g", risk_class=RiskClass.E1_ROUTINE,
                executor=Executor.ENGINEER, mission_id="m1",
                allowed_paths=["tests/"], allowed_tests=["tests/tx.py"],
                acceptance_criteria=["it holds"])
    base.update(kw)
    return EngineeringTaskV0(**base)


from portfolio_automation.engineer_worker.roadmap_guard import RoadmapAuthorization

_DIFF = "--- a/tests/tx.py\n+++ b/tests/tx.py\n+assert True\n"
ROADMAP = RoadmapAuthorization.for_mission("m1")


def _attempt(n: int = 1):
    return AttemptEvidence(
        attempt_id=f"a{n}", executor=Executor.ENGINEER, worker_claim="done",
        changed_paths=["tests/tx.py"], diff_text=_DIFF, tests_run=["tests/tx.py"],
        test_results={"tests/tx.py": "PASS"}, py_compile_ok=True,
        canonical_repo_touched=False)


class Spy:
    def __init__(self, verdict=SupervisorVerdict.PASS):
        self.calls = 0
        self.verdict = verdict
        self.seen = []

    def __call__(self, packet):
        self.calls += 1
        self.seen.append(packet)
        return SupervisorDecision(verdict=self.verdict, reasons=["ok"])


def _now():
    return "2026-01-01T00:00:00+00:00"


# ── the seam cannot be omitted ─────────────────────────────────────────────
def test_certify_attempt_has_no_default_certification_context():
    """Omitting it is a TypeError, not a silent ephemeral dispatch."""
    with pytest.raises(TypeError):
        certify_attempt(_task(), _attempt(), Spy(), _now, "v1")


def test_run_task_refuses_a_non_durable_context(legacy_ctx):
    spy = Spy()
    with pytest.raises(CertificationUnavailable):
        run_task(_task(), Lvl.A1_ASSISTED_ENGINEERING, POLICY,
                 lambda t, n: _attempt(n), lambda t, v: _attempt(9), spy,
                 _now, lambda: "v1", certification=legacy_ctx, roadmap=ROADMAP)
    assert spy.calls == 0, "the reviewer is not reached on a refused context"


def test_run_mission_refuses_a_non_durable_context(legacy_ctx):
    spy = Spy()
    with pytest.raises(CertificationUnavailable):
        run_mission(POLICY, [_task()], Lvl.A1_ASSISTED_ENGINEERING,
                    lambda t, n: _attempt(n), lambda t, v: _attempt(9), spy,
                    _now, lambda: "v1", certification=legacy_ctx, roadmap=ROADMAP)
    assert spy.calls == 0


def test_a_pass_cannot_be_constructed_without_evidence_refs():
    """Closes the route that bypasses the durable path entirely: build the
    verification object directly and hand it to status_for_verdict."""
    with pytest.raises(ValueError):
        EngineeringVerificationV0(
            verification_id="v", task_id="t", attempt_id="a",
            verdict=VerificationVerdict.PASS, deterministic_ok=True,
            protected_path_ok=True, scope_ok=True, policy_ok=True, tests_ok=True,
            canonical_repo_untouched=True)


# ── a verified run leaves the full durable trail ───────────────────────────
def test_a_verified_task_leaves_persisted_bytes_and_a_complete_journal(durable_ctx):
    spy = Spy()
    result = run_task(_task(), Lvl.A1_ASSISTED_ENGINEERING, POLICY,
                      lambda t, n: _attempt(n), lambda t, v: _attempt(9), spy,
                      _now, lambda: "v1", certification=durable_ctx, roadmap=ROADMAP)

    assert result.final_status == "VERIFIED"
    assert spy.calls == 1

    events, intact = read_events_strict(durable_ctx.journal.path)
    assert intact
    kinds = [e["kind"] for e in events]
    for required in (LifecycleKind.PACKET_BUILT, LifecycleKind.PACKET_PERSISTED,
                     LifecycleKind.CANDIDATE_BOUND, LifecycleKind.DISPATCH_ATTEMPTED,
                     LifecycleKind.REVIEWER_CALLED, LifecycleKind.VERDICT_RETURNED,
                     LifecycleKind.VERDICT_PERSISTED):
        assert required.value in kinds, f"missing {required.value}"

    phash = next(e["packet_hash"] for e in events
                 if e["kind"] == LifecycleKind.PACKET_PERSISTED.value)
    assert durable_ctx.store.verify(phash).ok, "the preimage exists and verifies"
    assert phash in result.verification["evidence_refs"]


def test_the_reviewer_receives_the_reloaded_persisted_bytes(durable_ctx):
    spy = Spy()
    run_task(_task(), Lvl.A1_ASSISTED_ENGINEERING, POLICY,
             lambda t, n: _attempt(n), lambda t, v: _attempt(9), spy,
             _now, lambda: "v1", certification=durable_ctx, roadmap=ROADMAP)

    events, _ = read_events_strict(durable_ctx.journal.path)
    phash = next(e["packet_hash"] for e in events
                 if e["kind"] == LifecycleKind.PACKET_PERSISTED.value)
    stored = json.loads(durable_ctx.store.load(phash).decode("utf-8"))
    assert spy.seen[0] == stored, "the reviewer saw the artifact the store proved"


def test_the_envelope_preserves_every_key_the_reviewer_prompt_names():
    """The EW-0A packet is WRAPPED, not converted. gpt_supervisor's prompt names
    these fields; a ReviewPacket contains none of them."""
    packet = {"task": {"task_id": "t1"}, "requirements": ["r"],
              "acceptance_criteria": ["ac"], "diff": "d", "tests_run": ["x"],
              "test_results": {"x": "PASS"}, "changed_files": ["f"]}
    env = binding_envelope(packet, candidate_sha="a" * 40, mission_id="m1",
                           session_id="s1", attempt_id="a1",
                           acceptance_criteria=["ac"])
    for key in ("requirements", "acceptance_criteria", "diff", "tests_run",
                "test_results", "changed_files"):
        assert env[key] == packet[key]
    assert env["candidate_sha"] == "a" * 40
    assert env["task"]["attempt_id"] == "a1"


def test_two_attempts_with_identical_evidence_get_distinct_identities(durable_ctx):
    """Without attempt_id in the envelope, a repair attempt producing
    byte-identical evidence would replay the first attempt's verdict."""
    packet = {"task": {"task_id": "t1"}, "diff": "same", "tests_run": []}
    a = binding_envelope(packet, candidate_sha="a" * 40, mission_id="m1",
                         session_id="s", attempt_id="a1", acceptance_criteria=["x"])
    b = binding_envelope(packet, candidate_sha="a" * 40, mission_id="m1",
                         session_id="s", attempt_id="a2", acceptance_criteria=["x"])
    assert a != b


# ── refusals never become certifications ───────────────────────────────────
def test_a_refused_dispatch_leaves_the_work_unverified(durable_ctx):
    """No candidate binding -> no dispatch -> not verified, reviewer untouched."""
    ctx = ReviewContext.open(durable_ctx.store.repo_root, mission_id="m1",
                             session_id="s", reviewer_identity={"model": "stub"})
    spy = Spy()
    v = certify_attempt(_task(), _attempt(), spy, _now, "v1", certification=ctx)
    assert v.verdict is VerificationVerdict.SUPERVISOR_UNAVAILABLE
    assert v.verdict is not VerificationVerdict.PASS
    assert spy.calls == 0


def test_a_secret_bearing_packet_is_refused_before_anything_is_persisted(durable_ctx):
    """Screening precedes persistence: a credential in a read-only,
    content-addressed, tracked artifact cannot be cleaned up afterwards."""
    secret = "sk-" + "a" * 32
    spy = Spy()
    v = certify_attempt(_task(), AttemptEvidence(
        attempt_id="a1", executor=Executor.ENGINEER, worker_claim="done",
        changed_paths=["tests/tx.py"], tests_run=["tests/tx.py"],
        test_results={"tests/tx.py": "PASS"}, py_compile_ok=True,
        canonical_repo_touched=False, diff_text=secret),
        spy, _now, "v1", certification=durable_ctx)

    assert v.verdict is not VerificationVerdict.PASS
    assert spy.calls == 0
    root = durable_ctx.store.repo_root
    assert not any(secret.encode() in p.read_bytes()
                   for p in root.rglob("*") if p.is_file()), "no secret durably written"


def test_the_deterministic_gate_still_precedes_the_reviewer(durable_ctx):
    """A protected-path breach must not even reach durable dispatch."""
    spy = Spy()
    v = certify_attempt(
        _task(allowed_paths=["."]),
        AttemptEvidence(attempt_id="a1", executor=Executor.ENGINEER,
                        worker_claim="green", changed_paths=["config/ew0a_authority.json"],
                        canonical_repo_touched=False),
        spy, _now, "v1", certification=durable_ctx)
    assert v.verdict is VerificationVerdict.FAIL
    assert v.failure_class == FailureClass.POLICY_VIOLATION.value
    assert spy.calls == 0
