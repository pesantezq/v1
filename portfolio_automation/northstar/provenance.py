"""Small explicit provenance record for Northstar contracts.

Answers, for any canonical object:

    What produced this? From what source? Using what code/model? When?

Deliberately small and explicit — NOT an arbitrary metadata dictionary. New
provenance dimensions are added by schema evolution, not by stuffing a dict.

``recorded_at`` is acquisition metadata: it is EXCLUDED from every contract's
deterministic identity (re-producing identical information later must yield
the identical ID).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from portfolio_automation.northstar.canonical import (
    CanonicalizationError,
    encode_datetime,
    validate_contract_id,
)

PRODUCER_SOURCE_ADAPTER = "source_adapter"   # vendor/source ingestion adapter
PRODUCER_DERIVATION = "derivation"           # deterministic transformation (features)
PRODUCER_SYSTEM = "system"                   # other StockBot system component
PRODUCER_AI_WORKER = "ai_worker"             # research worker output (never truth)
PRODUCER_HUMAN = "human"                     # operator-entered

PRODUCER_TYPES = frozenset({
    PRODUCER_SOURCE_ADAPTER,
    PRODUCER_DERIVATION,
    PRODUCER_SYSTEM,
    PRODUCER_AI_WORKER,
    PRODUCER_HUMAN,
})


@dataclass(frozen=True, slots=True)
class Provenance:
    producer_id: str                      # e.g. "adapter.fmp_quotes" / "derivation.float_turnover"
    producer_type: str                    # one of PRODUCER_TYPES
    recorded_at: datetime                 # when this object was produced (tz-aware)
    code_version: Optional[str] = None    # git SHA / package version of the producer
    model_id: Optional[str] = None        # model identity+version when an AI/statistical model produced it
    source_id: Optional[str] = None       # DataSourceDescriptor.source_id when applicable
    transformation_id: Optional[str] = None  # derivation identity for derived objects

    def __post_init__(self) -> None:
        if not self.producer_id or not isinstance(self.producer_id, str):
            raise ValueError("producer_id is required")
        if self.producer_type not in PRODUCER_TYPES:
            raise ValueError(
                f"producer_type must be one of {sorted(PRODUCER_TYPES)}, "
                f"got {self.producer_type!r}"
            )
        try:
            encode_datetime(self.recorded_at)
        except CanonicalizationError as exc:
            raise ValueError(f"recorded_at: {exc}") from exc
        # Fail closed: a source adapter ingests FROM a source by definition —
        # source_adapter provenance without a source identity is incoherent.
        if self.producer_type == PRODUCER_SOURCE_ADAPTER and self.source_id is None:
            raise ValueError(
                "producer_type='source_adapter' requires source_id — an "
                "adapter's provenance must name the source it ingested from"
            )
        # When present, source_id must be a well-formed DataSourceDescriptor id.
        if self.source_id is not None:
            validate_contract_id("provenance.source_id", self.source_id, "src")

    def to_canonical_dict(self) -> dict:
        return {
            "producer_id": self.producer_id,
            "producer_type": self.producer_type,
            "recorded_at": self.recorded_at,
            "code_version": self.code_version,
            "model_id": self.model_id,
            "source_id": self.source_id,
            "transformation_id": self.transformation_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Provenance":
        from portfolio_automation.northstar.serde import parse_optional_datetime

        recorded = parse_optional_datetime(data.get("recorded_at"))
        if recorded is None:
            raise ValueError("provenance.recorded_at is required")
        return cls(
            producer_id=data["producer_id"],
            producer_type=data["producer_type"],
            recorded_at=recorded,
            code_version=data.get("code_version"),
            model_id=data.get("model_id"),
            source_id=data.get("source_id"),
            transformation_id=data.get("transformation_id"),
        )
