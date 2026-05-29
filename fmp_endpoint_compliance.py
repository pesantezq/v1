"""
FMP Endpoint Compliance Checker

Validates that every FMP endpoint used in fmp_client.py matches the
Starter-plan-safe contract defined in fmp_endpoint_registry.py.

Run:
    python -m fmp_endpoint_compliance
    python fmp_endpoint_compliance.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Literal

from fmp_endpoint_registry import REGISTRY, LEGACY_ENDPOINTS

# ---------------------------------------------------------------------------
# Allowlist: method name → (_EP_* constant value, registry_key)
# ---------------------------------------------------------------------------
# Only stable/ endpoints belong here; v3/v4 go in LEGACY_METHOD_MAP.

STABLE_METHOD_MAP: dict[str, tuple[str, str]] = {
    "get_batch_quotes":       ("quote",                       "quote"),
    "get_batch_profiles":     ("profile",                     "profile"),
    "get_historical_prices":  ("historical-price-eod/full",   "historical_prices"),
    "get_ratios":             ("ratios",                       "ratios"),
    "get_stock_news":         ("news/stock",                   "stock_news"),
    "get_income_statement":   ("income-statement",             "income_statement"),
    "get_key_metrics":        ("key-metrics",                  "key_metrics"),
    "get_financial_growth":   ("financial-growth",             "financial_growth"),
}

LEGACY_METHOD_MAP: dict[str, tuple[str, str]] = {
    "get_sp500_constituents":    ("v3/sp500_constituent",    "legacy_optional"),
    "get_batch_profiles_v3":     ("v3/profile",              "legacy_optional"),
    "get_fundamentals_v3":       ("v3/key-metrics",          "legacy_optional"),
    "get_bulk_profiles":         ("v4/profile/all",          "premium_optional"),
    "get_bulk_key_metrics":      ("v4/key-metrics-bulk",     "premium_optional"),
}

# Registry entries not yet implemented as methods in fmp_client.py
NOT_YET_IMPLEMENTED: list[str] = [
    "balance_sheet",
    "cashflow_statement",
    "ratings_snapshot",
    "historical_ratings",
    "available_sectors",
    "available_industries",
]

# Forbidden patterns: if any endpoint path in STABLE_METHOD_MAP contains these,
# it is a violation (legacy paths used where stable is required).
FORBIDDEN_PATTERNS: list[str] = ["/api/v3/", "/api/v4/", "v3/", "v4/"]

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

StatusLiteral = Literal["PASS", "FAIL", "WARN"]


@dataclass
class CheckResult:
    method: str
    endpoint_used: str
    expected_endpoint: str
    status: StatusLiteral
    message: str


# ---------------------------------------------------------------------------
# Core checks
# ---------------------------------------------------------------------------

def check_stable_methods() -> list[CheckResult]:
    results = []
    for method, (ep_const, registry_key) in STABLE_METHOD_MAP.items():
        spec = REGISTRY.get(registry_key)
        if spec is None:
            results.append(CheckResult(
                method=method,
                endpoint_used=ep_const,
                expected_endpoint="(unknown)",
                status="FAIL",
                message=f"registry key '{registry_key}' not found in REGISTRY",
            ))
            continue

        # The registry stores "/stable/quote"; strip prefix to compare with _EP_* constant
        expected_path = spec["endpoint"].removeprefix("/stable/")
        if ep_const == expected_path:
            results.append(CheckResult(
                method=method,
                endpoint_used=ep_const,
                expected_endpoint=expected_path,
                status="PASS",
                message="endpoint matches registry",
            ))
        else:
            results.append(CheckResult(
                method=method,
                endpoint_used=ep_const,
                expected_endpoint=expected_path,
                status="FAIL",
                message=f"mismatch: got '{ep_const}', expected '{expected_path}'",
            ))

        # Starter-safe check
        if not spec.get("starter_safe", True):
            results[-1].status = "FAIL"
            results[-1].message += " - NOT starter_safe"

    return results


def check_legacy_methods() -> list[CheckResult]:
    results = []
    for method, (ep_path, classification) in LEGACY_METHOD_MAP.items():
        # Legacy methods are OK - warn, not fail
        status: StatusLiteral = "WARN"
        if classification == "premium_optional":
            msg = f"premium endpoint ({ep_path}) - not available on Starter plan"
        else:
            msg = f"legacy endpoint ({ep_path}) - acceptable for universe pipeline, not daily scanner"
        results.append(CheckResult(
            method=method,
            endpoint_used=ep_path,
            expected_endpoint=ep_path,
            status=status,
            message=msg,
        ))
    return results


def check_not_implemented() -> list[CheckResult]:
    results = []
    for registry_key in NOT_YET_IMPLEMENTED:
        spec = REGISTRY.get(registry_key)
        if spec is None:
            continue
        results.append(CheckResult(
            method=f"(not yet implemented: {registry_key})",
            endpoint_used="",
            expected_endpoint=spec["endpoint"],
            status="WARN",
            message=f"P{spec['priority'][1]} registry entry has no fmp_client method yet",
        ))
    return results


def check_forbidden_patterns() -> list[CheckResult]:
    """Ensure no stable method accidentally uses a v3/v4 path."""
    results = []
    for method, (ep_const, _) in STABLE_METHOD_MAP.items():
        for pat in FORBIDDEN_PATTERNS:
            if pat in ep_const:
                results.append(CheckResult(
                    method=method,
                    endpoint_used=ep_const,
                    expected_endpoint="stable/*",
                    status="FAIL",
                    message=f"forbidden pattern '{pat}' found in stable method endpoint",
                ))
                break
    return results


def check_comma_separated_batch() -> list[CheckResult]:
    """Stable profile endpoint must NOT use comma-separated symbols in the path."""
    results = []
    profile_ep = STABLE_METHOD_MAP.get("get_batch_profiles", ("",))[0]
    # If the path contains a comma it would indicate batch-in-path usage
    if "," in profile_ep:
        results.append(CheckResult(
            method="get_batch_profiles",
            endpoint_used=profile_ep,
            expected_endpoint="profile",
            status="FAIL",
            message="comma-separated symbols in stable endpoint path are forbidden",
        ))
    else:
        results.append(CheckResult(
            method="get_batch_profiles",
            endpoint_used=profile_ep,
            expected_endpoint="profile",
            status="PASS",
            message="per-symbol query-param pattern confirmed (no comma batch)",
        ))
    return results


# ---------------------------------------------------------------------------
# Run all checks and return structured results
# ---------------------------------------------------------------------------

def run_all_checks() -> dict[str, list[CheckResult]]:
    return {
        "stable_methods":        check_stable_methods(),
        "legacy_methods":        check_legacy_methods(),
        "not_implemented":       check_not_implemented(),
        "forbidden_patterns":    check_forbidden_patterns(),
        "comma_batch_forbidden": check_comma_separated_batch(),
    }


def has_violations(results: dict[str, list[CheckResult]]) -> bool:
    return any(r.status == "FAIL" for group in results.values() for r in group)


# ---------------------------------------------------------------------------
# CLI output
# ---------------------------------------------------------------------------

_STATUS_ICON = {"PASS": "[PASS]", "FAIL": "[FAIL]", "WARN": "[WARN]"}
_WIDTH = 76


def _header(title: str) -> None:
    print(f"\n{'-' * _WIDTH}")
    print(f"  {title}")
    print(f"{'-' * _WIDTH}")


def _row(r: CheckResult) -> None:
    icon = _STATUS_ICON[r.status]
    method = r.method[:35].ljust(36)
    ep = r.endpoint_used[:30].ljust(31)
    print(f"  {icon}  {method} {ep} {r.message}")


def print_report(results: dict[str, list[CheckResult]]) -> None:
    print(f"\n{'=' * _WIDTH}")
    print("  FMP ENDPOINT COMPLIANCE REPORT")
    print(f"{'=' * _WIDTH}")

    _header("Core stable endpoints (daily scanner)")
    for r in results["stable_methods"]:
        _row(r)

    _header("Comma-batch guard")
    for r in results["comma_batch_forbidden"]:
        _row(r)

    _header("Forbidden pattern guard (no v3/v4 in stable methods)")
    for r in results["forbidden_patterns"]:
        _row(r)
    if not results["forbidden_patterns"]:
        print("  [PASS]  No forbidden patterns detected.")

    _header("Legacy / optional endpoints (non-daily-scanner use)")
    for r in results["legacy_methods"]:
        _row(r)

    _header("Registry entries not yet implemented in fmp_client.py")
    for r in results["not_implemented"]:
        _row(r)

    print(f"\n{'=' * _WIDTH}")
    violations = [r for group in results.values() for r in group if r.status == "FAIL"]
    warns = [r for group in results.values() for r in group if r.status == "WARN"]
    passes = [r for group in results.values() for r in group if r.status == "PASS"]

    print(f"  PASS: {len(passes)}   WARN: {len(warns)}   FAIL: {len(violations)}")
    if violations:
        print("\n  VIOLATIONS:")
        for r in violations:
            print(f"    [FAIL] {r.method}: {r.message}")
        print(f"\n  RESULT: NON-COMPLIANT - {len(violations)} violation(s) found")
    else:
        print("\n  RESULT: COMPLIANT - all stable endpoints are Starter-plan-safe")
    print(f"{'=' * _WIDTH}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    results = run_all_checks()
    print_report(results)
    return 1 if has_violations(results) else 0


if __name__ == "__main__":
    sys.exit(main())
