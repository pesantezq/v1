"""StrategyPassport — governed strategy identity / evidence / status (milestone 3).

Boundaries (docs/NORTHSTAR_CONTRACTS.md §6-7; authority: config/agent_policy.yaml).
This is the highest-governance-risk contract in milestone 3; it is written to STOP
at the existing governance boundary, never to weaken it.

* A StrategyPassport records the governed IDENTITY, EVIDENCE TRAIL, certification
  STATUS, and lifecycle STAGE of a strategy/capability
  (candidate → challenger → certified → retained/reduced/suspended/retired).
  It records governance state; it is NOT production permission, deployment
  authority, capital authority, or automatic promotion.
* Hard boundary — a passport GRANTS NO authority to itself or anything else:
  - it has NO production/deployment/capital-authority field, and authority-claiming
    keys (production_enabled/deploy/capital_eligible/may_receive_capital/promote/
    approve/authorize/execute/allocate…) are structurally rejected from ``attributes``.
  - recording ``lifecycle_stage='certified'`` records a STATUS whose operational
    meaning is defined by the governance/production control plane ELSEWHERE. This
    contract deliberately does NOT define what any stage ENTITLES. Deciding that
    mapping (production-promotion policy, capital eligibility, protected risk
    policy, irreversible certification semantics) is an E4/HUMAN_REQUIRED decision
    made outside the contract layer — it is not invented here.
* Certification status changes are APPEND-STYLE: a change is a NEW passport
  (``spp_``) that ``supersedes`` the prior version by id — never a silent mutation.
  The dataclass is frozen and identity-bearing, so a status can only change by
  issuing a superseding version, preserving the full auditable lineage.
* Commercial attractiveness can never alter a passport's evidence trail or the
  certification standard: the evidence basis is exactly the referenced evidence;
  the stage is an explicit recorded governance decision, not derived here from any
  commercial signal.
* A passport REFERENCES its evidence by id only (ExperimentResult ``exr_``,
  OutcomeRecord ``out_``, ResearchClaim ``rcl_``, raw EvidenceRef); it never
  contains or rewrites them. A passport must cite an evidence trail (structural
  completeness) — this is NOT a certification threshold.

All ref/label collections are unordered sets (kernel input-set semantics).
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
PASSPORT_CONTRACT_TYPE = "strategy_passport"

# Lifecycle stages from the architecture (docs/NORTHSTAR_CONTRACTS.md §6). RECORDED
# governance states whose operational meaning is authoritative ELSEWHERE; the
# passport records the stage, it does not define what the stage entitles.
LIFECYCLE_STAGES = frozenset({
    "candidate", "challenger", "certified", "retained", "reduced", "suspended", "retired",
})

# A StrategyPassport records governance state and GRANTS NO production/capital
# authority. Keys that would assert such authority are rejected from attributes —
# there is nowhere in this contract to encode "certified => gets capital/production".
_AUTHORITY_KEYS = frozenset({
    "production_enabled", "production", "production_ready", "deploy", "deployed",
    "deployment", "capital_allocated", "capital_eligible", "capital_eligibility",
    "may_receive_capital", "eligibility", "approve", "approved", "approval",
    "authorize", "authorized", "authorization", "execute", "execution", "promote",
    "promotion", "trade", "allocate", "allocation", "certify", "certification",
})


def _validate_aware(name: str, value: datetime) -> None:
    try:
        encode_datetime(value)
    except CanonicalizationError as exc:
        raise ValueError(f"{name}: {exc}") from exc


def _reject_authority_keys(obj: Any, path: str = "attributes") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in _AUTHORITY_KEYS:
                raise ValueError(
                    f"{path}.{k}: a StrategyPassport records governance state and grants NO "
                    "production/capital authority — authority-claiming fields are rejected; the "
                    "entitlement mapping is an E4/human decision made outside the contract layer"
                )
            _reject_authority_keys(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _reject_authority_keys(v, f"{path}[{i}]")


@dataclass(frozen=True, slots=True)
class StrategyPassport:
    """Governed strategy identity / evidence trail / certification status. Records
    governance state; grants NO production, deployment, or capital authority."""

    strategy_id: str                                 # the governed strategy/capability identity this passport is FOR
    lifecycle_stage: str                             # one of LIFECYCLE_STAGES (recorded status; meaning defined elsewhere)
    as_of: datetime                                  # PIT: when this governance-state version is asserted (required, aware)
    status_rationale: str                            # why this stage/status (required justification)
    provenance: Provenance = None                    # type: ignore[assignment]  # REQUIRED attribution — NOT identity-bearing
    experiment_result_ids: Tuple[str, ...] = ()      # exr_… evidence trail
    outcome_record_ids: Tuple[str, ...] = ()         # out_… evidence trail
    research_claim_ids: Tuple[str, ...] = ()         # rcl_… evidence trail
    evidence_refs: Tuple[EvidenceRef, ...] = ()      # raw evidence snapshots (optional)
    supersedes_passport_id: Optional[str] = None     # spp_… the prior version this supersedes (append-style; None for first)
    attributes_canonical: str = field(init=False)
    attributes_hash: str = field(init=False)
    notes: Optional[str] = None                      # NOT identity-bearing
    schema_version: str = SCHEMA_VERSION
    contract_type: str = field(default=PASSPORT_CONTRACT_TYPE, init=False)
    # Construction-only input (snapshot-payload discipline).
    attributes: Any = None                           # optional descriptive attributes; authority keys rejected

    def __post_init__(self) -> None:
        for name in ("strategy_id", "status_rationale"):
            value = getattr(self, name)
            if not value or not isinstance(value, str):
                raise ValueError(f"{name} is required")
        if self.lifecycle_stage not in LIFECYCLE_STAGES:
            raise ValueError(
                f"lifecycle_stage must be one of {sorted(LIFECYCLE_STAGES)}, got {self.lifecycle_stage!r}"
            )
        _validate_aware("as_of", self.as_of)
        object.__setattr__(self, "experiment_result_ids",
                           normalize_string_set("experiment_result_ids", self.experiment_result_ids, allow_empty=True))
        object.__setattr__(self, "outcome_record_ids",
                           normalize_string_set("outcome_record_ids", self.outcome_record_ids, allow_empty=True))
        object.__setattr__(self, "research_claim_ids",
                           normalize_string_set("research_claim_ids", self.research_claim_ids, allow_empty=True))
        for rid in self.experiment_result_ids:
            validate_contract_id("experiment_result_ids entry", rid, "exr")
        for oid in self.outcome_record_ids:
            validate_contract_id("outcome_record_ids entry", oid, "out")
        for cid in self.research_claim_ids:
            validate_contract_id("research_claim_ids entry", cid, "rcl")
        object.__setattr__(self, "evidence_refs",
                           normalize_ref_set("evidence_refs", self.evidence_refs, EvidenceRef, "snapshot_id"))
        # A passport must cite an evidence trail (structural completeness — NOT a
        # certification threshold; it says nothing about how much evidence any
        # stage requires, only that a governed record states its basis).
        if not (self.experiment_result_ids or self.outcome_record_ids
                or self.research_claim_ids or self.evidence_refs):
            raise ValueError(
                "a StrategyPassport must cite an evidence trail (experiment result / outcome / "
                "research claim / evidence ref)"
            )
        if self.supersedes_passport_id is not None:
            validate_contract_id("supersedes_passport_id", self.supersedes_passport_id, "spp")
        raw = self.attributes if self.attributes is not None else {}
        if not isinstance(raw, dict):
            raise ValueError("attributes must be a JSON object or omitted")
        _reject_authority_keys(raw)
        object.__setattr__(self, "attributes_canonical", canonical_dumps(raw))
        object.__setattr__(self, "attributes_hash", content_hash(raw))
        object.__setattr__(self, "attributes", None)
        if not isinstance(self.provenance, Provenance):
            raise ValueError("provenance must be a Provenance (required attribution)")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"schema_version must be {SCHEMA_VERSION!r} (this kernel), "
                f"got {self.schema_version!r}"
            )

    def attributes_copy(self) -> dict:
        """A fresh deep copy of the descriptive attributes; mutating it changes nothing."""
        return json.loads(self.attributes_canonical)

    def _identity_payload(self) -> dict:
        # A governance-state version is identity: strategy, stage, as_of, evidence
        # trail, superseded-version, and rationale. provenance/notes are attribution.
        return {
            "contract_type": PASSPORT_CONTRACT_TYPE,
            "schema_era": schema_era(self.schema_version),
            "strategy_id": self.strategy_id,
            "lifecycle_stage": self.lifecycle_stage,
            "as_of": self.as_of,
            "experiment_result_ids": sorted(self.experiment_result_ids),
            "outcome_record_ids": sorted(self.outcome_record_ids),
            "research_claim_ids": sorted(self.research_claim_ids),
            "evidence_refs": sorted(r.snapshot_id for r in self.evidence_refs),
            "supersedes_passport_id": self.supersedes_passport_id,
            "status_rationale": self.status_rationale,
            "attributes_hash": self.attributes_hash,
        }

    @property
    def strategy_passport_id(self) -> str:
        return deterministic_id("spp", self._identity_payload())

    def to_canonical_dict(self) -> dict:
        return {
            "contract_type": self.contract_type,
            "schema_version": self.schema_version,
            "strategy_passport_id": self.strategy_passport_id,
            "strategy_id": self.strategy_id,
            "lifecycle_stage": self.lifecycle_stage,
            "as_of": self.as_of,
            "experiment_result_ids": sorted(self.experiment_result_ids),
            "outcome_record_ids": sorted(self.outcome_record_ids),
            "research_claim_ids": sorted(self.research_claim_ids),
            "evidence_refs": [r.to_canonical_dict() for r in sorted(self.evidence_refs, key=lambda r: r.snapshot_id)],
            "supersedes_passport_id": self.supersedes_passport_id,
            "status_rationale": self.status_rationale,
            "attributes": json.loads(self.attributes_canonical),
            "attributes_hash": self.attributes_hash,
            "notes": self.notes,
            "provenance": self.provenance.to_canonical_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StrategyPassport":
        from portfolio_automation.northstar.serde import (
            parse_optional_datetime,
            require_schema_version,
        )

        if data.get("contract_type") != PASSPORT_CONTRACT_TYPE:
            raise ValueError(f"not a {PASSPORT_CONTRACT_TYPE}: {data.get('contract_type')!r}")
        require_schema_version(data, expected=SCHEMA_VERSION, contract=PASSPORT_CONTRACT_TYPE)
        as_of = parse_optional_datetime(data.get("as_of"))
        if as_of is None:
            raise ValueError("as_of is required")
        obj = cls(
            strategy_id=data["strategy_id"],
            lifecycle_stage=data["lifecycle_stage"],
            as_of=as_of,
            status_rationale=data["status_rationale"],
            provenance=Provenance.from_dict(data["provenance"]),
            experiment_result_ids=data.get("experiment_result_ids", ()),
            outcome_record_ids=data.get("outcome_record_ids", ()),
            research_claim_ids=data.get("research_claim_ids", ()),
            evidence_refs=parse_ref_list("evidence_refs", data.get("evidence_refs", ()),
                                         EvidenceRef.from_dict),
            supersedes_passport_id=data.get("supersedes_passport_id"),
            notes=data.get("notes"),
            schema_version=data["schema_version"],
            attributes=data.get("attributes"),
        )
        for name, recorded, actual in (
            ("attributes_hash", data.get("attributes_hash"), obj.attributes_hash),
            ("strategy_passport_id", data.get("strategy_passport_id"), obj.strategy_passport_id),
        ):
            if recorded is not None and recorded != actual:
                raise ValueError(f"{name} mismatch — serialized identity does not reproduce")
        return obj
