"""A review of working-tree content is not certification of a committed candidate.

These tests reproduce the exact Session 2 defect. The packet was assembled by
reading files off disk, ``candidate_sha`` was resolved with ``git rev-parse
HEAD`` BEFORE the work was committed, and that same string was handed back as
``expected_sha``. Both sides of the check came from the caller, so it agreed, and
an independent reviewer returned PASS naming a commit that contained none of the
work.

Every test below constructs that situation — a real base commit, a real
implementation commit, a real packet — rather than asserting a nearby property,
and each asserts the reviewer was called ZERO times. "Did not dispatch" is the
claim; counting calls is what proves it.
"""
from __future__ import annotations

import pytest

from portfolio_automation.engineer_worker.review_candidate import (
    CandidateRefusal, GitRepoView, bind_candidate, file_digest)
from portfolio_automation.engineer_worker.review_packet import (
    Criterion, Evidence, EvidenceKind, ReviewPacket, dispatch_review)

SHA_A = "a" * 40          # base: the session's starting commit
SHA_B = "b" * 40          # the commit that actually contains the implementation
SHA_C = "c" * 40          # HEAD moves here mid-flight

IMPL_PATH = "portfolio_automation/evidence_gateway/revisions.py"
TEST_PATH = "tests/test_evidence_gateway_revisions.py"
IMPL_AT_B = "def resolve_visibility(snapshots, as_of):\n    return 'admitted first'\n"
TEST_AT_B = "def test_late_revision_cannot_leak_backward():\n    assert True\n"


class FakeRepo:
    """A repository with a real history: paths exist at some commits, not others.

    The gate's whole purpose is to refuse commits that lack the work, so the
    fake must be able to represent that — a stub that answers every query
    identically could not fail the way a real repository fails."""

    def __init__(self, head, trees):
        self._head = head
        self._trees = trees          # {sha: {path: content}}
        self.reads = []

    def head_sha(self):
        return self._head

    def file_at(self, sha, path):
        self.reads.append((sha, path))
        return self._trees.get(sha, {}).get(path)

    def move_head_to(self, sha):
        self._head = sha


def _repo(head=SHA_B):
    return FakeRepo(head, {
        SHA_A: {},                                        # base predates the work
        SHA_B: {IMPL_PATH: IMPL_AT_B, TEST_PATH: TEST_AT_B},
        SHA_C: {IMPL_PATH: IMPL_AT_B + "# a later edit\n", TEST_PATH: TEST_AT_B},
    })


def _verification(paths=(IMPL_PATH, TEST_PATH), contents=None, task_id="t1"):
    """A deterministic PASS record carrying the hashes of what it verified."""
    contents = contents or {IMPL_PATH: IMPL_AT_B, TEST_PATH: TEST_AT_B}
    return {"kind": "DeterministicVerification", "task_id": task_id, "result": "PASS",
            "verification_id": "dv-1",
            "verified_files": {p: file_digest(contents[p]) for p in paths}}


def _packet(candidate_sha):
    p = ReviewPacket(candidate_sha=candidate_sha, mission_id="m", task_id="t1",
                     session_id="s1")
    p.add_criterion(Criterion("SAFETY", "PIT admission precedes supersession",
                              (EvidenceKind.SOURCE, EvidenceKind.TEST_SOURCE),
                              ("impl", "tests")))
    p.add_evidence(Evidence("impl", EvidenceKind.SOURCE, content=IMPL_AT_B,
                            source_path=IMPL_PATH))
    p.add_evidence(Evidence("tests", EvidenceKind.TEST_SOURCE, content=TEST_AT_B,
                            source_path=TEST_PATH))
    p.bind("SAFETY", "impl", "tests")
    return p


class Spy:
    def __init__(self):
        self.calls = 0

    def __call__(self, packet):
        self.calls += 1
        return "PASS"


