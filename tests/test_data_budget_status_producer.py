from __future__ import annotations
import json, sys, unittest, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from portfolio_automation.data_budget.status_producer import build_status, write_status_artifacts
from portfolio_automation.data_budget.usage_ledger import UsageLedger


class TestStatusProducer(unittest.TestCase):
    def _seed(self, td):
        lg = UsageLedger(Path(td) / "fmp_budget.db")
        lg.record(run_mode="daily", endpoint="quote", symbols=["AAPL"],
                  cache_hit=False, bytes_=1000, skipped_reason=None, ts="2026-06-15T09:00:00+00:00")
        lg.record(run_mode="daily", endpoint="quote", symbols=["MSFT"],
                  cache_hit=True, bytes_=0, skipped_reason=None, ts="2026-06-15T09:00:01+00:00")
        lg.record(run_mode="discovery", endpoint="quote", symbols=["X"],
                  cache_hit=False, bytes_=0, skipped_reason="bandwidth_guard", ts="2026-06-15T09:00:02+00:00")
        return lg

    def test_build_status_observe_only_and_fields(self):
        with tempfile.TemporaryDirectory() as td:
            lg = self._seed(td)
            usage, cache, budget = build_status(
                ledger=lg, cache_dir=Path(td) / "cache",
                portfolio_symbols=[], month="2026-06",
                monthly_bandwidth_gb=20, run_modes={})
            self.assertTrue(usage["observe_only"])
            self.assertTrue(budget["observe_only"])
            self.assertEqual(budget["monthly_bandwidth_gb_cap"], 20)
            self.assertTrue(budget["discovery_skipped_due_to_budget"])
            self.assertAlmostEqual(cache["cache_hit_rate"], 1 / 3, places=2)

    def test_write_creates_three_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            lg = self._seed(td)
            out = Path(td) / "outputs"
            write_status_artifacts(ledger=lg, cache_dir=Path(td) / "cache",
                                    portfolio_symbols=[], month="2026-06",
                                    monthly_bandwidth_gb=20, run_modes={}, base_dir=out)
            for name in ("fmp_usage_status.json", "fmp_cache_status.json", "data_budget_status.json"):
                p = out / "latest" / name
                self.assertTrue(p.exists(), name)
                self.assertTrue(json.loads(p.read_text())["observe_only"])


if __name__ == "__main__":
    unittest.main()
