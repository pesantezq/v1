"""
Tests for portfolio_automation/news/run_news_intelligence.py.

The runner is a thin wiring layer; correctness of the underlying producer is
already covered by tests/test_fmp_news_intelligence.py. Here we focus on:

  - ticker-universe collection (dedup across holdings/watchlist/discovery)
  - budget enforcement (max_universe cap, holdings never trimmed)
  - graceful degradation when FMP fails or returns nothing
  - end-to-end run() produces a valid summary dict and writes the artifact
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_automation.news.run_news_intelligence import (
    collect_ticker_universe,
    fetch_news_articles,
    run,
)


def _write_config(root: Path, holdings_symbols: list[str]) -> None:
    cfg = {
        "portfolio": {
            "holdings": [{"symbol": s, "shares": 1} for s in holdings_symbols],
        }
    }
    (root / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


def _write_watchlist(root: Path, tickers: list[str]) -> None:
    p = root / "outputs" / "latest" / "watchlist_signals.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"results": [{"ticker": t} for t in tickers]}))


def _write_decision_plan(root: Path, symbols: list[str]) -> None:
    p = root / "outputs" / "latest" / "decision_plan.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"decisions": [{"symbol": s} for s in symbols]}))


def _write_discovery(root: Path, tickers: list[str]) -> None:
    p = root / "outputs" / "sandbox" / "discovery" / "emerging_candidates.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"candidates": [{"ticker": t} for t in tickers]}))


class TestCollectTickerUniverse(unittest.TestCase):
    def test_dedups_across_sources(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, ["AAPL", "QQQ"])
            _write_watchlist(root, ["AAPL", "NVDA"])         # AAPL is a holding → dropped
            _write_decision_plan(root, ["QQQ", "MSFT"])       # QQQ is a holding → dropped
            _write_discovery(root, ["NVDA", "TSLA"])          # NVDA already in watchlist → dropped

            holdings, watchlist, discovery = collect_ticker_universe(root)

            self.assertEqual(holdings, ["AAPL", "QQQ"])
            self.assertEqual(watchlist, ["NVDA", "MSFT"])
            self.assertEqual(discovery, ["TSLA"])

    def test_holdings_never_trimmed_by_budget(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, ["A", "B", "C", "D"])
            _write_watchlist(root, ["E", "F", "G"])

            holdings, watchlist, discovery = collect_ticker_universe(root, max_total=5)
            self.assertEqual(holdings, ["A", "B", "C", "D"])
            self.assertEqual(watchlist, ["E"])  # budget leaves only 1 slot
            self.assertEqual(discovery, [])

    def test_missing_artifacts_safe(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # No config, no watchlist, no plan, no discovery
            holdings, watchlist, discovery = collect_ticker_universe(root)
            self.assertEqual(holdings, [])
            self.assertEqual(watchlist, [])
            self.assertEqual(discovery, [])


class TestFetchNewsArticles(unittest.TestCase):
    def test_empty_ticker_list_returns_empty(self):
        self.assertEqual(fetch_news_articles([]), [])

    def test_fmp_exception_returns_empty(self):
        bad_client = MagicMock()
        bad_client.get_stock_news.side_effect = RuntimeError("FMP down")
        self.assertEqual(fetch_news_articles(["AAPL"], fmp_client=bad_client), [])

    def test_non_list_response_returns_empty(self):
        weird_client = MagicMock()
        weird_client.get_stock_news.return_value = {"unexpected": "shape"}
        self.assertEqual(fetch_news_articles(["AAPL"], fmp_client=weird_client), [])

    def test_passes_through_normal_list(self):
        good_client = MagicMock()
        good_client.get_stock_news.return_value = [
            {"title": "headline", "ticker_sentiment": [{"ticker": "AAPL"}]}
        ]
        result = fetch_news_articles(["AAPL"], fmp_client=good_client)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "headline")


class TestRunEndToEnd(unittest.TestCase):
    def test_writes_artifact_with_zero_articles(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_config(root, ["AAPL"])
            empty_client = MagicMock()
            empty_client.get_stock_news.return_value = []

            summary = run(root=root, fmp_client=empty_client)

            self.assertEqual(summary.get("articles_fetched"), 0)
            self.assertEqual(summary.get("universe_size"), 1)
            artifact = root / "outputs" / "latest" / "news_intelligence.json"
            self.assertTrue(artifact.exists(), "producer should still write a valid empty artifact")
            payload = json.loads(artifact.read_text())
            self.assertIn("evidence_packets", payload)

    def test_run_returns_error_dict_on_unexpected_failure(self):
        # Force an error inside collect by passing a path with broken config.json
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config.json").write_text("{ not valid json")
            # Even on a broken config, collect_ticker_universe degrades safely
            # to empty lists → the runner still completes without raising.
            summary = run(root=root, fmp_client=MagicMock(get_stock_news=lambda *a, **k: []))
            self.assertIn("articles_fetched", summary)
            self.assertEqual(summary.get("articles_fetched"), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