# ── R1 — a base SHA cannot certify an implementation committed later ───────
def test_r1_base_sha_cannot_certify_implementation():
    """THE Session 2 defect, reproduced. The packet names base A while the work
    lives at B. HEAD is B, so the packet is describing a commit that is not
    checked out and does not contain the files."""
    repo = _repo(head=SHA_B)
    packet = _packet(SHA_A)
    spy = Spy()

    binding = bind_candidate(packet, repo, verifications=[_verification()])
    out = dispatch_review(packet, spy, candidate=binding, screen=False)

    assert out.dispatched is False
    assert spy.calls == 0, "the reviewer must never see an unbound candidate"
    assert out.next_action == "REVIEW_NOT_DISPATCHED"
    assert CandidateRefusal.PACKET_SHA_IS_NOT_HEAD in binding.refusals
    assert binding.checks["REVIEW_CANDIDATE_SHA_EQUALS_GIT_HEAD"] == "NO"


def test_r1_the_old_expected_sha_check_would_have_passed_this():
    """Why the previous gate did not catch it: expected_sha compares the caller's
    string to the caller's string, so supplying the base SHA on both sides
    agrees. The binding disagrees because one side comes from git."""
    repo = _repo(head=SHA_B)
    packet = _packet(SHA_A)
    assert packet.candidate_sha == SHA_A          # what expected_sha=SHA_A compares

    binding = bind_candidate(packet, repo, verifications=[_verification()])
    assert binding.ok is False


def test_r1_base_sha_also_lacks_the_task_diff():
    """Independent of the SHA comparison: the files are not there at A."""
    repo = _repo(head=SHA_A)
    packet = _packet(SHA_A)
    spy = Spy()

    binding = bind_candidate(packet, repo, verifications=[_verification()])
    out = dispatch_review(packet, spy, candidate=binding, screen=False)

    assert out.dispatched is False and spy.calls == 0
    assert CandidateRefusal.CANDIDATE_MISSING_TASK_DIFF in binding.refusals
    assert CandidateRefusal.VERIFICATION_NOT_BOUND_TO_CANDIDATE in binding.refusals
    assert binding.checks["REVIEW_CANDIDATE_CONTAINS_TASK_DIFF"] == "NO"


# ── R2 — semantic review may not precede deterministic verification ────────
def test_r2_no_deterministic_pass_refuses_dispatch():
    repo = _repo(head=SHA_B)
    packet = _packet(SHA_B)
    spy = Spy()

    binding = bind_candidate(packet, repo, verifications=[])
    out = dispatch_review(packet, spy, candidate=binding, screen=False)

    assert out.dispatched is False
    assert spy.calls == 0
    assert CandidateRefusal.NO_DETERMINISTIC_PASS in binding.refusals
    assert binding.checks["DETERMINISTIC_VERIFICATION_PASS_EXISTS"] == "NO"


def test_r2_a_failing_verification_is_not_a_pass():
    repo = _repo(head=SHA_B)
    packet = _packet(SHA_B)
    spy = Spy()
    failed = {**_verification(), "result": "FAIL"}

    binding = bind_candidate(packet, repo, verifications=[failed])
    out = dispatch_review(packet, spy, candidate=binding, screen=False)

    assert out.dispatched is False and spy.calls == 0
    assert CandidateRefusal.NO_DETERMINISTIC_PASS in binding.refusals


def test_r2_a_pass_for_a_different_task_does_not_transfer():
    repo = _repo(head=SHA_B)
    packet = _packet(SHA_B)
    spy = Spy()

    binding = bind_candidate(packet, repo,
                             verifications=[_verification(task_id="some-other-task")])
    out = dispatch_review(packet, spy, candidate=binding, screen=False)

    assert out.dispatched is False and spy.calls == 0
    assert CandidateRefusal.NO_DETERMINISTIC_PASS in binding.refusals


