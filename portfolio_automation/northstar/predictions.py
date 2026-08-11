"""PredictionTask + PredictionRecord — the prediction contract family (0B.2).

Boundaries (architecture law, docs/NORTHSTAR_CONTRACTS.md §6):

* A PredictionTask defines the QUESTION (universe, as_of, horizon, target,
  allowed evidence/feature scope). It never carries results.
* A PredictionRecord records one ESTIMATE with mandatory uncertainty and the
  exact evidence/features it consumed. **Prediction is never a portfolio
  action**: no allocation, sizing, order, or approval surface exists on these
  contracts, and none may be added — capital determination belongs to the
  future certified Capital & Risk Engine via a separate CapitalProposal that
  REFERENCES prediction ids (milestone 3).
* Resolution linkage is by REFERENCE ONLY: a future OutcomeRecord will point
  at ``prediction_id``. A PredictionRecord has no mutable "resolved" field —
  records are immutable; resolution is a new object, never an edit.

Universe/evidence/feature scopes are UNORDERED SETS (kernel input-set
semantics): identity sorts them, duplicates are rejected.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Tuple

from portfolio_automation.northstar._collections import (
    normalize_ref_set,
    normalize_string_set,
)
from portfolio_automation.northstar.canonical import (
    CanonicalizationError,
    canonical_dumps,
    deterministic_id,
    encode_datetime,
    schema_era,
    validate_contract_id,
)
from portfolio_automation.northstar.evidence import EvidenceRef
from portfolio_automation.northstar.provenance import Provenance

SCHEMA_VERSION = "1.0.0"
TASK_CONTRACT_TYPE = "prediction_task"
RECORD_CONTRACT_TYPE = "prediction_record"

#: Value kinds a prediction estimate/uncertainty may take (kernel v1): finite
#: numeric scalar, bool, categorical string, or a small numeric sequence
#: (e.g. quantiles) — mirrors FeatureRecord's value discipline.
_MAX_SEQUENCE = 32

# Fields that would turn a prediction into an action — structurally banned.
_FORBIDDEN_SURFACE = frozenset({
    "allocation", "allocate", "size", "sizing", "weight", "order", "execute",
    "execution", "trade", "buy", "sell", "approve", "approval", "promote",
    "promotion", "position",
})


def _validate_scalar_or_sequence(name: str, value: Any) -> None:
    import math

    if isinstance(value, bool) or isinstance(value, (int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
        return
    if isinstance(value, (list, tuple)):
        if not value or len(value) > _MAX_SEQUENCE:
            raise ValueError(f"{name} sequence must have 1..{_MAX_SEQUENCE} elements")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
            raise ValueError(f"{name} sequence must be numeric")
        if not all(math.isfinite(float(v)) for v in value):
            raise ValueError(f"{name} sequence must be finite")
        return
    raise ValueError(
        f"unsupported {name} type {type(value).__name__} — kernel v1 allows "
        "int/float/bool/str or a small numeric sequence"
    )


def _validate_aware(name: str, value: datetime) -> None:
    try:
        encode_datetime(value)
    except CanonicalizationError as exc:
        raise ValueError(f"{name}: {exc}") from exc


def _reject_action_surface(name: str, mapping: Optional[dict]) -> None:
    """No prediction contract may smuggle an allocation/action field."""
    if not mapping:
        return

    def _scan(obj: Any, path: str) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(k, str) and k.lower() in _FORBIDDEN_SURFACE:
                    raise ValueError(
                        f"{name}{path}.{k}: predictions are never portfolio actions — "
                        "allocation/action fields are structurally banned"
                    )
                _scan(v, f"{path}.{k}")
        elif isinstance(obj, (list, tuple)):
            for i, v in enumerate(obj):
                _scan(v, f"{path}[{i}]")

    _scan(mapping, "")


@dataclass(frozen=True, slots=True)
class PredictionTask:
    """The prediction QUESTION. Immutable; a changed question is a new task."""

    entity_ids: Tuple[str, ...]          # universe (unordered set, non-empty)
    as_of: datetime                      # PIT anchor: evidence must satisfy known_at <= as_of (0C)
    horizon_days: int                    # forecast horizon
    target: str                          # namespaced target, e.g. "return.total"
    allowed_evidence_types: Tuple[str, ...]   # evidence-type scope (non-empty; "*" = explicitly unrestricted, sole value)
    provenance: Provenance               # REQUIRED attribution (who defined this question) — NOT identity-bearing
    allowed_feature_names: Tuple[str, ...] = ()  # feature scope (may be empty = features not required)
    notes: Optional[str] = None                # human context — NOT identity-bearing
    target_params_canonical: Optional[str] = field(init=False, default=None)
    schema_version: str = SCHEMA_VERSION
    contract_type: str = field(default=TASK_CONTRACT_TYPE, init=False)
    # Construction-only input (snapshot-payload discipline): validated, frozen
    # to canonical bytes, and the caller's mutable reference is dropped —
    # task identity can never change after construction.
    target_params: Optional[dict] = None       # strict-JSON params refining the target (identity-bearing)

    def __post_init__(self) -> None:
        # Strict container discipline + sorted canonical normalization.
        object.__setattr__(self, "entity_ids",
                           normalize_string_set("entity_ids", self.entity_ids, allow_empty=False))
        object.__setattr__(self, "allowed_evidence_types",
                           normalize_string_set("allowed_evidence_types", self.allowed_evidence_types,
                                                allow_empty=False, wildcard=True))
        object.__setattr__(self, "allowed_feature_names",
                           normalize_string_set("allowed_feature_names", self.allowed_feature_names,
                                                allow_empty=True))
        if not isinstance(self.provenance, Provenance):
            raise ValueError("provenance must be a Provenance (required attribution)")
        _validate_aware("as_of", self.as_of)
        if not isinstance(self.horizon_days, int) or isinstance(self.horizon_days, bool) or self.horizon_days <= 0:
            raise ValueError("horizon_days must be a positive integer")
        if not self.target or not isinstance(self.target, str):
            raise ValueError("target is required")
        raw_params = self.target_params
        if raw_params is not None:
            if not isinstance(raw_params, dict) or not raw_params:
                raise ValueError("target_params must be a non-empty mapping or None")
            _reject_action_surface("target_params", raw_params)
            # Freeze by value: strict canonical validation, then the canonical
            # string is the truth and the caller's mutable dict is dropped.
            object.__setattr__(self, "target_params_canonical", canonical_dumps(raw_params))
        object.__setattr__(self, "target_params", None)
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"schema_version must be {SCHEMA_VERSION!r} (this kernel), "
                f"got {self.schema_version!r}"
            )

    def target_params_copy(self) -> Optional[dict]:
        """A FRESH deep copy of target_params (or None); mutating it changes nothing."""
        if self.target_params_canonical is None:
            return None
        return json.loads(self.target_params_canonical)

    def _identity_payload(self) -> dict:
        return {
            "contract_type": TASK_CONTRACT_TYPE,
            "schema_era": schema_era(self.schema_version),
            "entity_ids": sorted(self.entity_ids),
            "as_of": self.as_of,
            "horizon_days": self.horizon_days,
            "target": self.target,
            "target_params": self.target_params_copy(),
            "allowed_evidence_types": sorted(self.allowed_evidence_types),
            "allowed_feature_names": sorted(self.allowed_feature_names),
        }

    @property
    def task_id(self) -> str:
        return deterministic_id("ptk", self._identity_payload())

    def to_canonical_dict(self) -> dict:
        return {
            "contract_type": self.contract_type,
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "entity_ids": sorted(self.entity_ids),
            "as_of": self.as_of,
            "horizon_days": self.horizon_days,
            "target": self.target,
            "target_params": self.target_params_copy(),
            "allowed_evidence_types": sorted(self.allowed_evidence_types),
            "allowed_feature_names": sorted(self.allowed_feature_names),
            "notes": self.notes,
            "provenance": self.provenance.to_canonical_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PredictionTask":
        from portfolio_automation.northstar.serde import (
            parse_optional_datetime,
            require_schema_version,
        )

        if data.get("contract_type") != TASK_CONTRACT_TYPE:
            raise ValueError(f"not a {TASK_CONTRACT_TYPE}: {data.get('contract_type')!r}")
        require_schema_version(data, expected=SCHEMA_VERSION, contract=TASK_CONTRACT_TYPE)
        as_of = parse_optional_datetime(data.get("as_of"))
        if as_of is None:
            raise ValueError("as_of is required")
        obj = cls(
            entity_ids=tuple(data["entity_ids"]),
            as_of=as_of,
            horizon_days=data["horizon_days"],
            target=data["target"],
            target_params=data.get("target_params"),
            allowed_evidence_types=tuple(data["allowed_evidence_types"]),
            provenance=Provenance.from_dict(data["provenance"]),
            allowed_feature_names=tuple(data.get("allowed_feature_names", ())),
            notes=data.get("notes"),
            schema_version=data["schema_version"],
        )
        recorded = data.get("task_id")
        if recorded is not None and recorded != obj.task_id:
            raise ValueError("task_id mismatch — serialized identity does not reproduce")
        return obj


@dataclass(frozen=True, slots=True)
class PredictionRecord:
    """One estimate for one entity under one PredictionTask.

    Uncertainty is REQUIRED — a prediction with implied certainty is invalid.
    Evidence refs are REQUIRED — a prediction that cannot state its evidence
    is invalid. Provenance model identity, when present, must agree with the
    record's explicit model fields (kernel consistency rule).
    """

    task_id: str                          # ptk_… (validated)
    entity_id: str
    as_of: datetime                       # when the estimate takes effect (tz-aware)
    horizon_days: int                     # self-describing copy of the task horizon
    prediction_kind: str                  # namespaced, e.g. "point_estimate", "probability", "quantiles"
    prediction_value: Optional[Any]       # scalar or small numeric sequence; None ONLY when abstained
    uncertainty_kind: Optional[str]       # namespaced, e.g. "stdev", "quantile_band"; None ONLY when abstained
    uncertainty_value: Optional[Any]      # REQUIRED unless abstained — no implied certainty
    model_id: str
    model_version: str
    evidence_refs: Tuple[EvidenceRef, ...]     # unordered set; NON-EMPTY unless abstained
    provenance: Provenance
    feature_ids: Tuple[str, ...] = ()          # ftr_… ids (unordered set, may be empty)
    abstained: bool = False               # first-class abstention — never encoded via 0/NaN/magic values
    abstention_reason: Optional[str] = None    # REQUIRED iff abstained
    schema_version: str = SCHEMA_VERSION
    contract_type: str = field(default=RECORD_CONTRACT_TYPE, init=False)

    def __post_init__(self) -> None:
        validate_contract_id("task_id", self.task_id, "ptk")
        for name in ("entity_id", "prediction_kind", "model_id", "model_version"):
            value = getattr(self, name)
            if not value or not isinstance(value, str):
                raise ValueError(f"{name} is required")
        _validate_aware("as_of", self.as_of)
        if not isinstance(self.horizon_days, int) or isinstance(self.horizon_days, bool) or self.horizon_days <= 0:
            raise ValueError("horizon_days must be a positive integer")
        # First-class abstention: mutually exclusive shapes, no magic values.
        if self.abstained:
            if not self.abstention_reason or not isinstance(self.abstention_reason, str):
                raise ValueError("an abstaining prediction must state its abstention_reason")
            for name in ("prediction_value", "uncertainty_kind", "uncertainty_value"):
                if getattr(self, name) is not None:
                    raise ValueError(
                        f"{name} must be None when abstained — abstention is never "
                        "encoded via zeros, NaNs, or placeholder values"
                    )
        else:
            if self.abstention_reason is not None:
                raise ValueError("abstention_reason requires abstained=True")
            if self.prediction_value is None:
                raise ValueError("prediction_value is required unless abstained")
            if not self.uncertainty_kind or not isinstance(self.uncertainty_kind, str):
                raise ValueError("uncertainty_kind is required unless abstained")
            _validate_scalar_or_sequence("prediction_value", self.prediction_value)
            if self.uncertainty_value is None:
                raise ValueError("uncertainty_value is required — no implied certainty")
            _validate_scalar_or_sequence("uncertainty_value", self.uncertainty_value)
        # Sequence immutability: numeric sequences freeze to tuples so caller
        # mutation can never change value, equality, serialization, or the id;
        # equivalent list/tuple inputs normalize identically.
        for name in ("prediction_value", "uncertainty_value"):
            value = getattr(self, name)
            if isinstance(value, list):
                object.__setattr__(self, name, tuple(value))
        # Strict container discipline + sorted canonical normalization.
        object.__setattr__(
            self, "evidence_refs",
            normalize_ref_set("evidence_refs", self.evidence_refs, EvidenceRef, "snapshot_id"),
        )
        if not self.abstained and not self.evidence_refs:
            raise ValueError(
                "a prediction that cannot state its evidence is invalid — "
                "evidence_refs must be non-empty (empty is permitted only when abstained)"
            )
        object.__setattr__(
            self, "feature_ids",
            normalize_string_set("feature_ids", self.feature_ids, allow_empty=True),
        )
        for fid in self.feature_ids:
            validate_contract_id("feature_ids entry", fid, "ftr")
        if not isinstance(self.provenance, Provenance):
            raise ValueError("provenance must be a Provenance")
        # Provenance model identity must not contradict the explicit fields.
        if self.provenance.model_id is not None:
            expected = f"{self.model_id}@{self.model_version}"
            if self.provenance.model_id != expected:
                raise ValueError(
                    f"provenance.model_id contradicts the record's model identity: "
                    f"{self.provenance.model_id!r} != {expected!r}"
                )
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"schema_version must be {SCHEMA_VERSION!r} (this kernel), "
                f"got {self.schema_version!r}"
            )

    @staticmethod
    def _value_view(value: Any) -> Any:
        return list(value) if isinstance(value, (list, tuple)) else value

    def _identity_payload(self) -> dict:
        return {
            "contract_type": RECORD_CONTRACT_TYPE,
            "schema_era": schema_era(self.schema_version),
            "task_id": self.task_id,
            "entity_id": self.entity_id,
            "as_of": self.as_of,
            "horizon_days": self.horizon_days,
            "prediction_kind": self.prediction_kind,
            "prediction_value": self._value_view(self.prediction_value),
            "uncertainty_kind": self.uncertainty_kind,
            "uncertainty_value": self._value_view(self.uncertainty_value),
            "model_id": self.model_id,
            "model_version": self.model_version,
            "abstained": self.abstained,
            "abstention_reason": self.abstention_reason,
            "evidence_refs": sorted(r.snapshot_id for r in self.evidence_refs),
            "feature_ids": sorted(self.feature_ids),
        }

    @property
    def prediction_id(self) -> str:
        return deterministic_id("prd", self._identity_payload())

    def to_canonical_dict(self) -> dict:
        return {
            "contract_type": self.contract_type,
            "schema_version": self.schema_version,
            "prediction_id": self.prediction_id,
            "task_id": self.task_id,
            "entity_id": self.entity_id,
            "as_of": self.as_of,
            "horizon_days": self.horizon_days,
            "prediction_kind": self.prediction_kind,
            "prediction_value": self._value_view(self.prediction_value),
            "uncertainty_kind": self.uncertainty_kind,
            "uncertainty_value": self._value_view(self.uncertainty_value),
            "model_id": self.model_id,
            "model_version": self.model_version,
            "abstained": self.abstained,
            "abstention_reason": self.abstention_reason,
            "evidence_refs": [r.to_canonical_dict() for r in sorted(self.evidence_refs, key=lambda r: r.snapshot_id)],
            "feature_ids": sorted(self.feature_ids),
            "provenance": self.provenance.to_canonical_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PredictionRecord":
        from portfolio_automation.northstar.serde import (
            parse_optional_datetime,
            require_schema_version,
        )

        if data.get("contract_type") != RECORD_CONTRACT_TYPE:
            raise ValueError(f"not a {RECORD_CONTRACT_TYPE}: {data.get('contract_type')!r}")
        require_schema_version(data, expected=SCHEMA_VERSION, contract=RECORD_CONTRACT_TYPE)
        as_of = parse_optional_datetime(data.get("as_of"))
        if as_of is None:
            raise ValueError("as_of is required")
        obj = cls(
            task_id=data["task_id"],
            entity_id=data["entity_id"],
            as_of=as_of,
            horizon_days=data["horizon_days"],
            prediction_kind=data["prediction_kind"],
            prediction_value=data.get("prediction_value"),
            uncertainty_kind=data.get("uncertainty_kind"),
            uncertainty_value=data.get("uncertainty_value"),
            model_id=data["model_id"],
            model_version=data["model_version"],
            evidence_refs=tuple(EvidenceRef.from_dict(r) for r in data.get("evidence_refs", ())),
            feature_ids=tuple(data.get("feature_ids", ())),
            provenance=Provenance.from_dict(data["provenance"]),
            abstained=data.get("abstained", False),
            abstention_reason=data.get("abstention_reason"),
            schema_version=data["schema_version"],
        )
        recorded = data.get("prediction_id")
        if recorded is not None and recorded != obj.prediction_id:
            raise ValueError("prediction_id mismatch — serialized identity does not reproduce")
        return obj
