from __future__ import annotations
import sys, unittest, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from portfolio_automation.data_budget.usage_ledger import UsageLedger


class TestUsageLedger(unittest.TestCase):
    def _ledger(self, td):
        return UsageLedger(Path(td) / "fmp_budget.db")

    def test_record_and_count(self):
        with tempfile.TemporaryDirectory() as td:
            lg = self._ledger(td)
            lg.record(run_mode="daily", endpoint="quote", symbols=["AAPL"],
                      cache_hit=False, bytes_=100, skipped_reason=None,
                      ts="2026-06-15T09:00:00+00:00")
            lg.record(run_mode="daily", endpoint="quote", symbols=["MSFT"],
                      cache_hit=True, bytes_=0, skipped_reason=None,
                      ts="2026-06-15T09:00:01+00:00")
            self.assertEqual(lg.calls_in_run(run_mode="daily", since="2026-06-15T00:00:00+00:00"), 1)

    def test_monthly_bytes_sums_only_month(self):
        with tempfile.TemporaryDirectory() as td:
            lg = self._ledger(td)
            lg.record(run_mode="daily", endpoint="eod", symbols=["AAPL"],
                      cache_hit=False, bytes_=500, skipped_reason=None,
                      ts="2026-06-15T09:00:00+00:00")
            lg.record(run_mode="daily", endpoint="eod", symbols=["AAPL"],
                      cache_hit=False, bytes_=999, skipped_reason=None,
                      ts="2026-05-30T09:00:00+00:00")
            self.assertEqual(lg.monthly_bytes(month="2026-06"), 500)

    def test_cache_hit_rate(self):
        with tempfile.TemporaryDirectory() as td:
            lg = self._ledger(td)
            for hit in (True, True, False, True):
                lg.record(run_mode="gui_refresh", endpoint="quote-short",
                          symbols=["AAPL"], cache_hit=hit, bytes_=0 if hit else 10,
                          skipped_reason=None, ts="2026-06-15T09:00:00+00:00")
            self.assertAlmostEqual(lg.cache_hit_rate(month="2026-06"), 0.75)


if __name__ == "__main__":
    unittest.main()