def test_r2_a_pass_that_names_no_files_cannot_bind_to_a_commit():
    """A PASS record with no verified_files is an assertion, not a binding: there
    is nothing tying it to any particular tree."""
    repo = _repo(head=SHA_B)
    packet = _packet(SHA_B)
    spy = Spy()
    unbindable = {"kind": "DeterministicVerification", "task_id": "t1",
                  "result": "PASS"}

    binding = bind_candidate(packet, repo, verifications=[unbindable])
    out = dispatch_review(packet, spy, candidate=binding, screen=False)

    assert out.dispatched is False and spy.calls == 0
    assert CandidateRefusal.VERIFICATION_NOT_BOUND_TO_CANDIDATE in binding.refusals


def test_r2_a_pass_that_verified_different_content_does_not_transfer():
    """Verification ran, then the tree changed before the commit. The PASS
    describes the earlier content and must not travel to this commit."""
    repo = _repo(head=SHA_B)
    packet = _packet(SHA_B)
    spy = Spy()
    stale = _verification(contents={IMPL_PATH: "an earlier draft\n",
                                    TEST_PATH: TEST_AT_B})

    binding = bind_candidate(packet, repo, verifications=[stale])
    out = dispatch_review(packet, spy, candidate=binding, screen=False)

    assert out.dispatched is False and spy.calls == 0
    assert CandidateRefusal.VERIFICATION_NOT_BOUND_TO_CANDIDATE in binding.refusals
    assert any("differs from what was verified" in d for d in binding.details)


# ── R3 — HEAD moved between packet construction and dispatch ───────────────
def test_r3_head_moved_after_packet_construction_refuses_dispatch():
    repo = _repo(head=SHA_B)
    packet = _packet(SHA_B)
    spy = Spy()

    binding = bind_candidate(packet, repo, verifications=[_verification()])
    assert binding.ok, "bound cleanly while HEAD was still B"

    repo.move_head_to(SHA_C)          # a commit lands mid-flight
    out = dispatch_review(packet, spy, candidate=binding, screen=False)

    assert out.dispatched is False
    assert spy.calls == 0
    assert out.next_action == "REVIEW_NOT_DISPATCHED"
    assert (CandidateRefusal.HEAD_MOVED_BEFORE_DISPATCH
            in out.candidate_binding.refusals)
    assert out.candidate_binding.checks["HEAD_UNCHANGED_AT_DISPATCH"] == "NO"


def test_r3_the_original_binding_is_not_mutated_by_the_recheck():
    """The recheck returns a new refusal rather than editing the record of what
    was true at binding time."""
    repo = _repo(head=SHA_B)
    packet = _packet(SHA_B)
    binding = bind_candidate(packet, repo, verifications=[_verification()])
    repo.move_head_to(SHA_C)

    moved = binding.recheck_head()

    assert binding.ok is True and moved.ok is False
    assert moved.head_at_binding == SHA_B


# ── R4 — the exact candidate dispatches, exactly once ──────────────────────
def test_r4_exact_candidate_dispatches_exactly_once():
    repo = _repo(head=SHA_B)
    packet = _packet(SHA_B)
    spy = Spy()

    binding = bind_candidate(packet, repo, verifications=[_verification()])
    out = dispatch_review(packet, spy, candidate=binding, screen=False)

    assert binding.ok
    assert out.dispatched is True
    assert spy.calls == 1, "exactly one review of the candidate"
    assert out.candidate_sha == SHA_B
    assert out.next_action == "VERDICT_RECORDED"
    assert binding.checks == {
        "REVIEW_CANDIDATE_SHA_EQUALS_GIT_HEAD": "YES",
        "DETERMINISTIC_VERIFICATION_PASS_EXISTS": "YES",
        "DETERMINISTIC_PASS_BOUND_TO_CANDIDATE": "YES",
        "REVIEW_CANDIDATE_CONTAINS_TASK_DIFF": "YES",
        "EVIDENCE_MATCHES_CANDIDATE": "YES",
        # PENDING is correct HERE: this is the binding-time object, and the
        # freshness question has not been asked yet.
        "HEAD_UNCHANGED_AT_DISPATCH": "PENDING",
    }
    # ...but PENDING was the DEFECT once dispatch had happened. recheck_head()
    # returned `self` unchanged when HEAD was stationary, so the successful path
    # recorded an unresolved freshness check beside candidate_bound=YES -- which
    # is what every binding record in the crashed 0C session actually says. The
    # terminal resolution now answers YES or NO, so a restarted reader can tell
    # "checked and fine" from "never checked".
    assert out.candidate_binding.checks["HEAD_UNCHANGED_AT_DISPATCH"] == "YES"
    assert out.head_resolution.verdict == "YES"
    assert out.head_resolution.resolution_reason == "UNCHANGED"
    assert out.head_resolution.head_at_dispatch == SHA_B
    assert out.to_dict()["reviewer_called"] == "YES"


