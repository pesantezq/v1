# tests/test_sp500_constituent_freshness.py
"""A last-good constituent cache must carry BOTH plausibility and freshness.

Gap closed 2026-08-03 (second hardening pass): the resolver shipped in
`e36fa01c` validated a cached universe's SIZE (>=400 rows) but nothing else, so a
structurally plausible list stayed trusted indefinitely. 503 rows from an ancient
cache read exactly like 503 rows scraped a minute ago.

Freshness policy (documented in universe/sp500_constituents.py):
  fresh    age <= CACHE_FRESH_MAX_DAYS (7, aligned with degraded_mode.DEFAULT_STALE_DAYS)
  stale    CACHE_FRESH_MAX_DAYS < age <= CACHE_USABLE_MAX_DAYS (30)  -> usable, degraded
  expired  age > CACHE_USABLE_MAX_DAYS                              -> fail closed
  unknown  timestamp missing/unparseable                            -> fail closed

Time is injected everywhere; no test reads the wall clock.
"""
from __future__ import annotations

import json

import pytest

from universe import sp500_constituents as sc

NOW = "2026-08-03T12:00:00+00:00"


def _rows(n, prefix="SYM"):
    return [{"symbol": f"{prefix}{i}", "name": f"Co {i}", "sector": "Industrials",
             "subSector": "Widgets"} for i in range(n)]


def _cache(tmp_path, rows, fetched_at, name="c.json", source="free_scrape"):
    path = tmp_path / name
    path.write_text(json.dumps({
        "schema_version": 1, "fetched_at": fetched_at, "source": source,
        "count": len(rows), "constituents": rows,
    }))
    return path


class _Dead:
    """Both live sources unavailable — the only condition that reaches the cache."""

    def get_sp500_constituents(self, ttl_days=7):
        raise RuntimeError("FMP authentication failed (HTTP 403)")


def _boom():
    raise RuntimeError("free source unreachable")


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

@pytest.mark.parametrize("fetched_at,expected", [
    ("2026-08-03T11:00:00+00:00", "fresh"),     # hours old
    ("2026-07-28T12:00:00+00:00", "fresh"),     # 6d
    ("2026-07-27T12:00:00+00:00", "fresh"),     # exactly 7d — boundary is inclusive
    ("2026-07-26T12:00:00+00:00", "stale"),     # 8d
    ("2026-07-04T12:00:00+00:00", "stale"),     # 30d — boundary inclusive
    ("2026-07-03T11:00:00+00:00", "expired"),   # >30d
    ("2025-01-01T00:00:00+00:00", "expired"),   # ancient
])
def test_freshness_classification_boundaries(fetched_at, expected):
    state, age = sc.classify_cache_freshness(fetched_at, NOW)
    assert state == expected
    assert age is not None and age >= 0


def test_missing_timestamp_is_unknown_never_fresh():
    state, age = sc.classify_cache_freshness(None, NOW)
    assert state == "unknown"
    assert age is None


def test_malformed_timestamp_is_unknown_never_fresh():
    for bad in ("not-a-date", "", "2026-13-45", 12345):
        state, age = sc.classify_cache_freshness(bad, NOW)
        assert state == "unknown", f"{bad!r} classified as {state}"
        assert age is None


def test_naive_timestamp_is_handled_as_utc_not_rejected():
    """Timestamps written before tz-awareness was enforced must still classify."""
    state, _ = sc.classify_cache_freshness("2026-08-03T11:00:00", NOW)
    assert state == "fresh"


def test_future_timestamp_is_not_trusted_as_fresh():
    """A clock-skewed or hand-edited future stamp must not certify freshness."""
    state, _ = sc.classify_cache_freshness("2027-01-01T00:00:00+00:00", NOW)
    assert state == "unknown"


# --------------------------------------------------------------------------
# Resolver behaviour per freshness state
# --------------------------------------------------------------------------

def test_fresh_cache_is_usable_but_degraded(tmp_path):
    path = _cache(tmp_path, _rows(503), "2026-08-01T12:00:00+00:00")
    res = sc.resolve_constituents(client=_Dead(), cache_path=path, fetcher=_boom, now=NOW)
    assert res.source == "cache"
    assert res.freshness == "fresh"
    assert res.degraded is True          # any cache read is degraded by definition
    assert res.age_days == pytest.approx(2.0, abs=0.05)
    assert res.count == 503


def test_stale_cache_is_usable_and_marked_stale(tmp_path):
    path = _cache(tmp_path, _rows(503), "2026-07-20T12:00:00+00:00")   # 14d
    res = sc.resolve_constituents(client=_Dead(), cache_path=path, fetcher=_boom, now=NOW)
    assert res.source == "cache"
    assert res.freshness == "stale"
    assert res.degraded is True
    assert "stale" in res.detail.lower()


