"""Durable, content-addressed storage for the exact bytes a reviewer was sent.

WHY THIS EXISTS.

A real machine crash on 2026-08-16 interrupted a certification session. Task
state, verification state and review history all reconstructed cleanly from the
append-only session ledgers -- with one exception. The ledgers recorded
``packet_hash`` and ``size_bytes`` for every review packet, and nothing else.
A digest with no preimage cannot answer the only question that matters when a
verdict is disputed: EXACTLY WHAT DID THE REVIEWER SEE? Two REPAIR verdicts on
that candidate could not be re-examined against the artifact that produced
them, because the artifact no longer existed anywhere.

This module stores the preimage.

WHY THE BYTES, NOT THE OBJECT.

``ReviewPacket.to_supervisor_packet`` emits ``source_files`` and
``supporting_evidence`` as lists built from ``evidence`` insertion order, and
``json.dumps(sort_keys=True)`` sorts object keys, not list elements. The same
logical packet rebuilt with artifacts added in a different order therefore
hashes DIFFERENTLY. Reconstruction that rebuilt a packet and re-hashed it would
report a spurious mismatch -- and the obvious "fix", normalising the order
inside the hash, would silently change what every recorded ``pkt_`` digest
refers to, orphaning the history this exists to protect.

So the rule is absolute: hash over the STORED BYTES, dispatch the RELOADED
bytes, never re-derive them from a rebuilt object.

FAIL CLOSED.

Every failure path here yields a refusal, never a pass. A packet that is absent,
unreadable, corrupt, hash-mismatched or bound to a different candidate cannot be
certified, and this module never repairs, re-serialises or substitutes an
artifact to make a verification succeed. Laundering a mismatch into agreement is
the exact defect the certification gate exists to prevent.
"""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Sequence

from portfolio_automation.engineer_worker import EXPERIMENTAL_MARKER
from portfolio_automation.engineer_worker.review_packet import (
    canonical_packet_bytes, packet_hash_of_bytes, packet_sha256_of_bytes,
)

SCHEMA_KIND = EXPERIMENTAL_MARKER
PACKET_STORE_SCHEMA_VERSION = "engineering.review_packet_store.v0"

#: Tracked under docs/ alongside the repository's other durable-record
#: conventions. A certification preimage in a gitignored directory is not
#: evidence: it cannot travel with the commit it certifies, and nobody who did
#: not run the session could audit it.
DEFAULT_STORE_REL = "docs/review_packets"
DEFAULT_INDEX_REL = "docs/EW0A_REVIEW_PACKETS.jsonl"


class StoreRefusal(str, Enum):
    """Why a persisted packet could NOT be trusted. Every value blocks review."""

    ABSENT = "ABSENT"
    UNREADABLE = "UNREADABLE"
    HASH_MISMATCH = "HASH_MISMATCH"
    MALFORMED_PAYLOAD = "MALFORMED_PAYLOAD"
    CONTENT_COLLISION = "CONTENT_COLLISION"
    BINDING_MISMATCH = "BINDING_MISMATCH"
    NOT_SCREENED = "NOT_SCREENED"


class PacketStoreError(ValueError):
    """Raised for conditions that must abort dispatch rather than be reported."""

    def __init__(self, refusal: StoreRefusal, detail: str = "") -> None:
        super().__init__(f"{refusal.value}: {detail}" if detail else refusal.value)
        self.refusal = refusal
        self.detail = detail


