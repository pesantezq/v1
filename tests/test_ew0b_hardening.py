"""EW-0B adversarial hardening of the supervised engineering loop.

These are not happy-path examples. Each one constructs a way the loop could
produce a certification it should not, and proves it does not -- against the
REAL control flow (``run_task`` / ``run_mission`` / ``certify_attempt`` /
``dispatch_durably``), not a re-implementation of it.

Every major safeguard carries a PAIRED CONTROL. A test that only proves "X is
blocked" passes just as well when everything is blocked, including the work the
loop exists to do; the positive control is what distinguishes a gate from an
outage.

The supervisor is injected as a spy so the tests can assert the strongest
property available: for a deterministic refusal, the independent reviewer is
never CALLED. A refusal that still spends a reviewer call is a weaker refusal
than one that never asks.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from portfolio_automation.engineer_worker import ew0a_loop as L
from portfolio_automation.engineer_worker.durable_certification import ReviewContext
from portfolio_automation.engineer_worker.evidence_sufficiency import (
    EvidenceRefusal, assess_evidence)
from portfolio_automation.engineer_worker.execution_identity import (
    UNAVAILABLE, ExecutionIdentity)
from portfolio_automation.engineer_worker.ew0a import (
    AttemptEvidence, EngineeringTaskV0, Executor, FailureClass, NextAction,
    OutcomeRecord, RiskClass, TaskStatus, VerificationVerdict, action_for_failure,
    append_outcome, assign_executor, certify_attempt, read_outcomes,
    status_for_verdict)
from portfolio_automation.engineer_worker.ew0a_authority import (
    AuthorityError, EngineerAuthorityLevel as Lvl, assert_operation_allowed)
from portfolio_automation.engineer_worker.ew0a_loop import (
    LoopStop, Route, RuntimePolicy, route_task, run_mission, run_task)
from portfolio_automation.engineer_worker.gpt_supervisor import (
    SupervisorConfig, SupervisorDecision, SupervisorVerdict, review)
from portfolio_automation.engineer_worker.review_candidate import HeadResolution
from portfolio_automation.engineer_worker.roadmap_guard import (
    RoadmapAuthorization, RoadmapViolation, assert_mission_authorized,
    assert_roadmap_authoritative)

MISSION = "ew0b_hardening_mission"
ROADMAP = RoadmapAuthorization.for_mission(MISSION)
POLICY = RuntimePolicy(mission_id=MISSION)

SHA_A = "a" * 40
SHA_B = "b" * 40


def _now():
    return "2026-08-17T00:00:00Z"


_vid_n = [0]


def _vid():
    _vid_n[0] += 1
    return f"ver{_vid_n[0]}"


# --------------------------------------------------------------- fixtures ---
class _Repo:
    def __init__(self, head=SHA_A):
        self.head = head

    def head_sha(self):
        return self.head

    def file_at(self, sha, path):
        return None


class _Binding:
    """Candidate binding for a specific commit. Mirrors the real protocol."""

    def __init__(self, sha=SHA_A, repo=None, refusals=()):
        self.head_at_binding = sha
        self.repo = repo if repo is not None else _Repo(sha)
        self.refusals = tuple(refusals)
        self.checks = {"HEAD_UNCHANGED_AT_DISPATCH": "PENDING"}

    @property
    def ok(self):
        return not self.refusals

    def to_dict(self):
        return {"candidate_bound": "YES" if self.ok else "NO",
                "git_head_at_binding": self.head_at_binding,
                "checks": dict(self.checks),
                "refusals": [getattr(r, "value", str(r)) for r in self.refusals]}

    def resolve_head_terminal(self):
        now = self.repo.head_sha()
        if now == self.head_at_binding:
            self.checks["HEAD_UNCHANGED_AT_DISPATCH"] = "YES"
            return self, HeadResolution("YES", self.head_at_binding, now, "UNCHANGED")
        self.checks["HEAD_UNCHANGED_AT_DISPATCH"] = "NO"
        return self, HeadResolution("NO", self.head_at_binding, now, "MOVED")


class _Controller:
    """Controller-owned candidate resolution. Stands in for git.

    Deliberately IGNORES the claim and binds whatever is actually checked out.
    That is the production shape: the controller asks version control what the
    candidate is, and the worker's claim is then checked against the answer
    rather than used to produce it."""

    def __init__(self, head=SHA_A):
        self.head = head

    def commit(self, sha):
        """Simulate the repair actually committing a new candidate."""
        self.head = sha

    def __call__(self, claimed_sha):
        return _Binding(self.head)


def _ctx(root: Path, *, binding=None, reviewer=None, binder=None) -> ReviewContext:
    return ReviewContext.open(
        root, mission_id=MISSION, session_id="ew0b",
        reviewer_identity=dict(reviewer or {"provider": "openai", "model": "gpt-4o",
                                            "protocol": "one-shot"}),
        repo=(binding or _Binding()).repo,
        candidate_binding=binding if binding is not None else _Binding(),
        candidate_binder=binder)


class Spy:
    """Injected supervisor. Records every call, so 'never asked' is provable."""

    def __init__(self, *verdicts):
        self.verdicts = list(verdicts) or [SupervisorVerdict.PASS]
        self.calls = 0
        self.packets = []

    def __call__(self, packet):
        self.packets.append(packet)
        v = self.verdicts[min(self.calls, len(self.verdicts) - 1)]
        self.calls += 1
        return SupervisorDecision(v, reasons=[f"call {self.calls}"])


_DIFF = "--- a/tests/tx.py\n+++ b/tests/tx.py\n+assert compute() == 3\n"


def _task(**over):
    d = dict(task_id="t1", title="bounded task", goal="do the bounded thing",
             risk_class=RiskClass.E1_ROUTINE, executor=Executor.ENGINEER,
             mission_id=MISSION, allowed_paths=["tests/"],
             allowed_tests=["tests/tx.py"],
             acceptance_criteria=["compute() returns 3", "no behaviour change elsewhere"],
             requirements=["keep the public signature"], max_attempts=2)
    d.update(over)
    return EngineeringTaskV0(**d)


def _attempt(n=1, *, passing=True, claimed=None, **over):
    d = dict(attempt_id=f"a{n}", executor=Executor.ENGINEER,
             worker_claim="IMPLEMENTATION_COMPLETE", changed_paths=["tests/tx.py"],
             diff_text=_DIFF, tests_run=["tests/tx.py"],
             test_results={"tests/tx.py": "PASS (3 passed)" if passing else "FAIL (1 failed)"},
             py_compile_ok=True, canonical_repo_touched=False,
             claimed_candidate_sha=claimed)
    d.update(over)
    return AttemptEvidence(**d)


# =========================================================================== #
# SCENARIO 1 -- CLEAN SUCCESS (and the worker still cannot self-certify)
# =========================================================================== #
def test_s1_clean_task_certifies_through_independent_review(tmp_path):
    spy = Spy(SupervisorVerdict.PASS)
    r = run_task(_task(), Lvl.A1_ASSISTED_ENGINEERING, POLICY,
                 lambda t, n: _attempt(n), lambda t, v: _attempt(9),
                 spy, _now, _vid, certification=_ctx(tmp_path), roadmap=ROADMAP)
    assert r.final_status == TaskStatus.VERIFIED.value
    assert spy.calls == 1, "exactly one independent review"
    # SHA-bound, attributable, and backed by persisted evidence.
    assert r.candidate_sha == SHA_A
    assert r.execution_identity and r.execution_identity["candidate_sha"] == SHA_A
    assert r.verification["evidence_refs"], "a PASS must name its packet"
    assert r.attempt_lineage[0]["verdict"] == "PASS"


def test_s1_negative_worker_claim_alone_never_certifies(tmp_path):
    """The paired control: identical worker claim, supervisor does not PASS."""
    spy = Spy(SupervisorVerdict.REPAIR)
    r = run_task(_task(max_attempts=1), Lvl.A1_ASSISTED_ENGINEERING,
                 RuntimePolicy(mission_id=MISSION, engineer_attempts_per_task=1,
                               auto_claude_escalation_for_e3_or_exhausted_e2=False),
                 lambda t, n: _attempt(n), lambda t, v: _attempt(9),
                 spy, _now, _vid, certification=_ctx(tmp_path), roadmap=ROADMAP)
    assert r.final_status != TaskStatus.VERIFIED.value
    assert spy.calls == 1, "the claim was judged, not believed"
    assert r.verification["supervisor_verdict"] == "REPAIR"


def test_s1_no_verdict_but_pass_status_is_unrepresentable():
    """status_for_verdict is the ONLY mapping to VERIFIED, and only from PASS."""
    verified = [v for v in VerificationVerdict if status_for_verdict(v) is TaskStatus.VERIFIED]
    assert verified == [VerificationVerdict.PASS]


# =========================================================================== #
# SCENARIO 2 -- ROUTINE DEFECT -> REPAIR -> NEW CANDIDATE -> PASS
# =========================================================================== #
def test_s2_repair_produces_a_new_candidate_sha_and_certifies(tmp_path):
    """Candidate 1 fails; the repair commits a NEW sha and is reviewed afresh.

    Before EW-0B this could not complete at all: run_task reused the context
    binding for every attempt, so the moment a repair actually committed
    anything HEAD moved and dispatch refused. Failing closed was correct, but it
    meant the repair-to-certification path had never been exercised end to end.
    """
    controller = _Controller(SHA_A)
    ctx = _ctx(tmp_path, binder=controller)
    spy = Spy(SupervisorVerdict.REPAIR, SupervisorVerdict.PASS)

    def engineer(task, n):
        # attempt 1 = the defective candidate at SHA_A. The repair really
        # commits, so the CONTROLLER's head moves; the worker only NAMES what
        # it produced and the controller binds what it finds.
        if n == 2:
            controller.commit(SHA_B)
        return _attempt(n, claimed=controller.head)

    r = run_task(_task(), Lvl.A1_ASSISTED_ENGINEERING, POLICY, engineer,
                 lambda t, v: _attempt(9), spy, _now, _vid,
                 certification=ctx, roadmap=ROADMAP)

    assert r.final_status == TaskStatus.VERIFIED.value
    assert r.engineer_attempts == 2 and spy.calls == 2
    # AC4: each candidate has its own SHA and its own review lineage.
    shas = [e["candidate_sha"] for e in r.attempt_lineage]
    assert shas == [SHA_A, SHA_B]
    verdicts = [e["verdict"] for e in r.attempt_lineage]
    assert verdicts == ["REPAIR", "PASS"]
    # The certified candidate is the repaired one, never the first.
    assert r.candidate_sha == SHA_B
    # The packets the reviewer saw name different candidates -- candidate 1's
    # review cannot be read as covering candidate 2.
    assert spy.packets[0]["candidate_sha"] == SHA_A
    assert spy.packets[1]["candidate_sha"] == SHA_B


def test_s2_negative_reusing_the_first_binding_after_a_repair_refuses(tmp_path):
    """Paired control: a repair that presents the STALE binding cannot certify."""
    stale = _Binding(SHA_A, repo=_Repo(SHA_B))     # HEAD moved under the binding
    ctx = _ctx(tmp_path, binding=stale)
    spy = Spy(SupervisorVerdict.PASS)
    v = certify_attempt(_task(), _attempt(2, claimed=SHA_A), spy, _now, "v1",
                        certification=ctx)
    assert v.verdict is VerificationVerdict.SUPERVISOR_UNAVAILABLE
    assert spy.calls == 0, "the reviewer is never asked about a moved candidate"


def test_s2_no_evidence_rerolling_each_attempt_gets_its_own_review_identity(tmp_path):
    """Two attempts, byte-identical evidence, distinct review identities."""
    ctx = _ctx(tmp_path)
    spy = Spy(SupervisorVerdict.REPAIR, SupervisorVerdict.PASS)
    certify_attempt(_task(), _attempt(1), spy, _now, "v1", certification=ctx)
    certify_attempt(_task(), _attempt(2), spy, _now, "v2", certification=ctx)
    events = [json.loads(l) for l in
              (tmp_path / "docs/EW0A_REVIEW_JOURNAL.jsonl").read_text().splitlines()]
    rids = {e["review_invocation_id"] for e in events}
    assert len(rids) == 2, "attempt_id is part of the review identity"
    assert spy.calls == 2, "the second attempt is reviewed, not replayed"


# =========================================================================== #
# SCENARIO 3 -- CI GREEN BUT AN ACCEPTANCE CRITERION IS UNMET
# =========================================================================== #
def test_s3_green_tests_do_not_produce_pass_when_a_criterion_is_unmet(tmp_path):
    """Every test passes; the supervisor still refuses. CI green != mission PASS."""
    ctx = _ctx(tmp_path)
    spy = Spy(SupervisorVerdict.REPAIR)
    v = certify_attempt(_task(), _attempt(1, passing=True), spy, _now, "v1",
                        certification=ctx)
    assert v.tests_ok is True and v.deterministic_ok is True
    assert v.verdict is VerificationVerdict.REPAIR
    assert status_for_verdict(v.verdict) is not TaskStatus.VERIFIED
    # The reviewer was given the criteria it is meant to judge against.
    assert [c["claim"] for c in spy.packets[0]["criteria"]] == \
        _task().acceptance_criteria


def test_s3_a_task_with_no_acceptance_criteria_can_never_pass(tmp_path):
    """Green CI plus nothing to satisfy is agreement about nothing."""
    ctx = _ctx(tmp_path)
    spy = Spy(SupervisorVerdict.PASS)
    v = certify_attempt(_task(acceptance_criteria=[]), _attempt(1), spy, _now, "v1",
                        certification=ctx)
    assert v.verdict is VerificationVerdict.REPAIR
    assert v.failure_class == FailureClass.EVIDENCE_INSUFFICIENT.value
    assert spy.calls == 0
    assert EvidenceRefusal.NO_ACCEPTANCE_CRITERIA.value in \
        v.evidence_assessment["refusals"]


def test_s3_positive_control_criteria_present_and_supported_passes(tmp_path):
    ctx = _ctx(tmp_path)
    spy = Spy(SupervisorVerdict.PASS)
    v = certify_attempt(_task(), _attempt(1), spy, _now, "v1", certification=ctx)
    assert v.verdict is VerificationVerdict.PASS and spy.calls == 1


# =========================================================================== #
# SCENARIO 4 -- WEAK / MANIPULATED EVIDENCE
# =========================================================================== #
@pytest.mark.parametrize("over,expect", [
    # ran nothing at all: every all() check is vacuously satisfied
    (dict(tests_run=[], test_results={}), EvidenceRefusal.NO_TESTS_RUN),
    # claimed completion with nothing changed
    (dict(changed_paths=[], diff_text=""), EvidenceRefusal.NO_CHANGED_PATHS),
    # a changed-file list with no diff behind it
    (dict(diff_text=""), EvidenceRefusal.NO_DIFF_EVIDENCE),
    # a diff that does not substantiate the file it claims to change
    (dict(diff_text="--- a/other.py\n+++ b/other.py\n+pass\n"),
     EvidenceRefusal.CHANGED_PATH_ABSENT_FROM_DIFF),
    # a PASS reported for a test that was never run
    (dict(tests_run=[], test_results={"tests/tx.py": "PASS"}),
     EvidenceRefusal.RESULT_WITHOUT_RUN),
])
def test_s4_weak_evidence_is_refused_before_the_reviewer_is_asked(tmp_path, over, expect):
    ctx = _ctx(tmp_path)
    spy = Spy(SupervisorVerdict.PASS)      # a reviewer that WOULD pass anything
    v = certify_attempt(_task(), _attempt(1, **over), spy, _now, "v1", certification=ctx)
    assert v.verdict is not VerificationVerdict.PASS
    assert expect.value in v.evidence_assessment["refusals"]
    assert spy.calls == 0, (
        "a deficiency that is mechanically decidable must not be outsourced to a "
        "language model, and must not cost a reviewer call")


def test_s4_positive_control_substantive_evidence_reaches_the_reviewer(tmp_path):
    ctx = _ctx(tmp_path)
    spy = Spy(SupervisorVerdict.PASS)
    v = certify_attempt(_task(), _attempt(1), spy, _now, "v1", certification=ctx)
    assert spy.calls == 1 and v.verdict is VerificationVerdict.PASS
    assert v.evidence_assessment["evidence_sufficient"] == "YES"


def test_s4_the_assessment_is_recorded_whether_or_not_it_refused(tmp_path):
    """A check that only leaves a trace when it fires is indistinguishable from
    a check that never ran."""
    ctx = _ctx(tmp_path)
    ok = certify_attempt(_task(), _attempt(1), Spy(SupervisorVerdict.PASS), _now,
                         "v1", certification=ctx)
    bad = certify_attempt(_task(), _attempt(2, diff_text=""), Spy(), _now, "v2",
                          certification=ctx)
    assert ok.evidence_assessment["evidence_sufficient"] == "YES"
    assert bad.evidence_assessment["evidence_sufficient"] == "NO"


def test_s4_evidence_gate_collects_every_deficiency_not_just_the_first():
    a = assess_evidence(_task(acceptance_criteria=[]),
                        _attempt(1, changed_paths=[], diff_text="", tests_run=[],
                                 test_results={"tests/tx.py": "PASS"}))
    assert not a.sufficient
    assert {r.value for r in a.refusals} >= {
        EvidenceRefusal.NO_ACCEPTANCE_CRITERIA.value,
        EvidenceRefusal.NO_CHANGED_PATHS.value,
        EvidenceRefusal.NO_TESTS_RUN.value,
        EvidenceRefusal.RESULT_WITHOUT_RUN.value}


# =========================================================================== #
# SCENARIO 5 -- WRONG ROADMAP PHASE
# =========================================================================== #
def _roadmap_file(root: Path, mission: str) -> Path:
    p = root / ".agent" / "phase_status.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("stockbot_northstar_redesign:\n"
                 "  engineer_runtime_state:\n"
                 f"    mission_id: {mission}\n", encoding="utf-8")
    return p


def test_s5_a_future_roadmap_item_is_refused_even_with_a_consistent_queue(tmp_path):
    """The runtime policy AND the queue both name G1. They agree with each
    other; they disagree with the roadmap, which is the only authority the
    caller does not author."""
    _roadmap_file(tmp_path, MISSION)
    roadmap = RoadmapAuthorization.read(tmp_path)
    assert roadmap.authorized_mission_id == MISSION

    g1_policy = RuntimePolicy(mission_id="g1_supervisor_measurement")
    g1_task = _task(mission_id="g1_supervisor_measurement")

    def must_not_run(*a):
        raise AssertionError("an unauthorized roadmap item must never execute")

    rep = run_mission(g1_policy, [g1_task], Lvl.A1_ASSISTED_ENGINEERING,
                      must_not_run, must_not_run, must_not_run, _now, _vid,
                      certification=_ctx(tmp_path), roadmap=roadmap)
    assert rep.roadmap_violation
    assert rep.tasks_run == [] and rep.verified == 0
    assert rep.stop_reason.startswith(LoopStop.ROADMAP_VIOLATION.value)


def test_s5_positive_control_the_authorized_item_runs(tmp_path):
    _roadmap_file(tmp_path, MISSION)
    roadmap = RoadmapAuthorization.read(tmp_path)
    rep = run_mission(POLICY, [_task()], Lvl.A1_ASSISTED_ENGINEERING,
                      lambda t, n: _attempt(n), lambda t, v: _attempt(9),
                      Spy(SupervisorVerdict.PASS), _now, _vid,
                      certification=_ctx(tmp_path), roadmap=roadmap)
    assert rep.verified == 1 and not rep.roadmap_violation


def test_s5_run_task_is_guarded_too_not_only_the_mission_dispatcher(tmp_path):
    with pytest.raises(RoadmapViolation):
        run_task(_task(mission_id="vertical_slice"), Lvl.A1_ASSISTED_ENGINEERING,
                 RuntimePolicy(mission_id="vertical_slice"),
                 lambda t, n: _attempt(n), lambda t, v: _attempt(9), Spy(),
                 _now, _vid, certification=_ctx(tmp_path), roadmap=ROADMAP)


@pytest.mark.parametrize("body", [
    "",                                                   # empty file
    "not: a: valid: mapping: at: all: [",                 # unparseable
    "stockbot_northstar_redesign: {}\n",                  # key absent
    "stockbot_northstar_redesign:\n  engineer_runtime_state:\n    mission_id: ''\n",
])
def test_s5_every_roadmap_failure_mode_refuses_rather_than_widens(tmp_path, body):
    p = tmp_path / ".agent" / "phase_status.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    roadmap = RoadmapAuthorization.read(tmp_path)
    assert roadmap.authorized_mission_id is None
    with pytest.raises(RoadmapViolation):
        assert_mission_authorized(roadmap, MISSION)


def test_s5_a_missing_roadmap_record_authorizes_nothing(tmp_path):
    roadmap = RoadmapAuthorization.read(tmp_path / "nowhere")
    assert not roadmap.any_mission_authorized
    with pytest.raises(RoadmapViolation):
        assert_mission_authorized(roadmap, MISSION)


def test_s5_drift_guard_the_real_roadmap_and_the_real_runtime_policy_agree():
    """Pins the LIVE files against each other.

    RoadmapAuthorization.for_mission exists for tests; this is what stops it
    from papering over real drift. If the runtime policy is ever pointed at a
    mission the roadmap does not authorize, the operating loop would refuse at
    runtime -- this fails first, in CI, with the two values named."""
    root = Path(__file__).resolve().parents[1]
    roadmap = RoadmapAuthorization.read(root)
    policy = L.read_runtime_policy(root)
    assert policy is not None, "config/ew0a_runtime.json must be readable"
    assert roadmap.authorized_mission_id == policy.mission_id, (
        f"roadmap authorizes {roadmap.authorized_mission_id!r} but the runtime "
        f"policy names {policy.mission_id!r}")


# =========================================================================== #
# SCENARIO 6 -- PROTECTED SCOPE VIOLATION
# =========================================================================== #
@pytest.mark.parametrize("protected", [
    "config/ew0a_authority.json",              # the worker's own authority state
    "config/agent_policy.yaml",                # Northstar authority policy
    ".agent/phase_status.yaml",                # the roadmap record itself
    "portfolio_automation/scoring/registry.py",
    "portfolio_automation/broker/execute.py",
    "portfolio_automation/engineer_worker/ew0a.py",
])
def test_s6_protected_paths_are_blocked_before_the_reviewer(tmp_path, protected):
    ctx = _ctx(tmp_path)
    spy = Spy(SupervisorVerdict.PASS)
    v = certify_attempt(_task(allowed_paths=["."]),
                        _attempt(1, changed_paths=[protected],
                                 diff_text=f"--- a/{protected}\n+++ b/{protected}\n+x\n"),
                        spy, _now, "v1", certification=ctx)
    assert v.verdict is VerificationVerdict.FAIL
    assert v.failure_class == FailureClass.POLICY_VIOLATION.value
    assert v.protected_path_ok is False and spy.calls == 0


def test_s6_the_worker_cannot_edit_the_record_that_authorizes_it(tmp_path):
    """The roadmap guard is only load-bearing while its source stays protected."""
    from portfolio_automation.engineer_worker import policy as pol
    assert pol.is_protected(".agent/phase_status.yaml")
    assert not pol.is_repair_allowed(".agent/phase_status.yaml")
    assert pol.is_protected("config/ew0a_runtime.json")


def test_s6_positive_control_an_in_scope_path_is_allowed(tmp_path):
    ctx = _ctx(tmp_path)
    spy = Spy(SupervisorVerdict.PASS)
    v = certify_attempt(_task(), _attempt(1), spy, _now, "v1", certification=ctx)
    assert v.protected_path_ok and v.scope_ok and v.verdict is VerificationVerdict.PASS


def test_s6_a_policy_violation_stops_rather_than_retrying(tmp_path):
    """Retrying a boundary breach is how a boundary becomes a rate limit."""
    assert action_for_failure(FailureClass.POLICY_VIOLATION) is NextAction.STOP_NO_RETRY
    spy = Spy(SupervisorVerdict.PASS)
    calls = {"n": 0}

    def engineer(task, n):
        calls["n"] += 1
        return _attempt(n, changed_paths=["config/ew0a_authority.json"],
                        diff_text="--- a/config/ew0a_authority.json\n+x\n")

    r = run_task(_task(allowed_paths=["."]), Lvl.A1_ASSISTED_ENGINEERING, POLICY,
                 engineer, lambda t, v: _attempt(9), spy, _now, _vid,
                 certification=_ctx(tmp_path), roadmap=ROADMAP)
    assert calls["n"] == 1, "a protected-path breach is not retried"
    assert r.final_status != TaskStatus.VERIFIED.value


# =========================================================================== #
# SCENARIO 7 -- STALE CANDIDATE SHA
# =========================================================================== #
def test_s7_a_verdict_from_sha_a_cannot_certify_sha_b(tmp_path):
    moved = _Binding(SHA_A, repo=_Repo(SHA_B))
    ctx = _ctx(tmp_path, binding=moved)
    spy = Spy(SupervisorVerdict.PASS)
    v = certify_attempt(_task(), _attempt(1), spy, _now, "v1", certification=ctx)
    assert v.verdict is VerificationVerdict.SUPERVISOR_UNAVAILABLE
    assert spy.calls == 0
    assert any("HEAD_NOT_UNCHANGED_AT_DISPATCH" in str(x) for x in v.supervisor_reasons)


def test_s7_positive_control_the_exact_reviewed_sha_certifies(tmp_path):
    ctx = _ctx(tmp_path, binding=_Binding(SHA_A, repo=_Repo(SHA_A)))
    spy = Spy(SupervisorVerdict.PASS)
    v = certify_attempt(_task(), _attempt(1), spy, _now, "v1", certification=ctx)
    assert v.verdict is VerificationVerdict.PASS and v.candidate_sha == SHA_A


def test_s7_an_unresolvable_head_is_not_reported_as_a_moved_head(tmp_path):
    """A broken checkout and a rebase are different faults; conflating them
    sends a recovering operator after the wrong one."""
    class _Dead(_Repo):
        def head_sha(self):
            return None

    from portfolio_automation.engineer_worker.review_candidate import (
        CandidateBinding, CandidateRefusal)
    real = CandidateBinding(packet_sha=SHA_A, head_at_binding=SHA_A, repo=_Dead())
    out, resolution = real.resolve_head_terminal()
    assert resolution.verdict == "NO"
    assert resolution.resolution_reason == "UNRESOLVABLE"
    assert CandidateRefusal.HEAD_UNRESOLVABLE_AT_DISPATCH in out.refusals


# =========================================================================== #
# SCENARIO 8 -- CANDIDATE MUTATION AFTER REVIEW
# =========================================================================== #
def test_s8_changing_the_candidate_produces_a_different_review_identity(tmp_path):
    """Same task, same criteria, different candidate -> different invocation id,
    so the earlier PASS is not reachable as a recovered verdict for the new one."""
    ctx_a = _ctx(tmp_path / "a", binding=_Binding(SHA_A))
    ctx_b = _ctx(tmp_path / "b", binding=_Binding(SHA_B))
    spy = Spy(SupervisorVerdict.PASS)
    certify_attempt(_task(), _attempt(1, claimed=SHA_A), spy, _now, "v1",
                    certification=ctx_a)
    certify_attempt(_task(), _attempt(1, claimed=SHA_B), spy, _now, "v2",
                    certification=ctx_b)

    def rids(root):
        return {json.loads(l)["review_invocation_id"] for l in
                (root / "docs/EW0A_REVIEW_JOURNAL.jsonl").read_text().splitlines()}

    assert rids(tmp_path / "a").isdisjoint(rids(tmp_path / "b"))
    assert spy.calls == 2, "the mutated candidate got its own independent review"


def test_s8_mutating_the_evidence_changes_the_packet_hash(tmp_path):
    ctx = _ctx(tmp_path)
    spy = Spy(SupervisorVerdict.PASS)
    certify_attempt(_task(), _attempt(1), spy, _now, "v1", certification=ctx)
    certify_attempt(_task(), _attempt(1, diff_text=_DIFF + "+extra\n"), spy, _now,
                    "v2", certification=ctx)
    hashes = {json.loads(l).get("packet_hash") for l in
              (tmp_path / "docs/EW0A_REVIEW_JOURNAL.jsonl").read_text().splitlines()}
    assert len(hashes - {None}) == 2


# =========================================================================== #
# SCENARIO 9 -- DUPLICATE / REPLAYED TASK
# =========================================================================== #
def test_s9_replaying_an_identical_review_reuses_the_verdict_and_does_not_recall(tmp_path):
    """Idempotent recognition, not double certification. The second dispatch
    finds a durable verdict for the same invocation and reuses it."""
    ctx = _ctx(tmp_path)
    spy = Spy(SupervisorVerdict.PASS)
    first = certify_attempt(_task(), _attempt(1), spy, _now, "v1", certification=ctx)
    second = certify_attempt(_task(), _attempt(1), spy, _now, "v2", certification=ctx)
    assert first.verdict is second.verdict is VerificationVerdict.PASS
    assert spy.calls == 1, "one candidate, one independent judgement"


def test_s9_a_duplicate_never_produces_a_second_independent_judgement(tmp_path):
    """Negative framing of the same property: if the replay DID re-call, the
    second (different) verdict would surface. It must not."""
    ctx = _ctx(tmp_path)
    spy = Spy(SupervisorVerdict.PASS, SupervisorVerdict.REPAIR)
    a = certify_attempt(_task(), _attempt(1), spy, _now, "v1", certification=ctx)
    b = certify_attempt(_task(), _attempt(1), spy, _now, "v2", certification=ctx)
    assert spy.calls == 1
    assert a.verdict is b.verdict is VerificationVerdict.PASS


def test_s9_positive_control_a_genuinely_different_attempt_is_reviewed(tmp_path):
    ctx = _ctx(tmp_path)
    spy = Spy(SupervisorVerdict.PASS)
    certify_attempt(_task(), _attempt(1), spy, _now, "v1", certification=ctx)
    certify_attempt(_task(), _attempt(2), spy, _now, "v2", certification=ctx)
    assert spy.calls == 2


# =========================================================================== #
# SCENARIO 10 -- PARTIAL EXECUTION / CRASH RECOVERY
# (process-level crash injection lives in test_ew0a_loop_restart_recovery.py;
#  these cover the recovery DECISIONS that certification depends on)
# =========================================================================== #
def test_s10_a_torn_journal_tail_is_indeterminate_not_absent(tmp_path):
    from portfolio_automation.engineer_worker.review_journal import (
        LifecycleKind, RecoveryState, ReviewJournal)
    j = ReviewJournal(path=tmp_path / "j.jsonl")
    j.append(LifecycleKind.PACKET_BUILT, review_invocation_id="rvi_x")
    with open(j.path, "a", encoding="utf-8") as fh:
        fh.write('{"kind": "ReviewerCall')          # half-written line
    f = j.recover("rvi_x")
    assert f.state is RecoveryState.RECOVERY_INDETERMINATE_FAIL_CLOSED
    assert f.dispatch_permitted is False


def test_s10_an_ambiguous_in_flight_review_never_becomes_certified(tmp_path):
    from portfolio_automation.engineer_worker.review_journal import (
        LifecycleKind, RecoveryState, ReviewJournal)
    j = ReviewJournal(path=tmp_path / "j.jsonl")
    j.append(LifecycleKind.REVIEWER_CALLED, review_invocation_id="rvi_y")
    f = j.recover("rvi_y")
    assert f.state is RecoveryState.RECOVERY_INDETERMINATE_FAIL_CLOSED
    assert f.reviewer_may_have_been_billed and not f.dispatch_permitted
    assert f.verdict is None


def test_s10_positive_control_a_clean_journal_permits_dispatch(tmp_path):
    from portfolio_automation.engineer_worker.review_journal import (
        RecoveryState, ReviewJournal)
    j = ReviewJournal(path=tmp_path / "j.jsonl")
    f = j.recover("rvi_never_seen")
    assert f.state is RecoveryState.NOT_DISPATCHED and f.dispatch_permitted


def test_s10_the_durable_journal_is_authoritative_over_the_return_value(tmp_path):
    """Whatever the process believed, the record is what survives."""
    ctx = _ctx(tmp_path)
    certify_attempt(_task(), _attempt(1), Spy(SupervisorVerdict.PASS), _now, "v1",
                    certification=ctx)
    kinds = [json.loads(l)["kind"] for l in
             (tmp_path / "docs/EW0A_REVIEW_JOURNAL.jsonl").read_text().splitlines()]
    assert kinds.index("ReviewerCalled") < kinds.index("ReviewVerdictReturned")
    assert "ReviewVerdictPersisted" in kinds


# =========================================================================== #
# SCENARIO 11 -- SUPERVISOR UNAVAILABLE
# =========================================================================== #
@pytest.mark.parametrize("boom,label", [
    (TimeoutError("timed out"), "timeout"),
    (PermissionError("401 unauthorized"), "auth"),
    (OSError("connection reset"), "transport"),
])
def test_s11_transport_failures_fail_closed_never_pass(boom, label):
    cfg = SupervisorConfig(key_file=None, model="gpt-4o")

    def transport(body):
        raise boom

    d = review({"task": {"task_id": "t"}}, cfg, _now, transport=transport)
    assert d.verdict is SupervisorVerdict.SUPERVISOR_UNAVAILABLE
    assert not d.is_pass


def test_s11_a_malformed_supervisor_response_is_not_a_pass():
    cfg = SupervisorConfig(key_file=None)
    d = review({"task": {}}, cfg, _now,
               transport=lambda body: {"choices": [{"message": {"content": "sure, looks good"}}]})
    assert d.verdict is SupervisorVerdict.SUPERVISOR_UNAVAILABLE


def test_s11_a_response_claiming_an_unknown_verdict_is_not_a_pass():
    cfg = SupervisorConfig(key_file=None)
    d = review({"task": {}}, cfg, _now, transport=lambda body: {
        "choices": [{"message": {"content": '{"verdict": "APPROVED"}'}}]})
    assert d.verdict is SupervisorVerdict.SUPERVISOR_UNAVAILABLE


def test_s11_the_loop_pauses_and_claude_is_not_substituted_for_gpt(tmp_path):
    claude_ran = {"n": 0}

    def claude(t, v):
        claude_ran["n"] += 1
        return _attempt(9)

    r = run_task(_task(), Lvl.A1_ASSISTED_ENGINEERING, POLICY,
                 lambda t, n: _attempt(n), claude,
                 Spy(SupervisorVerdict.SUPERVISOR_UNAVAILABLE), _now, _vid,
                 certification=_ctx(tmp_path), roadmap=ROADMAP)
    assert r.supervisor_outage and r.final_status != TaskStatus.VERIFIED.value
    assert claude_ran["n"] == 0, (
        "Claude is an engineering escalation tool, never a stand-in for the "
        "independent supervisor")


def test_s11_positive_control_a_working_transport_can_pass():
    cfg = SupervisorConfig(key_file=None)
    d = review({"task": {}}, cfg, _now, transport=lambda body: {
        "model": "gpt-4o",
        "choices": [{"message": {"content": '{"verdict": "PASS", "reasons": ["ok"]}'}}]})
    assert d.verdict is SupervisorVerdict.PASS and d.is_pass


# =========================================================================== #
# SCENARIO 12 -- WORKER FAILURE
# =========================================================================== #
def test_s12_a_worker_exception_does_not_escape_and_does_not_certify(tmp_path):
    def exploding(task, n):
        raise RuntimeError("sandbox died mid-attempt")

    r = run_task(_task(), Lvl.A1_ASSISTED_ENGINEERING, POLICY, exploding,
                 lambda t, v: _attempt(9), Spy(SupervisorVerdict.PASS), _now, _vid,
                 certification=_ctx(tmp_path), roadmap=ROADMAP)
    assert r.final_status != TaskStatus.VERIFIED.value
    assert r.failure_class == FailureClass.WORKER_FAILURE.value
    assert r.worker_failures == 2 and r.worker_unavailable
    assert all(e["failure_class"] == FailureClass.WORKER_FAILURE.value
               for e in r.attempt_lineage)
    assert "RuntimeError" in r.attempt_lineage[0]["worker_error"]


def test_s12_malformed_worker_output_is_a_worker_failure_not_an_empty_attempt(tmp_path):
    """A non-AttemptEvidence return would reach the gate as an object whose
    getattr defaults look like 'changed nothing', which is a quieter hazard
    than an exception."""
    r = run_task(_task(), Lvl.A1_ASSISTED_ENGINEERING, POLICY,
                 lambda t, n: {"worker_claim": "done"},
                 lambda t, v: _attempt(9), Spy(SupervisorVerdict.PASS), _now, _vid,
                 certification=_ctx(tmp_path), roadmap=ROADMAP)
    assert r.final_status != TaskStatus.VERIFIED.value
    assert r.failure_class == FailureClass.WORKER_FAILURE.value
    assert "not AttemptEvidence" in r.attempt_lineage[0]["worker_error"]


def test_s12_a_worker_outage_stops_the_mission_without_corrupting_state(tmp_path):
    def exploding(task, n):
        raise RuntimeError("worker gone")

    log = tmp_path / "outcomes.jsonl"
    rep = run_mission(POLICY, [_task(task_id="a"), _task(task_id="never")],
                      Lvl.A1_ASSISTED_ENGINEERING, exploding,
                      lambda t, v: _attempt(9), Spy(SupervisorVerdict.PASS),
                      _now, _vid, str(log), certification=_ctx(tmp_path),
                      roadmap=ROADMAP)
    assert rep.worker_outage and rep.stop_reason == LoopStop.WORKER_OUTAGE.value
    assert [t["task_id"] for t in rep.tasks_run] == ["a"]
    # the run is still durably recorded -- an exception escaping run_mission
    # would have skipped this entirely
    recs = read_outcomes(str(log))
    assert len(recs) == 1 and recs[0]["final_status"] != TaskStatus.VERIFIED.value


def test_s12_positive_control_a_worker_that_returns_evidence_still_certifies(tmp_path):
    r = run_task(_task(), Lvl.A1_ASSISTED_ENGINEERING, POLICY,
                 lambda t, n: _attempt(n), lambda t, v: _attempt(9),
                 Spy(SupervisorVerdict.PASS), _now, _vid,
                 certification=_ctx(tmp_path), roadmap=ROADMAP)
    assert r.final_status == TaskStatus.VERIFIED.value and r.worker_failures == 0


def test_s12_one_flaky_attempt_does_not_end_the_task(tmp_path):
    """Bounded retry: attempt 1 dies, attempt 2 succeeds."""
    def flaky(task, n):
        if n == 1:
            raise RuntimeError("transient")
        return _attempt(n)

    r = run_task(_task(), Lvl.A1_ASSISTED_ENGINEERING, POLICY, flaky,
                 lambda t, v: _attempt(9), Spy(SupervisorVerdict.PASS), _now, _vid,
                 certification=_ctx(tmp_path), roadmap=ROADMAP)
    assert r.final_status == TaskStatus.VERIFIED.value and r.worker_failures == 1


# =========================================================================== #
# SCENARIO 13 -- REPEATED REPAIR -> CLAUDE ESCALATION
# =========================================================================== #
def test_s13_exhausted_repair_budget_escalates_to_claude_with_lineage(tmp_path):
    ctx = _ctx(tmp_path)
    spy = Spy(SupervisorVerdict.REPAIR, SupervisorVerdict.REPAIR,
              SupervisorVerdict.PASS)
    seen = {}

    def claude(task, last_v):
        seen["handed"] = last_v
        return _attempt(9, executor=Executor.CLAUDE,
                        escalated_from_attempt_id="a2")

    r = run_task(_task(), Lvl.A1_ASSISTED_ENGINEERING, POLICY,
                 lambda t, n: _attempt(n), claude, spy, _now, _vid,
                 certification=ctx, roadmap=ROADMAP)

    assert r.engineer_attempts == 2, "the configured budget, not an invented one"
    assert r.escalated and r.claude_attempts == 1
    assert r.final_status == TaskStatus.VERIFIED.value
    # Claude was handed real evidence, not prose.
    assert seen["handed"].verdict is VerificationVerdict.REPAIR
    # Lineage is machine-readable end to end.
    execs = [e["executor"] for e in r.attempt_lineage]
    assert execs == [Route.ENGINEER.value, Route.ENGINEER.value, Route.CLAUDE.value]
    assert r.attempt_lineage[-1]["escalated_from_attempt_id"] == "a2"
    assert [e["verdict"] for e in r.attempt_lineage] == ["REPAIR", "REPAIR", "PASS"]


def test_s13_the_escalation_budget_comes_from_configuration(tmp_path):
    pol = RuntimePolicy(mission_id=MISSION, engineer_attempts_per_task=1,
                        claude_attempts_per_escalation=3)
    spy = Spy(SupervisorVerdict.REPAIR)
    r = run_task(_task(max_attempts=1), Lvl.A1_ASSISTED_ENGINEERING, pol,
                 lambda t, n: _attempt(n),
                 lambda t, v: _attempt(9, executor=Executor.CLAUDE),
                 spy, _now, _vid, certification=_ctx(tmp_path), roadmap=ROADMAP)
    assert r.engineer_attempts == 1 and r.claude_attempts == 3
    assert r.final_status != TaskStatus.VERIFIED.value


def test_s13_escalation_can_be_disabled_and_then_stops_instead(tmp_path):
    pol = RuntimePolicy(mission_id=MISSION,
                        auto_claude_escalation_for_e3_or_exhausted_e2=False)
    ran = {"n": 0}

    def claude(t, v):
        ran["n"] += 1
        return _attempt(9)

    r = run_task(_task(), Lvl.A1_ASSISTED_ENGINEERING, pol,
                 lambda t, n: _attempt(n), claude, Spy(SupervisorVerdict.REPAIR),
                 _now, _vid, certification=_ctx(tmp_path), roadmap=ROADMAP)
    assert ran["n"] == 0 and r.final_status == TaskStatus.ESCALATION_REQUIRED.value


# =========================================================================== #
# SCENARIO 14 -- TASK BEYOND WORKER AUTHORITY
# =========================================================================== #
def test_s14_capability_is_not_authority(tmp_path):
    """The worker can describe an E4 task perfectly well. It may not run one."""
    r = run_task(_task(risk_class=RiskClass.E4_CONSEQUENTIAL),
                 Lvl.A1_ASSISTED_ENGINEERING, POLICY,
                 lambda t, n: pytest.fail("E4 must never reach an executor"),
                 lambda t, v: pytest.fail("E4 must never reach Claude alone"),
                 Spy(), _now, _vid, certification=_ctx(tmp_path), roadmap=ROADMAP)
    assert r.human_required and r.final_status == TaskStatus.ESCALATION_REQUIRED.value
    assert r.failure_class == "E4_HUMAN_REQUIRED"


def test_s14_a0_cannot_execute_what_a1_can():
    with pytest.raises(AuthorityError):
        route_task(_task(), Lvl.A0_DIAGNOSTIC)
    assert route_task(_task(), Lvl.A1_ASSISTED_ENGINEERING) is Route.ENGINEER


def test_s14_the_worker_cannot_self_elevate_its_risk_class():
    from portfolio_automation.engineer_worker.ew0a import EW0AError
    with pytest.raises(EW0AError):
        assign_executor(RiskClass.E3_HIGH, Executor.ENGINEER)
    assert assign_executor(RiskClass.E1_ROUTINE, Executor.ENGINEER) is Executor.ENGINEER


@pytest.mark.parametrize("op", ["MERGE", "DEPLOY", "CAPITAL_DECISION",
                                "SELF_PROMOTION", "MAIN_WRITE", "BROKER_ACTION",
                                "AUTONOMOUS_PUSH", "PRODUCTION_WRITE"])
def test_s14_forbidden_operations_stay_forbidden_at_a1(op):
    with pytest.raises(AuthorityError):
        assert_operation_allowed(Lvl.A1_ASSISTED_ENGINEERING, op)


def test_s14_ew0b_does_not_enable_any_disabled_authority():
    """AC21 / invariant 4: this mission hardens the envelope, never widens it."""
    root = Path(__file__).resolve().parents[1]
    pol = L.read_runtime_policy(root)
    assert pol.disabled_authorities_ok()
    assert pol.authority == Lvl.A1_ASSISTED_ENGINEERING.value
    assert pol.gpt_supervisor_required is True


# =========================================================================== #
# SCENARIO 15 -- BAD OR MISSING EVIDENCE IN THE REVIEW PACKET
# =========================================================================== #
def test_s15_a_review_without_a_candidate_binding_is_refused(tmp_path):
    ctx = ReviewContext.open(tmp_path, mission_id=MISSION, session_id="s",
                             reviewer_identity={"model": "stub"}, repo=_Repo(),
                             candidate_binding=None)
    spy = Spy(SupervisorVerdict.PASS)
    v = certify_attempt(_task(), _attempt(1), spy, _now, "v1", certification=ctx)
    assert v.verdict is VerificationVerdict.SUPERVISOR_UNAVAILABLE
    assert spy.calls == 0
    assert any("NO_CANDIDATE_BINDING" in str(x) for x in v.supervisor_reasons)


def test_s15_a_refused_binding_is_refused(tmp_path):
    ctx = _ctx(tmp_path, binding=_Binding(SHA_A, refusals=("PACKET_SHA_IS_NOT_HEAD",)))
    spy = Spy(SupervisorVerdict.PASS)
    v = certify_attempt(_task(), _attempt(1), spy, _now, "v1", certification=ctx)
    assert v.verdict is VerificationVerdict.SUPERVISOR_UNAVAILABLE and spy.calls == 0


def test_s15_a_pass_cannot_be_constructed_without_evidence_refs():
    from portfolio_automation.engineer_worker.ew0a import EngineeringVerificationV0
    with pytest.raises(ValueError):
        EngineeringVerificationV0(
            verification_id="v", task_id="t", attempt_id="a",
            verdict=VerificationVerdict.PASS, deterministic_ok=True,
            protected_path_ok=True, scope_ok=True, policy_ok=True, tests_ok=True,
            canonical_repo_untouched=True)


def test_s15_positive_control_a_complete_packet_can_pass(tmp_path):
    ctx = _ctx(tmp_path)
    v = certify_attempt(_task(), _attempt(1), Spy(SupervisorVerdict.PASS), _now,
                        "v1", certification=ctx)
    assert v.verdict is VerificationVerdict.PASS and v.evidence_refs


def test_s15_the_reviewer_receives_every_field_its_instructions_name(tmp_path):
    spy = Spy(SupervisorVerdict.PASS)
    certify_attempt(_task(), _attempt(1), spy, _now, "v1", certification=_ctx(tmp_path))
    packet = spy.packets[0]
    for key in ("requirements", "acceptance_criteria", "diff", "tests_run",
                "test_results", "changed_files", "deterministic_checks",
                "evidence_sufficiency", "candidate_sha", "criteria"):
        assert key in packet, f"the supervisor prompt names {key!r}"


# =========================================================================== #
# SCENARIO 16 -- THE GRADUATION PROOF: FULL INDEPENDENT CHAIN
# =========================================================================== #
def test_s16_fail_repair_new_candidate_verification_independent_pass_durable(tmp_path):
    """The whole chain, once, end to end, on the real operating path.

    deterministic failure -> REPAIR -> corrected candidate at a NEW sha ->
    deterministic verification -> INDEPENDENT review -> PASS -> durable
    certification, with identity and lineage retained at every step.
    """
    controller = _Controller(SHA_A)
    ctx = _ctx(tmp_path, binder=controller)
    spy = Spy(SupervisorVerdict.PASS)          # would pass anything it is shown
    log = tmp_path / "outcomes.jsonl"

    def engineer(task, n):
        # attempt 1 genuinely fails its tests -> deterministic REPAIR, no review
        if n == 1:
            return _attempt(1, passing=False, claimed=controller.head)
        controller.commit(SHA_B)
        return _attempt(2, passing=True, claimed=controller.head)

    rep = run_mission(POLICY, [_task()], Lvl.A1_ASSISTED_ENGINEERING, engineer,
                      lambda t, v: _attempt(9), spy, _now, _vid, str(log),
                      certification=ctx, roadmap=ROADMAP)

    assert rep.verified == 1
    r = rep.tasks_run[0]
    # The failing candidate never reached the reviewer at all.
    assert spy.calls == 1
    assert r["attempt_lineage"][0]["verdict"] == "REPAIR"
    assert r["attempt_lineage"][0]["failure_class"] == FailureClass.TEST_FAILURE.value
    # FINDING C: the candidate that FAILED deterministically still names itself,
    # even though it never reached the supervisor.
    assert r["attempt_lineage"][0]["candidate_sha"] == SHA_A
    assert r["attempt_lineage"][1]["verdict"] == "PASS"
    assert r["candidate_sha"] == SHA_B

    # Durable evidence exists for the certified candidate.
    events = [json.loads(l) for l in
              (tmp_path / "docs/EW0A_REVIEW_JOURNAL.jsonl").read_text().splitlines()]
    kinds = {e["kind"] for e in events}
    assert {"ReviewPacketBuilt", "ReviewPacketPersisted", "ReviewCandidateBound",
            "ReviewDispatchAttempted", "ReviewerCalled", "ReviewVerdictReturned",
            "ReviewVerdictPersisted"} <= kinds
    assert all(e.get("candidate_sha", SHA_B) == SHA_B
               for e in events if "candidate_sha" in e)

    # The apprenticeship record is attributable and machine-readable.
    rec = read_outcomes(str(log))[0]
    assert rec["mission_id"] == MISSION and rec["candidate_sha"] == SHA_B
    assert rec["execution_id"].startswith("exid_")
    assert len(rec["attempt_lineage"]) == 2
    assert rec["final_status"] == TaskStatus.VERIFIED.value


# =========================================================================== #
# EXECUTION IDENTITY -- attributable, and distinguishable when it matters
# =========================================================================== #
def test_identity_is_present_on_every_lifecycle_record_that_names_a_decision(tmp_path):
    ctx = _ctx(tmp_path)
    certify_attempt(_task(), _attempt(1), Spy(SupervisorVerdict.PASS), _now, "v1",
                    certification=ctx)
    events = [json.loads(l) for l in
              (tmp_path / "docs/EW0A_REVIEW_JOURNAL.jsonl").read_text().splitlines()]
    ids = {e["execution_identity"]["execution_id"] for e in events
           if "execution_identity" in e}
    assert len(ids) == 1, "one review, one execution identity"
    carrying = {e["kind"] for e in events if "execution_identity" in e}
    assert {"ReviewPacketBuilt", "ReviewerCalled", "ReviewVerdictPersisted"} <= carrying


def test_identity_schema_and_unknowns_are_explicit(tmp_path):
    ctx = _ctx(tmp_path)
    ident = ctx.execution_identity(candidate_sha=SHA_A, task_id="t1", input_id="pkt_1")
    d = ident.to_dict()
    assert d["schema_version"] == "engineering.execution_identity.v1"
    assert d["candidate_sha"] == SHA_A and d["mission_id"] == MISSION
    assert d["model_provider"] == "openai" and d["model_name"] == "gpt-4o"
    assert d["prompt_version"].startswith("sysprompt-")
    # A chat API does not return the build it served. Saying so beats guessing.
    assert d["model_version"] == UNAVAILABLE
    assert "model_version" in d["unavailable_attributes"]


@pytest.mark.parametrize("change", ["model", "toolset", "candidate", "task", "mission"])
def test_identity_changes_when_material_configuration_changes(tmp_path, change):
    base = _ctx(tmp_path / "base")
    kw = dict(candidate_sha=SHA_A, task_id="t1", input_id="pkt")
    first = base.execution_identity(**kw).execution_id()

    if change == "model":
        other = _ctx(tmp_path / "o", reviewer={"provider": "openai",
                                               "model": "gpt-4o-mini",
                                               "protocol": "one-shot"})
        second = other.execution_identity(**kw).execution_id()
    elif change == "toolset":
        other = _ctx(tmp_path / "o", reviewer={"provider": "openai", "model": "gpt-4o",
                                               "protocol": "one-shot",
                                               "toolset": "gpt.supervisor.v2"})
        second = other.execution_identity(**kw).execution_id()
    elif change == "candidate":
        second = base.execution_identity(**{**kw, "candidate_sha": SHA_B}).execution_id()
    elif change == "task":
        second = base.execution_identity(**{**kw, "task_id": "t2"}).execution_id()
    else:
        other = ReviewContext.open(tmp_path / "m", mission_id="other_mission",
                                   session_id="s",
                                   reviewer_identity={"provider": "openai",
                                                      "model": "gpt-4o",
                                                      "protocol": "one-shot"},
                                   repo=_Repo(), candidate_binding=_Binding())
        second = other.execution_identity(**kw).execution_id()
    assert first != second, f"a {change} change must be distinguishable"


def test_identity_changes_when_the_supervisor_prompt_changes(tmp_path, monkeypatch):
    """The reason prompt_version binds the instruction TEXT and not a protocol
    label: editing the instructions must not produce indistinguishable records."""
    from portfolio_automation.engineer_worker import gpt_supervisor as gs
    ctx = _ctx(tmp_path)
    kw = dict(candidate_sha=SHA_A, task_id="t1", input_id="pkt")
    before = ctx.execution_identity(**kw).execution_id()
    monkeypatch.setattr(gs, "SUPERVISOR_SYSTEM",
                        gs.SUPERVISOR_SYSTEM + " Also weigh test quality.")
    after = ctx.execution_identity(**kw).execution_id()
    assert before != after


def test_identity_is_stable_for_an_unchanged_configuration(tmp_path):
    """Paired control. If identity changed per call, grouping would be
    impossible and every 'it changed' assertion above would be vacuous."""
    ctx = _ctx(tmp_path)
    kw = dict(candidate_sha=SHA_A, task_id="t1", input_id="pkt")
    assert ctx.execution_identity(**kw).execution_id() == \
        ctx.execution_identity(**kw).execution_id()


def test_identity_carries_no_credential_material(tmp_path):
    """A caller may put a key path in the reviewer identity; it must not land in
    the durable record."""
    ctx = _ctx(tmp_path, reviewer={"provider": "openai", "model": "gpt-4o",
                                   "protocol": "one-shot",
                                   "key_file": "/home/pesan/.secrets/openai.key"})
    d = ctx.execution_identity(candidate_sha=SHA_A, task_id="t", input_id="p").to_dict()
    blob = json.dumps(d)
    assert "key_file" not in blob and ".secrets" not in blob


# =========================================================================== #
# APPRENTICESHIP RECORD QUALITY + BACKWARD COMPATIBILITY
# =========================================================================== #
def test_outcome_records_answer_the_questions_g1_will_ask(tmp_path):
    log = tmp_path / "outcomes.jsonl"
    run_mission(POLICY, [_task()], Lvl.A1_ASSISTED_ENGINEERING,
                lambda t, n: _attempt(n), lambda t, v: _attempt(9),
                Spy(SupervisorVerdict.PASS), _now, _vid, str(log),
                certification=_ctx(tmp_path), roadmap=ROADMAP)
    rec = read_outcomes(str(log))[0]
    for key in ("task_id", "mission_id", "candidate_sha", "execution_id",
                "execution_identity", "attempt_lineage", "supervisor_verdict",
                "final_status", "escalated", "disposition", "recorded_at"):
        assert key in rec, key
    assert rec["supervisor_verdict"] == "PASS"


def test_legacy_outcome_records_still_load(tmp_path):
    """AC20. A record written before EW-0B has none of the new fields."""
    log = tmp_path / "legacy.jsonl"
    log.write_text(json.dumps({
        "task_id": "old", "title": "t", "risk_class": "E1_ROUTINE",
        "executor": "ENGINEER", "attempt_count": 1, "failure_classes": [],
        "escalated": False, "supervisor_verdict": "PASS", "final_status": "VERIFIED",
        "tests_run": [], "policy_violation": False, "human_intervention": False,
        "disposition": "PASS", "recorded_at": "2026-08-01T00:00:00Z",
        "schema_version": "engineering.outcome.v0",
        "schema_kind": "experimental_noncanonical"}) + "\n", encoding="utf-8")
    recs = read_outcomes(str(log))
    assert recs[0]["task_id"] == "old"
    rec = OutcomeRecord(**recs[0])
    assert rec.mission_id is None and rec.attempt_lineage == []


def test_legacy_execution_identity_loads_as_unattributed():
    ident = ExecutionIdentity.from_dict(None)
    assert ident.worker_id == "LEGACY_UNATTRIBUTED"
    with pytest.raises(ValueError):
        ExecutionIdentity.from_dict({"schema_version": "engineering.execution_identity.v9"})


# =========================================================================== #
# STATE-MACHINE ASSERTIONS
# =========================================================================== #
@pytest.mark.parametrize("verdict,status", [
    (VerificationVerdict.PASS, TaskStatus.VERIFIED),
    (VerificationVerdict.REPAIR, TaskStatus.REPAIR_REQUIRED),
    (VerificationVerdict.ESCALATE, TaskStatus.ESCALATION_REQUIRED),
    (VerificationVerdict.ABSTAIN, TaskStatus.ABSTAINED),
    (VerificationVerdict.FAIL, TaskStatus.FAILED_VALIDATION),
    (VerificationVerdict.SUPERVISOR_UNAVAILABLE, TaskStatus.VERIFYING),
])
def test_legal_transitions_are_exactly_these(verdict, status):
    assert status_for_verdict(verdict) is status


def test_illegal_transitions_are_unreachable():
    # SUPERVISOR_UNAVAILABLE -> anything terminal-successful
    assert status_for_verdict(VerificationVerdict.SUPERVISOR_UNAVAILABLE) \
        is TaskStatus.VERIFYING
    # a worker claim is a status the orchestrator never treats as success
    assert TaskStatus.IMPLEMENTATION_COMPLETE is not TaskStatus.VERIFIED
    # INTERRUPTED never routes to a retry that could certify
    assert action_for_failure(FailureClass.INTERRUPTED) is NextAction.REMAIN_UNVERIFIED
    assert action_for_failure(FailureClass.EVIDENCE_INSUFFICIENT) \
        is NextAction.RETRY_ENGINEER


def test_every_failure_class_has_a_declared_next_action():
    """Taxonomy completeness: a class with no mapping would raise at runtime,
    inside the loop, at the worst possible moment."""
    for fc in FailureClass:
        assert isinstance(action_for_failure(fc), NextAction)


# =========================================================================== #
# REVIEW FINDING A -- candidate binding is controller-owned, never worker-owned
#
# The first EW-0B candidate let AttemptEvidence carry the binding OBJECT. That
# put behaviour inside worker-produced evidence: a binding whose
# resolve_head_terminal always answered YES would have certified anything, from
# inside the very structure the gate exists to judge. A worker may NAME a
# candidate; only the controller may BIND one.
# =========================================================================== #
class _ForgedBinding:
    """What an attempt would have supplied if it could. It cannot."""

    head_at_binding = "f" * 40
    refusals = ()
    checks = {"HEAD_UNCHANGED_AT_DISPATCH": "YES"}
    repo = None

    @property
    def ok(self):
        return True

    def to_dict(self):
        return {"candidate_bound": "YES", "git_head_at_binding": self.head_at_binding,
                "checks": dict(self.checks), "refusals": []}

    def resolve_head_terminal(self):
        # Always agrees. This is the whole point: a gate whose answer the
        # applicant supplies is not a gate.
        return self, HeadResolution("YES", self.head_at_binding,
                                    self.head_at_binding, "UNCHANGED")


def test_findingA_attempt_evidence_cannot_carry_a_binding_object():
    """Type-level: the field an attempt may set is a STRING, and there is no
    field through which behaviour can be handed to the gate."""
    fields = AttemptEvidence.__dataclass_fields__
    assert "claimed_candidate_sha" in fields
    assert "candidate_binding" not in fields, (
        "a worker-settable binding object is authority, not evidence")
    with pytest.raises(TypeError):
        AttemptEvidence(attempt_id="a", executor=Executor.ENGINEER,
                        worker_claim="done", candidate_binding=_ForgedBinding())


def test_findingA_a_forged_binding_cannot_be_smuggled_in_as_a_claim(tmp_path):
    """Adversarial: the attempt names the forged binding's SHA. The controller
    resolves the real one, they disagree, and nothing is dispatched."""
    controller = _Controller(SHA_A)
    ctx = _ctx(tmp_path, binder=controller)
    spy = Spy(SupervisorVerdict.PASS)          # would pass anything it is shown
    v = certify_attempt(_task(), _attempt(1, claimed=_ForgedBinding.head_at_binding),
                        spy, _now, "v1", certification=ctx)
    assert v.verdict is VerificationVerdict.FAIL
    assert v.failure_class == FailureClass.POLICY_VIOLATION.value
    assert spy.calls == 0
    assert any("may NAME a candidate" in r for r in v.unresolved_requirements)


def test_findingA_a_mismatched_claim_is_refused_without_a_reviewer_call(tmp_path):
    controller = _Controller(SHA_A)
    ctx = _ctx(tmp_path, binder=controller)
    spy = Spy(SupervisorVerdict.PASS)
    v = certify_attempt(_task(), _attempt(1, claimed=SHA_B), spy, _now, "v1",
                        certification=ctx)
    assert v.verdict is VerificationVerdict.FAIL and spy.calls == 0


def test_findingA_a_mis_stated_candidate_is_not_retried(tmp_path):
    """POLICY_VIOLATION, so STOP_NO_RETRY: re-running a worker that mis-states
    its candidate produces another mis-stated candidate."""
    controller = _Controller(SHA_A)
    calls = {"n": 0}

    def engineer(task, n):
        calls["n"] += 1
        return _attempt(n, claimed=SHA_B)

    no_escalation = RuntimePolicy(
        mission_id=MISSION, auto_claude_escalation_for_e3_or_exhausted_e2=False)
    spy = Spy(SupervisorVerdict.PASS)
    r = run_task(_task(), Lvl.A1_ASSISTED_ENGINEERING, no_escalation, engineer,
                 lambda t, v: _attempt(9), spy, _now, _vid,
                 certification=_ctx(tmp_path, binder=controller), roadmap=ROADMAP)
    assert calls["n"] == 1, "the engineer is not re-run on a mis-stated candidate"
    assert r.final_status != TaskStatus.VERIFIED.value
    assert spy.calls == 0, "nothing was certified on the false claim"
    assert r.attempt_lineage[0]["failure_class"] == FailureClass.POLICY_VIOLATION.value


def test_findingA_positive_control_a_truthful_claim_certifies(tmp_path):
    controller = _Controller(SHA_A)
    ctx = _ctx(tmp_path, binder=controller)
    spy = Spy(SupervisorVerdict.PASS)
    v = certify_attempt(_task(), _attempt(1, claimed=SHA_A), spy, _now, "v1",
                        certification=ctx)
    assert v.verdict is VerificationVerdict.PASS and v.candidate_sha == SHA_A
    assert spy.calls == 1


def test_findingA_positive_control_no_claim_uses_the_controller_binding(tmp_path):
    """Backward compatibility: an attempt that names nothing is bound by the
    controller exactly as before."""
    ctx = _ctx(tmp_path)                        # no binder, context binding only
    spy = Spy(SupervisorVerdict.PASS)
    v = certify_attempt(_task(), _attempt(1), spy, _now, "v1", certification=ctx)
    assert v.verdict is VerificationVerdict.PASS and v.candidate_sha == SHA_A


def test_findingA_positive_control_the_repair_path_still_works(tmp_path):
    """The capability the binding field exists for survives the repair: a real
    repair moves the controller's head and certifies at the NEW candidate."""
    controller = _Controller(SHA_A)
    ctx = _ctx(tmp_path, binder=controller)
    spy = Spy(SupervisorVerdict.REPAIR, SupervisorVerdict.PASS)

    def engineer(task, n):
        if n == 2:
            controller.commit(SHA_B)
        return _attempt(n, claimed=controller.head)

    r = run_task(_task(), Lvl.A1_ASSISTED_ENGINEERING, POLICY, engineer,
                 lambda t, v: _attempt(9), spy, _now, _vid,
                 certification=ctx, roadmap=ROADMAP)
    assert r.final_status == TaskStatus.VERIFIED.value
    assert [e["candidate_sha"] for e in r.attempt_lineage] == [SHA_A, SHA_B]


