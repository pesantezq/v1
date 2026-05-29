"""
FMP Endpoint Registry Compliance Tests (10 tests — Task E)

Verifies:
  1.  Core stable endpoints pass compliance
  2.  Re-introducing v3 for get_profile fails
  3.  Re-introducing v3 for get_historical_prices fails
  4.  Re-introducing v3 for get_stock_news fails
  5.  v4 bulk endpoints are warning-only (not FAIL)
  6.  v3 sp500_constituent is warning-only when isolated
  7.  Unknown endpoint is flagged as FAIL
  8.  Registry covers all implemented core methods
  9.  Daily scanner path doesn't call legacy v3 methods
  10. Comma-separated stable/profile usage is forbidden
"""

import pytest

from fmp_endpoint_compliance import (
    STABLE_METHOD_MAP,
    LEGACY_METHOD_MAP,
    NOT_YET_IMPLEMENTED,
    check_stable_methods,
    check_legacy_methods,
    check_forbidden_patterns,
    check_comma_separated_batch,
    run_all_checks,
    has_violations,
    CheckResult,
)
from fmp_endpoint_registry import REGISTRY, get_core_daily_required


# ---------------------------------------------------------------------------
# 1. Core stable endpoints all pass compliance
# ---------------------------------------------------------------------------

class TestCoreStableEndpointsPass:
    def test_all_stable_methods_pass(self):
        results = check_stable_methods()
        failures = [r for r in results if r.status == "FAIL"]
        assert failures == [], (
            "Core stable method(s) failed compliance:\n"
            + "\n".join(f"  {r.method}: {r.message}" for r in failures)
        )

    def test_all_stable_methods_covered(self):
        """Every method in STABLE_METHOD_MAP must produce a result."""
        results = check_stable_methods()
        covered = {r.method for r in results}
        assert covered == set(STABLE_METHOD_MAP.keys())

    def test_full_check_no_violations(self):
        results = run_all_checks()
        assert not has_violations(results), (
            "Unexpected violations in full compliance check"
        )


# ---------------------------------------------------------------------------
# 2. Re-introducing v3 for get_profile fails
# ---------------------------------------------------------------------------

class TestV3ProfileFails:
    def test_v3_profile_detected_as_violation(self, monkeypatch):
        monkeypatch.setitem(STABLE_METHOD_MAP, "get_batch_profiles", ("v3/profile", "profile"))
        results = check_stable_methods()
        profile_result = next(r for r in results if r.method == "get_batch_profiles")
        assert profile_result.status == "FAIL", (
            "v3/profile in stable method map should be a FAIL"
        )

    def test_forbidden_pattern_catches_v3_profile(self, monkeypatch):
        monkeypatch.setitem(STABLE_METHOD_MAP, "get_batch_profiles", ("v3/profile", "profile"))
        results = check_forbidden_patterns()
        profile_violation = [r for r in results if r.method == "get_batch_profiles"]
        assert profile_violation, "Forbidden-pattern check must catch v3/ prefix"
        assert profile_violation[0].status == "FAIL"


# ---------------------------------------------------------------------------
# 3. Re-introducing v3 for get_historical_prices fails
# ---------------------------------------------------------------------------

class TestV3HistoricalFails:
    def test_v3_historical_endpoint_is_violation(self, monkeypatch):
        monkeypatch.setitem(
            STABLE_METHOD_MAP,
            "get_historical_prices",
            ("v3/historical-price-full", "historical_prices"),
        )
        results = check_stable_methods()
        hist_result = next(r for r in results if r.method == "get_historical_prices")
        assert hist_result.status == "FAIL"

    def test_forbidden_pattern_catches_v3_historical(self, monkeypatch):
        monkeypatch.setitem(
            STABLE_METHOD_MAP,
            "get_historical_prices",
            ("v3/historical-price-full", "historical_prices"),
        )
        violations = check_forbidden_patterns()
        assert any(r.method == "get_historical_prices" for r in violations), (
            "Forbidden-pattern check must catch v3/historical-price-full"
        )


# ---------------------------------------------------------------------------
# 4. Re-introducing v3 for get_stock_news fails
# ---------------------------------------------------------------------------

