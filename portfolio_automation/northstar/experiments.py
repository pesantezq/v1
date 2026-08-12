"""ExperimentSpec — the preregistered experiment definition (milestone 3).

Boundaries (docs/NORTHSTAR_CONTRACTS.md §6; authority: config/agent_policy.yaml):

* An ExperimentSpec is an IMMUTABLE, PREREGISTERED test definition: it names the
  falsifiable hypothesis (a ResearchClaim, ``rcl_``), the universe, the
  point-in-time ``as_of`` bound, the evaluation windows, the metrics, the
  preregistered success/abandon gates, and the allowed evidence classes. It
  defines WHAT will be tested BEFORE any result exists (the Intraday-Lab
  preregistration/identity-era discipline).
* Identity IS the full preregistration: any change to the hypothesis, universe,
  as_of, windows, metrics, gates, or allowed evidence is a DIFFERENT experiment
  (a new ``exs_`` id). Results reference the spec; they never edit it.
* An ExperimentSpec carries NO result data and NO portfolio-action authority.
  The separation is structural — this contract has no result or action fields
  and is frozen — so result data can never be written back into the
  specification, and the spec can never authorize a portfolio action.

All label collections are unordered sets (kernel input-set semantics).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Tuple

from portfolio_automation.northstar._collections import (
    normalize_ref_set,
    normalize_string_set,
    parse_ref_list,
)
from portfolio_automation.northstar.canonical import (
    CanonicalizationError,
    canonical_dumps,
    content_hash,
    deterministic_id,
    encode_datetime,
    schema_era,
    validate_contract_id,
)
from portfolio_automation.northstar.evidence import EvidenceRef
from portfolio_automation.northstar.provenance import Provenance

SCHEMA_VERSION = "1.0.0"
SPEC_CONTRACT_TYPE = "experiment_spec"
RESULT_CONTRACT_TYPE = "experiment_result"

# An ExperimentResult reports observations; it is a certification INPUT, never a
# promotion/approval/action. Authority-claiming observation keys are rejected.
_AUTHORITY_KEYS = frozenset({
    "approved", "approval", "approve", "certified", "certification", "certify",
    "promote", "promotion", "production_ready", "production", "authorize",
    "authorized", "execute", "execution", "trade", "allocate", "allocation",
})


def _validate_aware(name: str, value: datetime) -> None:
    try:
        encode_datetime(value)
    except CanonicalizationError as exc:
        raise ValueError(f"{name}: {exc}") from exc


def _reject_authority_keys(obj: Any, path: str = "observations") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in _AUTHORITY_KEYS:
                raise ValueError(
                    f"{path}.{k}: an ExperimentResult is a certification INPUT, never "
                    "a promotion/approval/action — authority-claiming fields are rejected"
                )
            _reject_authority_keys(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _reject_authority_keys(v, f"{path}[{i}]")


@dataclass(frozen=True, slots=True)
class ExperimentSpec:
    """An immutable, preregistered experiment definition. NOT a result; NOT an action."""

    hypothesis_claim_id: str                     # rcl_… — the falsifiable ResearchClaim under test
    universe: Tuple[str, ...]                     # entities/scope covered (non-empty; "*" = unrestricted, sole value)
    as_of: datetime                              # PIT bound: only evidence known_at <= as_of is admissible (0C)
    evaluation_windows: Tuple[str, ...]          # non-empty; e.g. "20d", "60d"
    metrics: Tuple[str, ...]                     # non-empty; namespaced, e.g. "return.excess_spy_20d"
    success_gate: str                            # preregistered success criterion
    abandon_gate: str                            # preregistered abandon/failure criterion
    provenance: Provenance = None                # type: ignore[assignment]  # REQUIRED attribution — NOT identity-bearing
    allowed_evidence_types: Tuple[str, ...] = ("*",)   # evidence classes the experiment may consume ("*" = unrestricted)
    notes: Optional[str] = None                  # NOT identity-bearing
    schema_version: str = SCHEMA_VERSION
    contract_type: str = field(default=SPEC_CONTRACT_TYPE, init=False)

    def __post_init__(self) -> None:
        for name in ("hypothesis_claim_id", "success_gate", "abandon_gate"):
            value = getattr(self, name)
            if not value or not isinstance(value, str):
                raise ValueError(f"{name} is required — an experiment must preregister it")
        # The hypothesis under test must be a well-formed ResearchClaim id.
        validate_contract_id("hypothesis_claim_id", self.hypothesis_claim_id, "rcl")
        _validate_aware("as_of", self.as_of)
        object.__setattr__(self, "universe",
                           normalize_string_set("universe", self.universe,
                                                allow_empty=False, wildcard=True))
        object.__setattr__(self, "evaluation_windows",
                           normalize_string_set("evaluation_windows", self.evaluation_windows,
                                                allow_empty=False))
        object.__setattr__(self, "metrics",
                           normalize_string_set("metrics", self.metrics, allow_empty=False))
        object.__setattr__(self, "allowed_evidence_types",
                           normalize_string_set("allowed_evidence_types", self.allowed_evidence_types,
                                                allow_empty=False, wildcard=True))
        if not isinstance(self.provenance, Provenance):
            raise ValueError("provenance must be a Provenance (required attribution)")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"schema_version must be {SCHEMA_VERSION!r} (this kernel), "
                f"got {self.schema_version!r}"
            )

    def _identity_payload(self) -> dict:
        # The full preregistration is identity-bearing: any change is a NEW
        # experiment. provenance/notes are attribution, never identity.
        return {
            "contract_type": SPEC_CONTRACT_TYPE,
            "schema_era": schema_era(self.schema_version),
            "hypothesis_claim_id": self.hypothesis_claim_id,
            "universe": sorted(self.universe),
            "as_of": self.as_of,
            "evaluation_windows": sorted(self.evaluation_windows),
            "metrics": sorted(self.metrics),
            "success_gate": self.success_gate,
            "abandon_gate": self.abandon_gate,
            "allowed_evidence_types": sorted(self.allowed_evidence_types),
        }

    @property
    def experiment_spec_id(self) -> str:
        return deterministic_id("exs", self._identity_payload())

    def to_canonical_dict(self) -> dict:
        return {
            "contract_type": self.contract_type,
            "schema_version": self.schema_version,
            "experiment_spec_id": self.experiment_spec_id,
            "hypothesis_claim_id": self.hypothesis_claim_id,
            "universe": sorted(self.universe),
            "as_of": self.as_of,
            "evaluation_windows": sorted(self.evaluation_windows),
            "metrics": sorted(self.metrics),
            "success_gate": self.success_gate,
            "abandon_gate": self.abandon_gate,
            "allowed_evidence_types": sorted(self.allowed_evidence_types),
            "notes": self.notes,
            "provenance": self.provenance.to_canonical_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ExperimentSpec":
        from portfolio_automation.northstar.serde import (
            parse_optional_datetime,
            require_schema_version,
        )

        if data.get("contract_type") != SPEC_CONTRACT_TYPE:
            raise ValueError(f"not a {SPEC_CONTRACT_TYPE}: {data.get('contract_type')!r}")
        require_schema_version(data, expected=SCHEMA_VERSION, contract=SPEC_CONTRACT_TYPE)
        as_of = parse_optional_datetime(data.get("as_of"))
        if as_of is None:
            raise ValueError("as_of is required")
        obj = cls(
            hypothesis_claim_id=data["hypothesis_claim_id"],
            universe=data["universe"],
            as_of=as_of,
            evaluation_windows=data["evaluation_windows"],
            metrics=data["metrics"],
            success_gate=data["success_gate"],
            abandon_gate=data["abandon_gate"],
            provenance=Provenance.from_dict(data["provenance"]),
            allowed_evidence_types=data.get("allowed_evidence_types", ("*",)),
            notes=data.get("notes"),
            schema_version=data["schema_version"],
        )
        recorded = data.get("experiment_spec_id")
        if recorded is not None and recorded != obj.experiment_spec_id:
            raise ValueError("experiment_spec_id mismatch — serialized identity does not reproduce")
        return obj


@dataclass(frozen=True, slots=True)
class ExperimentResult:
    """Results produced under one ExperimentSpec. A certification INPUT, never a promotion.

    Boundaries: an ExperimentResult REFERENCES its ExperimentSpec by id and can
    never rewrite it — it holds NO spec fields (the preregistered universe,
    metrics, and gates live only in the spec), so preregistered criteria can
    never be retrospectively manipulated. ``observations`` is a strict-JSON
    document frozen to canonical bytes at construction (snapshot-payload
    discipline): caller mutation can never alter the stored result or its
    identity, and authority-claiming observation keys (approve/certify/allocate…)
    are structurally rejected — a result is a certification input, never a
    promotion or action. Reproducibility: the result pins the evidence it
    consumed, the model, and the code version that produced it. A partial run
    must declare itself explicitly.
    """

    experiment_spec_id: str                       # exs_… — the spec this result is for; NEVER edited
    provenance: Provenance                        # required; code/model provenance (reproducibility)
    windows_evaluated: Tuple[str, ...]            # non-empty; which of the spec's windows were run
    evidence_refs: Tuple[EvidenceRef, ...] = ()   # evidence consumed (unordered set; may be empty for pure computation)
    partial: bool = False                         # a partial run must say so explicitly
    partial_reason: Optional[str] = None
    observations_canonical: str = field(init=False)
    observations_hash: str = field(init=False)
    notes: Optional[str] = None                   # NOT identity-bearing
    schema_version: str = SCHEMA_VERSION
    contract_type: str = field(default=RESULT_CONTRACT_TYPE, init=False)
    # Construction-only input (snapshot-payload discipline).
    observations: Any = None

    def __post_init__(self) -> None:
        validate_contract_id("experiment_spec_id", self.experiment_spec_id, "exs")
        if not isinstance(self.provenance, Provenance):
            raise ValueError("provenance must be a Provenance (required attribution)")
        object.__setattr__(self, "windows_evaluated",
                           normalize_string_set("windows_evaluated", self.windows_evaluated,
                                                allow_empty=False))
        object.__setattr__(self, "evidence_refs",
                           normalize_ref_set("evidence_refs", self.evidence_refs, EvidenceRef, "snapshot_id"))
        if self.partial:
            if not self.partial_reason or not isinstance(self.partial_reason, str):
                raise ValueError("a partial ExperimentResult must state its partial_reason")
        elif self.partial_reason is not None:
            raise ValueError("partial_reason requires partial=True")
        raw = self.observations
        if not isinstance(raw, dict) or not raw:
            raise ValueError("observations must be a non-empty JSON object (a result reports metrics)")
        _reject_authority_keys(raw)
        object.__setattr__(self, "observations_canonical", canonical_dumps(raw))
        object.__setattr__(self, "observations_hash", content_hash(raw))
        object.__setattr__(self, "observations", None)
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"schema_version must be {SCHEMA_VERSION!r} (this kernel), "
                f"got {self.schema_version!r}"
            )

    def observations_copy(self) -> dict:
        """A fresh deep copy of the observations; mutating it changes nothing."""
        return json.loads(self.observations_canonical)

    def _identity_payload(self) -> dict:
        # Reproducibility identity: the result pins its spec, the consumed
        # evidence, completeness, the model, and the code version that produced
        # it (an experiment result identifies exactly what it ran).
        return {
            "contract_type": RESULT_CONTRACT_TYPE,
            "schema_era": schema_era(self.schema_version),
            "experiment_spec_id": self.experiment_spec_id,
            "evidence_refs": sorted(r.snapshot_id for r in self.evidence_refs),
            "windows_evaluated": sorted(self.windows_evaluated),
            "partial": self.partial,
            "partial_reason": self.partial_reason,
            "model_id": self.provenance.model_id,
            "code_version": self.provenance.code_version,
            "observations_hash": self.observations_hash,
        }

    @property
    def experiment_result_id(self) -> str:
        return deterministic_id("exr", self._identity_payload())

    def to_canonical_dict(self) -> dict:
        return {
            "contract_type": self.contract_type,
            "schema_version": self.schema_version,
            "experiment_result_id": self.experiment_result_id,
            "experiment_spec_id": self.experiment_spec_id,
            "evidence_refs": [r.to_canonical_dict() for r in sorted(self.evidence_refs, key=lambda r: r.snapshot_id)],
            "windows_evaluated": sorted(self.windows_evaluated),
            "partial": self.partial,
            "partial_reason": self.partial_reason,
            "observations": json.loads(self.observations_canonical),
            "observations_hash": self.observations_hash,
            "notes": self.notes,
            "provenance": self.provenance.to_canonical_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ExperimentResult":
        from portfolio_automation.northstar.serde import require_schema_version

        if data.get("contract_type") != RESULT_CONTRACT_TYPE:
            raise ValueError(f"not a {RESULT_CONTRACT_TYPE}: {data.get('contract_type')!r}")
        require_schema_version(data, expected=SCHEMA_VERSION, contract=RESULT_CONTRACT_TYPE)
        obj = cls(
            experiment_spec_id=data["experiment_spec_id"],
            provenance=Provenance.from_dict(data["provenance"]),
            windows_evaluated=data["windows_evaluated"],
            evidence_refs=parse_ref_list("evidence_refs", data.get("evidence_refs", ()),
                                         EvidenceRef.from_dict),
            partial=data.get("partial", False),
            partial_reason=data.get("partial_reason"),
            notes=data.get("notes"),
            schema_version=data["schema_version"],
            observations=data.get("observations"),
        )
        for name, recorded, actual in (
            ("observations_hash", data.get("observations_hash"), obj.observations_hash),
            ("experiment_result_id", data.get("experiment_result_id"), obj.experiment_result_id),
        ):
            if recorded is not None and recorded != actual:
                raise ValueError(f"{name} mismatch — serialized identity does not reproduce")
        return obj
