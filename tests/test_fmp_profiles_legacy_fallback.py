# tests/test_fmp_profiles_legacy_fallback.py
"""get_batch_profiles_v3 must survive the v3 legacy retirement.

``full_scan()`` needs three inputs: the constituent list, profiles (to pre-filter
by market cap), and quotes. As of 2026-08-03 ``/api/v3/profile/{csv}`` returns 403
for this key, so the profile input was dead and full_scan could not run at all.

The per-symbol ``stable/profile`` endpoint still works, but it names the field
``marketCap`` where v3 named it ``mktCap``. That difference is not cosmetic:
``main.py``'s non-premium path pre-filters with
``_prof_map[s].get('mktCap', 0) >= min_mkt_cap`` and has NO quote fallback there,
so unnormalized rows would leave the qualifying set EMPTY and full_scan would
yield zero candidates while every call reported success.
"""
from unittest.mock import MagicMock

import pytest

from fmp_client import FMPClient


@pytest.fixture
def client(tmp_path):
    c = FMPClient.__new__(FMPClient)          # bypass __init__/network setup
    c._cache = MagicMock()
    c._cache.get.return_value = None
    c._cache.get_stale.return_value = None
    c._counter = MagicMock()
    c._counter.would_exceed.return_value = False
    c._counter.today_count = 0
    c._budget = 1500
    return c


def _stable_profile(sym, cap):
    return {"symbol": sym, "companyName": f"{sym} Inc", "sector": "Technology",
            "marketCap": cap, "price": 100.0}


def test_falls_back_to_stable_per_symbol_when_v3_403s(client, monkeypatch):
    def _raw_get(path, params, base_url=None):
        raise RuntimeError("FMP authentication failed (HTTP 403)")

    monkeypatch.setattr(client, "_raw_get", _raw_get)
    monkeypatch.setattr(
        client, "get_profile",
        lambda sym, ttl_days=7: _stable_profile(sym, 3e12),
    )

    rows = client.get_batch_profiles_v3(["AAPL", "MSFT"])
    assert {r["symbol"] for r in rows} == {"AAPL", "MSFT"}


def test_fallback_rows_are_normalized_to_the_v3_mktcap_contract(client, monkeypatch):
    """The whole point: downstream reads mktCap, so the fallback must supply it."""
    monkeypatch.setattr(
        client, "_raw_get",
        lambda path, params, base_url=None: (_ for _ in ()).throw(RuntimeError("HTTP 403")),
    )
    monkeypatch.setattr(
        client, "get_profile",
        lambda sym, ttl_days=7: _stable_profile(sym, 3_000_000_000_000),
    )

    rows = client.get_batch_profiles_v3(["AAPL"])
    assert rows[0]["mktCap"] == 3_000_000_000_000
    # main.py pre-filters on exactly this expression:
    assert float(rows[0].get("mktCap", 0) or 0) >= 5e9
    assert rows[0]["sector"] == "Technology"


def test_legacy_path_still_used_when_it_works(client, monkeypatch):
    """Legacy subscribers keep the cheap batched call (5 calls, not 500)."""
    calls = []

    def _raw_get(path, params, base_url=None):
        calls.append(path)
        return [{"symbol": "AAPL", "mktCap": 3e12, "sector": "Technology"}]

    monkeypatch.setattr(client, "_raw_get", _raw_get)
    monkeypatch.setattr(
        client, "get_profile",
        lambda sym, ttl_days=7: pytest.fail("stable fallback must not be used"),
    )

    rows = client.get_batch_profiles_v3(["AAPL"])
    assert rows[0]["mktCap"] == 3e12
    assert any("v3/profile" in c for c in calls)


def test_empty_stable_fallback_returns_empty_not_raises(client, monkeypatch):
    """A total profile outage must degrade, not crash the pipeline — the
    surrounding scanner stage reports it via the sufficiency guard instead."""
    monkeypatch.setattr(
        client, "_raw_get",
        lambda path, params, base_url=None: (_ for _ in ()).throw(RuntimeError("HTTP 403")),
    )
    monkeypatch.setattr(client, "get_profile", lambda sym, ttl_days=7: None)

    assert client.get_batch_profiles_v3(["AAPL", "MSFT"]) == []
