"""The Research Store persists evidence. It does not decide what evidence means.

These tests pin the boundary as hard as the behaviour: no as_of, no winner, no
ranking, no silent deduplication of disagreeing sources, and no reconstruction
of anything that cannot prove it is what was stored.
"""
from __future__ import annotations

import ast
import inspect
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from portfolio_automation.evidence_gateway.revisions import resolve_visibility
from portfolio_automation.northstar.canonical import canonical_dumps
from portfolio_automation.northstar.evidence import EvidenceRef, EvidenceSnapshot
from portfolio_automation.northstar.pit import PointInTime
from portfolio_automation.northstar.provenance import Provenance
from portfolio_automation.research_store import (
    GetResult, ResearchStore, ResearchStoreError, StoreRefusal,
)
from portfolio_automation.research_store import store as store_module

REPO = Path(__file__).resolve().parents[1]
STORE_SRC = Path(store_module.__file__)

SRC_A = "src_" + "a" * 32
SRC_B = "src_" + "b" * 32
JAN10 = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
JAN15 = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
FEB20 = datetime(2026, 2, 20, 12, 0, tzinfo=timezone.utc)


def _pit(known_at=JAN10, retrieved_at=None):
    return PointInTime(known_at=known_at, known_at_basis="source_reported",
                       retrieved_at=retrieved_at)


def _snap(payload, *, known_at=JAN10, src=SRC_A, entity="AAPL",
          etype="fundamental.revenue", supersedes=None, retrieved_at=None):
    return EvidenceSnapshot(
        source_id=src, entity_id=entity, entity_type="company", evidence_type=etype,
        pit=_pit(known_at, retrieved_at),
        provenance=Provenance(producer_id="adapter.test", producer_type="source_adapter",
                              recorded_at=known_at, source_id=src),
        supersedes_snapshot_id=supersedes, payload=payload)


def _store(tmp_path, name="evidence.db") -> ResearchStore:
    return ResearchStore(db_path=tmp_path / name)


def _overwrite(store: ResearchStore, sid: str, blob: bytes) -> None:
    """Simulate disk/DB tampering. Not part of the public surface."""
    import hashlib
    store._conn.execute(
        "UPDATE evidence_snapshots SET canonical = ?, record_sha256 = ? "
        "WHERE snapshot_id = ?", (blob, hashlib.sha256(blob).hexdigest(), sid))


# ── RS1 canonical round trip ───────────────────────────────────────────────
def test_stored_evidence_round_trips_byte_identically(tmp_path):
    store = _store(tmp_path)
    a = _snap({"revenue": 100, "currency": "USD"}, retrieved_at=FEB20)
    store.put(a)

    got = store.get(a.snapshot_id).snapshot
    assert got.to_json() == a.to_json()
    assert got.snapshot_id == a.snapshot_id
    assert got.payload_hash == a.payload_hash
    assert got.payload_copy() == a.payload_copy()
    assert got.supersedes_snapshot_id == a.supersedes_snapshot_id
    # Non-identity metadata survives too. Identity equality alone would hide
    # its loss, and the acquisition audit trail is exactly what would vanish.
    assert got.pit.retrieved_at == a.pit.retrieved_at
    assert got.provenance.recorded_at == a.provenance.recorded_at


def test_stored_bytes_are_the_exact_canonical_serialization(tmp_path):
    store = _store(tmp_path)
    a = _snap({"revenue": 100})
    store.put(a)
    assert store.raw_bytes(a.snapshot_id) == a.to_json().encode("utf-8")


# ── RS2 immutability ───────────────────────────────────────────────────────
def test_identical_bytes_are_idempotent_and_do_not_rewrite(tmp_path):
    store = _store(tmp_path)
    a = _snap({"revenue": 100})

    first = store.put(a)
    second = store.put(a)

    assert first.written is True and second.written is False
    assert store.snapshot_ids() == (a.snapshot_id,), "one identity, one record"
    assert store.raw_bytes(a.snapshot_id) == a.to_json().encode("utf-8")


