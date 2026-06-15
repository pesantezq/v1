from __future__ import annotations
import json, sys, unittest, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gui_v2.data.dash_data_budget import data_budget_view


class TestDataBudgetView(unittest.TestCase):
    def test_view_reads_three_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            latest = Path(td) / "outputs" / "latest"
            latest.mkdir(parents=True)
            (latest / "fmp_usage_status.json").write_text(json.dumps(
                {"observe_only": True, "calls_by_run_mode": {"daily": 12}}))
            (latest / "fmp_cache_status.json").write_text(json.dumps(
                {"observe_only": True, "cache_hit_rate": 0.82, "portfolio_fresh": {"AAPL": True}}))
            (latest / "data_budget_status.json").write_text(json.dumps(
                {"observe_only": True, "overall_status": "ok",
                 "monthly_bandwidth_pct": 0.10, "discovery_skipped_due_to_budget": False}))
            v = data_budget_view(Path(td))
            self.assertEqual(v["calls_this_run"], 12)
            self.assertEqual(v["cache_hit_rate_pct"], 82.0)
            self.assertEqual(v["bandwidth_pct"], 10.0)
            self.assertFalse(v["discovery_skipped"])
            self.assertEqual(v["portfolio_fresh"], {"AAPL": True})

    def test_view_degrades_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            v = data_budget_view(Path(td))
            self.assertFalse(v["available"])


if __name__ == "__main__":
    unittest.main()