@dataclass(frozen=True)
class BindingFacts:
    """The identity a packet's bytes claim for themselves.

    These live INSIDE the hashed payload, which is what makes them
    self-authenticating: altering any of them changes the serialization and so
    changes ``packet_hash``. A packet cannot be silently re-pointed at another
    candidate, mission or task without becoming a different packet."""

    candidate_sha: Optional[str]
    mission_id: Optional[str]
    task_id: Optional[str]
    session_id: Optional[str]
    criterion_ids: tuple[str, ...]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "BindingFacts":
        task = payload.get("task") or {}
        if not isinstance(task, dict):
            task = {}
        criteria = payload.get("criteria") or []
        ids: list[str] = []
        if isinstance(criteria, list):
            for c in criteria:
                if isinstance(c, dict) and c.get("criterion_id") is not None:
                    ids.append(str(c["criterion_id"]))
        return cls(
            candidate_sha=payload.get("candidate_sha"),
            mission_id=payload.get("mission_id"),
            task_id=task.get("task_id"),
            session_id=task.get("session_id"),
            criterion_ids=tuple(sorted(ids)))

    def mismatches(self, other: "BindingFacts") -> tuple[str, ...]:
        out: list[str] = []
        for name in ("candidate_sha", "mission_id", "task_id", "session_id"):
            if getattr(self, name) != getattr(other, name):
                out.append(f"{name}: {getattr(self, name)!r} != {getattr(other, name)!r}")
        if self.criterion_ids != other.criterion_ids:
            out.append(f"criterion_ids: {list(self.criterion_ids)} != "
                       f"{list(other.criterion_ids)}")
        return tuple(out)

    def to_dict(self) -> dict[str, Any]:
        return {"candidate_sha": self.candidate_sha, "mission_id": self.mission_id,
                "task_id": self.task_id, "session_id": self.session_id,
                "criterion_ids": list(self.criterion_ids)}


@dataclass(frozen=True)
class PersistedPacket:
    packet_hash: str
    packet_sha256: str
    store_rel: str
    size_bytes: int
    binding: BindingFacts
    #: False when an identical artifact already existed. A duplicate write of
    #: byte-identical content is idempotent, not an error.
    written: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": PACKET_STORE_SCHEMA_VERSION,
                "schema_kind": SCHEMA_KIND,
                "packet_hash": self.packet_hash, "packet_sha256": self.packet_sha256,
                "packet_blob_rel": self.store_rel, "packet_blob_bytes": self.size_bytes,
                "binding": self.binding.to_dict(), "newly_written": self.written}


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    packet_hash: str
    refusals: tuple[StoreRefusal, ...] = ()
    details: tuple[str, ...] = ()
    payload: Optional[dict[str, Any]] = None
    blob: Optional[bytes] = None

    def to_dict(self) -> dict[str, Any]:
        return {"packet_verified": self.ok, "packet_hash": self.packet_hash,
                "refusals": [r.value for r in self.refusals],
                "details": list(self.details)}


