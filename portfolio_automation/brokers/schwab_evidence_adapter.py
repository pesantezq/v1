# portfolio_automation/brokers/schwab_evidence_adapter.py
"""Schwab evidence adapter: BrokerPortfolioSnapshot -> canonical EvidenceSnapshot,
plus admission through the EXISTING EvidenceGateway and the compatibility
projections the legacy consumers read.

Three concerns are kept apart on purpose:

    NETWORK / SECRETS      schwab_client + schwab_auth_manager (not imported here)
    PURE NORMALIZATION     broker_models.normalize_accounts + BrokerPortfolioSnapshot
    EVIDENCE CONSTRUCTION  this module — pure, deterministic, secret-free

Determinism: for equivalent normalized inputs the payload and ``payload_hash``
are identical; the canonical ``snapshot_id`` additionally binds ``known_at``
(possession time, per the kernel's one sanctioned conservative rule), so it
is identical only when ``retrieved_at`` is identical. That is the intended
PIT semantics, not an accident.

Admission delegates to :func:`evidence_gateway.admission.admit` unchanged —
no gateway rule is duplicated or weakened here. A REFUSED snapshot yields no
projection: this module cannot be used to turn refused evidence into truth.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from portfolio_automation.brokers import broker_evidence as BE
from portfolio_automation.evidence_gateway.admission import AdmissionDecision, admit
from portfolio_automation.northstar.canonical import encode_datetime
from portfolio_automation.northstar.evidence import EvidenceRef, EvidenceSnapshot
from portfolio_automation.northstar.pit import PointInTime
from portfolio_automation.northstar.provenance import Provenance

PRODUCER_ID = "adapter.schwab_trader_api_accounts_positions"
ADAPTER_VERSION = "1"


class EvidenceNotAdmitted(RuntimeError):
    """Raised by :func:`require_admitted` when a projection is requested from
    evidence the gateway did not admit."""


def to_evidence_snapshot(bps: BE.BrokerPortfolioSnapshot) -> EvidenceSnapshot:
    """One broker observation -> one canonical EvidenceSnapshot.

    PIT: ``retrieved_at`` is the acquisition instant; ``observed_at`` is the
    source's own timestamp when it supplied one (Schwab's account payload does
    not, so it stays absent — never fabricated); ``known_at`` is derived via
    the kernel's ONE sanctioned rule (possession).
    Provenance: ``code_version`` carries the source commit and
    ``transformation_id`` the normalizer identity + sync id, so a run is
    traceable without entering the snapshot's identity."""
    pit = PointInTime(retrieved_at=bps.retrieved_at,
                      observed_at=bps.effective_at).with_conservative_known_at()
    descriptor = BE.data_source_descriptor()
    provenance = Provenance(
        producer_id=PRODUCER_ID,
        producer_type="source_adapter",
        recorded_at=bps.retrieved_at,
        code_version=bps.source_commit,
        source_id=descriptor.source_id,
        transformation_id=(f"{bps.normalizer_id}:v{bps.normalizer_version}"
                           f":adapter_v{ADAPTER_VERSION}:sync={bps.sync_id}"),
    )
    return EvidenceSnapshot(
        source_id=descriptor.source_id,
        entity_id=BE.ENTITY_ID,
        entity_type=BE.ENTITY_TYPE,
        evidence_type=BE.EVIDENCE_TYPE,
        pit=pit,
        provenance=provenance,
        payload=bps.payload(),
    )


def admit_broker_snapshot(snapshot: EvidenceSnapshot, as_of: datetime,
                          ref: Optional[EvidenceRef] = None) -> AdmissionDecision:
    """Whole-evidence admission through the existing gateway (PIT -> identity
    -> provenance -> reference). Pure; never raises."""
    return admit(snapshot, as_of, ref=ref)


def require_admitted(snapshot: EvidenceSnapshot, decision: AdmissionDecision) -> None:
    if not isinstance(decision, AdmissionDecision) or not decision.admitted:
        reason = getattr(getattr(decision, "reason", None), "value", "NO_DECISION")
        raise EvidenceNotAdmitted(f"refused broker evidence cannot become truth: {reason}")
    if decision.snapshot_id != snapshot.snapshot_id:
        raise EvidenceNotAdmitted("admission decision does not name this snapshot")


# ---------------------------------------------------------------------------
# Compatibility projections — derived FROM admitted evidence, never authorities
# ---------------------------------------------------------------------------

def _generated_at(snapshot: EvidenceSnapshot) -> str:
    return encode_datetime(snapshot.pit.retrieved_at)


def project_snapshot_dict(snapshot: EvidenceSnapshot, decision: AdmissionDecision) -> dict[str, Any]:
    """``schwab_portfolio_snapshot.json`` shape (legacy consumers) + the
    evidence identity it was derived from. Refuses unadmitted evidence."""
    require_admitted(snapshot, decision)
    payload = snapshot.payload_copy()
    ts = _generated_at(snapshot)
    return {
        "generated_at": ts, "observe_only": True, "source": "schwab",
        "snapshot_timestamp": ts,
        "accounts": [{
            "account_id_masked": a.get("account_id_masked"), "account_type": a.get("account_type"),
            "total_market_value": a.get("total_market_value"), "cash": a.get("cash"),
            "positions_count": a.get("positions_count"),
        } for a in payload.get("accounts", [])],
        "totals": {"market_value": payload.get("totals", {}).get("market_value", 0.0),
                   "cash": payload.get("totals", {}).get("cash", 0.0)},
        "evidence_snapshot_id": snapshot.snapshot_id,
        "evidence_payload_hash": snapshot.payload_hash,
        "projection_of": "broker_evidence_latest_admitted.json",
    }


def project_positions_dict(snapshot: EvidenceSnapshot, decision: AdmissionDecision) -> dict[str, Any]:
    """``schwab_positions.json`` shape (legacy consumers) + evidence identity."""
    require_admitted(snapshot, decision)
    payload = snapshot.payload_copy()
    ts = _generated_at(snapshot)
    rows = [{
        "symbol": p.get("symbol"), "quantity": p.get("quantity"), "market_value": p.get("market_value"),
        "average_cost": p.get("average_cost"), "asset_type": p.get("asset_type"),
        "account_ref_masked": p.get("account_ref_masked"), "source_timestamp": ts,
    } for p in payload.get("positions", [])]
    return {
        "generated_at": ts, "observe_only": True, "source": "schwab", "positions": rows,
        "evidence_snapshot_id": snapshot.snapshot_id,
        "evidence_payload_hash": snapshot.payload_hash,
        "projection_of": "broker_evidence_latest_admitted.json",
    }