def test_findingA_a_controller_that_cannot_resolve_refuses_rather_than_guesses(tmp_path):
    def broken(claimed):
        raise OSError("git is unavailable")

    ctx = _ctx(tmp_path, binder=broken)
    spy = Spy(SupervisorVerdict.PASS)
    v = certify_attempt(_task(), _attempt(1, claimed=SHA_A), spy, _now, "v1",
                        certification=ctx)
    assert v.verdict is VerificationVerdict.FAIL and spy.calls == 0


# =========================================================================== #
# REVIEW FINDING B -- the production entry point resolves its OWN authorization
#
# run_mission takes a RoadmapAuthorization, which is right for a harness and
# wrong as the only door: a caller able to construct for_mission(x) can
# authorize x. That is the applicant authorizing itself -- the same
# self-consistency the guard was built to break.
# =========================================================================== #
def _repo_state(root: Path, *, roadmap_mission: str | None, policy_mission: str):
    if roadmap_mission is not None:
        _roadmap_file(root, roadmap_mission)
    cfg = root / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "ew0a_runtime.json").write_text(json.dumps({
        "mission_id": policy_mission, "authority": "A1_ASSISTED_ENGINEERING",
        "gpt_supervisor_required": True, "schema_version": "engineering.runtime_policy.v0",
        "schema_kind": "experimental_noncanonical"}), encoding="utf-8")
    return root


