"""
Institutional Intelligence subsystem.

Extracts point-in-time-safe signals from large institutional-manager
disclosures (initially SEC Form 13F) and tests bounded institutional strategies
inside the existing Strategy Lab + simulation-governance architecture.

INVARIANTS (enforced across the package, its tests, and health checks):
  * observe-only / sandbox-first — never writes ``outputs/latest/decision_plan.json``,
    never mutates the six protected decision scores, production allocations,
    brokerage state, or production watchlist state.
  * ``feeds_decision_engine`` is always ``False``.
  * Every artifact honestly distinguishes point-in-time availability, filing
    period, filing date, freshness, inferred interpretation, and limitations.
  * No look-ahead: a signal is available no earlier than the filing's public
    availability date (never the quarter-end).
  * No raw network calls outside the governed SEC client; no SEC User-Agent /
    contact value or credential ever appears in an artifact or log.

This package is inert by default: live SEC ingestion is disabled until an
operator supplies the User-Agent and verifies manager CIKs.
"""

from __future__ import annotations

SCHEMA_VERSION = "1"
PACKAGE_SOURCE = "institutional_intelligence"
