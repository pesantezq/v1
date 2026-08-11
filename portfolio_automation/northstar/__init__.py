"""Northstar canonical contracts (Phase 0B).

The stable domain language used by future Northstar components. This package
contains CONTRACTS ONLY — no engines, no runtimes, no vendor integrations, no
pipeline wiring. See docs/NORTHSTAR_CONTRACTS.md for the full architecture and
.agent/phase_status.yaml:northstar_phase_0b for milestone status.

Milestone 1 (evidence kernel) implements:

* canonical  — strict canonical JSON, content hashing, deterministic IDs
* pit        — point-in-time semantics (observed/published/known/retrieved,
               effective period, timing basis; no fabricated time)
* provenance — small explicit provenance record
* sources    — DataSourceDescriptor
* evidence   — EvidenceSnapshot + EvidenceRef
* features   — FeatureRecord

Later Phase 0B milestones add the prediction/research/experiment and
capital/exit/outcome/passport families. Consumers must never depend on vendor
response schemas — vendors adapt INTO these contracts (Evidence Plane), never
the other way around.
"""
from portfolio_automation.northstar.canonical import (  # noqa: F401
    CanonicalizationError,
    canonical_dumps,
    content_hash,
    deterministic_id,
)
from portfolio_automation.northstar.pit import PointInTime  # noqa: F401
from portfolio_automation.northstar.provenance import Provenance  # noqa: F401
from portfolio_automation.northstar.sources import DataSourceDescriptor  # noqa: F401
from portfolio_automation.northstar.evidence import EvidenceRef, EvidenceSnapshot  # noqa: F401
from portfolio_automation.northstar.features import FeatureRecord  # noqa: F401