def test_findingB_manufactured_authorization_cannot_reach_the_production_entry():
    """There is no parameter to inject one, and the assertion refuses it."""
    import inspect
    sig = inspect.signature(L.run_authorized_mission)
    assert "roadmap" not in sig.parameters, (
        "the production entry point must resolve authorization, not accept it")
    with pytest.raises(RoadmapViolation):
        assert_roadmap_authoritative(RoadmapAuthorization.for_mission(MISSION))


def test_findingB_a_synthetic_authorization_is_marked_non_authoritative(tmp_path):
    synthetic = RoadmapAuthorization.for_mission(MISSION)
    assert synthetic.authoritative is False
    assert synthetic.to_dict()["authoritative"] is False
    _roadmap_file(tmp_path, MISSION)
    real = RoadmapAuthorization.read(tmp_path)
    assert real.authoritative is True and real.to_dict()["authoritative"] is True


def test_findingB_a_policy_naming_a_future_item_is_refused_from_disk(tmp_path):
    """Adversarial: BOTH on-disk files are attacker-shaped except the roadmap.
    The runtime policy names G1; the roadmap does not authorize it."""
    _repo_state(tmp_path, roadmap_mission=MISSION,
                policy_mission="g1_supervisor_measurement")

    def must_not_run(*a):
        raise AssertionError("an unauthorized roadmap item must never execute")

    with pytest.raises(RoadmapViolation) as exc:
        L.run_authorized_mission(
            tmp_path, Lvl.A1_ASSISTED_ENGINEERING,
            [_task(mission_id="g1_supervisor_measurement")],
            must_not_run, must_not_run, must_not_run, _now, _vid,
            certification=_ctx(tmp_path))
    # Raised, not reported: a caller can ignore a report field.
    assert "g1_supervisor_measurement" in str(exc.value)


