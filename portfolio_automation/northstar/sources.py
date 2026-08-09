"""DataSourceDescriptor — identity + characterization of an evidence source.

External data sources are REPLACEABLE Evidence Plane inputs (Northstar
standing requirement): vendors adapt INTO canonical evidence; consumers never
see vendor schemas. A descriptor identifies and characterizes a
provider/dataset so snapshots can carry stable source identity.

Deliberately NOT modeled here: API authentication, endpoints, request
mechanics — those belong to future source adapters and runtime config.
Descriptors are persisted/hashed contract objects, so credentials are
structurally rejected (see ``_reject_secret_material``).

Identity: ``source_id`` derives from (provider, dataset, source_type) only.
Characterization fields (access/rights/cost/PIT capability/status) describe
the source and may be re-stated over time WITHOUT minting a new source
identity — a re-characterized source is the same source.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from portfolio_automation.northstar.canonical import deterministic_id

SCHEMA_VERSION = "1.0.0"
CONTRACT_TYPE = "data_source_descriptor"

SOURCE_TYPES = frozenset({
    "market_data", "fundamental", "regulatory", "ownership", "short_interest",
    "insider", "analyst", "news", "social", "options", "fund_flows", "macro",
    "transcripts", "search_interest", "alternative", "other",
})
ACCESS_CLASSES = frozenset({"api", "file", "scrape", "manual", "unknown"})
RIGHTS_CLASSES = frozenset({
    "private_research", "internal_use", "commercial_allowed",
    "commercial_unknown", "unknown",
})
COST_CLASSES = frozenset({"free", "existing_subscription", "paid_optional", "unknown"})
PIT_CAPABILITIES = frozenset({"native_pit", "reconstructable", "none", "unknown"})
HISTORICAL_CAPABILITIES = frozenset({"deep", "limited", "none", "unknown"})
SOURCE_STATUSES = frozenset({"active", "probe_only", "planned", "retired"})

# Structural secret guard: descriptors are persisted + hashed, so anything that
# smells like credential material is a hard error. This is a tripwire, not a
# scanner — real credentials simply have no field to live in.
_SECRET_PATTERN = re.compile(
    r"(api[_-]?key|apikey|secret|password|bearer\s|authorization:|token=|sk-[A-Za-z0-9]{8,})",
    re.IGNORECASE,
)


def _reject_secret_material(name: str, value: Optional[str]) -> None:
    if value and _SECRET_PATTERN.search(value):
        raise ValueError(
            f"{name} appears to contain credential material — descriptors must "
            "never carry authentication; that belongs to adapter runtime config"
        )


@dataclass(frozen=True, slots=True)
class DataSourceDescriptor:
    provider: str                 # e.g. "fmp", "sec_edgar", "finra"
    dataset: str                  # e.g. "quotes_daily", "13f_holdings", "short_interest"
    source_type: str              # one of SOURCE_TYPES
    access_class: str = "unknown"
    rights_class: str = "unknown"       # never claim rights we cannot verify
    cost_class: str = "unknown"
    pit_capability: str = "unknown"     # can the source serve/reconstruct point-in-time views?
    historical_capability: str = "unknown"
    status: str = "planned"
    notes: Optional[str] = None
    schema_version: str = SCHEMA_VERSION
    contract_type: str = field(default=CONTRACT_TYPE, init=False)

    def __post_init__(self) -> None:
        for name, value in (("provider", self.provider), ("dataset", self.dataset)):
            if not value or not isinstance(value, str):
                raise ValueError(f"{name} is required")
            _reject_secret_material(name, value)
        _reject_secret_material("notes", self.notes)
        for name, value, allowed in (
            ("source_type", self.source_type, SOURCE_TYPES),
            ("access_class", self.access_class, ACCESS_CLASSES),
            ("rights_class", self.rights_class, RIGHTS_CLASSES),
            ("cost_class", self.cost_class, COST_CLASSES),
            ("pit_capability", self.pit_capability, PIT_CAPABILITIES),
            ("historical_capability", self.historical_capability, HISTORICAL_CAPABILITIES),
            ("status", self.status, SOURCE_STATUSES),
        ):
            if value not in allowed:
                raise ValueError(f"{name} must be one of {sorted(allowed)}, got {value!r}")

    @property
    def source_id(self) -> str:
        """Deterministic identity from (provider, dataset, source_type) only."""
        return deterministic_id(
            "src",
            {
                "contract_type": CONTRACT_TYPE,
                "provider": self.provider,
                "dataset": self.dataset,
                "source_type": self.source_type,
            },
        )

    def to_canonical_dict(self) -> dict:
        return {
            "contract_type": self.contract_type,
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "provider": self.provider,
            "dataset": self.dataset,
            "source_type": self.source_type,
            "access_class": self.access_class,
            "rights_class": self.rights_class,
            "cost_class": self.cost_class,
            "pit_capability": self.pit_capability,
            "historical_capability": self.historical_capability,
            "status": self.status,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DataSourceDescriptor":
        if data.get("contract_type") != CONTRACT_TYPE:
            raise ValueError(f"not a {CONTRACT_TYPE}: {data.get('contract_type')!r}")
        if not isinstance(data.get("schema_version"), str):
            raise ValueError("schema_version is required")
        obj = cls(
            provider=data["provider"],
            dataset=data["dataset"],
            source_type=data["source_type"],
            access_class=data.get("access_class", "unknown"),
            rights_class=data.get("rights_class", "unknown"),
            cost_class=data.get("cost_class", "unknown"),
            pit_capability=data.get("pit_capability", "unknown"),
            historical_capability=data.get("historical_capability", "unknown"),
            status=data.get("status", "planned"),
            notes=data.get("notes"),
            schema_version=data["schema_version"],
        )
        recorded = data.get("source_id")
        if recorded is not None and recorded != obj.source_id:
            raise ValueError(
                "source_id mismatch — serialized identity does not reproduce "
                "from (provider, dataset, source_type)"
            )
        return obj
