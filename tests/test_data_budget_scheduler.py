from __future__ import annotations
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from portfolio_automation.data_budget.scheduler import RunModeScheduler, DEFAULT_RUN_MODES


class TestScheduler(unittest.TestCase):
    def test_default_priorities(self):
        s = RunModeScheduler(DEFAULT_RUN_MODES)
        self.assertEqual(s.priority("daily"), "high")
        self.assertEqual(s.priority("discovery"), "low")

    def test_historical_replay_is_cache_only(self):
        s = RunModeScheduler(DEFAULT_RUN_MODES)
        self.assertEqual(s.call_budget("historical_replay"), 0)

    def test_low_priority_skipped_when_bandwidth_exhausted(self):
        s = RunModeScheduler(DEFAULT_RUN_MODES)
        self.assertTrue(s.should_skip("discovery", bandwidth_exhausted=True))
        self.assertFalse(s.should_skip("daily", bandwidth_exhausted=True))

    def test_run_budget_exceeded(self):
        s = RunModeScheduler(DEFAULT_RUN_MODES)
        b = s.call_budget("gui_refresh")
        self.assertTrue(s.over_run_budget("gui_refresh", calls_so_far=b))
        self.assertFalse(s.over_run_budget("gui_refresh", calls_so_far=b - 1))

    def test_uncapped_daily_when_budget_zero(self):
        s = RunModeScheduler({"daily": {"call_budget": 0, "priority": "high"}})
        self.assertFalse(s.over_run_budget("daily", calls_so_far=10_000))


if __name__ == "__main__":
    unittest.main()