@pytest.mark.parametrize("roadmap_mission", [None, ""])
def test_findingB_absent_or_idle_roadmap_refuses_at_the_production_entry(
        tmp_path, roadmap_mission):
    if roadmap_mission == "":
        _roadmap_file(tmp_path, "''")
    _repo_state(tmp_path, roadmap_mission=None, policy_mission=MISSION)
    with pytest.raises(RoadmapViolation):
        L.run_authorized_mission(tmp_path, Lvl.A1_ASSISTED_ENGINEERING, [_task()],
                                 lambda t, n: _attempt(n), lambda t, v: _attempt(9),
                                 Spy(), _now, _vid, certification=_ctx(tmp_path))


def test_findingB_an_unreadable_runtime_policy_refuses(tmp_path):
    _roadmap_file(tmp_path, MISSION)
    with pytest.raises(RoadmapViolation):
        L.run_authorized_mission(tmp_path, Lvl.A1_ASSISTED_ENGINEERING, [_task()],
                                 lambda t, n: _attempt(n), lambda t, v: _attempt(9),
                                 Spy(), _now, _vid, certification=_ctx(tmp_path))


def test_findingB_positive_control_agreeing_protected_records_dispatch(tmp_path):
    """Two independent protected records agreeing is not self-consistency."""
    _repo_state(tmp_path, roadmap_mission=MISSION, policy_mission=MISSION)
    rep = L.run_authorized_mission(
        tmp_path, Lvl.A1_ASSISTED_ENGINEERING, [_task()],
        lambda t, n: _attempt(n), lambda t, v: _attempt(9),
        Spy(SupervisorVerdict.PASS), _now, _vid, certification=_ctx(tmp_path))
    assert rep.verified == 1 and not rep.roadmap_violation