def test_expired_cache_fails_closed(tmp_path):
    path = _cache(tmp_path, _rows(503), "2026-01-01T12:00:00+00:00")
    with pytest.raises(sc.ConstituentSourceError) as exc:
        sc.resolve_constituents(client=_Dead(), cache_path=path, fetcher=_boom, now=NOW)
    assert "expired" in str(exc.value).lower()


def test_cache_with_unknown_age_fails_closed(tmp_path):
    """Cannot certify currency => cannot serve it. 503 plausible rows are not enough."""
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"constituents": _rows(503), "count": 503}))  # no fetched_at
    with pytest.raises(sc.ConstituentSourceError) as exc:
        sc.resolve_constituents(client=_Dead(), cache_path=path, fetcher=_boom, now=NOW)
    assert "unknown" in str(exc.value).lower() or "timestamp" in str(exc.value).lower()


def test_short_cache_is_rejected_regardless_of_freshness(tmp_path):
    """Plausibility and freshness are independent gates; both must pass."""
    path = _cache(tmp_path, _rows(50), NOW)      # brand new but implausible
    with pytest.raises(sc.ConstituentSourceError):
        sc.resolve_constituents(client=_Dead(), cache_path=path, fetcher=_boom, now=NOW)


def test_corrupt_cache_is_rejected(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{not json")
    with pytest.raises(sc.ConstituentSourceError):
        sc.resolve_constituents(client=_Dead(), cache_path=path, fetcher=_boom, now=NOW)


def test_unreadable_cache_is_rejected_without_permission_bits(tmp_path):
    """UID-independent: a DIRECTORY where a file is required. Works as root."""
    path = tmp_path / "c.json"
    path.mkdir()
    with pytest.raises(sc.ConstituentSourceError):
        sc.resolve_constituents(client=_Dead(), cache_path=path, fetcher=_boom, now=NOW)


# --------------------------------------------------------------------------
# Live sources are always fresh, and must not be shadowed by cache policy
# --------------------------------------------------------------------------

def test_live_free_source_reports_fresh_and_not_degraded(tmp_path):
    res = sc.resolve_constituents(client=_Dead(), cache_path=tmp_path / "c.json",
                                  fetcher=lambda: _rows(503), now=NOW)
    assert res.source == "free_scrape"
    assert res.freshness == "fresh"
    assert res.degraded is False
    assert res.age_days == 0.0
    assert res.fetched_at == NOW


def test_expired_cache_does_not_block_a_working_live_source(tmp_path):
    """Freshness policy gates the FALLBACK, never a live read."""
    path = _cache(tmp_path, _rows(503), "2025-01-01T00:00:00+00:00")
    res = sc.resolve_constituents(client=_Dead(), cache_path=path,
                                  fetcher=lambda: _rows(500), now=NOW)
    assert res.source == "free_scrape"
    assert res.freshness == "fresh"


def test_fmp_source_reports_fresh(tmp_path):
    class _OK:
        def get_sp500_constituents(self, ttl_days=7):
            return _rows(503)

    res = sc.resolve_constituents(client=_OK(), cache_path=tmp_path / "c.json",
                                  fetcher=_boom, now=NOW)
    assert res.source == "fmp"
    assert res.freshness == "fresh"
    assert res.degraded is False


# --------------------------------------------------------------------------
# Determinism + transportable payload
# --------------------------------------------------------------------------

def test_resolution_exposes_a_transportable_payload(tmp_path):
    path = _cache(tmp_path, _rows(503), "2026-07-20T12:00:00+00:00")
    res = sc.resolve_constituents(client=_Dead(), cache_path=path, fetcher=_boom, now=NOW)
    payload = res.as_payload()
    assert payload["source"] == "cache"
    assert payload["count"] == 503
    assert payload["freshness"] == "stale"
    assert payload["degraded"] is True
    assert payload["age_days"] is not None
    assert payload["fetched_at"]
    # JSON-serializable so it can be transported to the run-summary artifact.
    json.dumps(payload)


def test_injected_now_makes_freshness_deterministic(tmp_path):
    path = _cache(tmp_path, _rows(503), "2026-07-20T12:00:00+00:00")
    a = sc.resolve_constituents(client=_Dead(), cache_path=path, fetcher=_boom, now=NOW)
    b = sc.resolve_constituents(client=_Dead(), cache_path=path, fetcher=_boom, now=NOW)
    assert a.as_payload() == b.as_payload()


def test_thresholds_are_module_constants_not_inline_literals():
    assert sc.CACHE_FRESH_MAX_DAYS == 7        # == degraded_mode.DEFAULT_STALE_DAYS
    assert sc.CACHE_USABLE_MAX_DAYS == 30
    assert sc.CACHE_FRESH_MAX_DAYS < sc.CACHE_USABLE_MAX_DAYS
