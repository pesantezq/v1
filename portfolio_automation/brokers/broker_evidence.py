# portfolio_automation/brokers/broker_evidence.py
"""BrokerPortfolioSnapshot — the versioned, governed contract for Schwab
portfolio truth: the normalized result of ONE successful read-only broker
synchronization.

Where it sits in the Northstar evidence architecture:

    Schwab API -> schwab_client -> broker_models.normalize_accounts
        -> BrokerPortfolioSnapshot (this module: pure, versioned, secret-free)
        -> schwab_evidence_adapter -> canonical EvidenceSnapshot
        -> evidence_gateway.admit -> ADMITTED | REFUSED

It REUSES the kernel rather than duplicating it: source identity is a
``DataSourceDescriptor`` (``src_…``), the payload hash is the kernel's
``content_hash``, and the canonical identity is a kernel ``deterministic_id``.
Run-specific fields (``sync_id``, ``retrieved_at``, ``source_commit``) are
carried on the contract but EXCLUDED from its canonical identity, so two syncs
that observe the same portfolio produce the same ``broker_snapshot_id`` and
the same ``payload_hash``.

Structurally excluded (a tripwire rejects them at construction): raw account
numbers, access/refresh tokens, client secret, authorization codes/headers,
usernames/passwords. Account references are the masked ``…NNNN`` form only.

Pure: no network, no file I/O, no secrets, no trade surface.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from portfolio_automation.brokers import broker_models as bm
from portfolio_automation.northstar.canonical import (
    CanonicalizationError, canonical_dumps, content_hash, deterministic_id, encode_datetime,
    schema_era)
from portfolio_automation.northstar.sources import DataSourceDescriptor

CONTRACT_TYPE = "broker_portfolio_snapshot"
SCHEMA_VERSION = "1.0.0"

#: Human label for the source; the kernel-derived ``source_id`` (src_…) is the identity.
SOURCE_LABEL = "schwab_trader_api"
SOURCE_PROVIDER = "schwab"
SOURCE_DATASET = "trader_api_accounts_positions"
#: Read-only endpoints the acquisition uses. Provenance, not configuration.
ENDPOINTS: tuple[str, ...] = (
    "/trader/v1/accounts/accountNumbers",
    "/trader/v1/accounts?fields=positions",
)
NORMALIZER_ID = "portfolio_automation.brokers.broker_models.normalize_accounts"
NORMALIZER_VERSION = "1"

EVIDENCE_TYPE = "broker.portfolio_snapshot"
ENTITY_ID = "SCHWAB_PORTFOLIO"
ENTITY_TYPE = "other"

_FORBIDDEN_KEYS = frozenset({
    "access_token", "refresh_token", "client_secret", "authorization", "code",
    "id_token", "accesstoken", "refreshtoken", "clientsecret", "idtoken",
    "accountnumber", "account_number", "accountid", "account_id",
    "password", "username", "hashvalue", "encryptedaccount",
})
#: ``mask_account`` output: an ellipsis alone, or an ellipsis + last four.
_MASKED_REF = re.compile(r"^…(\S{4})?$")


class BrokerEvidenceError(ValueError):
    """The contract refused to be built. Messages never carry secret bytes."""


def data_source_descriptor() -> DataSourceDescriptor:
    """The Schwab Trader API as a Northstar source. Rights honestly classified
    as private research use; no historical/PIT capability is claimed because a
    broker account snapshot is a live read, not a reconstructable history."""
    return DataSourceDescriptor(
        provider=SOURCE_PROVIDER,
        dataset=SOURCE_DATASET,
        source_type="other",
        access_class="api",
        rights_class="private_research",
        cost_class="free",
        pit_capability="none",
        historical_capability="none",
        status="active",
        notes=("Read-only Schwab Trader API accounts + positions for the operator's "
               "own account. Observe-only; the adapter exposes no order or trade surface."),
    )


def _finite(value: Optional[float], name: str) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise BrokerEvidenceError(f"{name} is not numeric")
    if not math.isfinite(f):
        raise BrokerEvidenceError(f"{name} is not finite")
    return f


def _scan_for_secret_material(obj: Any, path: str = "$") -> None:
    """Fail closed on any key or string that looks like credential or raw
    account material. Over-refusal is acceptable; a leak into a persisted,
    hashed contract is not."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower().replace("-", "_") in _FORBIDDEN_KEYS:
                raise BrokerEvidenceError(f"forbidden field {k!r} at {path}")
            _scan_for_secret_material(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _scan_for_secret_material(v, f"{path}[{i}]")
    elif isinstance(obj, str):
        if bm._SECRET_RE.search(obj) or re.search(r"(?i)\bbearer\s+\S+", obj):
            raise BrokerEvidenceError(f"credential-looking text at {path}")


@dataclass(frozen=True)
class BrokerPortfolioSnapshot:
    """One normalized, secret-free broker observation. Construct via
    :meth:`from_normalized`; direct construction validates identically."""

    sync_id: str
    retrieved_at: datetime
    accounts: tuple[dict[str, Any], ...]
    positions: tuple[dict[str, Any], ...]
    totals: dict[str, Any]
    effective_at: Optional[datetime] = None
    source_commit: str = "UNAVAILABLE"
    source_label: str = SOURCE_LABEL
    endpoints: tuple[str, ...] = ENDPOINTS
    normalizer_id: str = NORMALIZER_ID
    normalizer_version: str = NORMALIZER_VERSION
    schema_version: str = SCHEMA_VERSION
    contract_type: str = field(default=CONTRACT_TYPE, init=False)

    # ------------------------------------------------------------ validate
    def __post_init__(self) -> None:
        if not self.sync_id or not isinstance(self.sync_id, str):
            raise BrokerEvidenceError("sync_id is required")
        if self.schema_version != SCHEMA_VERSION:
            raise BrokerEvidenceError(f"schema_version must be {SCHEMA_VERSION!r}")
        for name in ("retrieved_at", "effective_at"):
            value = getattr(self, name)
            if value is None:
                continue
            if not isinstance(value, datetime):
                raise BrokerEvidenceError(f"{name} must be a datetime")
            try:
                encode_datetime(value)
            except CanonicalizationError as exc:
                raise BrokerEvidenceError(f"{name}: {exc}") from exc
        if not isinstance(self.accounts, tuple) or not isinstance(self.positions, tuple):
            raise BrokerEvidenceError("accounts and positions must be tuples")
        for i, a in enumerate(self.accounts):
            ref = a.get("account_id_masked")
            if not isinstance(ref, str) or not _MASKED_REF.match(ref):
                raise BrokerEvidenceError(f"accounts[{i}]: account reference is not masked")
            _finite(a.get("total_market_value"), f"accounts[{i}].total_market_value")
            _finite(a.get("cash"), f"accounts[{i}].cash")
        for i, p in enumerate(self.positions):
            if not isinstance(p.get("symbol"), str) or not p["symbol"]:
                raise BrokerEvidenceError(f"positions[{i}]: symbol is required")
            ref = p.get("account_ref_masked")
            if ref is not None and (not isinstance(ref, str) or not _MASKED_REF.match(ref)):
                raise BrokerEvidenceError(f"positions[{i}]: account reference is not masked")
            _finite(p.get("quantity"), f"positions[{i}].quantity")
            _finite(p.get("market_value"), f"positions[{i}].market_value")
            _finite(p.get("average_cost"), f"positions[{i}].average_cost")
        _scan_for_secret_material(self.payload())
        try:
            canonical_dumps(self.payload())
        except CanonicalizationError as exc:
            raise BrokerEvidenceError(f"payload is not canonical: {exc}") from exc

    # --------------------------------------------------------- construction
    @classmethod
    def from_normalized(cls, snap: bm.BrokerSnapshot, *, sync_id: str, retrieved_at: datetime,
                        source_commit: str = "UNAVAILABLE",
                        effective_at: Optional[datetime] = None) -> "BrokerPortfolioSnapshot":
        """Build from the pure normalizer's output. Deterministic ordering:
        accounts by masked ref, positions by (account ref, symbol)."""
        accounts = tuple(sorted((
            {
                "account_id_masked": a.account_id_masked,
                "account_type": a.account_type,
                "total_market_value": a.total_market_value,
                "cash": a.cash,
                "positions_count": len(a.positions),
            } for a in snap.accounts), key=lambda d: str(d["account_id_masked"])))
        positions = tuple(sorted((
            {
                "symbol": p.symbol,
                "quantity": p.quantity,
                "market_value": p.market_value,
                "average_cost": p.average_cost,
                "asset_type": p.asset_type,
                "account_ref_masked": p.account_ref_masked,
            } for a in snap.accounts for p in a.positions),
            key=lambda d: (str(d["account_ref_masked"]), str(d["symbol"]))))
        totals = {
            "market_value": sum(a.total_market_value or 0.0 for a in snap.accounts),
            "cash": sum(a.cash or 0.0 for a in snap.accounts),
        }
        return cls(sync_id=sync_id, retrieved_at=retrieved_at, accounts=accounts,
                   positions=positions, totals=totals, effective_at=effective_at,
                   source_commit=source_commit)

    # ------------------------------------------------------------- identity
    @property
    def source_id(self) -> str:
        return data_source_descriptor().source_id

    def payload(self) -> dict[str, Any]:
        """The domain facts — what becomes the canonical EvidenceSnapshot payload.
        Run-specific fields (sync_id, retrieved_at, source_commit) are NOT here;
        they travel in the PIT/provenance envelope."""
        return {
            "source_label": self.source_label,
            "endpoints": list(self.endpoints),
            "normalizer_id": self.normalizer_id,
            "normalizer_version": self.normalizer_version,
            "account_count": len(self.accounts),
            "position_count": len(self.positions),
            "accounts": [dict(a) for a in self.accounts],
            "positions": [dict(p) for p in self.positions],
            "totals": dict(self.totals),
            "observe_only": True,
            "read_only": True,
        }

    @property
    def payload_hash(self) -> str:
        return content_hash(self.payload())

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "contract_type": CONTRACT_TYPE,
            "schema_era": schema_era(self.schema_version),
            "source_id": self.source_id,
            "payload_hash": self.payload_hash,
        }

    @property
    def broker_snapshot_id(self) -> str:
        """Canonical identity: same portfolio facts -> same id, across runs."""
        return deterministic_id("bps", self._identity_payload())

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_type": self.contract_type,
            "schema_version": self.schema_version,
            "broker_snapshot_id": self.broker_snapshot_id,
            "source_id": self.source_id,
            "source_label": self.source_label,
            "sync_id": self.sync_id,
            "retrieved_at": encode_datetime(self.retrieved_at),
            "effective_at": encode_datetime(self.effective_at) if self.effective_at else None,
            "source_commit": self.source_commit,
            "normalizer_id": self.normalizer_id,
            "normalizer_version": self.normalizer_version,
            "endpoints": list(self.endpoints),
            "payload_hash": self.payload_hash,
            "payload": self.payload(),
        }