def test_different_bytes_under_the_same_identity_fail_closed(tmp_path):
    """RS2. Re-acquisition legitimately produces the same snapshot_id with
    different bytes, because retrieved_at is excluded from identity. The store
    cannot choose between two acquisition envelopes without inventing a policy,
    and silently discarding one would rewrite acquisition history."""
    store = _store(tmp_path)
    r1 = _snap({"revenue": 100}, retrieved_at=JAN15)
    r2 = _snap({"revenue": 100}, retrieved_at=FEB20)
    assert r1.snapshot_id == r2.snapshot_id
    assert r1.to_json() != r2.to_json()

    store.put(r1)
    original = store.raw_bytes(r1.snapshot_id)
    with pytest.raises(ResearchStoreError) as exc:
        store.put(r2)

    assert exc.value.refusal is StoreRefusal.IDENTITY_CONTENT_CONFLICT
    assert store.raw_bytes(r1.snapshot_id) == original, "stored evidence untouched"


def test_the_module_contains_no_update_or_delete_or_upsert():
    """OR IGNORE silently drops a divergent record and reports success; OR
    REPLACE destroys evidence. Both exist elsewhere in the repo for mutable
    cached state and would be copied by reflex."""
    src = STORE_SRC.read_text(encoding="utf-8")
    body = "\n".join(ln for ln in src.splitlines()
                     if not ln.strip().startswith("#"))
    for banned in ("OR REPLACE", "OR IGNORE", "UPDATE evidence_snapshots",
                   "DELETE FROM"):
        assert banned not in body, f"{banned} must never appear in an evidence store"


# ── RS3 / RS11 corruption fails closed ─────────────────────────────────────
def test_tampered_payload_is_refused_and_not_reconstructed(tmp_path):
    store = _store(tmp_path)
    a = _snap({"revenue": 100})
    store.put(a)

    doc = json.loads(a.to_json())
    doc["payload"]["revenue"] = 999
    _overwrite(store, a.snapshot_id, canonical_dumps(doc).encode("utf-8"))

    res = store.get(a.snapshot_id)
    assert res.ok is False
    assert res.snapshot is None, "a refusal must not hand back a usable object"
    assert StoreRefusal.PAYLOAD_HASH_MISMATCH in res.refusals


def test_a_coherent_forgery_is_caught_by_the_record_key(tmp_path):
    """The check only a STORE can make.

    Swap the stored bytes for a DIFFERENT but legitimately built snapshot: its
    payload, payload_hash and snapshot_id all agree with each other, so every
    intra-object check passes and admit() cannot catch it -- a bare snapshot has
    no stored counterpart to disagree with. The record key is that counterpart."""
    store = _store(tmp_path)
    a = _snap({"revenue": 100})
    other = _snap({"revenue": 999})
    assert a.snapshot_id != other.snapshot_id
    store.put(a)
    _overwrite(store, a.snapshot_id, other.to_json().encode("utf-8"))

    res = store.get(a.snapshot_id)
    assert res.ok is False
    assert StoreRefusal.SNAPSHOT_ID_MISMATCH in res.refusals
    assert res.snapshot is None, "internally consistent is not authentic"


