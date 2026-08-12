"""OutcomeRecord — the resolved-outcome / component-attribution contract (milestone 3).

Boundaries (docs/NORTHSTAR_CONTRACTS.md §6-7; authority: config/agent_policy.yaml):

* An OutcomeRecord is EVIDENCE about what happened — it enables component-level
  attribution so that prediction quality, allocation quality, exit quality, and
  end-to-end portfolio performance stay SEPARATELY measurable (the North Star
  requirement). It is not permission for anything to happen.
* It REFERENCES prior artifacts by id only, never contains or rewrites them:
  - what a prediction predicted            (``prd_`` PredictionRecord ids)
  - what an allocation proposed            (``cap_`` CapitalProposal ids)
  - what an exit proposed, where relevant  (``xit_`` ExitProposal ids)
  - what authoritative action occurred, IF such a reference exists elsewhere
    (``realized_action_refs`` — opaque strings; the contract layer defines no
    action/execution contract, so this references, it never invents one)
  Holding only ids, an OutcomeRecord structurally cannot retrospectively rewrite a
  prediction or a proposal.
* Attribution is NEVER collapsed into a single generic "strategy success" score.
  ``component_outcomes`` is keyed strictly by recognized attribution DIMENSIONS
  (prediction / allocation / exit / portfolio / reference); any other top-level key
  — a blended score, an authority claim — is structurally rejected. The distinct
  questions stay distinct.
* Resolved/unresolved is explicit: an unresolved outcome carries no measurements; a
  resolved (or partially resolved) one must. The outcome payload is snapshot-frozen
  to canonical bytes at construction — caller mutation can never alter it or its
  identity — and authority-claiming keys (approve/certify/promote/execute…) are
  rejected: a resolved result is a measurement, never a promotion or an action.

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
OUTCOME_CONTRACT_TYPE = "outcome_record"

# The ONLY recognized top-level attribution dimensions. Keeping outcome
# measurements keyed by these — and rejecting anything else — is what keeps the
# distinct questions distinct: prediction quality, allocation quality, exit (sell)
# quality, and end-to-end portfolio performance are never blended into one score.
# "reference" carries counterfactual / benchmark information.
OUTCOME_DIMENSIONS = frozenset({"prediction", "allocation", "exit", "portfolio", "reference"})

# Resolved-state semantics: an unresolved outcome cannot carry measurements.
RESOLUTION_STATES = frozenset({"resolved", "partially_resolved", "unresolved"})

# An OutcomeRecord is EVIDENCE, never permission — authority-claiming keys rejected.
_AUTHORITY_KEYS = frozenset({
    "approved", "approval", "approve", "certified", "certification", "certify",
    "promote", "promotion", "production_ready", "production", "authorize",
    "authorized", "execute", "execution", "trade", "order", "broker",
})


def _validate_aware(name: str, value: datetime) -> None:
    try:
        encode_datetime(value)
    except CanonicalizationError as exc:
        raise ValueError(f"{name}: {exc}") from exc


def _reject_authority_keys(obj: Any, path: str = "component_outcomes") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in _AUTHORITY_KEYS:
                raise ValueError(
                    f"{path}.{k}: an OutcomeRecord is EVIDENCE about what happened, "
                    "never permission — authority-claiming fields are rejected"
                )
            _reject_authority_keys(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _reject_authority_keys(v, f"{path}[{i}]")


def _validate_component_outcomes(co: dict) -> None:
    for k in co:
        if k not in OUTCOME_DIMENSIONS:
            raise ValueError(
                f"component_outcomes key {k!r} is not a recognized attribution dimension "
                f"{sorted(OUTCOME_DIMENSIONS)} — outcome attribution must stay separated "
                "(prediction/allocation/exit quality and end-to-end portfolio performance are "
                "distinct measurements, never one blended 'strategy success' score)"
            )
    _reject_authority_keys(co)


@dataclass(frozen=True, slots=True)
class OutcomeRecord:
    """Resolved outcome enabling component-level attribution. Evidence, not permission."""

    resolution_as_of: datetime                       # PIT at which the outcome is measured/known (required, aware)
    resolution_status: str                           # one of RESOLUTION_STATES
    provenance: Provenance = None                    # type: ignore[assignment]  # REQUIRED attribution — NOT identity-bearing
    prediction_record_ids: Tuple[str, ...] = ()      # prd_… what was predicted
    capital_proposal_ids: Tuple[str, ...] = ()       # cap_… what allocation proposed
    exit_proposal_ids: Tuple[str, ...] = ()          # xit_… what exit proposed (where relevant)
    realized_action_refs: Tuple[str, ...] = ()       # opaque refs to authoritative action records that occurred ELSEWHERE
    evidence_refs: Tuple[EvidenceRef, ...] = ()      # evidence establishing the resolved outcome (PIT-clean)
    component_outcomes_canonical: str = field(init=False)
    component_outcomes_hash: str = field(init=False)
    notes: Optional[str] = None                      # NOT identity-bearing
    schema_version: str = SCHEMA_VERSION
    contract_type: str = field(default=OUTCOME_CONTRACT_TYPE, init=False)
    # Construction-only input (snapshot-payload discipline).
    component_outcomes: Any = None                   # dict keyed by OUTCOME_DIMENSIONS; per-dimension measurements

    def __post_init__(self) -> None:
        _validate_aware("resolution_as_of", self.resolution_as_of)
        if self.resolution_status not in RESOLUTION_STATES:
            raise ValueError(
                f"resolution_status must be one of {sorted(RESOLUTION_STATES)}, "
                f"got {self.resolution_status!r}"
            )
        object.__setattr__(self, "prediction_record_ids",
                           normalize_string_set("prediction_record_ids", self.prediction_record_ids, allow_empty=True))
        object.__setattr__(self, "capital_proposal_ids",
                           normalize_string_set("capital_proposal_ids", self.capital_proposal_ids, allow_empty=True))
        object.__setattr__(self, "exit_proposal_ids",
                           normalize_string_set("exit_proposal_ids", self.exit_proposal_ids, allow_empty=True))
        object.__setattr__(self, "realized_action_refs",
                           normalize_string_set("realized_action_refs", self.realized_action_refs, allow_empty=True))
        for pid in self.prediction_record_ids:
            validate_contract_id("prediction_record_ids entry", pid, "prd")
        for cid in self.capital_proposal_ids:
            validate_contract_id("capital_proposal_ids entry", cid, "cap")
        for xid in self.exit_proposal_ids:
            validate_contract_id("exit_proposal_ids entry", xid, "xit")
        # An outcome must attribute to SOMETHING (a prediction, a proposal, or a
        # realized action). Evidence alone attributes nothing.
        if not (self.prediction_record_ids or self.capital_proposal_ids
                or self.exit_proposal_ids or self.realized_action_refs):
            raise ValueError(
                "an OutcomeRecord must reference at least one attributed artifact "
                "(prediction / capital proposal / exit proposal / realized action)"
            )
        object.__setattr__(self, "evidence_refs",
                           normalize_ref_set("evidence_refs", self.evidence_refs, EvidenceRef, "snapshot_id"))
        raw = self.component_outcomes if self.component_outcomes is not None else {}
        if not isinstance(raw, dict):
            raise ValueError("component_outcomes must be a JSON object keyed by attribution dimension, or omitted")
        if self.resolution_status == "unresolved":
            if raw:
                raise ValueError("an unresolved OutcomeRecord cannot carry resolved measurements")
        else:
            if not raw:
                raise ValueError(
                    f"a {self.resolution_status} OutcomeRecord must carry component_outcomes "
                    "(per-dimension measurements)"
                )
        _validate_component_outcomes(raw)
        object.__setattr__(self, "component_outcomes_canonical", canonical_dumps(raw))
        object.__setattr__(self, "component_outcomes_hash", content_hash(raw))
        object.__setattr__(self, "component_outcomes", None)
        if not isinstance(self.provenance, Provenance):
            raise ValueError("provenance must be a Provenance (required attribution)")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"schema_version must be {SCHEMA_VERSION!r} (this kernel), "
                f"got {self.schema_version!r}"
            )

    def component_outcomes_copy(self) -> dict:
        """A fresh deep copy of the per-dimension outcomes; mutating it changes nothing."""
        return json.loads(self.component_outcomes_canonical)

    def component(self, dimension: str) -> Optional[Any]:
        """The measurement for one attribution dimension (or None) — each is
        independently retrievable; they are never collapsed."""
        if dimension not in OUTCOME_DIMENSIONS:
            raise ValueError(f"unknown dimension {dimension!r}")
        return self.component_outcomes_copy().get(dimension)

    def _identity_payload(self) -> dict:
        return {
            "contract_type": OUTCOME_CONTRACT_TYPE,
            "schema_era": schema_era(self.schema_version),
            "resolution_as_of": self.resolution_as_of,
            "resolution_status": self.resolution_status,
            "prediction_record_ids": sorted(self.prediction_record_ids),
            "capital_proposal_ids": sorted(self.capital_proposal_ids),
            "exit_proposal_ids": sorted(self.exit_proposal_ids),
            "realized_action_refs": sorted(self.realized_action_refs),
            "evidence_refs": sorted(r.snapshot_id for r in self.evidence_refs),
            "component_outcomes_hash": self.component_outcomes_hash,
        }

    @property
    def outcome_record_id(self) -> str:
        return deterministic_id("out", self._identity_payload())

    def to_canonical_dict(self) -> dict:
        return {
            "contract_type": self.contract_type,
            "schema_version": self.schema_version,
            "outcome_record_id": self.outcome_record_id,
            "resolution_as_of": self.resolution_as_of,
            "resolution_status": self.resolution_status,
            "prediction_record_ids": sorted(self.prediction_record_ids),
            "capital_proposal_ids": sorted(self.capital_proposal_ids),
            "exit_proposal_ids": sorted(self.exit_proposal_ids),
            "realized_action_refs": sorted(self.realized_action_refs),
            "evidence_refs": [r.to_canonical_dict() for r in sorted(self.evidence_refs, key=lambda r: r.snapshot_id)],
            "component_outcomes": json.loads(self.component_outcomes_canonical),
            "component_outcomes_hash": self.component_outcomes_hash,
            "notes": self.notes,
            "provenance": self.provenance.to_canonical_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "OutcomeRecord":
        from portfolio_automation.northstar.serde import (
            parse_optional_datetime,
            require_schema_version,
        )

        if data.get("contract_type") != OUTCOME_CONTRACT_TYPE:
            raise ValueError(f"not a {OUTCOME_CONTRACT_TYPE}: {data.get('contract_type')!r}")
        require_schema_version(data, expected=SCHEMA_VERSION, contract=OUTCOME_CONTRACT_TYPE)
        resolution_as_of = parse_optional_datetime(data.get("resolution_as_of"))
        if resolution_as_of is None:
            raise ValueError("resolution_as_of is required")
        obj = cls(
            resolution_as_of=resolution_as_of,
            resolution_status=data["resolution_status"],
            provenance=Provenance.from_dict(data["provenance"]),
            prediction_record_ids=data.get("prediction_record_ids", ()),
            capital_proposal_ids=data.get("capital_proposal_ids", ()),
            exit_proposal_ids=data.get("exit_proposal_ids", ()),
            realized_action_refs=data.get("realized_action_refs", ()),
            evidence_refs=parse_ref_list("evidence_refs", data.get("evidence_refs", ()),
                                         EvidenceRef.from_dict),
            notes=data.get("notes"),
            schema_version=data["schema_version"],
            component_outcomes=data.get("component_outcomes"),
        )
        for name, recorded, actual in (
            ("component_outcomes_hash", data.get("component_outcomes_hash"), obj.component_outcomes_hash),
            ("outcome_record_id", data.get("outcome_record_id"), obj.outcome_record_id),
        ):
            if recorded is not None and recorded != actual:
                raise ValueError(f"{name} mismatch — serialized identity does not reproduce")
        return obj
