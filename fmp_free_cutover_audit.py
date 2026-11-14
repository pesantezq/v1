"""Offline, read-only FMP Starter -> Basic cutover evidence gate.

This module does NOT fetch market data, inspect keys/.env, change configuration,
schedule work, or authorize VS-002/trading. It consumes only the existing
code-owned endpoint registry and compliance method mapping.

Run from the repository root:
    python -m fmp_free_cutover_audit
    python -m fmp_free_cutover_audit --strict

The --strict exit status is 2 while any proof gate remains open. A zero exit
in ordinary display mode does NOT certify a safe provider cutover.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from typing import Any, Mapping

from fmp_endpoint_compliance import LEGACY_METHOD_MAP
from fmp_endpoint_registry import REGISTRY

OWNER_CUTOVER_DATE = "2026-11-27"
FMP_BASIC_DAILY_REQUEST_LIMIT = 250
ESTIMATED_MONTHLY_FULL_SCAN_REQUESTS = 3700

# Paths are source-inspection leads, not assertions about today's runtime.
COMPONENTS: dict[str, tuple[str, ...]] = {
    "holdings_prices": ("market_data.py", "main.py", "fmp_client.py"),
    "daily_scanner": ("watchlist_scanner/scanner.py", "watchlist_scanner/fundamentals_engine.py"),
    "universe_membership": ("universe/fmp_universe.py", "scripts/run_monthly_universe_refresh.sh"),
    "news_and_attention": (
        "portfolio_automation/news/fmp_news_intelligence.py",
        "portfolio_automation/crowd_intelligence/fmp_capability_probe.py",
    ),
    "budget_and_preflight": (
        "portfolio_automation/data_budget/factory.py",
        "scripts/preflight.sh",
    ),
    "vs002_fixed_evidence": (
        "portfolio_automation/vs002_evidence/production_runner.py",
        "fmp_client.py",
    ),
}

FMP_OFFICIAL_REFERENCES = (
    "https://site.financialmodelingprep.com/pricing-plans",
    "https://site.financialmodelingprep.com/faqs",
    "https://site.financialmodelingprep.com/insights/platform/"
    "can-you-downgrade-or-return-to-the-free-fmp-plan-later",
)


def assess_free_cutover(
    *,
    registry: Mapping[str, Mapping[str, Any]] | None = None,
    legacy_methods: Mapping[str, tuple[str, str]] | None = None,
    effective_date: str = OWNER_CUTOVER_DATE,
    basic_daily_limit: int = FMP_BASIC_DAILY_REQUEST_LIMIT,
    monthly_full_scan_calls: int = ESTIMATED_MONTHLY_FULL_SCAN_REQUESTS,
) -> dict[str, Any]:
    """Return reproducible, source-only readiness evidence; never perform I/O.

    A Starter-safe flag does NOT establish Free/Basic entitlement.
    Legacy-method presence is code exposure, not proof of a live call.
    """
    parsed_date = date.fromisoformat(effective_date)
    if basic_daily_limit < 1 or monthly_full_scan_calls < 0:
        raise ValueError("request limits must be nonnegative and daily limit positive")
    specs = REGISTRY if registry is None else registry
    legacy = LEGACY_METHOD_MAP if legacy_methods is None else legacy_methods

    required = [
        {
            "registry_key": key,
            "endpoint": spec.get("endpoint", ""),
            "starter_safe": spec.get("starter_safe", "unverified"),
            "basic_entitlement": "UNVERIFIED",
        }
        for key, spec in sorted(specs.items())
        if spec.get("required_daily") is True
    ]
    legacy_exposure = [
        {"method": method, "endpoint": endpoint, "classification": classification}
        for method, (endpoint, classification) in sorted(legacy.items())
    ]
    # This current universe path explicitly calls these methods. Other entries
    # may be present for compatibility; they are NOT asserted to run today.
    known_legacy_universe_calls = [
        item for item in legacy_exposure
        if item["method"] in {"get_sp500_constituents", "get_batch_profiles_v3"}
    ]
    adjusted = specs.get("historical_prices_dividend_adjusted", {})
    blockers = [
        {
            "code": "NO_VERIFIED_BASIC_ENTITLEMENT",
            "detail": "Starter-safe does not prove Basic access; verify exact required endpoints.",
            "count": len(required),
        },
        {
            "code": "LEGACY_UNIVERSE_PATH",
            "detail": "Basic/Free does not grant the discontinued v3/v4 Legacy API.",
            "count": len(known_legacy_universe_calls),
        },
        {
            "code": "FULL_SCAN_EXCEEDS_BASIC_DAILY_QUOTA",
            "detail": "The documented one-run monthly scan exceeds the Basic daily request limit.",
            "count": int(monthly_full_scan_calls > basic_daily_limit),
        },
        {
            "code": "VS002_ENDPOINT_AND_ADJUSTMENT_UNVERIFIED",
            "detail": "Never substitute /full, another vendor, or stale cache for frozen dividend-adjusted evidence.",
            "count": 1,
        },
        {
            "code": "POST_DOWNGRADE_RETAINED_DATA_RIGHTS_UNVERIFIED",
            "detail": "Cached paid-tier data retention and use require a terms/account-specific decision.",
            "count": 1,
        },
        {
            "code": "CUTOVER_BILLING_CONFIRMATION_PENDING",
            "detail": "Owner-requested downgrade is not a verified completed account change.",
            "count": 1,
        },
    ]
    if not required:
        blockers.append({
            "code": "NO_REQUIRED_DAILY_ENDPOINTS",
            "detail": "Registry has no required_daily entries; cannot establish coverage.",
            "count": 1,
        })

    return {
        "schema": "northstar_fmp_basic_cutover_readiness_v1",
        "effective_date": parsed_date.isoformat(),
        "owner_decision": "END_PAID_FMP_STARTER_USE_DOWNGRADE_TO_BASIC",
        "account_confirmation": "PENDING",
        "report_mode": "OFFLINE_SOURCE_ONLY_NO_NETWORK",
        "status": "NOT_READY",
        "basic_plan_daily_request_limit": basic_daily_limit,
        "documented_monthly_full_scan_requests": monthly_full_scan_calls,
        "daily_required_endpoints": required,
        "legacy_implemented_methods": legacy_exposure,
        "legacy_universe_call_sites": known_legacy_universe_calls,
        "vs002_required_endpoint": adjusted.get("endpoint", "MISSING_FROM_REGISTRY"),
        "components_to_trace": {k: list(v) for k, v in COMPONENTS.items()},
        "blockers": blockers,
        "official_refs": list(FMP_OFFICIAL_REFERENCES),
        "safety": "NO_DATA_FETCH_NO_SECRETS_NO_RUNTIME_CHANGE_NO_TRADING",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--effective-date", default=OWNER_CUTOVER_DATE,
        help="ISO date for the planned paid-tier cutover (default 2026-11-27)",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="Return exit 2 while the cutover is NOT_READY",
    )
    args = parser.parse_args(argv)
    try:
        report = assess_free_cutover(effective_date=args.effective_date)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 2 if args.strict and report["status"] != "READY" else 0


if __name__ == "__main__":
    raise SystemExit(main())
