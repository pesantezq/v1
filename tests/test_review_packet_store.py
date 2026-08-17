"""Durable review-packet bytes: identity, idempotence and fail-closed refusal.

The crash of 2026-08-16 left a certification whose reviewer input existed only
as a digest. These tests pin the property that fixes it: the bytes on disk are
the bytes that were hashed and the bytes the reviewer received, and anything
that cannot be proven to be those bytes is refused rather than reconstructed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from portfolio_automation.engineer_worker.review_packet import (
    Criterion, Evidence, EvidenceKind, PacketError, ReviewPacket,
    canonical_packet_bytes, packet_hash_of_bytes,
)
from portfolio_automation.engineer_worker.review_packet_store import (
    BindingFacts, PacketStore, PacketStoreError, StoreRefusal,
)


def _packet(candidate_sha: str = "a" * 40) -> ReviewPacket:
    p = ReviewPacket(candidate_sha=candidate_sha, mission_id="m1",
                     task_id="t1", session_id="s1")
    p.add_criterion(Criterion(criterion_id="C1", claim="the thing holds",
                              required_evidence=(EvidenceKind.SOURCE,)))
    p.add_evidence(Evidence(artifact_id="mod.py", kind=EvidenceKind.SOURCE,
                            content="def f():\n    return 1\n"))
    p.add_evidence(Evidence(artifact_id="r1", kind=EvidenceKind.TEST_RESULT,
                            content="3 passed", detail="3 passed"))
    p.bind("C1", "mod.py")
    return p


def _store(tmp_path: Path) -> PacketStore:
    return PacketStore(repo_root=tmp_path)


# ── identity: the stored bytes ARE the hashed bytes ────────────────────────
def test_persisted_bytes_are_exactly_the_hashed_bytes(tmp_path):
    p = _packet()
    blob = canonical_packet_bytes(p.to_supervisor_packet())
    rec = _store(tmp_path).persist(blob, expected_hash=p.packet_hash(), screened=True)

    on_disk = (tmp_path / rec.store_rel).read_bytes()
    assert on_disk == blob
    assert packet_hash_of_bytes(on_disk) == p.packet_hash()


def test_reloaded_bytes_recompute_the_same_packet_hash(tmp_path):
    p = _packet()
    blob = canonical_packet_bytes(p.to_supervisor_packet())
    store = _store(tmp_path)
    store.persist(blob, expected_hash=p.packet_hash(), screened=True)

    res = store.verify(p.packet_hash())
    assert res.ok
    assert res.blob == blob
    assert packet_hash_of_bytes(res.blob) == p.packet_hash()


def test_reloaded_payload_reserialises_to_identical_bytes(tmp_path):
    """Dict-level round trip is byte-stable, which is what lets dispatch send
    the RELOADED payload rather than a freshly built one."""
    p = _packet()
    p.add_evidence(Evidence(artifact_id="z.py", kind=EvidenceKind.SOURCE,
                            content="# café — non-ascii\n"))
    blob = canonical_packet_bytes(p.to_supervisor_packet())
    assert canonical_packet_bytes(json.loads(blob.decode("utf-8"))) == blob


def test_packet_hash_is_evidence_insertion_order_sensitive():
    """PINNED, not fixed.

    source_files/supporting_evidence are lists in evidence insertion order and
    sort_keys does not sort list elements. This is why reconstruction reloads
    bytes instead of rebuilding a packet. 'Fixing' it by sorting would change
    what every pkt_ digest already recorded in a merged ledger refers to.

    The sensitivity is PER BUCKET: two artifacts of different kinds land in
    different lists, so swapping them changes nothing. It takes two artifacts
    of the SAME kind -- which is the ordinary case for source files."""
    one = Evidence(artifact_id="a.py", kind=EvidenceKind.SOURCE, content="A\n")
    two = Evidence(artifact_id="b.py", kind=EvidenceKind.SOURCE, content="B\n")

    a = ReviewPacket(candidate_sha="a" * 40, mission_id="m1", task_id="t1")
    a.add_evidence(one)
    a.add_evidence(two)

    b = ReviewPacket(candidate_sha="a" * 40, mission_id="m1", task_id="t1")
    b.add_evidence(two)
    b.add_evidence(one)

    assert a.to_supervisor_packet()["source_files"] != \
        b.to_supervisor_packet()["source_files"]
    assert a.packet_hash() != b.packet_hash()


# ── binding lives inside the bytes ─────────────────────────────────────────
def test_binding_facts_are_read_from_the_bytes_not_supplied(tmp_path):
    p = _packet(candidate_sha="b" * 40)
    blob = canonical_packet_bytes(p.to_supervisor_packet())
    rec = _store(tmp_path).persist(blob, expected_hash=p.packet_hash(), screened=True)

    assert rec.binding.candidate_sha == "b" * 40
    assert rec.binding.mission_id == "m1"
    assert rec.binding.task_id == "t1"
    assert rec.binding.criterion_ids == ("C1",)


def test_context_cannot_forge_the_candidate_in_the_persisted_bytes():
    """context is splatted last, so a colliding key would win and the stored
    artifact would name a commit the gate never validated."""
    p = _packet()
    p.context = {"candidate_sha": "SPOOFED"}
    with pytest.raises(PacketError):
        p.to_supervisor_packet()


def test_binding_mismatch_between_bytes_and_expectation_refuses(tmp_path):
    p = _packet()
    blob = canonical_packet_bytes(p.to_supervisor_packet())
    store = _store(tmp_path)
    store.persist(blob, expected_hash=p.packet_hash(), screened=True)

    wrong = BindingFacts(candidate_sha="c" * 40, mission_id="m1", task_id="t1",
                         session_id="s1", criterion_ids=("C1",))
    res = store.verify(p.packet_hash(), expected_binding=wrong)
    assert res.ok is False
    assert StoreRefusal.BINDING_MISMATCH in res.refusals


# ── store semantics ────────────────────────────────────────────────────────
def test_duplicate_write_of_identical_bytes_is_idempotent(tmp_path):
    p = _packet()
    blob = canonical_packet_bytes(p.to_supervisor_packet())
    store = _store(tmp_path)

    first = store.persist(blob, expected_hash=p.packet_hash(), screened=True)
    before = (tmp_path / first.store_rel).stat().st_mtime_ns
    second = store.persist(blob, expected_hash=p.packet_hash(), screened=True)

    assert first.written is True and second.written is False
    assert (tmp_path / first.store_rel).stat().st_mtime_ns == before
    assert (tmp_path / first.store_rel).read_bytes() == blob


def test_same_hash_different_bytes_fails_closed(tmp_path):
    p = _packet()
    blob = canonical_packet_bytes(p.to_supervisor_packet())
    store = _store(tmp_path)
    store.persist(blob, expected_hash=p.packet_hash(), screened=True)

    path = store.path_for(p.packet_hash())
    os.chmod(path, 0o644)
    path.write_bytes(b'{"different":true}')

    with pytest.raises(PacketStoreError) as exc:
        store.persist(blob, expected_hash=p.packet_hash(), screened=True)
    assert exc.value.refusal is StoreRefusal.CONTENT_COLLISION
    assert path.read_bytes() == b'{"different":true}', "existing artifact untouched"


def test_persisted_artifact_is_read_only(tmp_path):
    p = _packet()
    blob = canonical_packet_bytes(p.to_supervisor_packet())
    rec = _store(tmp_path).persist(blob, expected_hash=p.packet_hash(), screened=True)
    mode = (tmp_path / rec.store_rel).stat().st_mode & 0o777
    assert mode == 0o444


def test_no_temp_files_remain_after_a_successful_write(tmp_path):
    p = _packet()
    blob = canonical_packet_bytes(p.to_supervisor_packet())
    rec = _store(tmp_path).persist(blob, expected_hash=p.packet_hash(), screened=True)
    leftovers = [q.name for q in (tmp_path / rec.store_rel).parent.iterdir()
                 if q.name.endswith(".tmp")]
    assert leftovers == []


def test_caller_hash_disagreeing_with_the_bytes_is_refused(tmp_path):
    p = _packet()
    blob = canonical_packet_bytes(p.to_supervisor_packet())
    with pytest.raises(PacketStoreError) as exc:
        _store(tmp_path).persist(blob, expected_hash="pkt_" + "0" * 32, screened=True)
    assert exc.value.refusal is StoreRefusal.HASH_MISMATCH


# ── the screen must precede persistence, structurally ──────────────────────
def test_persistence_without_screening_is_structurally_refused(tmp_path):
    """A credential written into a read-only, content-addressed artifact cannot
    be cleaned up. Screening is therefore a precondition, not a policy."""
    p = _packet()
    blob = canonical_packet_bytes(p.to_supervisor_packet())
    store = _store(tmp_path)
    with pytest.raises(PacketStoreError) as exc:
        store.persist(blob, expected_hash=p.packet_hash(), screened=False)
    assert exc.value.refusal is StoreRefusal.NOT_SCREENED
    assert not (tmp_path / store.store_rel).exists(), "nothing written"


# ── fail closed on every damaged-artifact shape ────────────────────────────
def test_absent_artifact_verifies_as_refusal_not_pass(tmp_path):
    res = _store(tmp_path).verify("pkt_" + "a" * 32)
    assert res.ok is False
    assert StoreRefusal.ABSENT in res.refusals


def test_corrupted_bytes_fail_hash_verification_and_are_not_repaired(tmp_path):
    p = _packet()
    blob = canonical_packet_bytes(p.to_supervisor_packet())
    store = _store(tmp_path)
    store.persist(blob, expected_hash=p.packet_hash(), screened=True)

    path = store.path_for(p.packet_hash())
    os.chmod(path, 0o644)
    mutated = blob.replace(b"return 1", b"return 2")
    assert mutated != blob
    path.write_bytes(mutated)

    res = store.verify(p.packet_hash())
    assert res.ok is False
    assert StoreRefusal.HASH_MISMATCH in res.refusals
    assert path.read_bytes() == mutated, "a mismatch is reported, never laundered"


def test_truncated_artifact_is_a_hash_mismatch_not_a_parse_excuse(tmp_path):
    """Crash-realistic damage. Integrity is decided by hashing, so a mutation
    that leaves valid JSON is caught just as surely as a torn file."""
    p = _packet()
    blob = canonical_packet_bytes(p.to_supervisor_packet())
    store = _store(tmp_path)
    store.persist(blob, expected_hash=p.packet_hash(), screened=True)

    path = store.path_for(p.packet_hash())
    os.chmod(path, 0o644)
    path.write_bytes(blob[: len(blob) // 2])

    res = store.verify(p.packet_hash())
    assert res.ok is False
    assert StoreRefusal.HASH_MISMATCH in res.refusals


def test_valid_json_mutation_is_still_caught(tmp_path):
    p = _packet()
    blob = canonical_packet_bytes(p.to_supervisor_packet())
    store = _store(tmp_path)
    store.persist(blob, expected_hash=p.packet_hash(), screened=True)

    payload = json.loads(blob.decode("utf-8"))
    payload["candidate_sha"] = "f" * 40
    path = store.path_for(p.packet_hash())
    os.chmod(path, 0o644)
    path.write_bytes(canonical_packet_bytes(payload))

    res = store.verify(p.packet_hash())
    assert res.ok is False, "still valid JSON, still not the reviewed artifact"
    assert StoreRefusal.HASH_MISMATCH in res.refusals


def test_verify_returns_a_refusal_rather_than_raising(tmp_path):
    """Refusals are data. A caller cannot swallow one with a bare except and
    carry on as though verification had succeeded."""
    res = _store(tmp_path).verify("pkt_" + "b" * 32)
    assert res.ok is False
    assert res.to_dict()["packet_verified"] is False


# ── index carries digests, never content ───────────────────────────────────
def test_index_row_carries_no_evidence_text(tmp_path):
    p = _packet()
    blob = canonical_packet_bytes(p.to_supervisor_packet())
    store = _store(tmp_path)
    rec = store.persist(blob, expected_hash=p.packet_hash(), screened=True)
    store.append_index(rec.to_dict())

    text = (tmp_path / store.index_rel).read_text(encoding="utf-8")
    assert p.packet_hash() in text
    assert "def f()" not in text, "the index must not become a second copy"
