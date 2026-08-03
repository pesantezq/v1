# tests/test_sp500_constituents.py
"""S&P 500 constituent resolution after FMP retired the v3 legacy API.

Context (2026-08-03): ``/api/v3/sp500_constituent`` returns HTTP 403 for any key
without a pre-2025-08-31 subscription, and ``/stable/sp500-constituent`` returns
402 Restricted on this plan. ``full_scan()`` — the only scanner path that can ADD
a symbol — therefore raised on its first line, which is why the watchlist could
only ever shrink (3 symbols on 2026-08-03).

These tests pin the replacement provider chain and, critically, that it FAILS
CLOSED rather than handing the scanner a plausible-looking short list. That is
the same defect class the ratchet itself belonged to: a guard that tests
availability instead of sufficiency.
"""
import json

import pytest

from universe import sp500_constituents as sc


# --------------------------------------------------------------------------
# A trimmed copy of the real Wikipedia markup (parsoid output, 2026-08-03),
# including the BRK.B dotted-ticker case that FMP spells BRK-B.
# --------------------------------------------------------------------------
_HTML = """
<table id="constituents">
<tbody><tr>
<th>Symbol</th><th>Security</th><th>GICS Sector</th><th>GICS Sub-Industry</th>
</tr>
<tr>
<td><a href="https://www.nyse.com/quote/XNYS:MMM">MMM</a></td>
<td><a href="/wiki/3M" title="3M">3M</a></td><td>Industrials</td>
<td>Industrial Conglomerates</td><td>Saint Paul, Minnesota</td>
</tr>
<tr>
<td><a href="https://www.nyse.com/quote/XNYS:BRK.B">BRK.B</a></td>
<td><a href="/wiki/Berkshire_Hathaway">Berkshire Hathaway</a></td>
<td>Financials</td><td>Multi-Sector Holdings</td><td>Omaha, Nebraska</td>
</tr>
<tr>
<td><a href="https://www.nasdaq.com/market-activity/stocks/nvda">NVDA</a></td>
<td><a href="/wiki/Nvidia">Nvidia</a></td><td>Information Technology</td>
<td>Semiconductors</td><td>Santa Clara, California</td>
</tr>
</tbody></table>
<table id="changes"><tbody><tr><td>ignore me</td></tr></tbody></table>
"""


def _rows(n, prefix="SYM"):
    """Build n synthetic constituent rows (for plausibility-floor tests)."""
    return [
        {"symbol": f"{prefix}{i}", "name": f"Co {i}", "sector": "Industrials",
         "subSector": "Widgets"}
        for i in range(n)
    ]


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

def test_parses_symbol_name_and_sector_from_constituents_table():
    rows = sc.parse_constituents_html(_HTML)
    assert [r["symbol"] for r in rows] == ["MMM", "BRK-B", "NVDA"]
    assert rows[0]["name"] == "3M"
    assert rows[0]["sector"] == "Industrials"
    assert rows[0]["subSector"] == "Industrial Conglomerates"


def test_dotted_tickers_are_normalized_to_fmp_dash_form():
    """Wikipedia writes BRK.B; every FMP endpoint expects BRK-B. A symbol that
    round-trips in the wrong dialect silently fails every downstream quote."""
    rows = sc.parse_constituents_html(_HTML)
    assert "BRK-B" in {r["symbol"] for r in rows}
    assert "BRK.B" not in {r["symbol"] for r in rows}


def test_only_the_constituents_table_is_read():
    """The page also carries a 'changes' table; scraping it would inject
    non-constituents into the universe."""
    rows = sc.parse_constituents_html(_HTML)
    assert all(r["symbol"] != "IGNORE ME" for r in rows)
    assert len(rows) == 3


def test_unparseable_html_yields_no_rows_rather_than_garbage():
    assert sc.parse_constituents_html("<html><body>nope</body></html>") == []
    assert sc.parse_constituents_html("") == []


# --------------------------------------------------------------------------
# Plausibility floor — sufficiency, not mere availability
# --------------------------------------------------------------------------

def test_short_scrape_is_rejected_not_returned():
    """A layout change that leaves 3 parseable rows must NOT become the
    universe. This is the exact failure the ratchet taught: the old weekly
    guard asked 'is it fallback?' when it should have asked 'is it enough?'"""
    with pytest.raises(sc.ConstituentSourceError) as exc:
        sc.validate_constituents(_rows(50))
    assert "50" in str(exc.value)


