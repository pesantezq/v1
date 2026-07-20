"""
Institutional Intelligence artifact writer.

Builds the observe-only artifacts with a full, honest invariant envelope and an
explicit status vocabulary. Every artifact declares
``feeds_decision_engine: false`` and lists its source limitations so no consumer
can mistake a delayed 13F disclosure for a live trade instruction.

Writes via the governed safe-write helpers to LATEST + SANDBOX only.
"""

from __future__ import annotations

from typing import Any

from . import SCHEMA_VERSION

# Status vocabulary.
STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_INSUFFICIENT = "insufficient_data"
STATUS_STALE = "stale"
STATUS_FAILED = "failed"
STATUS_DISABLED = "disabled"

# Honest, user-facing limitations attached to every artifact.
SOURCE_LIMITATIONS = (
    "13F disclosures are delayed — filed up to 45 days after quarter-end.",
    "Holdings are incomplete: long US 13(f) securities only; no shorts, no cash, "
    "no non-US or non-13(f) positions.",
    "Options cannot be fully reconstructed (no strike, expiration, premium, or "
    "written-vs-purchased direction) and are never read as directional.",
    "A filing is evidence, not a live trade instruction.",
)


def envelope(*, generated_at: str, data_as_of: str, source: str,
             warnings: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    """The invariant governance block shared by all institutional artifacts."""
    env = {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "generated_at": generated_at,
        "data_as_of": data_as_of,
        "observe_only": True,
        "no_trade": True,
        "simulation_active": True,
        "production_gated": True,
        "human_approval_required_for_production": True,
        "feeds_decision_engine": False,
        "sandbox_only": True,
        "source_limitations": list(SOURCE_LIMITATIONS),
        "warnings": list(warnings or []),
    }
    env.update(extra)
    return env


def build_symbol_record(
    *,
    symbol: str,
    as_of: str,
    consensus: dict[str, Any],
    latest_report_period: str | None,
    filing_age_days: int | None,
    price_staleness_penalty: float | None = None,
    manager_signals: list[dict] | None = None,
    evidence_refs: list[str] | None = None,
) -> dict[str, Any]:
    """A per-symbol institutional record (honest about age + interpretation)."""
    return {
        "symbol": symbol,
        "as_of": as_of,
        "latest_report_period": latest_report_period,
        "filing_age_days": filing_age_days,
        "consensus_state": consensus.get("consensus_state"),
        "consensus_score": consensus.get("consensus_score"),
        "consensus_confidence": consensus.get("consensus_confidence"),
        "effective_independent_managers": consensus.get("effective_independent_managers"),
        "crowding_score": consensus.get("crowding_score"),
        "price_staleness_penalty": price_staleness_penalty,
        "manager_signals": manager_signals or [],
        "top_reasons": list(consensus.get("reasons") or []),
        "warnings": list(consensus.get("warnings") or []),
        "evidence_refs": evidence_refs or [],
    }


def determine_status(
    *,
    enabled: bool,
    failed: bool,
    records: list[dict],
    stale_after_days: int,
    min_confidence: float,
) -> str:
    if not enabled:
        return STATUS_DISABLED
    if failed:
        return STATUS_FAILED
    if not records:
        return STATUS_INSUFFICIENT
    ages = [r.get("filing_age_days") for r in records if r.get("filing_age_days") is not None]
    if ages and all(a > stale_after_days for a in ages):
        return STATUS_STALE
    # Any record with usable confidence => ok; else insufficient.
    usable = [r for r in records
              if (r.get("consensus_confidence") or 0.0) >= min_confidence
              and r.get("consensus_state") not in (None, "insufficient_data")]
    if not usable:
        return STATUS_INSUFFICIENT
    degraded = any(r.get("warnings") for r in records)
    return STATUS_DEGRADED if degraded else STATUS_OK


def build_intelligence_artifact(*, records: list[dict], generated_at: str,
                                data_as_of: str) -> dict[str, Any]:
    env = envelope(generated_at=generated_at, data_as_of=data_as_of,
                   source="institutional_intelligence")
    env["record_count"] = len(records)
    env["records"] = records
    return env


def build_status_artifact(*, status: str, records: list[dict], generated_at: str,
                          data_as_of: str, enabled: bool,
                          live_ready: bool) -> dict[str, Any]:
    env = envelope(generated_at=generated_at, data_as_of=data_as_of,
                   source="institutional_intelligence_status")
    env.update({
        "overall_status": status,
        "enabled": enabled,
        "live_ingestion_ready": live_ready,
        "symbols_covered": len(records),
        "stale_symbols": sum(1 for r in records
                             if (r.get("filing_age_days") or 0) > 130),
        "unresolved_symbols": sum(1 for r in records
                                  if r.get("consensus_state") == "insufficient_data"),
    })
    return env
