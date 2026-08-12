"""CapitalProposal — the decision-family contract (milestone 3).

Boundaries (docs/NORTHSTAR_CONTRACTS.md §6; authority: config/agent_policy.yaml):

* A CapitalProposal is an INDEPENDENT, ADVISORY allocation proposal: sizing/limits
  that REFERENCE the PredictionRecord(s) and evidence it relies on. It is a
  proposal, nothing more.
* Hard boundaries (structural):
  - PredictionRecord != CapitalProposal: predictions are referenced by ``prd_`` id
    only, never contained or mutated here.
  - CapitalProposal != approval: it carries no approval/certification field.
  - CapitalProposal != execution / real portfolio action: it has no execution
    surface; execution/approval/trade/broker/order keys are structurally rejected
    from the proposed sizing. The human real-action gate lives OUTSIDE the contract
    layer (``production_control_plane``).
* This contract does NOT define capital/risk POLICY — it carries a proposal and
  references the constraints it was formed under; it never invents policy. If a
  new consequential capital/risk policy must be decided, that is an E4/human
  decision made elsewhere, not encoded here.

All ref collections are unordered sets (kernel input-set semantics).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

from portfolio_automation.northstar._collections import (
    normalize_ref_set,
    normalize_string_set,
    parse_ref_list,
)
from portfolio_automation.northstar.canonical import (
    canonical_dumps,
    content_hash,
    deterministic_id,
    schema_era,
    validate_contract_id,
)
from portfolio_automation.northstar.evidence import EvidenceRef
from portfolio_automation.northstar.provenance import Provenance

SCHEMA_VERSION = "1.0.0"
CAPITAL_PROPOSAL_CONTRACT_TYPE = "capital_proposal"

# A CapitalProposal PROPOSES allocation (that is its purpose) but can never
# approve, execute, or trade. Execution/approval-claiming keys are rejected from
# the sizing document; allocation/sizing content is allowed.
_EXECUTION_KEYS = frozenset({
    "approve", "approval", "approved", "certify", "certified", "certification",
    "execute", "execution", "executed", "trade", "traded", "order", "orders",
    "fill", "filled", "broker", "authorize", "authorized", "promote", "promotion",
    "production", "production_ready",
})


def _reject_execution_keys(obj: Any, path: str = "proposed_sizing") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in _EXECUTION_KEYS:
                raise ValueError(
                    f"{path}.{k}: a CapitalProposal is advisory — it may propose allocation "
                    "but can never approve/execute/trade; execution/approval keys are rejected"
                )
            _reject_execution_keys(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _reject_execution_keys(v, f"{path}[{i}]")


@dataclass(frozen=True, slots=True)
class CapitalProposal:
    """An advisory allocation proposal. NOT approval, NOT execution, NOT an action."""

    prediction_record_ids: Tuple[str, ...]        # prd_… the predictions this proposal relies on (>=1)
    rationale: str                                # why this allocation is proposed
    provenance: Provenance = None                 # type: ignore[assignment]  # REQUIRED attribution — NOT identity-bearing
    evidence_refs: Tuple[EvidenceRef, ...] = ()   # supporting evidence (unordered set)
    proposed_sizing_canonical: str = field(init=False)
    proposed_sizing_hash: str = field(init=False)
    notes: Optional[str] = None                   # NOT identity-bearing
    schema_version: str = SCHEMA_VERSION
    contract_type: str = field(default=CAPITAL_PROPOSAL_CONTRACT_TYPE, init=False)
    # Construction-only input (snapshot-payload discipline).
    proposed_sizing: Any = None                   # advisory sizing/limits document (strict JSON)

    def __post_init__(self) -> None:
        if not self.rationale or not isinstance(self.rationale, str):
            raise ValueError("rationale is required — an advisory proposal must justify itself")
        object.__setattr__(self, "prediction_record_ids",
                           normalize_string_set("prediction_record_ids", self.prediction_record_ids,
                                                allow_empty=False))
        for pid in self.prediction_record_ids:
            validate_contract_id("prediction_record_ids entry", pid, "prd")
        object.__setattr__(self, "evidence_refs",
                           normalize_ref_set("evidence_refs", self.evidence_refs, EvidenceRef, "snapshot_id"))
        raw = self.proposed_sizing
        if not isinstance(raw, dict) or not raw:
            raise ValueError("proposed_sizing must be a non-empty JSON object (the advisory allocation)")
        _reject_execution_keys(raw)
        object.__setattr__(self, "proposed_sizing_canonical", canonical_dumps(raw))
        object.__setattr__(self, "proposed_sizing_hash", content_hash(raw))
        object.__setattr__(self, "proposed_sizing", None)
        if not isinstance(self.provenance, Provenance):
            raise ValueError("provenance must be a Provenance (required attribution)")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"schema_version must be {SCHEMA_VERSION!r} (this kernel), "
                f"got {self.schema_version!r}"
            )

    def proposed_sizing_copy(self) -> dict:
        """A fresh deep copy of the advisory sizing; mutating it changes nothing."""
        return json.loads(self.proposed_sizing_canonical)

    def _identity_payload(self) -> dict:
        return {
            "contract_type": CAPITAL_PROPOSAL_CONTRACT_TYPE,
            "schema_era": schema_era(self.schema_version),
            "prediction_record_ids": sorted(self.prediction_record_ids),
            "evidence_refs": sorted(r.snapshot_id for r in self.evidence_refs),
            "rationale": self.rationale,
            "proposed_sizing_hash": self.proposed_sizing_hash,
        }

    @property
    def capital_proposal_id(self) -> str:
        return deterministic_id("cap", self._identity_payload())

    def to_canonical_dict(self) -> dict:
        return {
            "contract_type": self.contract_type,
            "schema_version": self.schema_version,
            "capital_proposal_id": self.capital_proposal_id,
            "prediction_record_ids": sorted(self.prediction_record_ids),
            "evidence_refs": [r.to_canonical_dict() for r in sorted(self.evidence_refs, key=lambda r: r.snapshot_id)],
            "rationale": self.rationale,
            "proposed_sizing": json.loads(self.proposed_sizing_canonical),
            "proposed_sizing_hash": self.proposed_sizing_hash,
            "notes": self.notes,
            "provenance": self.provenance.to_canonical_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CapitalProposal":
        from portfolio_automation.northstar.serde import require_schema_version

        if data.get("contract_type") != CAPITAL_PROPOSAL_CONTRACT_TYPE:
            raise ValueError(f"not a {CAPITAL_PROPOSAL_CONTRACT_TYPE}: {data.get('contract_type')!r}")
        require_schema_version(data, expected=SCHEMA_VERSION, contract=CAPITAL_PROPOSAL_CONTRACT_TYPE)
        obj = cls(
            prediction_record_ids=data["prediction_record_ids"],
            rationale=data["rationale"],
            provenance=Provenance.from_dict(data["provenance"]),
            evidence_refs=parse_ref_list("evidence_refs", data.get("evidence_refs", ()),
                                         EvidenceRef.from_dict),
            notes=data.get("notes"),
            schema_version=data["schema_version"],
            proposed_sizing=data.get("proposed_sizing"),
        )
        for name, recorded, actual in (
            ("proposed_sizing_hash", data.get("proposed_sizing_hash"), obj.proposed_sizing_hash),
            ("capital_proposal_id", data.get("capital_proposal_id"), obj.capital_proposal_id),
        ):
            if recorded is not None and recorded != actual:
                raise ValueError(f"{name} mismatch — serialized identity does not reproduce")
        return obj
