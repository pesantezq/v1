from __future__ import annotations
import json, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestDataBudgetConfig(unittest.TestCase):
    def test_config_has_data_budget_block(self):
        cfg = json.loads(Path("config.json").read_text())
        db = cfg.get("data_budget")
        self.assertIsInstance(db, dict)
        self.assertTrue(db.get("enabled"))
        self.assertEqual(db.get("monthly_bandwidth_gb"), 20)
        self.assertEqual(db.get("rate_per_min"), 240)
        self.assertEqual(db.get("burst"), 300)
        self.assertIn("daily", db.get("run_modes", {}))


if __name__ == "__main__":
    unittest.main()
