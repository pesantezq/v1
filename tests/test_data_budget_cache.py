from __future__ import annotations
import sys, unittest, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from portfolio_automation.data_budget.cache import SymbolDataPolicy, cache_stats
from fmp_client import _DiskCache


class TestSymbolPolicy(unittest.TestCase):
    def test_default_ttl_and_priority(self):
        with tempfile.TemporaryDirectory() as td:
            p = SymbolDataPolicy(Path(td) / "fmp_budget.db")
            self.assertEqual(p.ttl_for("AAPL", default=3600), 3600)
            self.assertEqual(p.priority_for("AAPL", default="medium"), "medium")

    def test_set_and_read_policy(self):
        with tempfile.TemporaryDirectory() as td:
            p = SymbolDataPolicy(Path(td) / "fmp_budget.db")
            p.set_policy("AAPL", ttl_seconds=7200, priority="high")
            self.assertEqual(p.ttl_for("AAPL", default=3600), 7200)
            self.assertEqual(p.priority_for("AAPL", default="medium"), "high")


class TestCacheStats(unittest.TestCase):
    def test_reports_file_count_and_freshness(self):
        with tempfile.TemporaryDirectory() as td:
            dc = _DiskCache(Path(td))
            dc.set("quote_AAPL", {"price": 1})
            stats = cache_stats(Path(td), fresh_keys=["quote_AAPL"], ttl_seconds=3600)
            self.assertEqual(stats["file_count"], 1)
            self.assertEqual(stats["fresh"]["quote_AAPL"], True)


if __name__ == "__main__":
    unittest.main()