class TestV3StockNewsFails:
    def test_v3_news_endpoint_is_violation(self, monkeypatch):
        monkeypatch.setitem(
            STABLE_METHOD_MAP,
            "get_stock_news",
            ("v3/stock_news", "stock_news"),
        )
        results = check_stable_methods()
        news_result = next(r for r in results if r.method == "get_stock_news")
        assert news_result.status == "FAIL"

    def test_forbidden_pattern_catches_v3_news(self, monkeypatch):
        monkeypatch.setitem(
            STABLE_METHOD_MAP,
            "get_stock_news",
            ("v3/stock_news", "stock_news"),
        )
        violations = check_forbidden_patterns()
        assert any(r.method == "get_stock_news" for r in violations)


# ---------------------------------------------------------------------------
# 5. v4 bulk endpoints are warning-only (WARN, not FAIL)
# ---------------------------------------------------------------------------

class TestV4BulkIsWarnOnly:
    def test_bulk_key_metrics_is_warn(self):
        results = check_legacy_methods()
        bulk_km = next(
            (r for r in results if r.method == "get_bulk_key_metrics"), None
        )
        assert bulk_km is not None, "get_bulk_key_metrics must be in legacy results"
        assert bulk_km.status == "WARN", (
            f"Expected WARN, got {bulk_km.status}: {bulk_km.message}"
        )

    def test_bulk_profiles_is_warn(self):
        results = check_legacy_methods()
        bulk_p = next(
            (r for r in results if r.method == "get_bulk_profiles"), None
        )
        assert bulk_p is not None
        assert bulk_p.status == "WARN"


# ---------------------------------------------------------------------------
# 6. v3 sp500_constituent is warning-only when isolated
# ---------------------------------------------------------------------------

class TestV3Sp500IsWarnOnly:
    def test_sp500_constituents_is_warn(self):
        results = check_legacy_methods()
        sp500 = next(
            (r for r in results if r.method == "get_sp500_constituents"), None
        )
        assert sp500 is not None, "get_sp500_constituents must appear in legacy results"
        assert sp500.status == "WARN", (
            f"v3 sp500_constituent should be WARN-only, got {sp500.status}"
        )

    def test_sp500_not_in_stable_map(self):
        assert "get_sp500_constituents" not in STABLE_METHOD_MAP, (
            "get_sp500_constituents must NOT appear in STABLE_METHOD_MAP"
        )


# ---------------------------------------------------------------------------
# 7. Unknown endpoint is flagged as FAIL
# ---------------------------------------------------------------------------

class TestUnknownEndpointFlagged:
    def test_unknown_registry_key_is_fail(self, monkeypatch):
        monkeypatch.setitem(
            STABLE_METHOD_MAP,
            "get_mystery_data",
            ("mystery/data", "nonexistent_registry_key"),
        )
        results = check_stable_methods()
        mystery = next(r for r in results if r.method == "get_mystery_data")
        assert mystery.status == "FAIL", (
            "An endpoint with no registry key must be FAIL"
        )
        assert "not found in REGISTRY" in mystery.message


# ---------------------------------------------------------------------------
# 8. Registry covers all implemented core methods
# ---------------------------------------------------------------------------

class TestRegistryCoversCoreMethods:
    def test_all_stable_methods_have_registry_entry(self):
        """Every method in STABLE_METHOD_MAP must map to a real REGISTRY key."""
        missing = []
        for method, (_, registry_key) in STABLE_METHOD_MAP.items():
            if registry_key not in REGISTRY:
                missing.append(f"{method} → '{registry_key}'")
        assert not missing, (
            "STABLE_METHOD_MAP references registry keys not in REGISTRY:\n"
            + "\n".join(f"  {m}" for m in missing)
        )

    def test_daily_required_keys_are_in_registry(self):
        required = get_core_daily_required()
        assert len(required) >= 4, "At least 4 daily-required endpoints expected"
        for key in required:
            assert key in REGISTRY

    def test_required_daily_endpoints_all_in_stable_map(self):
        required = get_core_daily_required()
        registry_keys_in_stable_map = {rk for _, rk in STABLE_METHOD_MAP.values()}
        uncovered = [k for k in required if k not in registry_keys_in_stable_map]
        assert not uncovered, (
            "Daily-required registry keys not covered by any stable method:\n"
            + "\n".join(f"  {k}" for k in uncovered)
        )