# ── working tree vs committed candidate ────────────────────────────────────
def test_working_tree_content_is_not_the_committed_candidate():
    """The core rule, tested directly. The packet's SOURCE artifact holds an
    edit that exists on disk but was never committed. Everything else lines
    up — right SHA, right HEAD, real deterministic PASS — and it still refuses,
    because the reviewer would otherwise judge text the candidate does not
    contain."""
    repo = _repo(head=SHA_B)
    packet = _packet(SHA_B)
    packet.evidence["impl"].content = IMPL_AT_B + "# uncommitted local edit\n"
    spy = Spy()

    binding = bind_candidate(packet, repo, verifications=[_verification()])
    out = dispatch_review(packet, spy, candidate=binding, screen=False)

    assert out.dispatched is False and spy.calls == 0
    assert CandidateRefusal.EVIDENCE_NOT_FROM_CANDIDATE in binding.refusals
    assert binding.checks["EVIDENCE_MATCHES_CANDIDATE"] == "NO"
    assert any("built from the working tree" in d for d in binding.details)


def test_evidence_without_a_source_path_is_not_compared():
    """Derived artifacts — a test-result summary, a prose note — have no file to
    compare against, and must not be forced to invent one."""
    repo = _repo(head=SHA_B)
    packet = _packet(SHA_B)
    packet.add_evidence(Evidence("result", EvidenceKind.TEST_RESULT,
                                 content="24 passed"))
    binding = bind_candidate(packet, repo, verifications=[_verification()])
    assert binding.ok


# ── fail-closed behaviour ──────────────────────────────────────────────────
def test_unresolvable_head_refuses_rather_than_assuming():
    repo = FakeRepo(None, {})
    packet = _packet(SHA_B)
    spy = Spy()

    binding = bind_candidate(packet, repo, verifications=[_verification()])
    out = dispatch_review(packet, spy, candidate=binding, screen=False)

    assert out.dispatched is False and spy.calls == 0
    assert CandidateRefusal.HEAD_UNRESOLVABLE in binding.refusals


def test_every_refusal_is_reported_not_just_the_first():
    """A repair should not have to rediscover the next problem one dispatch at
    a time."""
    repo = _repo(head=SHA_A)
    packet = _packet(SHA_B)
    binding = bind_candidate(packet, repo, verifications=[])
    assert {CandidateRefusal.PACKET_SHA_IS_NOT_HEAD,
            CandidateRefusal.NO_DETERMINISTIC_PASS,
            CandidateRefusal.CANDIDATE_MISSING_TASK_DIFF} <= set(binding.refusals)


def test_candidate_argument_is_required_and_has_no_bypass_value():
    """The Session 2 failure was an available check going unused. There is no
    default and no sentinel that skips the gate."""
    packet = _packet(SHA_B)
    with pytest.raises(TypeError):
        dispatch_review(packet, Spy(), screen=False)     # type: ignore[call-arg]


def test_git_repo_view_reports_absence_rather_than_raising(tmp_path):
    """A non-repository must read as 'cannot identify the candidate', which
    fails closed, rather than propagating an exception into the controller."""
    view = GitRepoView(tmp_path)
    assert view.head_sha() is None
    assert view.file_at(SHA_B, IMPL_PATH) is None
