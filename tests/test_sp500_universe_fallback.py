# tests/test_sp500_universe_fallback.py
"""SP500Universe must survive the loss of FMP's constituent endpoint.

Before 2026-08-03 this class was a 27-line wrapper with a single hard dependency
on ``client.get_sp500_constituents()``. When FMP retired the v3 legacy API that
call began returning 403, so ``get_symbols()`` raised and took ``full_scan()``
with it — leaving ``weekly_refresh()`` (which can only ever drop symbols) as the
sole surviving scanner path. These tests pin the fallback and the provenance
that makes a degraded read visible.
"""
import pytest

from universe.sp500 import SP500Universe
from universe.sp500_constituents import ConstituentSourceError


def _rows(n):
    return [
        {"symbol": f"SYM{i}", "name": f"Co {i}", "sector": "Industrials",
         "subSector": "Widgets"}
        for i in range(n)
    ]


class _Client403:
    """Reproduces the live 2026-08-03 condition."""

    def get_sp500_constituents(self, ttl_days=7):
        raise RuntimeError(
            "FMP authentication failed (HTTP 403) for "
            "https://financialmodelingprep.com/api/v3/sp500_constituent"
        )


class _ClientOK:
    def get_sp500_constituents(self, ttl_days=7):
        return _rows(503)


def test_get_symbols_survives_fmp_403_via_free_source(tmp_path):
    uni = SP500Universe(
        _Client403(),
        cache_path=tmp_path / "c.json",
        fetcher=lambda: _rows(500),
    )
    symbols = uni.get_symbols()
    assert len(symbols) == 500
    assert symbols == sorted(symbols)
    assert uni.last_resolution.source == "free_scrape"


def test_fmp_still_wins_when_available(tmp_path):
    uni = SP500Universe(
        _ClientOK(), cache_path=tmp_path / "c.json", fetcher=lambda: _rows(500)
    )
    assert len(uni.get_symbols()) == 503
    assert uni.last_resolution.source == "fmp"
    assert uni.last_resolution.degraded is False


def test_constituents_carry_sector_metadata(tmp_path):
    uni = SP500Universe(
        _Client403(), cache_path=tmp_path / "c.json", fetcher=lambda: _rows(500)
    )
    rows = uni.get_constituents()
    assert rows[0]["sector"] == "Industrials"


def test_degraded_cache_read_is_flagged(tmp_path):
    """A stale universe is usable but must not look live — that distinction is
    what the 3-symbol watchlist lacked for two months."""
    from universe import sp500_constituents as sc

    cache = tmp_path / "c.json"
    sc.write_cache(cache, _rows(480), source="free_scrape")

    def _boom():
        raise RuntimeError("wikipedia unreachable")

    uni = SP500Universe(_Client403(), cache_path=cache, fetcher=_boom)
    assert len(uni.get_symbols()) == 480
    assert uni.last_resolution.source == "cache"
    assert uni.last_resolution.degraded is True


def test_total_source_loss_raises_rather_than_returning_empty(tmp_path):
    def _boom():
        raise RuntimeError("wikipedia unreachable")

    uni = SP500Universe(
        _Client403(), cache_path=tmp_path / "missing.json", fetcher=_boom
    )
    with pytest.raises(ConstituentSourceError):
        uni.get_symbols()


def test_default_construction_keeps_the_legacy_one_arg_signature():
    """main.py:887 calls SP500Universe(fmp); that must keep working."""
    uni = SP500Universe(_ClientOK())
    assert uni.last_resolution is None
