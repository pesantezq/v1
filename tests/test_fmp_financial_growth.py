"""
Tests for get_fundamentals_v3 sourcing revenueGrowth correctly on the
Starter plan.

Root cause fixed here (2026-05-28): stable/key-metrics does NOT carry
revenueGrowth, and the old v3/financial-growth fallback is 403 on Starter.
get_fundamentals_v3 must source revenueGrowth from stable/financial-growth
(verified 200 + has the field), so weekly_refresh stops zeroing the watchlist.

Fully offline — _raw_get is patched.
"""

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from fmp_client import FMPClient, FMP_STABLE_BASE_URL


def _make_client(*, budget: int = 230) -> FMPClient:
    return FMPClient(
        api_key="test_key",
        daily_budget=budget,
        cache_dir=Path(tempfile.mkdtemp()),
    )


# key-metrics WITHOUT revenueGrowth — mirrors the real Starter response.
_KEY_METRICS_NO_REVGROWTH = [{
    "symbol": "AAPL",
    "returnOnEquity": 0.50,
    "freeCashFlowYield": 0.03,
    # NOTE: no 'revenueGrowth' key — this is the real stable/key-metrics schema
}]

_FINANCIAL_GROWTH = [{"symbol": "AAPL", "revenueGrowth": 0.18}]


class TestRevenueGrowthSource:

    def test_revenue_growth_sourced_from_financial_growth(self):
        """key-metrics lacks revenueGrowth -> it must come from financial-growth."""
        client = _make_client()

        def mock_raw(endpoint, params, **kwargs):
            if endpoint == "key-metrics":
                return _KEY_METRICS_NO_REVGROWTH
            if endpoint == "financial-growth":
                return _FINANCIAL_GROWTH
            return []

        with patch.object(client, "_raw_get", side_effect=mock_raw):
            rows = client.get_fundamentals_v3(["AAPL"])

        assert rows[0]["revenueGrowth"] == 0.18

    def test_financial_growth_uses_stable_base_url(self):
        """The revenue-growth fetch must hit the stable base URL (not legacy v3)."""
        client = _make_client()
        seen = []

        def mock_raw(endpoint, params, **kwargs):
            seen.append({"endpoint": endpoint, "base_url": kwargs.get("base_url")})
            if endpoint == "key-metrics":
                return _KEY_METRICS_NO_REVGROWTH
            if endpoint == "financial-growth":
                return _FINANCIAL_GROWTH
            return []

        with patch.object(client, "_raw_get", side_effect=mock_raw):
            client.get_fundamentals_v3(["AAPL"])

        fg = [c for c in seen if c["endpoint"] == "financial-growth"]
        assert fg, "financial-growth endpoint was never called"
        assert fg[0]["base_url"] == FMP_STABLE_BASE_URL

    def test_other_metrics_still_sourced_from_key_metrics(self):
        """roe / fcf_yield must still come from key-metrics (no regression)."""
        client = _make_client()

        def mock_raw(endpoint, params, **kwargs):
            if endpoint == "key-metrics":
                return _KEY_METRICS_NO_REVGROWTH
            if endpoint == "financial-growth":
                return _FINANCIAL_GROWTH
            return []

        with patch.object(client, "_raw_get", side_effect=mock_raw):
            rows = client.get_fundamentals_v3(["AAPL"])

        assert rows[0]["roe"] == 0.50
        assert rows[0]["freeCashFlowYield"] == 0.03
