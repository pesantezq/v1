from __future__ import annotations
import json, sys, unittest, tempfile, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from portfolio_automation.data_budget.run_status import run_data_budget_status


class TestRunStatus(unittest.TestCase):
    def test_writes_artifacts_in_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config.json").write_text(json.dumps({
                "data_budget": {"enabled": True, "monthly_bandwidth_gb": 20,
                                "rate_per_min": 240, "burst": 300, "run_modes": {}},
                "portfolio": {"holdings": [{"symbol": "AAPL"}]}}))
            prev = os.getcwd()
            try:
                os.chdir(root)
                run_data_budget_status(root=root)
            finally:
                os.chdir(prev)
            self.assertTrue((root / "outputs" / "latest" / "data_budget_status.json").exists())


if __name__ == "__main__":
    unittest.main()
