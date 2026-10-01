"""SQLite-backed immutable evidence store.

WHY SQLITE AND NOT A CONTENT-ADDRESSED FILE STORE.

``EvidenceSnapshot`` identity deliberately excludes acquisition metadata, so ONE
``snapshot_id`` legitimately corresponds to more than one canonical byte string.
A content-addressed store keys on ``hash(bytes)``, which puts those two strings
at two different addresses -- so the identity conflict this store exists to
refuse becomes invisible, and no error is ever raised. Keying such a store by
``snapshot_id`` instead means abandoning content addressing and hand-rolling a
key-value store plus an index, i.e. rebuilding SQLite badly.

WHY THE RECORD KEY IS THE ONLY DEFENCE AGAINST A COHERENT FORGERY.

Replace a stored record with a *different but legitimately built* snapshot and
every intra-object check passes: its payload, payload_hash and snapshot_id all
agree with each other. ``admit()`` cannot catch it -- a bare snapshot has no
stored counterpart to disagree with, which is why the gateway has no
SNAPSHOT_ID_MISMATCH reason code. A store creates that counterpart: the key was
recorded at write time by a separate act. Comparing the reconstructed identity
back against the key it was asked for is therefore the one integrity check only
this layer can make, and it is not optional.

WHY IDENTITY COLUMNS ARE REPLAYED ON RELOAD.

``EvidenceSnapshot.from_dict`` verifies ``snapshot_id`` and ``payload_hash``
ONLY when the record supplies them. Persist the canonical document without them
-- on the reasoning that they are derived anyway -- and a tampered payload is
accepted silently as a new, valid-looking snapshot. Replay them and the same
tamper is refused. They are stored as a second opinion, never as a cache.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

from portfolio_automation.northstar.evidence import EvidenceRef, EvidenceSnapshot

SCHEMA_VERSION = 1

#: Ordering is by snapshot_id everywhere. It is total, content-derived and --
#: critically -- semantically meaningless: a hash order cannot be misread as a
#: ranking, whereas ordering by known_at makes position 0 look like "the
#: current value" and quietly reintroduces winner selection.
_ORDER = "ORDER BY snapshot_id"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS evidence_snapshots (
    snapshot_id   TEXT PRIMARY KEY
                  CHECK (snapshot_id GLOB 'evs_*' AND length(snapshot_id) = 36),
    record_sha256 TEXT NOT NULL CHECK (length(record_sha256) = 64),
    payload_hash  TEXT NOT NULL CHECK (length(payload_hash) = 64),
    canonical     BLOB NOT NULL,
    source_id     TEXT NOT NULL,
    entity_id     TEXT NOT NULL,
    entity_type   TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    supersedes_snapshot_id TEXT,
    schema_version TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS ix_evs_entity
    ON evidence_snapshots(entity_id, evidence_type, snapshot_id);
CREATE INDEX IF NOT EXISTS ix_evs_source
    ON evidence_snapshots(source_id, snapshot_id);
"""


class StoreRefusal(str, Enum):
    """Why stored content could not be trusted. Every value blocks a read."""

    NOT_FOUND = "NOT_FOUND"
    NOT_CANONICAL = "NOT_CANONICAL"
    PAYLOAD_HASH_MISMATCH = "PAYLOAD_HASH_MISMATCH"
    #: Only a STORE can raise this. See the module docstring.
    SNAPSHOT_ID_MISMATCH = "SNAPSHOT_ID_MISMATCH"
    RECORD_DIGEST_MISMATCH = "RECORD_DIGEST_MISMATCH"
    IDENTITY_CONTENT_CONFLICT = "IDENTITY_CONTENT_CONFLICT"
    REF_DOES_NOT_MATCH = "REF_DOES_NOT_MATCH"
    NOT_AN_EVIDENCE_SNAPSHOT = "NOT_AN_EVIDENCE_SNAPSHOT"


class ResearchStoreError(ValueError):
    """A write that must abort rather than be reported as data."""

    def __init__(self, refusal: StoreRefusal, detail: str = "") -> None:
        super().__init__(f"{refusal.value}: {detail}" if detail else refusal.value)
        self.refusal = refusal
        self.detail = detail


@dataclass(frozen=True)
class PutResult:
    snapshot_id: str
    #: False when a byte-identical record already existed. Idempotent re-put is
    #: success, not an error -- but it is distinguishable, so a caller cannot
    #: mistake "wrote it again" for "wrote it once".
    written: bool
    record_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {"snapshot_id": self.snapshot_id, "written": self.written,
                "record_sha256": self.record_sha256}


@dataclass(frozen=True)
class GetResult:
    """A read outcome. Refusals are DATA.

    ``snapshot`` is None whenever ``ok`` is False. A result object that carried
    both a False flag and a usable object would be read for the object."""

    ok: bool
    snapshot_id: str
    snapshot: Optional[EvidenceSnapshot] = None
    refusals: tuple[StoreRefusal, ...] = ()
    details: tuple[str, ...] = ()

    @property
    def found(self) -> bool:
        """Present in the store at all -- distinct from usable.

        Damaged is not the same finding as absent: an auditor counting data
        loss must be able to tell a corrupted record from one never written."""
        return StoreRefusal.NOT_FOUND not in self.refusals

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "found": self.found, "snapshot_id": self.snapshot_id,
                "refusals": [r.value for r in self.refusals],
                "details": list(self.details)}


