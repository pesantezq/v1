"""Phase 2A tests: crowd-intelligence adapters, normalization, builder, artifacts.

Observe-only context. No GUI/advisory/decision/allocation/trading behavior here.
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_automation.crowd_intelligence import normalization as norm
from portfolio_automation.crowd_intelligence import crowd_signal_builder as builder
from portfolio_automation.crowd_intelligence import artifact_writer
from portfolio_automation.crowd_intelligence.capability_store import CapabilityStore
from portfolio_automation.crowd_intelligence.adapters import (
    news_adapter, analyst_adapter, insider_adapter, congress_adapter, attention_adapter,
)

_NOW = _dt.datetime(2026, 6, 15, 12, 0, 0)
_RECENT = "2026-06-15 09:00:00"


class FakeClient:
    """Stands in for the governed client — the ONLY network surface adapters may
    use is .get_json. No raw HTTP exists here, so a passing test proves adapters
    never bypass the governed path."""
    def __init__(self, by_path: dict):
        self.by_path = by_path
        self.calls: list[str] = []

    def get_json(self, path, params=None, *, ttl_seconds=3600, base_url=None):
        self.calls.append(path)
        return self.by_path.get(path, [])


def _full_client():
    return FakeClient({
        "/stable/news/stock": [{"title": "AAPL beats", "publishedDate": _RECENT, "site": "x", "symbol": "AAPL"}],
        "/stable/news/stock-latest": [{"title": "AAPL surges", "symbol": "AAPL", "publishedDate": _RECENT}],
        "/stable/fmp-articles": [], "/stable/news/general-latest": [],
        "/stable/news/crypto-latest": [], "/stable/news/forex-latest": [],
        "/stable/grades-consensus": [{"strongBuy": 5, "buy": 10, "hold": 3, "sell": 1, "strongSell": 0}],
        "/stable/grades": [{"action": "upgrade", "date": "2026-06-10", "newGrade": "Buy"}],
        "/stable/ratings-snapshot": [{"rating": "A-", "symbol": "AAPL"}],
        "/stable/historical-ratings": [{"rating": "A-", "date": "2026-06-01"}],
        "/stable/insider-trading/search": [
            {"acquisitionOrDisposition": "A", "filingDate": _RECENT, "securitiesTransacted": 100},
            {"acquisitionOrDisposition": "D", "filingDate": _RECENT, "securitiesTransacted": 40}],
        "/stable/insider-trading/statistics": [{"buySellRatio": 2.0}],
        "/stable/senate-trades": [{"type": "Purchase", "amount": "$1001-", "office": "Sen X"}],
        "/stable/house-trades": [{"type": "Sale", "amount": "$1001-", "office": "Rep Y"}],
        "/stable/biggest-gainers": [{"symbol": "AAPL", "changesPercentage": 5.2}],
        "/stable/biggest-losers": [],
        "/stable/most-actives": [{"symbol": "AAPL"}],
        "/stable/sector-performance-snapshot": [{"sector": "Technology", "changesPercentage": 1.2}],
        "/stable/industry-performance-snapshot": [],
    })


_ALL_USABLE = {
    "news": {"stock_news_search", "stock_news_latest", "fmp_articles", "general_news", "crypto_news", "forex_news"},
    "analyst": {"ratings_snapshot", "ratings_historical", "stock_grades", "grades_consensus"},
    "insider": {"latest_insider_trading", "search_insider_trades", "insider_trade_statistics"},
    "congress": {"senate_trading", "house_trading"},
    "attention": {"biggest_gainers", "biggest_losers", "most_active",
                  "sector_performance_snapshot", "industry_performance_snapshot"},
}


def _shared(client):
    return {
        "stock_news_latest": client.by_path["/stable/news/stock-latest"],
        "fmp_articles": [], "general_news": [], "crypto_news": [], "forex_news": [],
        "biggest_gainers": client.by_path["/stable/biggest-gainers"],
        "biggest_losers": client.by_path["/stable/biggest-losers"],
        "most_active": client.by_path["/stable/most-actives"],
        "sector_performance_snapshot": client.by_path["/stable/sector-performance-snapshot"],
        "industry_performance_snapshot": [],
    }


class TestNormalization(unittest.TestCase):
    def test_clamp(self):
        self.assertEqual(norm.clamp(5), 1.0)
        self.assertEqual(norm.clamp(-5), -1.0)
        self.assertEqual(norm.clamp(float("nan")), 0.0)

    def test_composite_clamped_and_social_zero_weight(self):
        c = norm.composite({"news": 1, "analyst": 1, "insider": 1, "congress": 1,
                            "attention": 1, "social_sentiment": 1})
        self.assertLessEqual(c, 1.0)
        # social has weight 0 -> changing it doesn't move composite
        a = norm.composite({"analyst": 0.5})
        b = norm.composite({"analyst": 0.5, "social_sentiment": 1.0})
        self.assertEqual(a, b)

    def test_winsorize_caps_outlier(self):
        out = norm.winsorize([1, 1, 1, 1, 100], p=0.5)
        self.assertLess(max(out), 100)


class TestAdapters(unittest.TestCase):
    def test_each_returns_neutral_empty_with_no_usable_endpoints(self):
        c = _full_client()
        for mod, cat in ((news_adapter, "news"), (analyst_adapter, "analyst"),
                         (insider_adapter, "insider"), (congress_adapter, "congress"),
                         (attention_adapter, "attention")):
            r = mod.run("AAPL", client=c, usable=set(), shared={}, now=_NOW)
            self.assertEqual(r.score, 0.0, cat)
            self.assertFalse(r.has_data, cat)
            self.assertEqual(r.enabled_endpoints, [], cat)

    def test_adapters_skip_disabled_endpoints(self):
        c = _full_client()
        # analyst with only ratings_snapshot usable -> grades_consensus disabled
        r = analyst_adapter.run("AAPL", client=c, usable={"ratings_snapshot"}, shared={}, now=_NOW)
        self.assertIn("grades_consensus", r.disabled_endpoints)
        self.assertNotIn("/stable/grades-consensus", c.calls)

    def test_analyst_positive_consensus(self):
        c = _full_client()
        r = analyst_adapter.run("AAPL", client=c, usable=_ALL_USABLE["analyst"], shared={}, now=_NOW)
        self.assertGreater(r.score, 0.0)
        self.assertTrue(r.has_data)

    def test_insider_net_pressure_and_winsorized(self):
        c = _full_client()
        r = insider_adapter.run("AAPL", client=c, usable=_ALL_USABLE["insider"], shared={}, now=_NOW)
        self.assertTrue(-1.0 <= r.score <= 1.0)

    def test_congress_clamped_low(self):
        c = _full_client()
        r = congress_adapter.run("AAPL", client=c, usable=_ALL_USABLE["congress"], shared={}, now=_NOW)
        self.assertLessEqual(abs(r.score), 0.5)  # dampened + capped

    def test_news_score_neutral_even_with_articles(self):
        c = _full_client()
        r = news_adapter.run("AAPL", client=c, usable=_ALL_USABLE["news"],
                             shared=_shared(c), now=_NOW)
        self.assertEqual(r.score, 0.0)   # no sentiment field -> neutral direction
        self.assertTrue(r.has_data)      # but velocity recorded
        self.assertTrue(any("articles" in x for x in r.reasons))

    def test_attention_gainer_positive(self):
        c = _full_client()
        r = attention_adapter.run("AAPL", client=c, usable=_ALL_USABLE["attention"],
                                  shared=_shared(c), now=_NOW)
        self.assertGreater(r.score, 0.0)


class TestBuilder(unittest.TestCase):
    def _caps_all_available(self):
        ids = [e for s in _ALL_USABLE.values() for e in s]
        return {"records": [{"endpoint_id": e, "status": "AVAILABLE"} for e in ids]}

    def test_social_disabled_and_neutral(self):
        c = _full_client()
        signals, events, status = builder.build_signals(
            ["AAPL"], client=c, capabilities=self._caps_all_available())
        s = signals[0]
        self.assertEqual(s.category_scores["social_sentiment"], 0.0)
        self.assertIn("social_sentiment", status["disabled_categories"])

    def test_congress_contribution_is_low_weight(self):
        # worst case congress score (+0.5) * weight 0.10 = 0.05 ceiling
        self.assertLessEqual(abs(norm.WEIGHTS["congress"] * 0.5), 0.05)

    def test_plan_locked_endpoint_excluded(self):
        c = _full_client()
        caps = {"records": [{"endpoint_id": "grades_consensus", "status": "PLAN_LOCKED"},
                            {"endpoint_id": "stock_grades", "status": "AVAILABLE"}]}
        signals, _, _ = builder.build_signals(["AAPL"], client=c, capabilities=caps)
        self.assertIn("grades_consensus", signals[0].disabled_sources)
        self.assertNotIn("/stable/grades-consensus", c.calls)

    def test_composite_in_range_and_persists(self):
        with tempfile.TemporaryDirectory() as td:
            c = _full_client()
            signals, events, status = builder.build_signals(
                ["AAPL"], client=c, capabilities=self._caps_all_available())
            self.assertTrue(-1.0 <= signals[0].composite_crowd_score <= 1.0)
            self.assertTrue(0.0 <= signals[0].confidence <= 1.0)
            store = CapabilityStore(Path(td) / "crowd_intelligence.db")
            store.record_events(events)
            store.upsert_daily([{
                "symbol": "AAPL", "signal_date": "2026-06-15",
                "composite_crowd_score": signals[0].composite_crowd_score,
                "confidence": signals[0].confidence, "created_at": "now",
                "news_score": 0, "analyst_score": 0, "insider_score": 0,
                "congress_score": 0, "attention_score": 0, "social_sentiment_score": 0,
                "enabled_sources_json": "[]", "disabled_sources_json": "[]", "explanation_json": "{}"}])
            self.assertGreaterEqual(store.raw_event_count(), 1)
            self.assertEqual(len(store.daily_rows()), 1)


class TestArtifactsAndGuardrails(unittest.TestCase):
    def test_artifacts_schema_and_decision_plan_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "outputs" / "latest").mkdir(parents=True)
            # a pre-existing decision artifact that MUST NOT change
            dp = root / "outputs" / "latest" / "decision_plan.json"
            dp.write_text('{"decisions": [{"symbol": "AAPL", "action": "HOLD"}]}')
            before = dp.read_bytes()

            c = _full_client()
            signals, events, status = builder.build_signals(
                ["AAPL"], client=c,
                capabilities={"records": [{"endpoint_id": "grades_consensus", "status": "AVAILABLE"}]})
            artifact_writer.write_artifacts(signals, status, base_dir=root / "outputs")

            j = json.loads((root / "outputs" / "latest" / "crowd_intelligence.json").read_text())
            self.assertTrue(j["observe_only"])
            sym = j["symbols"][0]
            for k in ("symbol", "composite_crowd_score", "confidence", "category_scores",
                      "enabled_sources", "disabled_sources", "top_reasons", "warnings",
                      "data_freshness", "source_records_count"):
                self.assertIn(k, sym)
            self.assertTrue((root / "outputs" / "latest" / "crowd_intelligence.md").exists())
            self.assertTrue((root / "outputs" / "latest" / "crowd_intelligence_status.json").exists())
            # decision_plan.json byte-identical — crowd layer never touches it
            self.assertEqual(dp.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