def test_plausible_list_passes_validation():
    rows = _rows(500)
    assert sc.validate_constituents(rows) == rows


def test_duplicate_symbols_are_collapsed():
    dupes = _rows(500) + _rows(5)
    assert len(sc.validate_constituents(dupes)) == 500


# --------------------------------------------------------------------------
# Provider chain
# --------------------------------------------------------------------------

class _FakeClient:
    def __init__(self, rows=None, exc=None):
        self._rows, self._exc, self.calls = rows, exc, 0

    def get_sp500_constituents(self, ttl_days=7):
        self.calls += 1
        if self._exc:
            raise self._exc
        return self._rows


def test_fmp_is_preferred_when_it_returns_a_plausible_list(tmp_path):
    client = _FakeClient(rows=_rows(503))
    res = sc.resolve_constituents(
        client=client, cache_path=tmp_path / "c.json", fetcher=lambda: _rows(500)
    )
    assert res.source == "fmp"
    assert res.degraded is False
    assert len(res.rows) == 503
    assert client.calls == 1


def test_falls_back_to_free_source_when_fmp_403s(tmp_path):
    """The live condition on 2026-08-03."""
    client = _FakeClient(exc=RuntimeError("FMP authentication failed (HTTP 403)"))
    res = sc.resolve_constituents(
        client=client, cache_path=tmp_path / "c.json", fetcher=lambda: _rows(500)
    )
    assert res.source == "free_scrape"
    assert res.degraded is False
    assert len(res.rows) == 500


def test_fmp_short_list_does_not_win_over_free_source(tmp_path):
    """A 3-row FMP response is the shape of the bug, not a usable universe."""
    client = _FakeClient(rows=_rows(3))
    res = sc.resolve_constituents(
        client=client, cache_path=tmp_path / "c.json", fetcher=lambda: _rows(500)
    )
    assert res.source == "free_scrape"


def test_successful_free_fetch_is_cached(tmp_path):
    cache = tmp_path / "c.json"
    sc.resolve_constituents(
        client=_FakeClient(exc=RuntimeError("403")), cache_path=cache,
        fetcher=lambda: _rows(500),
    )
    assert cache.exists()
    payload = json.loads(cache.read_text())
    assert len(payload["constituents"]) == 500
    assert payload["source"] == "free_scrape"
    assert payload.get("fetched_at")


def test_falls_back_to_last_good_cache_when_both_live_sources_fail(tmp_path):
    cache = tmp_path / "c.json"
    sc.write_cache(cache, _rows(499), source="free_scrape")

    def _boom():
        raise RuntimeError("wikipedia unreachable")

    res = sc.resolve_constituents(
        client=_FakeClient(exc=RuntimeError("403")), cache_path=cache, fetcher=_boom
    )
    assert res.source == "cache"
    assert res.degraded is True          # serving stale data is a degraded read
    assert len(res.rows) == 499


def test_fails_closed_when_every_source_is_unavailable(tmp_path):
    """No silent empty universe, and no tiny one either: the caller must be
    able to tell 'I could not determine the universe' from 'the universe is
    small'. Returning [] here is what let a 3-symbol scanner read healthy."""
    def _boom():
        raise RuntimeError("wikipedia unreachable")

    with pytest.raises(sc.ConstituentSourceError):
        sc.resolve_constituents(
            client=_FakeClient(exc=RuntimeError("403")),
            cache_path=tmp_path / "missing.json",
            fetcher=_boom,
        )


def test_corrupt_cache_is_not_treated_as_a_usable_source(tmp_path):
    cache = tmp_path / "c.json"
    cache.write_text("{not json")

    def _boom():
        raise RuntimeError("wikipedia unreachable")

    with pytest.raises(sc.ConstituentSourceError):
        sc.resolve_constituents(
            client=_FakeClient(exc=RuntimeError("403")), cache_path=cache,
            fetcher=_boom,
        )


def test_absent_client_still_resolves_via_free_source(tmp_path):
    res = sc.resolve_constituents(
        client=None, cache_path=tmp_path / "c.json", fetcher=lambda: _rows(500)
    )
    assert res.source == "free_scrape"