def _refuse(snapshot_id: str, refusal: StoreRefusal, detail: str) -> GetResult:
    return GetResult(False, snapshot_id, None, (refusal,), (detail,))


@dataclass
class ResearchStore:
    """Immutable, deterministic evidence persistence.

    There is deliberately no ``update`` and no ``delete``: evidence is never
    mutated, and a revision is a new snapshot linked by
    ``supersedes_snapshot_id``, not an edit of its predecessor."""

    db_path: Path
    _conn: Optional[sqlite3.Connection] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.db_path = Path(self.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: manage transactions explicitly so the existence
        # check and the insert sit inside ONE write-locked transaction. The
        # driver default takes the write lock late, so two writers could both
        # read-then-write.
        self._conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        # FULL, not the WAL default NORMAL: evidence acknowledged as durable
        # must survive power loss, not merely process loss.
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    def _migrate(self) -> None:
        # DDL is not transactional under this driver, so every statement is
        # IF NOT EXISTS and the version stamp is written LAST. A crash between
        # them leaves version absent and the next open re-runs it idempotently.
        assert self._conn is not None
        self._conn.executescript(_SCHEMA)
        row = self._conn.execute("SELECT version FROM schema_meta").fetchone()
        if row is None:
            self._conn.execute("INSERT INTO schema_meta (version) VALUES (?)",
                               (SCHEMA_VERSION,))
        elif row[0] != SCHEMA_VERSION:
            raise ResearchStoreError(
                StoreRefusal.NOT_CANONICAL,
                f"store schema version {row[0]} != {SCHEMA_VERSION}; refusing to "
                "read a store this code does not understand")

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- write ------------------------------------------------------------
    def put(self, snapshot: EvidenceSnapshot) -> PutResult:
        """Persist canonical evidence. Never overwrites.

        Identical canonical bytes are idempotent success. Different bytes under
        the same identity fail closed: the store cannot choose between two
        acquisition envelopes without inventing a policy, and silently
        discarding one would rewrite acquisition history."""
        if not isinstance(snapshot, EvidenceSnapshot):
            raise ResearchStoreError(
                StoreRefusal.NOT_AN_EVIDENCE_SNAPSHOT,
                f"put expects an EvidenceSnapshot, got {type(snapshot).__name__}; "
                "a raw mapping would bypass contract validation entirely")

        blob = snapshot.to_json().encode("utf-8")
        digest = hashlib.sha256(blob).hexdigest()
        sid = snapshot.snapshot_id

        assert self._conn is not None
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT record_sha256, canonical FROM evidence_snapshots "
                "WHERE snapshot_id = ?", (sid,)).fetchone()
            if row is not None:
                # Compare the DIGEST and the BYTES. A digest match alone trusts
                # the stored digest to describe the stored blob, which is
                # exactly the assumption a tamperer breaks.
                if row[0] == digest and row[1] == blob:
                    conn.execute("COMMIT")
                    return PutResult(sid, False, digest)
                conn.execute("ROLLBACK")
                raise ResearchStoreError(
                    StoreRefusal.IDENTITY_CONTENT_CONFLICT,
                    f"{sid} is already stored with different bytes "
                    f"(stored {row[0][:12]}, incoming {digest[:12]}); evidence is "
                    "never overwritten in place")
            # Plain INSERT. OR IGNORE would silently DROP a divergent record and
            # report success; OR REPLACE would silently DESTROY stored evidence.
            conn.execute(
                "INSERT INTO evidence_snapshots (snapshot_id, record_sha256, "
                "payload_hash, canonical, source_id, entity_id, entity_type, "
                "evidence_type, supersedes_snapshot_id, schema_version) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sid, digest, snapshot.payload_hash, blob, snapshot.source_id,
                 snapshot.entity_id, snapshot.entity_type, snapshot.evidence_type,
                 snapshot.supersedes_snapshot_id, snapshot.schema_version))
            conn.execute("COMMIT")
        except ResearchStoreError:
            raise
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return PutResult(sid, True, digest)

    # -- read -------------------------------------------------------------
    def get(self, snapshot_id: str) -> GetResult:
        """Retrieve by exact identity, reconstructing through the contract."""
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT record_sha256, canonical FROM evidence_snapshots "
            "WHERE snapshot_id = ?", (str(snapshot_id),)).fetchone()
        if row is None:
            return _refuse(str(snapshot_id), StoreRefusal.NOT_FOUND,
                           "no record under that identity")
        return self._reconstruct(str(snapshot_id), row[0], row[1])

    def _reconstruct(self, key: str, recorded_digest: str, blob: bytes) -> GetResult:
        """Two independent integrity layers, and both must hold.

        The record digest catches bit-rot in bytes the contract would still
        parse. The contract's own identity re-derivation catches a tamperer who
        also updated the digest, because they cannot make the canonical
        identity reproduce. The key comparison catches a coherent forgery that
        satisfies both."""
        if hashlib.sha256(blob).hexdigest() != recorded_digest:
            return _refuse(key, StoreRefusal.RECORD_DIGEST_MISMATCH,
                           "stored bytes do not match the digest recorded with them")
        try:
            snapshot = EvidenceSnapshot.from_json(blob.decode("utf-8"))
        except UnicodeDecodeError as exc:
            return _refuse(key, StoreRefusal.NOT_CANONICAL, str(exc))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            return _refuse(key, StoreRefusal.NOT_CANONICAL, f"{type(exc).__name__}: {exc}")
        except ValueError as exc:
            # from_dict raises this when the replayed snapshot_id/payload_hash
            # do not reproduce -- i.e. the payload was tampered with.
            text = str(exc)
            refusal = (StoreRefusal.PAYLOAD_HASH_MISMATCH
                       if "payload_hash" in text or "snapshot_id" in text
                       else StoreRefusal.NOT_CANONICAL)
            return _refuse(key, refusal, text)

        if snapshot.snapshot_id != key:
            return _refuse(
                key, StoreRefusal.SNAPSHOT_ID_MISMATCH,
                f"record stored under {key} reconstructs as "
                f"{snapshot.snapshot_id}; internally consistent is not authentic")
        return GetResult(True, key, snapshot)

    def get_by_ref(self, ref: EvidenceRef) -> GetResult:
        """Resolve an EvidenceRef.

        ``EvidenceRef.matches`` compares only snapshot_id and payload_hash, so a
        ref can carry the right identity pair and lie about its routing fields
        and still satisfy it. The store holds those columns, so the store is the
        layer that can catch it -- matches() alone is NOT sufficient."""
        if not isinstance(ref, EvidenceRef):
            return _refuse(str(getattr(ref, "snapshot_id", "")),
                           StoreRefusal.REF_DOES_NOT_MATCH,
                           f"expected an EvidenceRef, got {type(ref).__name__}")
        result = self.get(ref.snapshot_id)
        if not result.ok:
            return result
        snapshot = result.snapshot
        assert snapshot is not None
        if not ref.matches(snapshot):
            return _refuse(ref.snapshot_id, StoreRefusal.REF_DOES_NOT_MATCH,
                           "ref identity does not match the stored snapshot")
        for name in ("source_id", "entity_id", "evidence_type"):
            claimed, actual = getattr(ref, name), getattr(snapshot, name)
            if claimed != actual:
                return _refuse(
                    ref.snapshot_id, StoreRefusal.REF_DOES_NOT_MATCH,
                    f"ref claims {name}={claimed!r} but the stored snapshot has "
                    f"{actual!r}; matches() does not compare routing fields")
        return result

    # -- corpus -----------------------------------------------------------
    def query(self, *, source_id: Optional[str] = None,
              entity_id: Optional[str] = None,
              entity_type: Optional[str] = None,
              evidence_type: Optional[str] = None) -> tuple[EvidenceSnapshot, ...]:
        """Return the matching corpus, in a stable content-derived order.

        Always a collection, even for a single match: a signature that CAN
        return "the" snapshot will be called that way when two match, and the
        caller will silently receive one of them. There is no ``as_of``
        parameter -- historical visibility is the gateway's decision, and a
        WHERE clause produces no reason codes."""
        clauses, params = [], []
        for column, value in (("source_id", source_id), ("entity_id", entity_id),
                              ("entity_type", entity_type),
                              ("evidence_type", evidence_type)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        assert self._conn is not None
        rows = self._conn.execute(
            f"SELECT snapshot_id, record_sha256, canonical FROM evidence_snapshots"
            f"{where} {_ORDER}", params).fetchall()

        out: list[EvidenceSnapshot] = []
        for key, digest, blob in rows:
            result = self._reconstruct(key, digest, blob)
            if not result.ok:
                # A corpus sweep must not silently skip bad evidence; that is
                # how destroyed evidence becomes "we never had it".
                raise ResearchStoreError(
                    result.refusals[0],
                    f"{key}: {result.details[0] if result.details else ''}")
            assert result.snapshot is not None
            out.append(result.snapshot)
        return tuple(out)

    def iter_all(self) -> Iterator[EvidenceSnapshot]:
        yield from self.query()

    def snapshot_ids(self) -> tuple[str, ...]:
        """Stored identities in deterministic order, without reconstructing."""
        assert self._conn is not None
        return tuple(r[0] for r in self._conn.execute(
            f"SELECT snapshot_id FROM evidence_snapshots {_ORDER}").fetchall())

    def raw_bytes(self, snapshot_id: str) -> Optional[bytes]:
        """Exact stored bytes, for verification and audit. Never reconstructed."""
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT canonical FROM evidence_snapshots WHERE snapshot_id = ?",
            (str(snapshot_id),)).fetchone()
        return None if row is None else row[0]

    def integrity_check(self) -> str:
        assert self._conn is not None
        return self._conn.execute("PRAGMA integrity_check").fetchone()[0]
