"""FeatureRecord — a canonical DERIVED transformation of evidence.

Critical distinction (an architecture invariant, test-enforced):

    EvidenceSnapshot = source/canonical evidence   (free_float = 34.2M)
    FeatureRecord    = derived transformation      (float_turnover_5d = 0.42)

A FeatureRecord must reference the EvidenceRefs it was derived from and the
derivation identity/version that produced it, so it is reproducible and can
never masquerade as raw evidence. Its ``as_of`` is the PIT anchor at which the
derivation was evaluated; Phase 0C will require every referenced snapshot to
satisfy ``known_at <= as_of``.

Value representation (kernel v1, per repo convention): JSON scalars — int,
float (finite), bool, str — or a small homogeneous numeric sequence (≤ 32
elements). Floats, not Decimal: the repository's quantitative code is
float-based throughout (Decimal appears only in a charting module), and the
canonical serializer gives deterministic float encoding; precision-sensitive
monetary work is not a Phase 0B concern and can arrive as a later additive
value kind. No uncontrolled ``Any``.

Identity (→ deterministic ``feature_id``): schema era (major version),
feature_name, derivation_id, derivation_version, entity_id, as_of, input
snapshot ids, value, quality. Excluded: provenance (production metadata).

Input semantics: ``inputs`` is an UNORDERED DEPENDENCY SET (intentional v1
semantics). A feature depends on evidence; it does not assign positional
roles to it — the derivation's own code decides how each snapshot is used,
and that decision is versioned by ``derivation_id``/``derivation_version``,
not by argument order. Identity therefore sorts input snapshot ids, so the
same refs in any order produce the same ``feature_id``; duplicate snapshot
ids are rejected (a set has no duplicates). Role-labeled inputs would be an
additive future change if a concrete correctness need arises.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Tuple

from portfolio_automation.northstar.canonical import (
    CanonicalizationError,
    deterministic_id,
    encode_datetime,
    schema_era,
)
from portfolio_automation.northstar.evidence import EvidenceRef
from portfolio_automation.northstar.provenance import Provenance

SCHEMA_VERSION = "1.0.0"
CONTRACT_TYPE = "feature_record"

QUALITY_OK = "ok"
QUALITY_DEGRADED = "degraded"     # produced, but from impaired inputs — consumers decide
QUALITY_MISSING = "missing"       # could not be derived; value is None, explicitly
QUALITIES = frozenset({QUALITY_OK, QUALITY_DEGRADED, QUALITY_MISSING})

_MAX_SEQUENCE = 32


def _validate_value(value: Any, quality: str) -> None:
    if quality == QUALITY_MISSING:
        if value is not None:
            raise ValueError("quality='missing' requires value=None (missingness is explicit)")
        return
    if isinstance(value, bool) or isinstance(value, (int, str)):
        return
    if isinstance(value, float):
        import math
        if not math.isfinite(value):
            raise ValueError("feature value must be a finite number")
        return
    if isinstance(value, (list, tuple)):
        if not value or len(value) > _MAX_SEQUENCE:
            raise ValueError(f"sequence values must have 1..{_MAX_SEQUENCE} elements")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
            raise ValueError("sequence values must be numeric")
        import math
        if not all(math.isfinite(float(v)) for v in value):
            raise ValueError("sequence values must be finite")
        return
    raise ValueError(
        f"unsupported feature value type {type(value).__name__} — kernel v1 allows "
        "int/float/bool/str or a small numeric sequence"
    )


@dataclass(frozen=True, slots=True)
class FeatureRecord:
    feature_name: str            # e.g. "float_turnover_5d"
    derivation_id: str           # e.g. "derivation.float_turnover"
    derivation_version: str      # version of the transformation code/spec
    entity_id: str
    as_of: datetime              # PIT anchor of the derivation (tz-aware)
    value: Any                   # validated by _validate_value
    inputs: Tuple[EvidenceRef, ...]   # the evidence this feature was derived from
    provenance: Provenance
    quality: str = QUALITY_OK
    schema_version: str = SCHEMA_VERSION
    contract_type: str = field(default=CONTRACT_TYPE, init=False)

    def __post_init__(self) -> None:
        for name in ("feature_name", "derivation_id", "derivation_version", "entity_id"):
            value = getattr(self, name)
            if not value or not isinstance(value, str):
                raise ValueError(f"{name} is required")
        try:
            encode_datetime(self.as_of)
        except CanonicalizationError as exc:
            raise ValueError(f"as_of: {exc}") from exc
        if self.quality not in QUALITIES:
            raise ValueError(f"quality must be one of {sorted(QUALITIES)}, got {self.quality!r}")
        _validate_value(self.value, self.quality)
        inputs = self.inputs
        if not isinstance(inputs, tuple):
            if isinstance(inputs, list):
                inputs = tuple(inputs)
                object.__setattr__(self, "inputs", inputs)
            else:
                raise ValueError("inputs must be a tuple of EvidenceRef")
        if not all(isinstance(r, EvidenceRef) for r in inputs):
            raise ValueError("every input must be an EvidenceRef")
        if self.quality != QUALITY_MISSING and not inputs:
            raise ValueError(
                "a derived feature must reference its input evidence "
                "(empty inputs allowed only for quality='missing')"
            )
        # inputs are an unordered dependency SET — duplicates are incoherent.
        snapshot_ids = [r.snapshot_id for r in inputs]
        if len(snapshot_ids) != len(set(snapshot_ids)):
            raise ValueError("inputs must not contain duplicate snapshot ids")
        if not isinstance(self.provenance, Provenance):
            raise ValueError("provenance must be a Provenance")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"schema_version must be {SCHEMA_VERSION!r} (this kernel), "
                f"got {self.schema_version!r}"
            )
        # Canonical provenance must not contradict the record's own explicit
        # derivation identity. The transformation_id format, when present, is
        # exactly "<derivation_id>@<derivation_version>" so it can never carry
        # a conflicting id or version. (derivation_id/derivation_version stay
        # explicit fields — they are identity-bearing; provenance is not.)
        if self.provenance.transformation_id is not None:
            expected = f"{self.derivation_id}@{self.derivation_version}"
            if self.provenance.transformation_id != expected:
                raise ValueError(
                    "provenance.transformation_id contradicts the record's "
                    f"derivation identity: {self.provenance.transformation_id!r} "
                    f"!= {expected!r}"
                )
        # A feature must not masquerade as raw evidence.
        if self.feature_name.startswith("evs_") or self.derivation_id.startswith("evs_"):
            raise ValueError("feature identity fields must not impersonate evidence ids")

    def _identity_payload(self) -> dict:
        """Identity-bearing fields; ``schema_era`` (major version) participates
        because a semantic schema change re-defines field meaning. ``inputs``
        are sorted snapshot ids — an unordered dependency set (see module
        docstring)."""
        return {
            "contract_type": CONTRACT_TYPE,
            "schema_era": schema_era(self.schema_version),
            "feature_name": self.feature_name,
            "derivation_id": self.derivation_id,
            "derivation_version": self.derivation_version,
            "entity_id": self.entity_id,
            "as_of": self.as_of,
            "value": list(self.value) if isinstance(self.value, (list, tuple)) else self.value,
            "quality": self.quality,
            "inputs": sorted(r.snapshot_id for r in self.inputs),
        }

    @property
    def feature_id(self) -> str:
        return deterministic_id("ftr", self._identity_payload())

    def to_canonical_dict(self) -> dict:
        return {
            "contract_type": self.contract_type,
            "schema_version": self.schema_version,
            "feature_id": self.feature_id,
            "feature_name": self.feature_name,
            "derivation_id": self.derivation_id,
            "derivation_version": self.derivation_version,
            "entity_id": self.entity_id,
            "as_of": self.as_of,
            "value": list(self.value) if isinstance(self.value, (list, tuple)) else self.value,
            "quality": self.quality,
            "inputs": [r.to_canonical_dict() for r in self.inputs],
            "provenance": self.provenance.to_canonical_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FeatureRecord":
        from portfolio_automation.northstar.serde import (
            parse_optional_datetime,
            require_schema_version,
        )

        if data.get("contract_type") != CONTRACT_TYPE:
            raise ValueError(f"not a {CONTRACT_TYPE}: {data.get('contract_type')!r}")
        require_schema_version(data, expected=SCHEMA_VERSION, contract=CONTRACT_TYPE)
        as_of = parse_optional_datetime(data.get("as_of"))
        if as_of is None:
            raise ValueError("as_of is required")
        obj = cls(
            feature_name=data["feature_name"],
            derivation_id=data["derivation_id"],
            derivation_version=data["derivation_version"],
            entity_id=data["entity_id"],
            as_of=as_of,
            value=data["value"],
            inputs=tuple(EvidenceRef.from_dict(r) for r in data.get("inputs", [])),
            provenance=Provenance.from_dict(data["provenance"]),
            quality=data.get("quality", QUALITY_OK),
            schema_version=data["schema_version"],
        )
        recorded = data.get("feature_id")
        if recorded is not None and recorded != obj.feature_id:
            raise ValueError("feature_id mismatch — serialized identity does not reproduce")
        return obj
