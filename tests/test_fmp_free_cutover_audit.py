"""Offline-only, no-provider-call tests for the November 27 FMP tier cutover."""

from __future__ import annotations

import json
import urllib.request

import pytest

from fmp_free_cutover_audit import (
    FMP_BASIC_DAILY_REQUEST_LIMIT,
    OWNER_CUTOVER_DATE,
    assess_free_cutover,
    main,
)


def test_default_is_not_ready_and_is_pinned_to_owner_date():
    report = assess_free_cutover()
    assert report["effective_date"] == OWNER_CUTOVER_DATE
    assert report["status"] == "NOT_READY"
    assert report["report_mode"] == "OFFLINE_SOURCE_ONLY_NO_NETWORK"
    assert report["account_confirmation"] == "PENDING"


def test_starter_safe_never_means_basic_verified():
    report = assess_free_cutover()
    assert len(report["daily_required_endpoints"]) >= 4
    assert all(
        ep["basic_entitlement"] == "UNVERIFIED"
        for ep in report["daily_required_endpoints"]
    )
    assert {"quote", "profile", "historical_prices"}.issubset(
        {ep["registry_key"] for ep in report["daily_required_endpoints"]}
    )


def test_legacy_universe_methods_are_exposed_not_claimed_executed():
    report = assess_free_cutover()
    methods = {m["method"] for m in report["legacy_universe_call_sites"]}
    assert methods == {"get_sp500_constituents", "get_batch_profiles_v3"}
    assert all(x["endpoint"].startswith("v3/") for x in report["legacy_universe_call_sites"])


def test_full_universe_refresh_cannot_fit_basic_daily_limit():
    report = assess_free_cutover()
    assert report["basic_plan_daily_request_limit"] == FMP_BASIC_DAILY_REQUEST_LIMIT
    assert report["documented_monthly_full_scan_requests"] > FMP_BASIC_DAILY_REQUEST_LIMIT
    blocking = {x["code"]: x for x in report["blockers"]}
    assert blocking["FULL_SCAN_EXCEEDS_BASIC_DAILY_QUOTA"]["count"] == 1


def test_missing_registry_data_fail_closed_instead_of_assuming_free():
    report = assess_free_cutover(registry={}, legacy_methods={})
    codes = {x["code"] for x in report["blockers"]}
    assert "NO_REQUIRED_DAILY_ENDPOINTS" in codes
    assert report["vs002_required_endpoint"] == "MISSING_FROM_REGISTRY"
    assert report["status"] == "NOT_READY"


def test_vs002_adjustment_identity_is_preserved_as_a_blocker():
    report = assess_free_cutover()
    assert report["vs002_required_endpoint"] == (
        "/stable/historical-price-eod/dividend-adjusted"
    )
    assert "VS002_ENDPOINT_AND_ADJUSTMENT_UNVERIFIED" in {
        x["code"] for x in report["blockers"]
    }


def test_audit_does_not_use_network_or_expose_secret(monkeypatch):
    def network_forbidden(*args, **kwargs):
        raise AssertionError("No network is permitted in FMP Basic audit")

    monkeypatch.setattr(urllib.request, "urlopen", network_forbidden)
    monkeypatch.setenv("FMP_API_KEY", "SHOULD_NEVER_APPEAR")
    payload = json.dumps(assess_free_cutover())
    assert "SHOULD_NEVER_APPEAR" not in payload
    assert "NO_DATA_FETCH_NO_SECRETS_NO_RUNTIME_CHANGE_NO_TRADING" in payload


def test_strict_cli_refuses_premature_cutover(capsys):
    assert main(["--strict"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "NOT_READY"
    assert out["effective_date"] == OWNER_CUTOVER_DATE


def test_display_cli_does_not_claim_readiness(capsys):
    assert main([]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "NOT_READY"


def test_invalid_dates_fail_before_any_provider_interaction():
    with pytest.raises(ValueError):
        assess_free_cutover(effective_date="2026-11-99")
    with pytest.raises(ValueError):
        assess_free_cutover(basic_daily_limit=0)
