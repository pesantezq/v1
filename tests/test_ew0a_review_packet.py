"""Review-packet completeness gate — fail closed BEFORE the reviewer is called.

An independent reviewer returned REPAIR on a Northstar 0C repair because its
packet lacked the artifacts proving the criteria it was asked to judge. That
refusal was correct, but it relied on the reviewer being careful. A less careful
reviewer would have PASSed an under-evidenced packet and the controller would
have banked a green verdict nobody had earned.

So the property under test throughout is not "the gate notices" — it is:

    an incomplete packet NEVER REACHES the semantic reviewer at all.

Every test below asserts the reviewer was not called, using a spy that records
invocations. A gate that reported incompleteness but still dispatched would pass
a naive assertion and fail these.
"""
from __future__ import annotations

import pytest

from portfolio_automation.engineer_worker.review_packet import (
    Criterion, Evidence, EvidenceKind, PacketError, ReviewPacket, dispatch_review)

SHA = "327a48f36584831b4ba86870093a817d990c908c"


class ReviewerSpy:
    """Records whether the semantic reviewer was invoked, and with what."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, packet: dict) -> str:
        self.calls.append(packet)
        return "PASS"

    @property
    def was_called(self) -> bool:
        return bool(self.calls)


def _packet(**kw) -> ReviewPacket:
    return ReviewPacket(candidate_sha=kw.pop("sha", SHA),
                        mission_id="northstar_0c_pit_evidence_gateway_research_store",
                        task_id="ns0c-repair-2", session_id="ns0c-evgw-foundation-001",
                        **kw)


TAMPER_CRITERION = Criterion(
    criterion_id="C1_TAMPER",
    claim="an actual tampered payload and an actual tampered hash are each refused",
    required_evidence=(EvidenceKind.TEST_SOURCE, EvidenceKind.TEST_RESULT),
    required_artifacts=("tests/test_evidence_gateway_admissibility.py",))

GUI_CRITERION = Criterion(
    criterion_id="C2_GUI",
    claim="the session is visible through the controller-owned dashboard",
    required_evidence=(EvidenceKind.DIFF, EvidenceKind.TEST_SOURCE),
    required_artifacts=("portfolio_automation/engineer_worker/ew0a_readmodels.py",
                        "tests/test_ew0a_readmodels.py"))


def _test_source(path: str) -> Evidence:
    return Evidence(artifact_id=path, kind=EvidenceKind.TEST_SOURCE,
                    content="def test_mutated_payload_content_is_refused():\n"
                            "    object.__setattr__(snap, 'payload_canonical', '{}')\n"
                            "    assert admit(snap, AS_OF).reason is PAYLOAD_HASH_MISMATCH\n")


def _diff(path: str) -> Evidence:
    return Evidence(artifact_id=path, kind=EvidenceKind.DIFF,
                    content="+    dashboard['active_session'] = _build_active_session(root)\n")


def _result() -> Evidence:
    return Evidence(artifact_id="focused-tests", kind=EvidenceKind.TEST_RESULT,
                    detail="50 passed", content="50 passed")


# ── TEST A: required test source absent ────────────────────────────────────
def test_a_missing_test_source_blocks_dispatch():
    spy = ReviewerSpy()
    p = _packet()
    p.add_criterion(TAMPER_CRITERION)
    p.add_evidence(_result())          # counts only — the proving artifact is absent
    out = dispatch_review(p, spy, screen=False)
    assert out.dispatched is False
    assert spy.was_called is False, "reviewer must never see an incomplete packet"
    assert out.completeness.complete is False
    assert out.next_action == "REPAIR_PACKET"
    assert any("tests/test_evidence_gateway_admissibility.py" in g
               for m in out.completeness.missing for g in m["gaps"])


# ── TEST B: read-model diff absent for a GUI criterion ─────────────────────
def test_b_missing_readmodel_diff_blocks_dispatch():
    spy = ReviewerSpy()
    p = _packet()
    p.add_criterion(GUI_CRITERION)
    p.add_evidence(_test_source("tests/test_ew0a_readmodels.py"))
    out = dispatch_review(p, spy, screen=False)
    assert out.dispatched is False and spy.was_called is False
    assert any("ew0a_readmodels.py" in g
               for m in out.completeness.missing for g in m["gaps"])


# ── TEST C: counts supplied where source semantics are required ────────────
def test_c_test_counts_cannot_substitute_for_test_source():
    """The exact substitution that produced the REPAIR: an aggregate count
    describes the proof rather than being it."""
    spy = ReviewerSpy()
    p = _packet()
    p.add_criterion(TAMPER_CRITERION)
    p.add_evidence(Evidence("tests/test_evidence_gateway_admissibility.py",
                            EvidenceKind.TEST_COUNT, detail="50 tests, all passing"))
    out = dispatch_review(p, spy, screen=False)
    assert out.dispatched is False and spy.was_called is False
    assert any("TEST_SOURCE" in g for m in out.completeness.missing for g in m["gaps"])


def test_c2_a_criterion_may_not_declare_only_counts_or_prose():
    """Fail closed at CONSTRUCTION: a criterion provable by counts alone is a
    criterion that cannot really be proven."""
    with pytest.raises(PacketError):
        Criterion(criterion_id="X", claim="something",
                  required_evidence=(EvidenceKind.TEST_COUNT, EvidenceKind.PROSE))


# ── TEST D: everything present ─────────────────────────────────────────────
def test_d_complete_packet_dispatches():
    spy = ReviewerSpy()
    p = _packet()
    p.add_criterion(TAMPER_CRITERION)
    p.add_evidence(_test_source("tests/test_evidence_gateway_admissibility.py"))
    p.add_evidence(_result())
    out = dispatch_review(p, spy, screen=False)
    assert out.dispatched is True
    assert spy.was_called is True
    assert out.completeness.complete is True
    assert out.packet_hash.startswith("pkt_")
    assert "C1_TAMPER" in out.completeness.satisfied
    # the reviewer actually received the proving source, not a summary of it
    sent = spy.calls[0]
    assert any("test_mutated_payload" in f["content"] for f in sent["source_files"])


# ── TEST E: criterion without a manifest entry ─────────────────────────────
def test_e_manifest_mismatch_fails_closed():
    spy = ReviewerSpy()
    p = _packet()
    p.add_criterion(TAMPER_CRITERION)
    p.add_evidence(_test_source("tests/test_evidence_gateway_admissibility.py"))
    p.add_evidence(_result())
    p.manifest = [{"criterion_id": "SOME_OTHER_CRITERION"}]   # bookkeeping error
    out = dispatch_review(p, spy, screen=False)
    assert out.dispatched is False and spy.was_called is False
    assert any(m["criterion_id"] == "MANIFEST" for m in out.completeness.missing)


def test_e2_no_criteria_at_all_fails_closed():
    spy = ReviewerSpy()
    out = dispatch_review(_packet(), spy, screen=False)
    assert out.dispatched is False and spy.was_called is False


# ── TEST F: candidate SHA mismatch ─────────────────────────────────────────
def test_f_candidate_sha_mismatch_fails_closed():
    """A packet built from one tree must not be presented as evidence for another."""
    spy = ReviewerSpy()
    p = _packet(sha="deadbeef" * 5)
    p.add_criterion(TAMPER_CRITERION)
    p.add_evidence(_test_source("tests/test_evidence_gateway_admissibility.py"))
    p.add_evidence(_result())
    out = dispatch_review(p, spy, expected_sha=SHA, screen=False)
    assert out.dispatched is False and spy.was_called is False
    assert any(m["criterion_id"] == "CANDIDATE_SHA" for m in out.completeness.missing)


# ── TEST G: packet hash tracks content ─────────────────────────────────────
def test_g_packet_hash_changes_when_evidence_changes():
    def build(body: str) -> str:
        p = _packet()
        p.add_criterion(TAMPER_CRITERION)
        p.add_evidence(Evidence("tests/test_evidence_gateway_admissibility.py",
                                EvidenceKind.TEST_SOURCE, content=body))
        p.add_evidence(_result())
        return p.packet_hash()

    first = build("def test_x(): assert True\n")
    assert first == build("def test_x(): assert True\n")      # deterministic
    assert first != build("def test_x(): assert False\n")     # content-bound


# ── TEST H: secret-screen refusal blocks dispatch ──────────────────────────
def test_h_secret_screen_refusal_blocks_dispatch():
    """A refused artifact is reported as withheld — never summarized around and
    called equivalent evidence, and never sent by weakening the screen."""
    spy = ReviewerSpy()
    p = _packet()
    p.add_criterion(TAMPER_CRITERION)
    # Assembled at runtime so THIS file carries no literal credential assignment.
    # A stored literal would make this very test file untransmittable to the
    # reviewer — which the gate proved by refusing an earlier draft of this
    # packet. Precision, not suppression: the runtime string is still a real
    # credential shape, so the screen is genuinely exercised.
    credential_shaped = "api" + "_key" + " = " + '"abcd1234efgh5678"'
    p.add_evidence(Evidence("tests/test_evidence_gateway_admissibility.py",
                            EvidenceKind.TEST_SOURCE,
                            content=credential_shaped + "\n"))
    p.add_evidence(_result())
    out = dispatch_review(p, spy, screen=True)
    assert out.dispatched is False and spy.was_called is False
    assert "tests/test_evidence_gateway_admissibility.py" in out.completeness.blocked_by_screen


# ── supporting invariants ──────────────────────────────────────────────────
def test_declared_but_empty_artifact_is_not_evidence():
    spy = ReviewerSpy()
    p = _packet()
    p.add_criterion(TAMPER_CRITERION)
    p.add_evidence(Evidence("tests/test_evidence_gateway_admissibility.py",
                            EvidenceKind.TEST_SOURCE, content="   \n"))
    p.add_evidence(_result())
    out = dispatch_review(p, spy, screen=False)
    assert out.dispatched is False and spy.was_called is False
    assert any("empty" in g for m in out.completeness.missing for g in m["gaps"])


def test_oversized_packet_fails_closed_rather_than_truncating():
    spy = ReviewerSpy()
    p = _packet()
    p.add_criterion(TAMPER_CRITERION)
    p.add_evidence(Evidence("tests/test_evidence_gateway_admissibility.py",
                            EvidenceKind.TEST_SOURCE, content="x" * 400_000))
    p.add_evidence(_result())
    out = dispatch_review(p, spy, screen=False)
    assert out.dispatched is False and spy.was_called is False
    assert any(m["criterion_id"] == "PACKET_SIZE" for m in out.completeness.missing)


def test_multiple_criteria_all_must_be_satisfied():
    spy = ReviewerSpy()
    p = _packet()
    p.add_criterion(TAMPER_CRITERION)
    p.add_criterion(GUI_CRITERION)
    p.add_evidence(_test_source("tests/test_evidence_gateway_admissibility.py"))
    p.add_evidence(_result())
    out = dispatch_review(p, spy, screen=False)      # GUI evidence absent
    assert out.dispatched is False and spy.was_called is False
    assert "C1_TAMPER" in out.completeness.satisfied
    assert any(m["criterion_id"] == "C2_GUI" for m in out.completeness.missing)
