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
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_automation.news.run_news_intelligence import (
    _load_fmp_budget,
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


class TestFmpBudgetPropagation(unittest.TestCase):
    """Regression: a config fmp_daily_calls_budget of 0 means 'no daily cap'
    (FMPClient.would_exceed treats budget <= 0 as uncapped). The runner must
    propagate that 0 verbatim, NOT coalesce it to None and fall back to the
    hardcoded 230-call default — which starved news fetching to empty after the
    2026-06-12 config change to budget=0."""

    def _run_in_dir(self, cfg: dict | None):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            if cfg is not None:
                (root / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
            prev = os.getcwd()
            try:
                os.chdir(root)
                return _load_fmp_budget()
            finally:
                os.chdir(prev)

    def test_zero_budget_preserved_as_uncapped(self):
        # The bug: this returned None (→ FMPClient default 230). Must return 0.
        self.assertEqual(self._run_in_dir({"api_limits": {"fmp_daily_calls_budget": 0}}), 0)

    def test_positive_budget_read_verbatim(self):
        self.assertEqual(self._run_in_dir({"api_limits": {"fmp_daily_calls_budget": 500}}), 500)

    def test_absent_key_returns_none(self):
        # Legacy fall-back: no key → None → caller uses FMPClient's own default.
        self.assertIsNone(self._run_in_dir({"api_limits": {}}))

    def test_missing_config_returns_none(self):
        self.assertIsNone(self._run_in_dir(None))

    def test_fetch_passes_zero_budget_to_fmpclient(self):
        """End-to-end: with config budget=0, fetch_news_articles must construct
        FMPClient(daily_budget=0), not fall through to the bare default ctor."""
        import tempfile
        recorded = {}

        def _fake_ctor(*args, **kwargs):
            recorded["daily_budget"] = kwargs.get("daily_budget", "MISSING")
            client = MagicMock()
            client.get_stock_news.return_value = []
            return client

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config.json").write_text(
                json.dumps({"api_limits": {"fmp_daily_calls_budget": 0}}), encoding="utf-8"
            )
            prev = os.getcwd()
            try:
                os.chdir(root)
                with patch("fmp_client.FMPClient", side_effect=_fake_ctor):
                    fetch_news_articles(["AAPL"])
            finally:
                os.chdir(prev)

        self.assertEqual(recorded.get("daily_budget"), 0,
                         "explicit 0 budget must reach FMPClient, not be dropped to the default")


if __name__ == "__main__":
    unittest.main(verbosity=2)