def _atomic_create(path: Path, blob: bytes) -> bool:
    """Create ``path`` with ``blob``. Returns True if this call wrote it.

    Uses a unique temp plus ``os.link`` rather than ``os.replace``: link fails
    with FileExistsError instead of clobbering, which makes "already present"
    an atomic outcome rather than a check-then-write race. The repository's
    existing tmp+replace helper uses a FIXED temp name, which two concurrent
    writers to a shared store would interleave on."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())          # bytes reach disk before they are named
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        except (AttributeError, OSError):
            if path.exists():
                return False
            os.replace(tmp, path)          # platforms without hardlink support
            tmp = None                     # replace consumed it
        try:                               # durable directory entry
            dfd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass                           # best effort; the bytes are already fsynced
        try:
            os.chmod(path, 0o444)          # immutable by convention AND by mode
        except OSError:
            pass
        return True
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


@dataclass(frozen=True)
class PacketStore:
    """Append-only, content-addressed packet store rooted in a checkout."""

    repo_root: Path
    store_rel: str = DEFAULT_STORE_REL
    index_rel: str = DEFAULT_INDEX_REL

    def rel_for(self, packet_hash: str) -> str:
        shard = packet_hash[4:6] if packet_hash.startswith("pkt_") else packet_hash[:2]
        return f"{self.store_rel}/{shard}/{packet_hash}.json"

    def path_for(self, packet_hash: str) -> Path:
        return Path(self.repo_root) / self.rel_for(packet_hash)

    # -- write ------------------------------------------------------------
    def persist(self, blob: bytes, *, expected_hash: str,
                screened: bool) -> PersistedPacket:
        """Store the exact reviewer bytes.

        ``screened`` is required and must be True. Persistence is the one step
        that makes content permanent, so an unscreened packet must be
        structurally unable to reach it -- a credential written into a
        content-addressed, read-only artifact cannot be cleaned up afterwards."""
        if not screened:
            raise PacketStoreError(
                StoreRefusal.NOT_SCREENED,
                "refusing to persist a packet that has not cleared the secret screen")
        actual = packet_hash_of_bytes(blob)
        if actual != expected_hash:
            raise PacketStoreError(
                StoreRefusal.HASH_MISMATCH,
                f"caller expected {expected_hash} but the bytes hash to {actual}")
        try:
            payload = json.loads(blob.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PacketStoreError(StoreRefusal.MALFORMED_PAYLOAD, str(exc)) from exc
        binding = BindingFacts.from_payload(payload)
        path = self.path_for(actual)
        written = _atomic_create(path, blob)
        if not written:
            existing = path.read_bytes()
            if existing != blob:
                # Same address, different content: truncated-hash collision or
                # tampering. Never overwrite, never choose a winner.
                raise PacketStoreError(
                    StoreRefusal.CONTENT_COLLISION,
                    f"{self.rel_for(actual)} already holds different bytes "
                    f"({len(existing)} vs {len(blob)})")
        return PersistedPacket(
            packet_hash=actual, packet_sha256=packet_sha256_of_bytes(blob),
            store_rel=self.rel_for(actual), size_bytes=len(blob),
            binding=binding, written=written)

    # -- read -------------------------------------------------------------
    def load(self, packet_hash: str) -> bytes:
        path = self.path_for(packet_hash)
        if not path.exists():
            raise PacketStoreError(StoreRefusal.ABSENT, self.rel_for(packet_hash))
        try:
            return path.read_bytes()
        except OSError as exc:
            raise PacketStoreError(StoreRefusal.UNREADABLE, str(exc)) from exc

    def verify(self, packet_hash: str, *,
               expected_binding: Optional[BindingFacts] = None) -> VerifyResult:
        """Reload and prove the artifact is the one that was reviewed.

        Never raises for a bad artifact -- a refusal is data, so a caller cannot
        accidentally swallow it with a bare except and proceed."""
        try:
            blob = self.load(packet_hash)
        except PacketStoreError as exc:
            return VerifyResult(False, packet_hash, (exc.refusal,), (exc.detail,))
        actual = packet_hash_of_bytes(blob)
        if actual != packet_hash:
            return VerifyResult(
                False, packet_hash, (StoreRefusal.HASH_MISMATCH,),
                (f"stored bytes hash to {actual}, not {packet_hash}; "
                 "the artifact is NOT repaired or re-serialised",))
        try:
            payload = json.loads(blob.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return VerifyResult(False, packet_hash, (StoreRefusal.MALFORMED_PAYLOAD,),
                                (str(exc),))
        if expected_binding is not None:
            found = BindingFacts.from_payload(payload)
            diffs = expected_binding.mismatches(found)
            if diffs:
                return VerifyResult(False, packet_hash, (StoreRefusal.BINDING_MISMATCH,),
                                    diffs)
        return VerifyResult(True, packet_hash, payload=payload, blob=blob)

    # -- index ------------------------------------------------------------
    def append_index(self, record: dict[str, Any]) -> None:
        """Append one row to the discovery index.

        The index carries digests, paths and binding facts -- never evidence
        text. A second copy of packet content would be a second place for a
        secret to land."""
        path = Path(self.repo_root) / self.index_rel
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"schema_version": PACKET_STORE_SCHEMA_VERSION,
               "schema_kind": SCHEMA_KIND, **record}
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=True, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