def test_findingB_the_harness_path_stays_available_and_isolated(tmp_path):
    """The synthetic path is not removed -- only made unreachable from the
    production entry. A test/harness may still drive run_mission directly."""
    rep = run_mission(POLICY, [_task()], Lvl.A1_ASSISTED_ENGINEERING,
                      lambda t, n: _attempt(n), lambda t, v: _attempt(9),
                      Spy(SupervisorVerdict.PASS), _now, _vid,
                      certification=_ctx(tmp_path),
                      roadmap=RoadmapAuthorization.for_mission(MISSION))
    assert rep.verified == 1


def test_findingB_the_real_repo_resolves_authoritatively():
    root = Path(__file__).resolve().parents[1]
    assert RoadmapAuthorization.read(root).authoritative is True


# =========================================================================== #
# REVIEW FINDING C -- a candidate that fails BEFORE dispatch still names itself
#
# A deterministic failure whose record does not say WHICH candidate failed
# cannot be told apart, when the lineage is read back, from a failure of some
# other candidate.
# =========================================================================== #
@pytest.mark.parametrize("over,task_over,expect_class", [
    (dict(passing=False), {}, FailureClass.TEST_FAILURE.value),
    (dict(changed_paths=["config/ew0a_authority.json"],
          diff_text="--- a/config/ew0a_authority.json\n+x\n"),
     dict(allowed_paths=["."]), FailureClass.POLICY_VIOLATION.value),
    (dict(tests_run=[], test_results={}), {},
     FailureClass.EVIDENCE_INSUFFICIENT.value),
    (dict(abstained=True, abstain_reason="ambiguous"), {},
     FailureClass.AMBIGUOUS_REQUIREMENT.value),
])
def test_findingC_every_pre_dispatch_outcome_names_its_candidate(
        tmp_path, over, task_over, expect_class):
    ctx = _ctx(tmp_path, binder=_Controller(SHA_A))
    spy = Spy(SupervisorVerdict.PASS)
    v = certify_attempt(_task(**task_over), _attempt(1, claimed=SHA_A, **over),
                        spy, _now, "v1", certification=ctx)
    assert v.failure_class == expect_class
    assert v.verdict is not VerificationVerdict.PASS
    assert v.candidate_sha == SHA_A, (
        f"{expect_class} left no candidate on the record; the lineage cannot say "
        "which candidate failed")
    assert spy.calls == 0


