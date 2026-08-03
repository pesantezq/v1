# tests/test_pe_research_resolver.py
"""RESEARCH-ONLY PE resolver — never feeds the production scanner.

Live findings this pins (measured 2026-08-03 against the actual Starter plan):
  * `stable/ratios` DOES carry a direct PE as ``priceToEarningsRatio``. The
    production path misses it because `get_fundamentals_v3` reads
    stable/key-metrics and looks for ``peRatio``/``priceEarningsRatio`` — neither
    of which key-metrics returns.
  * ``earningsYield`` in key-metrics is a DECIMAL (AAPL 0.029, not 2.9).
  * ``1/earningsYield`` reconciles tightly for profitable names (AAPL 0.04%,
    NVDA 0.02%, XOM 0.00%) but NOT universally: BA diverged 15.07% (87.20 direct
    vs 74.05 derived) and INTC 7.12%. So derived is a labelled fallback, never an
    equivalent.
  * Negative earnings are the real trap: INTC yields PE ≈ -615, and a negative PE
    PASSES a ``pe > 50`` bubble guard while meaning loss-making. It gets its own
    state instead of a number.
"""
from __future__ import annotations

import pytest

from portfolio_automation.research import pe_resolver as PR


class _Client:
    """Stub exposing only the two already-approved stable methods used here."""

    def __init__(self, ratios=None, key_metrics=None, raiser=None):
        self._ratios, self._km, self._raiser = ratios, key_metrics, raiser
        self.ratio_calls = 0

    def get_ratios(self, symbol, period="annual", limit=1, ttl_days=30):
        self.ratio_calls += 1
        if self._raiser:
            raise self._raiser
        return self._ratios

    def get_key_metrics(self, symbol, period="annual", limit=1, ttl_days=30):
        return self._km


AS_OF = "2026-08-03T20:00:00+00:00"


def _resolve(**kw):
    return PR.resolve_pe(kw.pop("client"), "TEST", as_of=AS_OF, **kw)


# --------------------------------------------------------------------------
# Direct source
# --------------------------------------------------------------------------

def test_direct_source_resolves_from_price_to_earnings_ratio():
    r = _resolve(client=_Client(ratios={"priceToEarningsRatio": 34.107}))
    assert r["pe_ratio"] == pytest.approx(34.107)
    assert r["quality"] == "direct"
    assert r["source"] == "stable_ratios"
    assert r["source_field"] == "priceToEarningsRatio"
    assert r["period"] == "annual"
    assert r["as_of"] == AS_OF


def test_direct_source_is_preferred_over_derived():
    client = _Client(ratios={"priceToEarningsRatio": 34.1},
                     key_metrics={"earningsYield": 0.05})   # would derive 20.0
    r = _resolve(client=client)
    assert r["quality"] == "direct"
    assert r["pe_ratio"] == pytest.approx(34.1)


def test_negative_direct_pe_is_flagged_negative_earnings_not_a_number():
    """INTC's real shape. A negative PE must never reach a `pe > 50` guard as a
    plain number — it would pass and look cheap."""
    r = _resolve(client=_Client(ratios={"priceToEarningsRatio": -614.6}))
    assert r["quality"] == "negative_earnings"
    assert r["pe_ratio"] is None
    assert r["raw_value"] == pytest.approx(-614.6)


# --------------------------------------------------------------------------
# Derived source
# --------------------------------------------------------------------------

def test_derived_from_earnings_yield_carries_derived_provenance():
    r = _resolve(client=_Client(ratios={}, key_metrics={"earningsYield": 0.04}))
    assert r["pe_ratio"] == pytest.approx(25.0)
    assert r["quality"] == "derived"
    assert r["source"] == "derived_earnings_yield"
    assert r["source_field"] == "earningsYield"


def test_negative_earnings_yield_is_negative_earnings_not_a_negative_pe():
    r = _resolve(client=_Client(ratios={}, key_metrics={"earningsYield": -0.002}))
    assert r["quality"] == "negative_earnings"
    assert r["pe_ratio"] is None