@pytest.mark.parametrize("damage", [
    lambda b: b[: len(b) // 2],
    lambda b: b + b"}",
    lambda b: b"",
    lambda b: b"\x00" * 64,
    lambda b: b'{"contract_type": "review_packet"}',
])
def test_malformed_stored_record_is_refused_not_partially_parsed(tmp_path, damage):
    store = _store(tmp_path)
    a = _snap({"revenue": 100})
    store.put(a)
    _overwrite(store, a.snapshot_id, damage(a.to_json().encode("utf-8")))

    res = store.get(a.snapshot_id)
    assert res.ok is False
    assert res.snapshot is None
    assert StoreRefusal.NOT_FOUND not in res.refusals, (
        "damaged is not absent; an auditor counting data loss must be able to "
        "tell a destroyed record from one never written")
    assert res.found is True


def test_bit_rot_that_still_parses_is_caught_by_the_record_digest(tmp_path):
    store = _store(tmp_path)
    a = _snap({"revenue": 100})
    store.put(a)
    store._conn.execute(
        "UPDATE evidence_snapshots SET record_sha256 = ? WHERE snapshot_id = ?",
        ("0" * 64, a.snapshot_id))

    res = store.get(a.snapshot_id)
    assert res.ok is False
    assert StoreRefusal.RECORD_DIGEST_MISMATCH in res.refusals


def test_a_corpus_sweep_refuses_rather_than_skipping_bad_evidence(tmp_path):
    """Silently skipping a corrupt row is how destroyed evidence becomes
    'we never had it'."""
    store = _store(tmp_path)
    a = _snap({"revenue": 100})
    b = _snap({"revenue": 111})
    store.put(a)
    store.put(b)
    _overwrite(store, a.snapshot_id, b"garbage")

    with pytest.raises(ResearchStoreError):
        store.query(entity_id="AAPL")


# ── RS4 missing is explicit ────────────────────────────────────────────────
def test_absent_snapshot_is_an_explicit_refusal_not_an_empty_artifact(tmp_path):
    res = _store(tmp_path).get("evs_" + "9" * 32)
    assert res.ok is False
    assert res.snapshot is None
    assert StoreRefusal.NOT_FOUND in res.refusals
    assert res.to_dict()["found"] is False
    assert not hasattr(res.snapshot, "payload_copy")


# ── RS5 EvidenceRef ────────────────────────────────────────────────────────
def test_correct_ref_resolves_and_matches(tmp_path):
    store = _store(tmp_path)
    a = _snap({"revenue": 100})
    store.put(a)
    res = store.get_by_ref(a.ref())
    assert res.ok is True
    assert a.ref().matches(res.snapshot) is True


def test_get_by_ref_is_genuinely_discriminating(tmp_path):
    store = _store(tmp_path)
    a, b = _snap({"revenue": 100}), _snap({"revenue": 111})
    store.put(a)
    store.put(b)
    assert store.get_by_ref(b.ref()).snapshot.snapshot_id == b.snapshot_id


def test_ref_with_correct_identity_but_lying_routing_fields_is_refused(tmp_path):
    """EvidenceRef.matches compares only snapshot_id and payload_hash, so a ref
    can name the wrong entity and still satisfy it. The store holds the routing
    columns, so the store is the layer that can catch it."""
    store = _store(tmp_path)
    a = _snap({"revenue": 100})
    store.put(a)
    liar = EvidenceRef(snapshot_id=a.snapshot_id, source_id=SRC_A, entity_id="MSFT",
                       evidence_type="fundamental.revenue", payload_hash=a.payload_hash)
    assert liar.matches(a) is True, "the contract's own check passes -- that is the point"

    res = store.get_by_ref(liar)
    assert res.ok is False
    assert StoreRefusal.REF_DOES_NOT_MATCH in res.refusals


def test_ref_for_an_absent_snapshot_fails_closed(tmp_path):
    store = _store(tmp_path)
    a = _snap({"revenue": 100})
    res = store.get_by_ref(a.ref())
    assert res.ok is False
    assert StoreRefusal.NOT_FOUND in res.refusals


# ── RS6 / RS13 deterministic query ─────────────────────────────────────────
def test_query_order_is_independent_of_insertion_order(tmp_path):
    """Two stores, same snapshots, opposite insertion orders. Comparing one
    store against itself would only prove determinism, not that ordering is
    content-derived."""
    snaps = [_snap({"revenue": v}, known_at=JAN10 + timedelta(days=i))
             for i, v in enumerate((100, 111, 122, 133))]

    fwd = _store(tmp_path, "fwd.db")
    for s in snaps:
        fwd.put(s)
    rev = _store(tmp_path, "rev.db")
    for s in reversed(snaps):
        rev.put(s)

    a = [s.snapshot_id for s in fwd.query(entity_id="AAPL")]
    b = [s.snapshot_id for s in rev.query(entity_id="AAPL")]
    assert a == b, "insertion order must not survive into query output"
    assert a == sorted(a), "ordering is content-derived, not arrival-derived"


def test_interleaved_insertion_gives_the_same_order(tmp_path):
    """Two orders can agree by luck; a third settles it."""
    snaps = [_snap({"revenue": v}) for v in (100, 111, 122, 133)]
    mixed = _store(tmp_path, "mixed.db")
    for i in (2, 0, 3, 1):
        mixed.put(snaps[i])
    assert [s.snapshot_id for s in mixed.query(entity_id="AAPL")] == \
        sorted(s.snapshot_id for s in snaps)


def test_query_filters_on_every_supported_dimension(tmp_path):
    store = _store(tmp_path)
    a = _snap({"revenue": 100}, src=SRC_A, entity="AAPL")
    b = _snap({"revenue": 111}, src=SRC_B, entity="MSFT", etype="fundamental.eps")
    store.put(a)
    store.put(b)

    assert [s.snapshot_id for s in store.query(source_id=SRC_B)] == [b.snapshot_id]
    assert [s.snapshot_id for s in store.query(entity_id="AAPL")] == [a.snapshot_id]
    assert [s.snapshot_id for s in store.query(evidence_type="fundamental.eps")] == \
        [b.snapshot_id]
    assert len(store.query(entity_type="company")) == 2
    assert len(store.query()) == 2


# ── RS7 / RS8 no winner, coexistence ───────────────────────────────────────
def test_independent_sources_for_the_same_entity_both_persist(tmp_path):
    """Multi-source coexistence is deliberate. An upsert keyed on
    (entity_id, evidence_type) would make the last-scraped provider win and
    permanently erase the disagreement."""
    store = _store(tmp_path)
    a = _snap({"revenue": 100}, src=SRC_A)
    b = _snap({"revenue": 108}, src=SRC_B)
    store.put(a)
    store.put(b)

    rows = store.query(entity_id="AAPL", evidence_type="fundamental.revenue")
    assert {s.snapshot_id for s in rows} == {a.snapshot_id, b.snapshot_id}
    assert {s.source_id for s in rows} == {SRC_A, SRC_B}


def test_a_snapshot_and_its_revision_both_persist_with_no_winner(tmp_path):
    store = _store(tmp_path)
    a = _snap({"revenue": 100}, known_at=JAN10)
    b = _snap({"revenue": 111}, known_at=FEB20, supersedes=a.snapshot_id)
    store.put(a)
    store.put(b)

    rows = store.query(entity_id="AAPL", evidence_type="fundamental.revenue")
    assert {s.snapshot_id for s in rows} == {a.snapshot_id, b.snapshot_id}
    assert store.get(a.snapshot_id).ok is True, (
        "a superseded snapshot stays retrievable -- supersession is annotation, "
        "not deletion")


def test_the_store_adds_no_supersession_verdict_to_a_stored_snapshot(tmp_path):
    """Whether A is superseded depends on as_of. A stored field would freeze one
    moment's answer as if it were timeless."""
    store = _store(tmp_path)
    a = _snap({"revenue": 100})
    store.put(a)
    back = store.get(a.snapshot_id).snapshot
    for banned in ("superseded", "is_superseded", "superseded_by", "is_current",
                   "active", "rank"):
        assert not hasattr(back, banned)
    assert back.to_json() == a.to_json(), "byte-identical; the store added nothing"


def test_no_public_name_or_parameter_implies_a_winner_or_a_time_view():
    banned = ("latest", "current", "newest", "best", "preferred", "winner",
              "effective", "active", "head", "tip", "as_of", "point_in_time",
              "admissib", "visible")
    for name in dir(ResearchStore):
        if name.startswith("_"):
            continue
        assert not any(b in name.lower() for b in banned), \
            f"winner or time semantics leaked into the name {name}"
        attr = getattr(ResearchStore, name)
        if callable(attr):
            for p in inspect.signature(attr).parameters:
                assert not any(b in p.lower() for b in banned), \
                    f"{name}({p}=...) belongs to the gateway, not the store"


def test_the_store_module_defines_no_winner_selecting_function():
    tree = ast.parse(STORE_SRC.read_text(encoding="utf-8"))
    names = {n.name.lower() for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    for banned in ("winner", "latest", "current", "preferred", "newest", "best"):
        assert not any(banned in n for n in names), f"winner policy leaked: {banned}"


def test_query_returns_a_collection_even_for_a_single_match(tmp_path):
    store = _store(tmp_path)
    a = _snap({"revenue": 100})
    store.put(a)
    rows = store.query(entity_id="AAPL")
    assert isinstance(rows, tuple) and len(rows) == 1


# ── RS9 / RS10 storage is not admission ────────────────────────────────────
def test_future_evidence_is_stored_faithfully(tmp_path):
    """Refusing to store it would mean the store had opinions about time, and
    would make the corpus depend on when you queried it."""
    store = _store(tmp_path)
    future = _snap({"revenue": 111}, known_at=FEB20)
    assert store.put(future).written is True
    assert store.get(future.snapshot_id).snapshot.to_json() == future.to_json()


def test_the_store_applies_no_time_filter_of_its_own(tmp_path):
    store = _store(tmp_path)
    a = _snap({"revenue": 100}, known_at=JAN10)
    b = _snap({"revenue": 111}, known_at=FEB20)
    store.put(a)
    store.put(b)
    assert len(store.query(entity_id="AAPL")) == 2, (
        "the corpus is complete; visibility is decided elsewhere")


def test_persistence_is_transparent_to_the_gateway(tmp_path):
    """The integration property the next task depends on: store-then-resolve
    must equal resolve-over-in-memory-objects."""
    store = _store(tmp_path)
    a = _snap({"revenue": 100}, known_at=JAN10)
    b = _snap({"revenue": 111}, known_at=FEB20, supersedes=a.snapshot_id)
    store.put(a)
    store.put(b)

    corpus = store.query(entity_id="AAPL", evidence_type="fundamental.revenue")
    from_store = resolve_visibility(corpus, JAN15)
    in_memory = resolve_visibility([a, b], JAN15)
    assert from_store.to_dict() == in_memory.to_dict()
    assert a.snapshot_id in str(from_store.to_dict())


def test_the_store_never_reads_a_clock():
    """A store that stamps wall-clock time cannot be replayed."""
    tree = ast.parse(STORE_SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            assert name not in {"now", "utcnow", "today", "time"}, \
                f"the store must not read a clock: {name}"


# ── RS12 reopen ────────────────────────────────────────────────────────────
def test_reopening_the_store_yields_byte_identical_evidence(tmp_path):
    a = _snap({"revenue": 100}, retrieved_at=FEB20)
    w = _store(tmp_path)
    w.put(a)
    w.close()

    r = _store(tmp_path)
    got = r.get(a.snapshot_id).snapshot
    assert got.to_json() == a.to_json()
    assert got.pit.retrieved_at == a.pit.retrieved_at


def test_identity_survives_a_separate_process(tmp_path):
    """A same-process reopen can be satisfied by a module-level cache. Only a
    subprocess proves the bytes are on disk."""
    a = _snap({"revenue": 100})
    s = _store(tmp_path)
    s.put(a)
    s.close()

    out = subprocess.run(
        [sys.executable, "-c",
         "import sys;"
         "from portfolio_automation.research_store import ResearchStore;"
         "print(ResearchStore(db_path=sys.argv[1]).get(sys.argv[2]).snapshot.to_json())",
         str(tmp_path / "evidence.db"), a.snapshot_id],
        capture_output=True, text=True, cwd=str(REPO))
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == a.to_json()


def test_query_order_is_identical_after_reopen(tmp_path):
    snaps = [_snap({"revenue": v}) for v in (100, 111, 122)]
    w = _store(tmp_path)
    for s in snaps:
        w.put(s)
    before = [s.snapshot_id for s in w.query(entity_id="AAPL")]
    w.close()

    after = [s.snapshot_id for s in _store(tmp_path).query(entity_id="AAPL")]
    assert before == after


# ── writes are typed and atomic ────────────────────────────────────────────
def test_put_refuses_a_raw_mapping(tmp_path):
    """A dict would bypass __post_init__ entirely -- entity-type validation,
    provenance agreement, payload canonicalisation."""
    store = _store(tmp_path)
    with pytest.raises(ResearchStoreError) as exc:
        store.put({"snapshot_id": "evs_" + "0" * 32})
    assert exc.value.refusal is StoreRefusal.NOT_AN_EVIDENCE_SNAPSHOT


def test_the_database_stays_intact_after_a_refused_write(tmp_path):
    store = _store(tmp_path)
    a = _snap({"revenue": 100}, retrieved_at=JAN15)
    store.put(a)
    with pytest.raises(ResearchStoreError):
        store.put(_snap({"revenue": 100}, retrieved_at=FEB20))
    assert store.integrity_check() == "ok"
    assert store.snapshot_ids() == (a.snapshot_id,)