# ---------------------------------------------------------------------------
# Regression: financial-growth endpoint correctness + coverage.
#
# revenueGrowth lives in stable/financial-growth (verified HTTP 200), NOT in
# stable/financial-statement-growth (the stale, never-validated path that
# zeroed the weekly watchlist on 2026-05-28). Lock both the registry endpoint
# value and the fact that get_financial_growth is registered under compliance.
# ---------------------------------------------------------------------------

class TestFinancialGrowthEndpoint:
    def test_registry_uses_verified_financial_growth_path(self):
        spec = REGISTRY.get("financial_growth")
        assert spec is not None, "financial_growth missing from REGISTRY"
        assert spec["endpoint"] == "/stable/financial-growth", (
            f"financial_growth must use the verified /stable/financial-growth "
            f"endpoint, got {spec['endpoint']!r}"
        )
        assert spec.get("starter_safe") is True

    def test_get_financial_growth_is_registered_stable(self):
        assert "get_financial_growth" in STABLE_METHOD_MAP, (
            "get_financial_growth is implemented and must be in STABLE_METHOD_MAP "
            "so its endpoint is under compliance coverage"
        )
        ep_const, registry_key = STABLE_METHOD_MAP["get_financial_growth"]
        assert ep_const == "financial-growth"
        assert registry_key == "financial_growth"

    def test_financial_growth_not_in_not_yet_implemented(self):
        assert "financial_growth" not in NOT_YET_IMPLEMENTED, (
            "financial_growth is now implemented as get_financial_growth"
        )


# ---------------------------------------------------------------------------
# 9. Daily scanner path doesn't call legacy v3 methods
# ---------------------------------------------------------------------------

class TestDailyScannerNoLegacyCalls:
    DAILY_SCANNER_METHODS = {
        "get_batch_quotes",
        "get_batch_profiles",
        "get_historical_prices",
        "get_ratios",
        "get_stock_news",
    }

    def test_daily_scanner_methods_not_in_legacy_map(self):
        overlap = self.DAILY_SCANNER_METHODS & set(LEGACY_METHOD_MAP.keys())
        assert not overlap, (
            "Daily scanner methods found in LEGACY_METHOD_MAP (must use stable):\n"
            + "\n".join(f"  {m}" for m in overlap)
        )

    def test_daily_scanner_methods_all_in_stable_map(self):
        missing = self.DAILY_SCANNER_METHODS - set(STABLE_METHOD_MAP.keys())
        assert not missing, (
            "Daily scanner methods not in STABLE_METHOD_MAP:\n"
            + "\n".join(f"  {m}" for m in missing)
        )

    def test_no_required_daily_endpoint_is_premium(self):
        required = get_core_daily_required()
        for key in required:
            spec = REGISTRY[key]
            assert spec["starter_safe"], (
                f"Daily-required endpoint '{key}' is NOT starter_safe"
            )


# ---------------------------------------------------------------------------
# 10. Comma-separated stable/profile usage is forbidden
# ---------------------------------------------------------------------------

class TestCommaSeparatedForbidden:
    def test_current_profile_has_no_comma(self):
        results = check_comma_separated_batch()
        assert len(results) == 1
        assert results[0].status == "PASS", (
            "Current profile endpoint must be PASS (no comma batch)"
        )

    def test_comma_in_profile_endpoint_is_fail(self, monkeypatch):
        monkeypatch.setitem(
            STABLE_METHOD_MAP,
            "get_batch_profiles",
            ("profile/AAPL,MSFT,GOOG", "profile"),
        )
        results = check_comma_separated_batch()
        assert results[0].status == "FAIL", (
            "Comma-separated symbols in stable endpoint path must be FAIL"
        )

    def test_registry_profile_is_per_symbol(self):
        """Registry confirms stable/profile is per-symbol, not batch."""
        spec = REGISTRY["profile"]
        assert spec["per_symbol"] is True, (
            "Registry should mark stable/profile as per_symbol=True"
        )