def test_zero_earnings_yield_is_invalid_not_infinite():
    r = _resolve(client=_Client(ratios={}, key_metrics={"earningsYield": 0.0}))
    assert r["quality"] == "invalid"
    assert r["pe_ratio"] is None


def test_near_zero_earnings_yield_is_rejected_rather_than_exploding():
    """1/1e-9 would be a 1-billion PE. Guarded by an explicit floor."""
    r = _resolve(client=_Client(ratios={}, key_metrics={"earningsYield": 1e-9}))
    assert r["quality"] == "invalid"
    assert r["pe_ratio"] is None


def test_percentage_scaled_earnings_yield_is_caught_not_silently_used():
    """If FMP ever returned 2.9 meaning 2.9%, 1/2.9 = 0.34 would be an absurd PE.
    A plausibility band catches the unit error instead of publishing it."""
    r = _resolve(client=_Client(ratios={}, key_metrics={"earningsYield": 2.9}))
    assert r["quality"] == "invalid"
    assert r["pe_ratio"] is None
    assert "implausible" in r["reason"]


# --------------------------------------------------------------------------
# Unavailable / malformed
# --------------------------------------------------------------------------

def test_no_source_is_unavailable_not_zero():
    r = _resolve(client=_Client(ratios={}, key_metrics={}))
    assert r["quality"] == "unavailable"
    assert r["pe_ratio"] is None


def test_malformed_values_never_become_pe_zero():
    for bad in ("n/a", "", [], {}, True):
        r = _resolve(client=_Client(ratios={"priceToEarningsRatio": bad},
                                    key_metrics={"earningsYield": bad}))
        assert r["pe_ratio"] is None, bad
        assert r["quality"] in ("unavailable", "invalid"), bad


def test_client_exception_degrades_to_unavailable_without_raising():
    r = _resolve(client=_Client(raiser=RuntimeError("HTTP 402")))
    assert r["quality"] == "unavailable"
    assert r["pe_ratio"] is None


def test_none_client_is_unavailable():
    assert PR.resolve_pe(None, "TEST", as_of=AS_OF)["quality"] == "unavailable"


# --------------------------------------------------------------------------
# Provenance + batch
# --------------------------------------------------------------------------

def test_every_result_carries_symbol_source_and_as_of():
    r = _resolve(client=_Client(ratios={"priceToEarningsRatio": 20.0}))
    for key in ("symbol", "pe_ratio", "source", "source_field", "period",
                "as_of", "quality", "reason"):
        assert key in r, key


def test_batch_resolution_summarises_coverage():
    class _Multi:
        def get_ratios(self, symbol, period="annual", limit=1, ttl_days=30):
            return {"A": {"priceToEarningsRatio": 12.0},
                    "B": {"priceToEarningsRatio": -5.0},
                    "C": {}}.get(symbol, {})

        def get_key_metrics(self, symbol, period="annual", limit=1, ttl_days=30):
            return {"C": {"earningsYield": 0.05}}.get(symbol, {})

    out = PR.resolve_pe_batch(_Multi(), ["A", "B", "C", "D"], as_of=AS_OF)
    assert out["summary"]["direct"] == 1
    assert out["summary"]["derived"] == 1
    assert out["summary"]["negative_earnings"] == 1
    assert out["summary"]["unavailable"] == 1
    assert out["summary"]["eligible"] == 4
    assert out["summary"]["coverage"] == pytest.approx(0.5)   # usable PEs / eligible
    assert out["by_symbol"]["A"]["quality"] == "direct"


def test_batch_is_research_only_and_declares_it():
    out = PR.resolve_pe_batch(_Client(ratios={}), ["X"], as_of=AS_OF)
    assert out["research_only"] is True
    assert out["feeds_production_scanner"] is False


def test_resolver_uses_only_already_approved_stable_methods():
    """No new endpoint is introduced: get_ratios and get_key_metrics are both
    already in fmp_endpoint_compliance's STABLE_METHOD_MAP."""
    from fmp_endpoint_compliance import STABLE_METHOD_MAP
    for method in PR.REQUIRED_CLIENT_METHODS:
        assert method in STABLE_METHOD_MAP, method
