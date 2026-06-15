from __future__ import annotations
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from portfolio_automation.data_budget.request_manifest import plan_quote_request, plan_price_request


class TestManifest(unittest.TestCase):
    def test_multi_symbol_quote_uses_batch(self):
        plan = plan_quote_request(["AAPL", "MSFT", "QQQ"], run_mode="daily")
        self.assertEqual(plan["method"], "get_batch_quotes")

    def test_single_symbol_gui_uses_quote_short(self):
        plan = plan_quote_request(["AAPL"], run_mode="gui_refresh")
        self.assertEqual(plan["method"], "get_quote_short")

    def test_single_symbol_daily_uses_batch(self):
        # outside gui_refresh, a single symbol still goes through batch (cached)
        plan = plan_quote_request(["AAPL"], run_mode="daily")
        self.assertEqual(plan["method"], "get_batch_quotes")

    def test_daily_price_prefers_eod(self):
        plan = plan_price_request(["AAPL", "MSFT"], run_mode="daily")
        self.assertEqual(plan["method"], "get_historical_prices")
        self.assertEqual(plan["ttl_days"], 1)


if __name__ == "__main__":
    unittest.main()