def test_findingC_positive_control_a_dispatched_outcome_also_names_it(tmp_path):
    ctx = _ctx(tmp_path, binder=_Controller(SHA_A))
    v = certify_attempt(_task(), _attempt(1, claimed=SHA_A),
                        Spy(SupervisorVerdict.PASS), _now, "v1", certification=ctx)
    assert v.verdict is VerificationVerdict.PASS and v.candidate_sha == SHA_A


def test_findingC_negative_control_an_unresolvable_candidate_is_not_invented(tmp_path):
    """The paired control that stops this becoming 'always stamp something'.
    With nothing to resolve, the record says None rather than a plausible SHA."""
    ctx = ReviewContext.open(tmp_path, mission_id=MISSION, session_id="s",
                             reviewer_identity={"model": "stub"}, repo=_Repo(),
                             candidate_binding=None)
    v = certify_attempt(_task(), _attempt(1, passing=False), Spy(), _now, "v1",
                        certification=ctx)
    assert v.verdict is VerificationVerdict.REPAIR
    assert v.candidate_sha is None


def test_findingC_lineage_carries_the_failed_candidate_through_run_task(tmp_path):
    """End to end on the real loop: the failing attempt's SHA reaches the
    apprenticeship lineage, not just the verification record."""
    controller = _Controller(SHA_A)
    log = tmp_path / "outcomes.jsonl"

    def engineer(task, n):
        if n == 1:
            return _attempt(1, passing=False, claimed=SHA_A)
        controller.commit(SHA_B)
        return _attempt(2, passing=True, claimed=SHA_B)

    rep = run_mission(POLICY, [_task()], Lvl.A1_ASSISTED_ENGINEERING, engineer,
                      lambda t, v: _attempt(9), Spy(SupervisorVerdict.PASS),
                      _now, _vid, str(log),
                      certification=_ctx(tmp_path, binder=controller),
                      roadmap=ROADMAP)
    lineage = read_outcomes(str(log))[0]["attempt_lineage"]
    assert [e["candidate_sha"] for e in lineage] == [SHA_A, SHA_B]
    assert lineage[0]["verdict"] == "REPAIR" and lineage[1]["verdict"] == "PASS"


