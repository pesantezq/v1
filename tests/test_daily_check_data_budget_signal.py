from __future__ import annotations
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from portfolio_automation.data_budget.health import data_budget_health


class TestDataBudgetHealth(unittest.TestCase):
    def test_ok_when_under_cap_and_no_skips(self):
        h = data_budget_health({"overall_status": "ok", "monthly_bandwidth_pct": 0.1,
                                "discovery_skipped_due_to_budget": False})
        self.assertEqual(h["status"], "green")

    def test_amber_when_near_cap(self):
        h = data_budget_health({"overall_status": "near_cap", "monthly_bandwidth_pct": 0.85,
                                "discovery_skipped_due_to_budget": False})
        self.assertEqual(h["status"], "amber")

    def test_amber_when_discovery_skipped(self):
        h = data_budget_health({"overall_status": "constrained", "monthly_bandwidth_pct": 1.02,
                                "discovery_skipped_due_to_budget": True})
        self.assertEqual(h["status"], "amber")
        self.assertIn("discovery", h["reason"])

    def test_missing_artifact_is_neutral(self):
        self.assertEqual(data_budget_health(None)["status"], "green")


if __name__ == "__main__":
    unittest.main()
