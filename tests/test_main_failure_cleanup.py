import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import main


class _FakeStore:
    def __init__(self):
        self.failed = []

    def get_last_successful_run(self, _mode):
        return None

    def is_completed(self, _run_id):
        return False

    def is_stale_running(self, _run_id, stale_minutes=30):
        return False

    def start_run(self, _run_id, _mode):
        return True

    def fail_run(self, run_id):
        self.failed.append(run_id)


class TestMainFailureCleanup(unittest.TestCase):
    def test_main_marks_started_run_failed_when_update_raises(self):
        fake_store = _FakeStore()
        fake_args = SimpleNamespace(
            config="config.json",
            env=None,
            profile=None,
            debug=False,
            dry_run=False,
            force_email=False,
            skip_email=True,
            run_mode="monthly",
            llm_provider=None,
        )
        fake_config = SimpleNamespace(
            investor=SimpleNamespace(name="Test Investor"),
            holdings=[object()],
            rebalance_rules=SimpleNamespace(band_threshold=0.12),
        )

        with patch("main.parse_arguments", return_value=fake_args), patch(
            "main.setup_logging", return_value=SimpleNamespace(
                info=lambda *args, **kwargs: None,
                warning=lambda *args, **kwargs: None,
                error=lambda *args, **kwargs: None,
                debug=lambda *args, **kwargs: None,
            )
        ), patch("main.acquire_run_lock", return_value=True), patch(
            "main.release_run_lock"
        ), patch("main.load_env"), patch(
            "main.load_config", return_value=fake_config
        ), patch(
            "main.PortfolioStateStore", return_value=fake_store
        ), patch(
            "main.run_portfolio_update", side_effect=RuntimeError("boom")
        ):
            exit_code = main.main()

        self.assertEqual(exit_code, 1)
        self.assertEqual(fake_store.failed, [f"{main.date.today().isoformat()}_monthly"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