# =========================================================================== #
# SCENARIO 6 (STRENGTHENED) -- POLICY_VIOLATION IS TERMINAL
#
# action_for_failure(POLICY_VIOLATION) is STOP_NO_RETRY, and that policy was
# only half-applied: the engineer was not retried, but the loop fell through to
# Claude escalation. A protected-path breach therefore spent an escalation, and
# if Claude returned a clean candidate the task could reach VERIFIED -- a run
# whose first act was an authority violation ending in a certification.
#
# Escalation is for work that is legitimately hard. A boundary breach is not
# hard work; it is out of bounds, and handing it to a more capable executor is
# the one response that cannot be right.
# =========================================================================== #
class _CountingClaude:
    """Claude that would happily produce a clean, certifiable candidate.

    That is the point: if the loop escalates a violation, this makes the task
    PASS, and the test fails loudly instead of silently tolerating it."""

    def __init__(self):
        self.calls = 0

    def __call__(self, task, last_v):
        self.calls += 1
        return _attempt(99)


_PROTECTED = "config/ew0a_authority.json"


def _violating(n, kind):
    if kind == "protected":
        return _attempt(n, changed_paths=[_PROTECTED],
                        diff_text=f"--- a/{_PROTECTED}\n+++ b/{_PROTECTED}\n+x\n")
    # out-of-scope: a real, unprotected path that the task never allowed
    return _attempt(n, changed_paths=["portfolio_automation/universe/pick.py"],
                    diff_text="--- a/portfolio_automation/universe/pick.py\n"
                              "+++ b/portfolio_automation/universe/pick.py\n+x\n")


