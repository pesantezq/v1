"""Phase 5 tests — deterministic security identity resolution.

Covers: FIGI exact wins, CUSIP exact, app-symbol map, unambiguous issuer match,
ambiguous issuer -> unresolved, missing CUSIP, no-mapping unresolved, priority
order, and the point-in-time rule (a ticker mapping out of its effective window
is NOT used; a timeless mapping always is; no later ticker projected backward).
"""

from __future__ import annotations

from datetime import date

from portfolio_automation.institutional_intelligence.security_identity import (
    METHOD_APP_SYMBOL,
    METHOD_CUSIP,
    METHOD_FIGI,
    METHOD_ISSUER,
    METHOD_UNRESOLVED,
    REASON_AMBIGUOUS_ISSUER,
    REASON_MAPPING_OUT_OF_WINDOW,
    REASON_NO_CUSIP,
    REASON_NO_MAPPING,
    MappingEntry,
    SecurityIdentityResolver,
    normalize_issuer,
)

_CUSIP = "037833100"
_FIGI = "BBG000B9XRY4"


def test_figi_exact_wins_over_cusip():
    r = SecurityIdentityResolver(
        figi_map={_FIGI: [MappingEntry("AAPL_FIGI", timeless=True)]},
        cusip_map={_CUSIP: [MappingEntry("AAPL_CUSIP", timeless=True)]},
    )
    res = r.resolve(cusip=_CUSIP, figi=_FIGI, issuer_name="APPLE INC")
    assert res.resolved and res.method == METHOD_FIGI and res.symbol == "AAPL_FIGI"


def test_cusip_exact():
    r = SecurityIdentityResolver(cusip_map={_CUSIP: [MappingEntry("AAPL", timeless=True)]})
    res = r.resolve(cusip=_CUSIP, figi=None, issuer_name="APPLE INC")
    assert res.method == METHOD_CUSIP and res.symbol == "AAPL"


def test_app_symbol_map():
    r = SecurityIdentityResolver(app_symbol_map={_CUSIP: [MappingEntry("AAPL", timeless=True,
                                                                       source="app")]})
    res = r.resolve(cusip=_CUSIP, figi=None, issuer_name="APPLE INC")
    assert res.method == METHOD_APP_SYMBOL and res.provenance == "app"


def test_unambiguous_issuer_match_lower_confidence():
    r = SecurityIdentityResolver(issuer_index={"APPLE INC": ["AAPL"]})
    res = r.resolve(cusip=_CUSIP, figi=None, issuer_name="Apple Inc.")
    assert res.method == METHOD_ISSUER and res.symbol == "AAPL"
    assert "issuer_name_match_lower_confidence" in res.warnings


def test_ambiguous_issuer_unresolved():
    r = SecurityIdentityResolver(issuer_index={"BOX": ["BOX", "BOXX"]})
    res = r.resolve(cusip=_CUSIP, figi=None, issuer_name="BOX")
    assert not res.resolved and res.reason == REASON_AMBIGUOUS_ISSUER  # never guesses


def test_missing_cusip_unresolved():
    r = SecurityIdentityResolver()
    res = r.resolve(cusip=None, figi=None, issuer_name="APPLE INC")
    assert not res.resolved and res.reason == REASON_NO_CUSIP


def test_no_mapping_unresolved():
    r = SecurityIdentityResolver()
    res = r.resolve(cusip=_CUSIP, figi=None, issuer_name="UNKNOWN CO")
    assert not res.resolved and res.reason == REASON_NO_MAPPING and res.symbol is None


def test_normalize_issuer():
    assert normalize_issuer("Apple Inc.") == "APPLE"
    assert normalize_issuer("Microsoft Corp") == "MICROSOFT"
    assert normalize_issuer(None) == ""


# --- point-in-time -------------------------------------------------------

def test_ticker_mapping_out_of_window_not_used():
    # A ticker mapping effective only from 2026-01-01 must NOT resolve a filing
    # evaluated in 2025 (no backward projection of a later ticker).
    r = SecurityIdentityResolver(cusip_map={_CUSIP: [
        MappingEntry("NEWTICK", effective_from=date(2026, 1, 1)),
    ]})
    res = r.resolve(cusip=_CUSIP, figi=None, issuer_name="X",
                    as_of=date(2025, 6, 1))
    assert not res.resolved and res.reason == REASON_MAPPING_OUT_OF_WINDOW


def test_ticker_mapping_in_window_used():
    r = SecurityIdentityResolver(cusip_map={_CUSIP: [
        MappingEntry("TICK", effective_from=date(2025, 1, 1), effective_to=date(2025, 12, 31)),
    ]})
    res = r.resolve(cusip=_CUSIP, figi=None, issuer_name="X", as_of=date(2025, 6, 1))
    assert res.resolved and res.symbol == "TICK"


def test_timeless_mapping_always_used():
    r = SecurityIdentityResolver(cusip_map={_CUSIP: [MappingEntry("PERM", timeless=True)]})
    # usable at any date, including far in the past
    res = r.resolve(cusip=_CUSIP, figi=None, issuer_name="X", as_of=date(2000, 1, 1))
    assert res.resolved and res.symbol == "PERM"


def test_reversioned_ticker_picks_window_correct_symbol():
    r = SecurityIdentityResolver(cusip_map={_CUSIP: [
        MappingEntry("OLD", effective_from=date(2020, 1, 1), effective_to=date(2024, 12, 31)),
        MappingEntry("NEW", effective_from=date(2025, 1, 1)),
    ]})
    assert r.resolve(cusip=_CUSIP, figi=None, issuer_name="X",
                     as_of=date(2023, 6, 1)).symbol == "OLD"
    assert r.resolve(cusip=_CUSIP, figi=None, issuer_name="X",
                     as_of=date(2025, 6, 1)).symbol == "NEW"
