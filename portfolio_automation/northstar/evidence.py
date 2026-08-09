"""EvidenceSnapshot + EvidenceRef — the canonical evidence unit and its pointer.

EvidenceSnapshot separates a CANONICAL ENVELOPE (identity, source, entity,
evidence type, point-in-time, hashes, provenance) from the DOMAIN PAYLOAD (the
source-shaped facts). The envelope is fully canonical and identity-bearing;
the payload is a strict-JSON document frozen at construction. This keeps the
contract general across future sources without becoming an uncontrolled blob
wrapper: every snapshot MUST declare source, entity, evidence type, and PIT
semantics, and its payload must canonicalize (no secrets, no Python objects,
no naive datetimes).

Immutability: the dataclass is frozen AND the payload is stored as canonical
bytes at construction. ``payload`` returns a fresh copy on every access —
mutating what a caller received can never alter the stored evidence or its
identity.

Revision: evidence is never mutated. A correction/revision becomes a NEW
snapshot whose ``supersedes_snapshot_id`` links to the prior one. Multi-source
coexistence is deliberate: the same entity/metric/period from two providers
produces two distinct valid snapshots (Phase 0C may later emit DATA_CONFLICT
from exactly such pairs; no uniqueness is imposed here).

Identity (identity-bearing fields → deterministic ``snapshot_id``):
    contract_type, source_id, entity_id, entity_type, evidence_type,
    PIT excluding retrieved_at, supersedes_snapshot_id, payload_hash
Excluded from identity (acquisition metadata): retrieved_at, provenance.
Re-acquiring identical information therefore reproduces the identical ID.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from portfolio_automation.northstar.canonical import (
    canonical_dumps,
    content_hash,
    deterministic_id,
)
from portfolio_automation.northstar.pit import PointInTime
from portfolio_automation.northstar.provenance import Provenance

SCHEMA_VERSION = "1.0.0"
CONTRACT_TYPE = "evidence_snapshot"
REF_CONTRACT_TYPE = "evidence_ref"

ENTITY_TYPES = frozenset({
    "symbol", "company", "etf", "index", "market", "sector", "macro", "other",
})


def _pit_identity_view(pit: PointInTime) -> dict:
    view = pit.to_canonical_dict()
    view.pop("retrieved_at")  # acquisition metadata never carries identity
    return view


@dataclass(frozen=True, slots=True)
class EvidenceSnapshot:
    source_id: str                      # DataSourceDescriptor.source_id
    entity_id: str                      # e.g. "AAPL"
    entity_type: str                    # one of ENTITY_TYPES
    evidence_type: str                  # namespaced, e.g. "fundamental.revenue"
    pit: PointInTime
    provenance: Provenance
    payload_canonical: str = field(init=False)   # frozen canonical JSON bytes (str)
    payload_hash: str = field(init=False)
    supersedes_snapshot_id: Optional[str] = None
    schema_version: str = SCHEMA_VERSION
    contract_type: str = field(default=CONTRACT_TYPE, init=False)
    # Construction-only input; immediately canonicalized and discarded as an
    # attribute reference (the canonical string is authoritative).
    payload: Any = None

    def __post_init__(self) -> None:
        for name, value in (
            ("source_id", self.source_id),
            ("entity_id", self.entity_id),
            ("evidence_type", self.evidence_type),
        ):
            if not value or not isinstance(value, str):
                raise ValueError(f"{name} is required")
        if self.entity_type not in ENTITY_TYPES:
            raise ValueError(
                f"entity_type must be one of {sorted(ENTITY_TYPES)}, got {self.entity_type!r}"
            )
        if not isinstance(self.pit, PointInTime):
            raise ValueError("pit must be a PointInTime")
        if not isinstance(self.provenance, Provenance):
            raise ValueError("provenance must be a Provenance")
        raw = self.payload
        if not isinstance(raw, dict) or not raw:
            raise ValueError("payload must be a non-empty mapping of domain facts")
        canonical = canonical_dumps(raw)  # strict: rejects secrets-unsafe types, naive datetimes
        object.__setattr__(self, "payload_canonical", canonical)
        object.__setattr__(self, "payload_hash", content_hash(raw))
        # Drop the caller's mutable reference: the canonical string is the truth.
        object.__setattr__(self, "payload", None)

    def payload_copy(self) -> dict:
        """A FRESH deep copy of the domain payload; mutating it changes nothing."""
        return json.loads(self.payload_canonical)

    @property
    def snapshot_id(self) -> str:
        return deterministic_id(
            "evs",
            {
                "contract_type": CONTRACT_TYPE,
                "source_id": self.source_id,
                "entity_id": self.entity_id,
                "entity_type": self.entity_type,
                "evidence_type": self.evidence_type,
                "pit": _pit_identity_view(self.pit),
                "supersedes_snapshot_id": self.supersedes_snapshot_id,
                "payload_hash": self.payload_hash,
            },
        )

    def revise(self, new_payload: dict, pit: PointInTime, provenance: Provenance) -> "EvidenceSnapshot":
        """A revision is a NEW immutable snapshot superseding this one."""
        return EvidenceSnapshot(
            source_id=self.source_id,
            entity_id=self.entity_id,
            entity_type=self.entity_type,
            evidence_type=self.evidence_type,
            pit=pit,
            provenance=provenance,
            supersedes_snapshot_id=self.snapshot_id,
            schema_version=self.schema_version,
            payload=new_payload,
        )

    def ref(self) -> "EvidenceRef":
        return EvidenceRef(
            snapshot_id=self.snapshot_id,
            source_id=self.source_id,
            entity_id=self.entity_id,
            evidence_type=self.evidence_type,
            payload_hash=self.payload_hash,
        )

    def to_canonical_dict(self) -> dict:
        return {
            "contract_type": self.contract_type,
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "source_id": self.source_id,
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "evidence_type": self.evidence_type,
            "pit": self.pit.to_canonical_dict(),
            "payload": json.loads(self.payload_canonical),
            "payload_hash": self.payload_hash,
            "supersedes_snapshot_id": self.supersedes_snapshot_id,
            "provenance": self.provenance.to_canonical_dict(),
        }

    def to_json(self) -> str:
        return canonical_dumps(self.to_canonical_dict())

    @classmethod
    def from_dict(cls, data: dict) -> "EvidenceSnapshot":
        if data.get("contract_type") != CONTRACT_TYPE:
            raise ValueError(f"not an {CONTRACT_TYPE}: {data.get('contract_type')!r}")
        if not isinstance(data.get("schema_version"), str):
            raise ValueError("schema_version is required")
        obj = cls(
            source_id=data["source_id"],
            entity_id=data["entity_id"],
            entity_type=data["entity_type"],
            evidence_type=data["evidence_type"],
            pit=PointInTime.from_dict(data["pit"]),
            provenance=Provenance.from_dict(data["provenance"]),
            supersedes_snapshot_id=data.get("supersedes_snapshot_id"),
            schema_version=data["schema_version"],
            payload=data["payload"],
        )
        for name, recorded, actual in (
            ("payload_hash", data.get("payload_hash"), obj.payload_hash),
            ("snapshot_id", data.get("snapshot_id"), obj.snapshot_id),
        ):
            if recorded is not None and recorded != actual:
                raise ValueError(f"{name} mismatch — serialized identity does not reproduce")
        return obj

    @classmethod
    def from_json(cls, text: str) -> "EvidenceSnapshot":
        return cls.from_dict(json.loads(text))


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """Lightweight immutable pointer to an EvidenceSnapshot.

    Future contracts (PredictionRecord, ExperimentSpec, WorkerResult,
    CapitalProposal, ExitProposal, OutcomeRecord) embed EvidenceRefs instead
    of copying snapshots, so any downstream artifact can prove exactly which
    evidence it depended on. Carries only what is needed to identify evidence
    uniquely and safely (id + integrity hash + minimal routing context).
    """

    snapshot_id: str
    source_id: str
    entity_id: str
    evidence_type: str
    payload_hash: str
    contract_type: str = field(default=REF_CONTRACT_TYPE, init=False)

    def __post_init__(self) -> None:
        for name in ("snapshot_id", "source_id", "entity_id", "evidence_type", "payload_hash"):
            value = getattr(self, name)
            if not value or not isinstance(value, str):
                raise ValueError(f"{name} is required")
        if not self.snapshot_id.startswith("evs_"):
            raise ValueError(f"snapshot_id does not look like an evidence id: {self.snapshot_id!r}")

    def matches(self, snapshot: EvidenceSnapshot) -> bool:
        """True iff this ref points at exactly that snapshot (id AND content)."""
        return (
            self.snapshot_id == snapshot.snapshot_id
            and self.payload_hash == snapshot.payload_hash
        )

    def to_canonical_dict(self) -> dict:
        return {
            "contract_type": self.contract_type,
            "snapshot_id": self.snapshot_id,
            "source_id": self.source_id,
            "entity_id": self.entity_id,
            "evidence_type": self.evidence_type,
            "payload_hash": self.payload_hash,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EvidenceRef":
        if data.get("contract_type") != REF_CONTRACT_TYPE:
            raise ValueError(f"not an {REF_CONTRACT_TYPE}: {data.get('contract_type')!r}")
        return cls(
            snapshot_id=data["snapshot_id"],
            source_id=data["source_id"],
            entity_id=data["entity_id"],
            evidence_type=data["evidence_type"],
            payload_hash=data["payload_hash"],
        )