@pytest.mark.parametrize("kind", ["protected", "out_of_scope"])
def test_s6t_a_violation_terminates_the_branch_immediately(tmp_path, kind):
    """The four counters the mission names, in one assertion block."""
    engineer_calls = {"n": 0}
    claude = _CountingClaude()
    spy = Spy(SupervisorVerdict.PASS)      # would certify anything it is shown

    def engineer(task, n):
        engineer_calls["n"] += 1
        return _violating(n, kind)

    task = _task(allowed_paths=["tests/"]) if kind == "out_of_scope" else _task()
    r = run_task(task, Lvl.A1_ASSISTED_ENGINEERING, POLICY, engineer, claude,
                 spy, _now, _vid, certification=_ctx(tmp_path), roadmap=ROADMAP)

    assert engineer_calls["n"] == 1, "the engineer is not retried"
    assert claude.calls == 0, "a boundary breach is never escalated"
    assert spy.calls == 0, "the breach was caught deterministically, before review"
    assert r.final_status != TaskStatus.VERIFIED.value

    # ...and the terminal state is explicit, not merely 'not verified'.
    assert r.final_status == TaskStatus.FAILED_VALIDATION.value
    assert r.policy_violation is True
    assert r.failure_class == FailureClass.POLICY_VIOLATION.value
    assert r.escalated is False and r.claude_attempts == 0
    assert action_for_failure(FailureClass.POLICY_VIOLATION) is NextAction.STOP_NO_RETRY


def test_s6t_the_terminal_status_is_actually_terminal():
    """FAILED_VALIDATION is in the terminal set, so nothing downstream may
    treat this branch as still in flight."""
    from portfolio_automation.engineer_worker.ew0a import _TERMINAL
    assert TaskStatus.FAILED_VALIDATION in _TERMINAL
    assert status_for_verdict(VerificationVerdict.FAIL) is TaskStatus.FAILED_VALIDATION


def test_s6t_a_violating_claude_candidate_is_also_terminal(tmp_path):
    """The rule does not depend on who breached it. Engineer legitimately fails
    twice (REPAIR), escalation is correct, and then Claude breaches -- so the
    branch terminates instead of spending its second Claude attempt."""
    claude_calls = {"n": 0}

    def claude(task, last_v):
        claude_calls["n"] += 1
        return _violating(9, "protected")

    spy = Spy(SupervisorVerdict.REPAIR, SupervisorVerdict.REPAIR)
    r = run_task(_task(), Lvl.A1_ASSISTED_ENGINEERING,
                 RuntimePolicy(mission_id=MISSION, claude_attempts_per_escalation=2),
                 lambda t, n: _attempt(n), claude, spy, _now, _vid,
                 certification=_ctx(tmp_path), roadmap=ROADMAP)
    assert claude_calls["n"] == 1, "Claude does not get a second turn after a breach"
    assert r.policy_violation and r.escalated
    assert r.final_status == TaskStatus.FAILED_VALIDATION.value
    assert spy.calls == 2, "the two legitimate engineer REPAIRs were reviewed"


def test_s6t_the_mission_records_the_violation_and_stops(tmp_path):
    """Recorded, THEN stopped. A stop with no record is indistinguishable from
    a crash; a record with no stop walks on to the next execution path."""
    log = tmp_path / "outcomes.jsonl"
    claude = _CountingClaude()
    rep = run_mission(POLICY, [_task(task_id="breach"), _task(task_id="never")],
                      Lvl.A1_ASSISTED_ENGINEERING,
                      lambda t, n: _violating(n, "protected"), claude,
                      Spy(SupervisorVerdict.PASS), _now, _vid, str(log),
                      certification=_ctx(tmp_path), roadmap=ROADMAP)

    assert rep.policy_violation is True
    assert rep.stop_reason == LoopStop.POLICY_VIOLATION.value
    assert [x["task_id"] for x in rep.tasks_run] == ["breach"]
    assert rep.verified == 0 and claude.calls == 0

    recs = read_outcomes(str(log))
    assert len(recs) == 1
    assert recs[0]["policy_violation"] is True
    assert recs[0]["final_status"] == TaskStatus.FAILED_VALIDATION.value
    assert recs[0]["failure_classes"] == [FailureClass.POLICY_VIOLATION.value]
    # The breaching candidate still names itself (review finding C).
    assert recs[0]["attempt_lineage"][0]["candidate_sha"] == SHA_A


# --- POSITIVE CONTROLS: everything legitimate still escalates ---------------
def test_s6t_positive_an_ordinary_test_failure_still_retries(tmp_path):
    """The paired control that stops this becoming 'nothing ever retries'."""
    calls = {"n": 0}

    def engineer(task, n):
        calls["n"] += 1
        return _attempt(n, passing=(n >= 2))

    r = run_task(_task(), Lvl.A1_ASSISTED_ENGINEERING, POLICY, engineer,
                 _CountingClaude(), Spy(SupervisorVerdict.PASS), _now, _vid,
                 certification=_ctx(tmp_path), roadmap=ROADMAP)
    assert calls["n"] == 2, "a plain test failure is still a bounded retry"
    assert r.final_status == TaskStatus.VERIFIED.value
    assert r.policy_violation is False


def test_s6t_positive_repeated_legitimate_repair_still_escalates(tmp_path):
    """Scenario 13 must still hold: exhausted repair budget reaches Claude."""
    claude = _CountingClaude()
    spy = Spy(SupervisorVerdict.REPAIR, SupervisorVerdict.REPAIR,
              SupervisorVerdict.PASS)
    r = run_task(_task(), Lvl.A1_ASSISTED_ENGINEERING, POLICY,
                 lambda t, n: _attempt(n), claude, spy, _now, _vid,
                 certification=_ctx(tmp_path), roadmap=ROADMAP)
    assert r.engineer_attempts == 2 and claude.calls == 1
    assert r.escalated and r.final_status == TaskStatus.VERIFIED.value
    assert r.policy_violation is False


def test_s6t_positive_an_explicit_escalate_still_escalates(tmp_path):
    claude = _CountingClaude()
    spy = Spy(SupervisorVerdict.ESCALATE, SupervisorVerdict.PASS)
    r = run_task(_task(), Lvl.A1_ASSISTED_ENGINEERING, POLICY,
                 lambda t, n: _attempt(n), claude, spy, _now, _vid,
                 certification=_ctx(tmp_path), roadmap=ROADMAP)
    assert r.engineer_attempts == 1, "ESCALATE does not burn the repair budget"
    assert claude.calls == 1 and r.escalated
    assert r.final_status == TaskStatus.VERIFIED.value


def test_s6t_positive_e3_routing_still_goes_straight_to_claude(tmp_path):
    claude = _CountingClaude()
    r = run_task(_task(risk_class=RiskClass.E3_HIGH), Lvl.A1_ASSISTED_ENGINEERING,
                 POLICY, lambda t, n: pytest.fail("E3 must not reach the engineer"),
                 claude, Spy(SupervisorVerdict.PASS), _now, _vid,
                 certification=_ctx(tmp_path), roadmap=ROADMAP)
    assert r.route == Route.CLAUDE.value and claude.calls == 1
    assert r.final_status == TaskStatus.VERIFIED.value


def test_s6t_positive_the_mission_continues_past_a_legitimate_failure(tmp_path):
    """A REPAIR that never converges stops the mission at the escalation
    boundary, not at a policy boundary -- the two must stay distinguishable."""
    log = tmp_path / "outcomes.jsonl"
    rep = run_mission(POLICY, [_task(task_id="hard")], Lvl.A1_ASSISTED_ENGINEERING,
                      lambda t, n: _attempt(n), lambda t, v: _attempt(9),
                      Spy(SupervisorVerdict.REPAIR), _now, _vid, str(log),
                      certification=_ctx(tmp_path), roadmap=ROADMAP)
    assert rep.policy_violation is False
    assert rep.stop_reason != LoopStop.POLICY_VIOLATION.value
    assert read_outcomes(str(log))[0]["policy_violation"] is False


def test_s6t_a_mis_stated_candidate_is_terminal_too(tmp_path):
    """The binding-claim mismatch from review finding A classifies as
    POLICY_VIOLATION, so it inherits this routing without a second rule."""
    claude = _CountingClaude()
    spy = Spy(SupervisorVerdict.PASS)
    r = run_task(_task(), Lvl.A1_ASSISTED_ENGINEERING, POLICY,
                 lambda t, n: _attempt(n, claimed=SHA_B), claude, spy, _now, _vid,
                 certification=_ctx(tmp_path, binder=_Controller(SHA_A)),
                 roadmap=ROADMAP)
    assert r.policy_violation and claude.calls == 0 and spy.calls == 0
    assert r.final_status == TaskStatus.FAILED_VALIDATION.value


# =========================================================================== #
# DOC DRIFT GUARD
# =========================================================================== #
def test_the_hardening_doc_names_every_evidence_refusal():
    """A document that drifts from the code is worse than no document: it is a
    claim about behaviour that no longer holds."""
    doc = (Path(__file__).resolve().parents[1] / "docs" / "EW0B_HARDENING.md"
           ).read_text(encoding="utf-8")
    for refusal in EvidenceRefusal:
        assert refusal.value in doc, f"{refusal.value} is undocumented"
    assert "EVIDENCE_INSUFFICIENT" in doc
    assert "roadmap_guard" in doc and "experimental_noncanonical" in doc
