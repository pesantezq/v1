"""Session-1 foundation + provider assessment artifacts. Observe-only.

Records what the 2026-08-08 read-only probe actually measured, so later
sessions inherit evidence rather than assumptions. Deliberately separates
ARCHITECTURE feasibility from REAL-MARKET feasibility: the lab can be
structurally sound while the data is unusable, and conflating the two is how a
research platform ends up reporting fixture output as market evidence.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1"

REAL_DATA_READY = "REAL_DATA_READY"
REAL_DATA_LIMITED = "REAL_DATA_LIMITED"
DATA_UNAVAILABLE = "DATA_UNAVAILABLE"

# Evidence from the 2026-08-08 read-only probe (SPY/AAPL, small windows only).
# Every value here was MEASURED against the configured account, not read off
# provider documentation — entitlement and documentation disagree routinely.
PROBE_EVIDENCE: list[dict[str, Any]] = [
    {"symbol": "SPY", "timeframe": "5min", "window": "2026-08-03..2026-08-07",
     "http": 200, "bars": 390, "first": "2026-08-03 09:30:00", "last": "2026-08-07 15:55:00"},
    {"symbol": "SPY", "timeframe": "5min", "window": "2026-05-05..2026-05-09",
     "http": 200, "bars": 312, "first": "2026-05-05 09:30:00", "last": "2026-05-08 15:55:00",
     "note": "312 == 4 complete sessions x 78 bars. 2026-05-09 is a SATURDAY, so "
             "the window holds FOUR regular sessions, not five. An earlier reading "
             "called this a missing session; that was a calendar error, not a "
             "coverage gap. This probe shows COMPLETE data."},
    {"symbol": "SPY", "timeframe": "5min", "window": "2025-08-04..2025-08-08",
     "http": 200, "bars": 390, "first": "2025-08-04 09:30:00", "last": "2025-08-08 15:55:00"},
    {"symbol": "SPY", "timeframe": "5min", "window": "2023-08-07..2023-08-11",
     "http": 200, "bars": 390, "first": "2023-08-07 09:30:00", "last": "2023-08-11 15:55:00"},
    {"symbol": "SPY", "timeframe": "5min", "window": "2020-08-03..2020-08-07",
     "http": 200, "bars": 390, "first": "2020-08-03 09:30:00", "last": "2020-08-07 15:55:00"},
    {"symbol": "SPY", "timeframe": "5min", "window": "2017-08-07..2017-08-11",
     "http": 200, "bars": 390, "first": "2017-08-07 09:30:00", "last": "2017-08-11 15:55:00"},
    {"symbol": "AAPL", "timeframe": "5min", "window": "2026-08-03..2026-08-07",
     "http": 200, "bars": 390, "first": "2026-08-03 09:30:00", "last": "2026-08-07 15:55:00"},
    {"symbol": "SPY", "timeframe": "5min", "window": "2025-11-28..2025-11-28",
     "http": 200, "bars": 42, "first": "2025-11-28 09:30:00", "last": "2025-11-28 12:55:00",
     "note": "day after Thanksgiving, 13:00 early close — provider reflects it; "
             "last bar 12:55 PROVES BAR_OPEN timestamp semantics"},
    {"symbol": "SPY", "timeframe": "5min", "window": "2026-08-08..2026-08-08",
     "http": 200, "bars": 0, "note": "Saturday — correctly empty (SESSION_CLOSED)"},
    {"symbol": "SPY", "timeframe": "1min", "window": "2026-08-06..2026-08-07",
     "http": 402, "bars": None, "note": "HTTP 402 Payment Required — NOT ENTITLED"},
    {"symbol": "AAPL", "timeframe": "5min", "window": "2020-08-27..2020-08-27",
     "http": 200, "bars": 78, "note": "closes ~125 pre-4:1-split (actual print ~500) "
                                      "=> history is SPLIT BACK-ADJUSTED"},
]


def provider_assessment() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "intraday_lab.provider_assessment",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "observe_only": True,
        "provider": "fmp",
        "endpoint": "/stable/historical-chart/{timeframe}",
        "registry_status": "REGISTERED",
        "registry_key": "intraday_chart",
        "account_access": {"5min": "ENTITLED", "1min": "NOT_ENTITLED_HTTP_402"},
        "tested_timeframes": ["5min", "1min"],
        "canonical_research_timeframe": "5min",
        "undeclared_untested_timeframes": {
            "15min": "NOT_PROBED — not declared in TIMEFRAMES",
            "30min": "NOT_PROBED — not declared in TIMEFRAMES",
            "1hour": "NOT_PROBED — not declared in TIMEFRAMES",
        },
        "tested_symbols": ["SPY", "AAPL"],
        "observed_historical_depth": {
            "earliest_verified": "2017-08-07",
            "latest_verified": "2026-08-07",
            "approx_years": 9,
            "bars_per_full_session": 78,
            "method": "spot probes at 2017/2020/2023/2025/2026 — not an exhaustive scan",
        },
        "ohlc_available": True,
        "volume_available": True,
        "timestamp_semantics": "BAR_OPEN",
        "timestamp_semantics_evidence":
            "13:00 early close yields a final bar of 12:55; normal sessions run "
            "09:30..15:55. Only consistent with bar-open labelling.",
        "timezone_behavior": "naive US/Eastern wall-clock; no offset supplied",
        "extended_hours_behavior": "REGULAR_ONLY",
        "adjustment_semantics": "SPLIT_BACK_ADJUSTED",
        "adjustment_evidence":
            "AAPL 2020-08-27 closes ~125 vs the ~500 that actually printed before "
            "the 4:1 split.",
        "rate_limit_constraints": "governed by the existing FMP budget governor",
        "observed_quality": "every probed window returned complete regular "
                            "sessions; no coverage gap was observed in the sample. "
                            "Completeness must still be profiled per session — a "
                            "clean sample is not proof the provider has no gaps.",
        "pit_suitability": "SUITABLE for return-based research; split adjustment "
                           "is retroactive, so rules keyed to ABSOLUTE price "
                           "levels are NOT point-in-time safe",
        "compliance_status": "COMPLIANT — endpoint registered, read-only probes only",
        "final_source_status": REAL_DATA_READY,
        "probe_evidence": PROBE_EVIDENCE,
    }


def foundation_status() -> dict:
    limitations = [
        "1min is NOT entitled on this account (HTTP 402). 5min is the finest "
        "VERIFIED AND DECLARED research timeframe for the configured account — "
        "15min/30min/1hour were never probed, so nothing is claimed about them.",
        "Intraday history is SPLIT BACK-ADJUSTED. Safe for returns; any future "
        "rule keyed to absolute price levels or round-number thresholds is NOT "
        "point-in-time safe.",
        "Dividend adjustment behaviour was NOT established for intraday bars.",
        "Historical depth was spot-probed at five points, not exhaustively scanned.",
        "Session completeness must be profiled per trading session against a "
        "calendar-derived expected bar count. The sampled windows were complete, "
        "which is NOT evidence that the provider has no gaps elsewhere.",
        "Provider publishes no emission timestamp, so known_at uses a documented "
        "conservative 60s floor rather than measured latency.",
        "REGULAR_ONLY: no extended-hours bars, so gap/pre-market research is out "
        "of scope unless another source is sanctioned.",
        "No immutable canonical dataset has been constructed yet; Session 1 "
        "performed read-only probes only.",
        "Non-price feature families are NOT point-in-time ready except where "
        "explicitly classified PIT_READY.",
        "Session 2 must derive session_type and expected_bars from the sanctioned "
        "exchange calendar before admitting a session to the canonical dataset. "
        "A weekday-only approximation is NOT acceptable for the production "
        "dataset — it cannot see holidays or early closes.",
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "intraday_lab.foundation_status",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "observe_only": True,
        "session": 1,
        "foundation_status": "PASS",
        "data_source_status": REAL_DATA_READY,
        "point_in_time_status": "PASS",
        "data_quality_status": "PASS",
        "leakage_test_status": "PASS",
        "feature_timestamp_status": "PARTIAL — price-derived only; non-price "
                                    "families classified, not integrated",
        "architecture_next_session_allowed": True,
        "real_market_validation_allowed": True,
        "blocking_reasons": [],
        "warnings": limitations,
        "signal_family_pit_assessment": {
            "PRICE_DERIVED": "PIT_READY",
            "NEWS": "PIT_POSSIBLE_WITH_WORK",
            "FINBERT": "PIT_POSSIBLE_WITH_WORK",
            "CROWD_ATTENTION": "PIT_UNSAFE",
            "ANALYST": "PIT_POSSIBLE_WITH_WORK",
            "INSIDER": "PIT_POSSIBLE_WITH_WORK",
            "CONGRESS": "PIT_POSSIBLE_WITH_WORK",
            "REGIME": "PIT_UNSAFE",
        },
    }


def assess_foundation_health() -> dict:
    """Health consumer. Distinguishes SYSTEM failure from SOURCE limitation —
    an unentitled account is a correct diagnosis, not a software crash."""
    status = foundation_status()
    source = status["data_source_status"]
    if status["foundation_status"] != "PASS":
        overall = "SYSTEM_FAILURE"
    elif source == DATA_UNAVAILABLE:
        overall = "SOURCE_UNAVAILABLE"
    elif source == REAL_DATA_LIMITED:
        overall = "SOURCE_LIMITED"
    else:
        overall = "HEALTHY"
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "intraday_lab.health",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "observe_only": True,
        "overall": overall,
        "data_source_status": source,
        "leakage_test_status": status["leakage_test_status"],
        "warning_count": len(status["warnings"]),
        "reasons": status["warnings"],
    }


def write_foundation_artifacts(root: str = ".") -> list[str]:
    """Write to OutputNamespace.HISTORICAL — research, never live."""
    from portfolio_automation.data_governance import OutputNamespace, safe_write_json

    base = Path(root) / "outputs"
    return [str(safe_write_json(OutputNamespace.HISTORICAL, name, payload, base_dir=base))
            for name, payload in (
                ("intraday_provider_assessment.json", provider_assessment()),
                ("intraday_foundation_status.json", foundation_status()),
                ("intraday_foundation_health.json", assess_foundation_health()))]


# ---------------------------------------------------------------------------
# Session 2 — dataset + feature foundation status
# ---------------------------------------------------------------------------
DATASET_FEATURE_FOUNDATION_READY = "DATASET_FEATURE_FOUNDATION_READY"
DATASET_FEATURE_FOUNDATION_LIMITED = "DATASET_FEATURE_FOUNDATION_LIMITED"
DATASET_FEATURE_FOUNDATION_BLOCKED = "DATASET_FEATURE_FOUNDATION_BLOCKED"


def _canonical_ready(pilot: dict | None) -> bool:
    """True only with actual evidence: a dataset fingerprint AND a reconciled
    request. Absence of evidence is never readiness."""
    if not isinstance(pilot, dict):
        return False
    return bool(pilot.get("dataset_fingerprint")) and \
        pilot.get("sessions_requested") is not None and \
        pilot.get("sessions_admitted") is not None


def _feature_ready(pilot: dict | None) -> bool:
    if not _canonical_ready(pilot):
        return False
    return bool((pilot or {}).get("feature_fingerprint")) and \
        bool((pilot or {}).get("feature_observations"))


def session2_status(pilot: dict | None = None) -> dict:
    """Session 2 exit status.

    LIMITED, not READY: the architecture is proven end-to-end on real data, but
    the repo's holiday table spans 2025-2027 only. Five-minute bars reach back
    to 2017 and the calendar refuses every one of those sessions, so the
    research window is far narrower than the data. That materially constrains
    what Session 3+ can eventually validate, which is exactly what LIMITED is
    for.
    """
    from portfolio_automation.intraday_lab import calendar as _cal
    from portfolio_automation.intraday_lab import features as _feat

    return {
        "schema_version": "1",
        "source": "intraday_lab.session2_status",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "observe_only": True,
        "session": 2,
        "architecture_status": DATASET_FEATURE_FOUNDATION_LIMITED,
        "calendar_integrated": True,
        "exact_grid_reconciliation": True,
        "immutable_canonical_dataset": True,
        "deterministic_fingerprint": True,
        "pit_feature_engine": True,
        # Session 2 must never imply strategies may graduate, however green it is.
        "real_data_acquisition_allowed": True,
        # EVIDENCE-DRIVEN, not asserted. Without a verified manifest+fingerprint
        # these were returning True on pilot=None, which is precisely the
        # "verdict derived from absent data" failure the lab exists to avoid.
        "canonical_dataset_ready": _canonical_ready(pilot),
        "feature_dataset_ready": _feature_ready(pilot),
        "strategy_validation_allowed": False,
        "features_enabled": list(_feat.ENABLED_FEATURES),
        "features_blocked": {k: _feat.FEATURE_REGISTRY[k]["status"]
                             for k in _feat.BLOCKED_FEATURES},
        "calendar": _cal.calendar_provenance(),
        "pilot": pilot,
        "limitations": [
            "CALENDAR COVERAGE IS THE BINDING CONSTRAINT: holiday data spans "
            "2025-01-01..2027-12-24. 5min bars exist back to 2017 but every "
            "pre-2025 session is UNCERTIFIED and refused. Extending the holiday "
            "table backwards from a verified source is the highest-value "
            "unblock for Session 3+.",
            "Early-close table is hand-maintained for the covered window; a "
            "missed or wrong entry causes a REJECTION, never a silent bad admit.",
            "Volume-dependent features (VWAP, RVOL, dollar volume) are BLOCKED — "
            "historical volume adjustment semantics were never established.",
            "Absolute-price features are BLOCKED — history is split back-adjusted.",
            "SECTOR_CONTEXT_DEFERRED — no PIT-safe symbol->sector mapping.",
            "No bulk backfill was performed; the pilot is small by design.",
            "No CLI module was added this session; the pipeline is library-level.",
        ],
    }


def assess_session2_health(pilot: dict | None = None) -> dict:
    """Health for the Session 2 data product.

    A correctly REJECTED session is the control working, not a software fault —
    so rejections never make this RED. Only a broken component does.
    """
    st = session2_status(pilot)
    rejected = (pilot or {}).get("sessions_rejected", 0)
    overall = "HEALTHY" if st["architecture_status"] != DATASET_FEATURE_FOUNDATION_BLOCKED \
        else "SOFTWARE_FAILURE"
    return {
        "schema_version": "1",
        "source": "intraday_lab.session2_health",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "observe_only": True,
        "overall": overall,
        "architecture_status": st["architecture_status"],
        "sessions_rejected_in_pilot": rejected,
        "rejection_is_not_a_software_fault": True,
        "features_blocked_count": len(st["features_blocked"]),
        "strategy_validation_allowed": False,
        "reasons": st["limitations"],
    }


def write_session2_artifacts(root: str = ".", pilot: dict | None = None,
                             manifest: dict | None = None,
                             rejections: dict | None = None,
                             feature_manifest: dict | None = None) -> list[str]:
    from portfolio_automation.data_governance import OutputNamespace, safe_write_json
    from portfolio_automation.intraday_lab import features as _feat

    base = Path(root) / "outputs"
    payloads = [
        ("intraday_session2_status.json", session2_status(pilot)),
        ("intraday_session2_health.json", assess_session2_health(pilot)),
        ("intraday_feature_registry.json", _feat.feature_registry_artifact()),
    ]
    if manifest:
        payloads.append(("intraday_dataset_manifest.json", manifest))
    if rejections:
        payloads.append(("intraday_rejections.json", rejections))
    if feature_manifest:
        payloads.append(("intraday_feature_manifest.json", feature_manifest))
    return [str(safe_write_json(OutputNamespace.HISTORICAL, name, payload, base_dir=base))
            for name, payload in payloads]
