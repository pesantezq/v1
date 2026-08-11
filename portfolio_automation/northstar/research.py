"""ResearchTask + WorkerResult + ResearchClaim — the research contract family.

Boundaries (docs/NORTHSTAR_CONTRACTS.md §6; authority: config/agent_policy.yaml):

* A ResearchTask defines BOUNDED research work, compatible with future
  sandbox/control-plane execution under the Prime-free Local R&D direction.
  It defines the question; it never executes anything.
* A WorkerResult is a research worker's structured output. **It is research
  material, never production truth**: findings claiming authority (approval,
  certification, promotion, execution, allocation…) are structurally
  rejected; abstention is a first-class outcome; confidence is mandatory when
  not abstaining.
* A ResearchClaim is a FALSIFIABLE hypothesis distilled from evidence/worker
  results — falsifiability is structural (a testable metric + direction are
  required). A claim is NOT certified alpha; certification arrives only via
  the future ExperimentSpec/ExperimentResult path (milestone 3) and StratLab.

All ref collections are unordered sets (kernel input-set semantics).
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
    content_hash,
    deterministic_id,
    encode_datetime,
    schema_era,
    validate_contract_id,
)
from portfolio_automation.northstar.evidence import EvidenceRef
from portfolio_automation.northstar.provenance import Provenance

SCHEMA_VERSION = "1.0.0"
TASK_CONTRACT_TYPE = "research_task"
RESULT_CONTRACT_TYPE = "worker_result"
CLAIM_CONTRACT_TYPE = "research_claim"

EFFORT_CLASSES = frozenset({"micro", "small", "standard", "extended"})

#: WorkerResult v1 producer types: a worker is an AI worker or a trusted
#: system tool. source_adapter/derivation/human provenance on a WorkerResult
#: is incoherent and rejected (0B.2 hardening, repair 4).
WORKER_PRODUCER_TYPES = frozenset({"ai_worker", "system"})
CLAIM_DIRECTIONS = frozenset({"increase", "decrease", "no_effect", "conditional"})

# Keys that would represent an authority claim inside worker findings —
# a WorkerResult can never be production truth, certification, or an action.
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


def _reject_authority_keys(obj: Any, path: str = "findings") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in _AUTHORITY_KEYS:
                raise ValueError(
                    f"{path}.{k}: a WorkerResult is research material, never "
                    "production truth — authority-claiming fields are rejected"
                )
            _reject_authority_keys(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _reject_authority_keys(v, f"{path}[{i}]")


@dataclass(frozen=True, slots=True)
class ResearchTask:
    """Bounded research work definition (sandbox-compatible; never executes)."""

    question: str
    as_of: datetime                            # PIT bound: workers may only see evidence with known_at <= as_of (0C)
    allowed_evidence_types: Tuple[str, ...]    # non-empty; "*" = explicitly unrestricted (sole value)
    output_expectation: str                    # namespaced, e.g. "worker_result.findings"
    provenance: Provenance = None              # type: ignore[assignment]  # REQUIRED attribution — NOT identity-bearing
    effort_class: str = "standard"             # one of EFFORT_CLASSES (budget/effort bound)
    scope_entities: Tuple[str, ...] = ()       # may be empty = not entity-scoped
    notes: Optional[str] = None                # NOT identity-bearing
    schema_version: str = SCHEMA_VERSION
    contract_type: str = field(default=TASK_CONTRACT_TYPE, init=False)

    def __post_init__(self) -> None:
        if not self.question or not isinstance(self.question, str):
            raise ValueError("question is required")
        if not self.output_expectation or not isinstance(self.output_expectation, str):
            raise ValueError("output_expectation is required")
        _validate_aware("as_of", self.as_of)
        if not isinstance(self.provenance, Provenance):
            raise ValueError("provenance must be a Provenance (required attribution)")
        object.__setattr__(self, "allowed_evidence_types",
                           normalize_string_set("allowed_evidence_types", self.allowed_evidence_types,
                                                allow_empty=False, wildcard=True))
        object.__setattr__(self, "scope_entities",
                           normalize_string_set("scope_entities", self.scope_entities, allow_empty=True))
        if self.effort_class not in EFFORT_CLASSES:
            raise ValueError(
                f"effort_class must be one of {sorted(EFFORT_CLASSES)}, got {self.effort_class!r}"
            )
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"schema_version must be {SCHEMA_VERSION!r} (this kernel), "
                f"got {self.schema_version!r}"
            )

    def _identity_payload(self) -> dict:
        return {
            "contract_type": TASK_CONTRACT_TYPE,
            "schema_era": schema_era(self.schema_version),
            "question": self.question,
            "as_of": self.as_of,
            "allowed_evidence_types": sorted(self.allowed_evidence_types),
            "output_expectation": self.output_expectation,
            "effort_class": self.effort_class,
            "scope_entities": sorted(self.scope_entities),
        }

    @property
    def research_task_id(self) -> str:
        return deterministic_id("rtk", self._identity_payload())

    def to_canonical_dict(self) -> dict:
        return {
            "contract_type": self.contract_type,
            "schema_version": self.schema_version,
            "research_task_id": self.research_task_id,
            "question": self.question,
            "as_of": self.as_of,
            "allowed_evidence_types": sorted(self.allowed_evidence_types),
            "output_expectation": self.output_expectation,
            "effort_class": self.effort_class,
            "scope_entities": sorted(self.scope_entities),
            "notes": self.notes,
            "provenance": self.provenance.to_canonical_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ResearchTask":
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
            question=data["question"],
            as_of=as_of,
            allowed_evidence_types=tuple(data["allowed_evidence_types"]),
            output_expectation=data["output_expectation"],
            provenance=Provenance.from_dict(data["provenance"]),
            effort_class=data.get("effort_class", "standard"),
            scope_entities=tuple(data.get("scope_entities", ())),
            notes=data.get("notes"),
            schema_version=data["schema_version"],
        )
        recorded = data.get("research_task_id")
        if recorded is not None and recorded != obj.research_task_id:
            raise ValueError("research_task_id mismatch — serialized identity does not reproduce")
        return obj


@dataclass(frozen=True, slots=True)
class WorkerResult:
    """A research worker's structured output. Research material, never truth.

    ``findings`` is a strict-JSON document frozen to canonical bytes at
    construction (snapshot-payload discipline): caller mutation can never
    alter the stored result or its identity. Authority-claiming keys are
    structurally rejected. Abstention is first-class: an abstaining worker
    provides a reason and no findings; a non-abstaining worker provides
    findings AND a confidence in [0, 1].
    """

    research_task_id: str                 # rtk_… (validated)
    worker_id: str                        # e.g. "worker.generic_researcher"
    provenance: Provenance
    evidence_refs: Tuple[EvidenceRef, ...] = ()   # evidence consumed (unordered set; may be empty for pure reasoning — disclosed by emptiness)
    abstained: bool = False
    abstention_reason: Optional[str] = None
    confidence: Optional[float] = None    # REQUIRED in [0,1] unless abstained
    findings_canonical: str = field(init=False)
    findings_hash: str = field(init=False)
    schema_version: str = SCHEMA_VERSION
    contract_type: str = field(default=RESULT_CONTRACT_TYPE, init=False)
    # Construction-only input (snapshot-payload discipline).
    findings: Any = None

    def __post_init__(self) -> None:
        validate_contract_id("research_task_id", self.research_task_id, "rtk")
        if not self.worker_id or not isinstance(self.worker_id, str):
            raise ValueError("worker_id is required")
        if not isinstance(self.provenance, Provenance):
            raise ValueError("provenance must be a Provenance")
        # Producer consistency (repair 4): the provenance must BE the worker.
        if self.provenance.producer_type not in WORKER_PRODUCER_TYPES:
            raise ValueError(
                f"WorkerResult provenance.producer_type must be one of "
                f"{sorted(WORKER_PRODUCER_TYPES)}, got {self.provenance.producer_type!r}"
            )
        if self.provenance.producer_id != self.worker_id:
            raise ValueError(
                f"provenance.producer_id {self.provenance.producer_id!r} must equal "
                f"worker_id {self.worker_id!r} — a result's provenance is its worker"
            )
        object.__setattr__(
            self, "evidence_refs",
            normalize_ref_set("evidence_refs", self.evidence_refs, EvidenceRef, "snapshot_id"),
        )
        raw = self.findings
        if self.abstained:
            if not self.abstention_reason or not isinstance(self.abstention_reason, str):
                raise ValueError("an abstaining worker must state its abstention_reason")
            if raw not in (None, {}):
                raise ValueError("an abstaining worker provides no findings")
            if self.confidence is not None:
                raise ValueError("confidence is meaningless when abstaining")
            raw = {}
        else:
            if self.abstention_reason is not None:
                raise ValueError("abstention_reason requires abstained=True")
            if not isinstance(raw, dict) or not raw:
                raise ValueError("a non-abstaining worker must provide non-empty findings")
            if self.confidence is None:
                raise ValueError("confidence is required — no implied certainty")
            if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
                raise ValueError("confidence must be a number in [0, 1]")
            if not (0.0 <= float(self.confidence) <= 1.0):
                raise ValueError("confidence must be within [0, 1]")
        _reject_authority_keys(raw)
        canonical = canonical_dumps(raw)
        object.__setattr__(self, "findings_canonical", canonical)
        object.__setattr__(self, "findings_hash", content_hash(raw))
        object.__setattr__(self, "findings", None)
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"schema_version must be {SCHEMA_VERSION!r} (this kernel), "
                f"got {self.schema_version!r}"
            )

    def findings_copy(self) -> dict:
        """A fresh deep copy of the findings; mutating it changes nothing."""
        return json.loads(self.findings_canonical)

    def _identity_payload(self) -> dict:
        # Model identity IS semantic (repair 5): the same findings produced by
        # a different model are a different result. provenance.recorded_at
        # stays non-identity-bearing (acquisition metadata, kernel-wide rule).
        # provenance.code_version is deliberately EXCLUDED from identity: a
        # WorkerResult is research material, never truth — reproduction and
        # attribution happen through the milestone-3 ExperimentSpec path,
        # which pins its own code identity; including worker code_version here
        # would fragment identity on every routine deployment without any
        # semantic change to the result.
        return {
            "contract_type": RESULT_CONTRACT_TYPE,
            "schema_era": schema_era(self.schema_version),
            "research_task_id": self.research_task_id,
            "worker_id": self.worker_id,
            "model_id": self.provenance.model_id,
            "evidence_refs": sorted(r.snapshot_id for r in self.evidence_refs),
            "abstained": self.abstained,
            "abstention_reason": self.abstention_reason,
            "confidence": self.confidence,
            "findings_hash": self.findings_hash,
        }

    @property
    def worker_result_id(self) -> str:
        return deterministic_id("wkr", self._identity_payload())

    def to_canonical_dict(self) -> dict:
        return {
            "contract_type": self.contract_type,
            "schema_version": self.schema_version,
            "worker_result_id": self.worker_result_id,
            "research_task_id": self.research_task_id,
            "worker_id": self.worker_id,
            "evidence_refs": [r.to_canonical_dict() for r in sorted(self.evidence_refs, key=lambda r: r.snapshot_id)],
            "abstained": self.abstained,
            "abstention_reason": self.abstention_reason,
            "confidence": self.confidence,
            "findings": json.loads(self.findings_canonical),
            "findings_hash": self.findings_hash,
            "provenance": self.provenance.to_canonical_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WorkerResult":
        from portfolio_automation.northstar.serde import require_schema_version

        if data.get("contract_type") != RESULT_CONTRACT_TYPE:
            raise ValueError(f"not a {RESULT_CONTRACT_TYPE}: {data.get('contract_type')!r}")
        require_schema_version(data, expected=SCHEMA_VERSION, contract=RESULT_CONTRACT_TYPE)
        obj = cls(
            research_task_id=data["research_task_id"],
            worker_id=data["worker_id"],
            provenance=Provenance.from_dict(data["provenance"]),
            evidence_refs=tuple(EvidenceRef.from_dict(r) for r in data.get("evidence_refs", ())),
            abstained=data.get("abstained", False),
            abstention_reason=data.get("abstention_reason"),
            confidence=data.get("confidence"),
            schema_version=data["schema_version"],
            findings=data.get("findings") if not data.get("abstained", False) else None,
        )
        for name, recorded, actual in (
            ("findings_hash", data.get("findings_hash"), obj.findings_hash),
            ("worker_result_id", data.get("worker_result_id"), obj.worker_result_id),
        ):
            if recorded is not None and recorded != actual:
                raise ValueError(f"{name} mismatch — serialized identity does not reproduce")
        return obj


@dataclass(frozen=True, slots=True)
class ResearchClaim:
    """A falsifiable research hypothesis. NOT certified alpha.

    Falsifiability is structural: a claim must name the testable metric and
    the hypothesized direction, and must cite at least one source (evidence
    ref or worker result). Certification happens only through the future
    ExperimentSpec → ExperimentResult → StratLab path — this contract carries
    no certification status by design.
    """

    claim: str                                  # the falsifiable statement
    testable_metric: str                        # namespaced, e.g. "return.excess_spy_20d"
    direction: str                              # one of CLAIM_DIRECTIONS
    provenance: Provenance = None                    # type: ignore[assignment]  # REQUIRED attribution — NOT identity-bearing
    evidence_refs: Tuple[EvidenceRef, ...] = ()      # supporting evidence (unordered set)
    worker_result_ids: Tuple[str, ...] = ()          # wkr_… ids (unordered set)
    scope_entities: Tuple[str, ...] = ()             # may be empty
    notes: Optional[str] = None                      # NOT identity-bearing
    schema_version: str = SCHEMA_VERSION
    contract_type: str = field(default=CLAIM_CONTRACT_TYPE, init=False)

    def __post_init__(self) -> None:
        for name in ("claim", "testable_metric"):
            value = getattr(self, name)
            if not value or not isinstance(value, str):
                raise ValueError(f"{name} is required — an untestable claim is not falsifiable")
        if self.direction not in CLAIM_DIRECTIONS:
            raise ValueError(
                f"direction must be one of {sorted(CLAIM_DIRECTIONS)}, got {self.direction!r}"
            )
        object.__setattr__(
            self, "evidence_refs",
            normalize_ref_set("evidence_refs", self.evidence_refs, EvidenceRef, "snapshot_id"),
        )
        object.__setattr__(self, "worker_result_ids",
                           normalize_string_set("worker_result_ids", self.worker_result_ids,
                                                allow_empty=True))
        object.__setattr__(self, "scope_entities",
                           normalize_string_set("scope_entities", self.scope_entities,
                                                allow_empty=True))
        for wid in self.worker_result_ids:
            validate_contract_id("worker_result_ids entry", wid, "wkr")
        if not self.evidence_refs and not self.worker_result_ids:
            raise ValueError(
                "a claim must cite at least one source: evidence_refs or worker_result_ids"
            )
        if not isinstance(self.provenance, Provenance):
            raise ValueError("provenance must be a Provenance (required attribution)")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"schema_version must be {SCHEMA_VERSION!r} (this kernel), "
                f"got {self.schema_version!r}"
            )

    def _identity_payload(self) -> dict:
        return {
            "contract_type": CLAIM_CONTRACT_TYPE,
            "schema_era": schema_era(self.schema_version),
            "claim": self.claim,
            "testable_metric": self.testable_metric,
            "direction": self.direction,
            "evidence_refs": sorted(r.snapshot_id for r in self.evidence_refs),
            "worker_result_ids": sorted(self.worker_result_ids),
            "scope_entities": sorted(self.scope_entities),
        }

    @property
    def claim_id(self) -> str:
        return deterministic_id("rcl", self._identity_payload())

    def to_canonical_dict(self) -> dict:
        return {
            "contract_type": self.contract_type,
            "schema_version": self.schema_version,
            "claim_id": self.claim_id,
            "claim": self.claim,
            "testable_metric": self.testable_metric,
            "direction": self.direction,
            "evidence_refs": [r.to_canonical_dict() for r in sorted(self.evidence_refs, key=lambda r: r.snapshot_id)],
            "worker_result_ids": sorted(self.worker_result_ids),
            "scope_entities": sorted(self.scope_entities),
            "notes": self.notes,
            "provenance": self.provenance.to_canonical_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ResearchClaim":
        from portfolio_automation.northstar.serde import require_schema_version

        if data.get("contract_type") != CLAIM_CONTRACT_TYPE:
            raise ValueError(f"not a {CLAIM_CONTRACT_TYPE}: {data.get('contract_type')!r}")
        require_schema_version(data, expected=SCHEMA_VERSION, contract=CLAIM_CONTRACT_TYPE)
        obj = cls(
            claim=data["claim"],
            testable_metric=data["testable_metric"],
            direction=data["direction"],
            provenance=Provenance.from_dict(data["provenance"]),
            evidence_refs=tuple(EvidenceRef.from_dict(r) for r in data.get("evidence_refs", ())),
            worker_result_ids=tuple(data.get("worker_result_ids", ())),
            scope_entities=tuple(data.get("scope_entities", ())),
            notes=data.get("notes"),
            schema_version=data["schema_version"],
        )
        recorded = data.get("claim_id")
        if recorded is not None and recorded != obj.claim_id:
            raise ValueError("claim_id mismatch — serialized identity does not reproduce")
        return obj
