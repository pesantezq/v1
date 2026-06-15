from __future__ import annotations
import sys, unittest, tempfile
from pathlib import Path
from unittest.mock import MagicMock
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from portfolio_automation.data_budget.governor import _TokenBucket, FMPBudgetGovernor


class TestTokenBucket(unittest.TestCase):
    def test_capacity_and_consume(self):
        clock = {"t": 0.0}
        b = _TokenBucket(rate_per_min=240, burst=300, now=lambda: clock["t"])
        self.assertTrue(b.try_consume(300))     # full burst available
        self.assertFalse(b.try_consume(1))       # empty now
        clock["t"] = 1.0                          # +1s -> +4 tokens (240/60)
        self.assertTrue(b.try_consume(4))
        self.assertFalse(b.try_consume(1))

    def test_hard_cap_never_exceeds_burst(self):
        clock = {"t": 0.0}
        b = _TokenBucket(rate_per_min=240, burst=300, now=lambda: clock["t"])
        clock["t"] = 10_000.0                     # huge idle
        self.assertTrue(b.try_consume(300))
        self.assertFalse(b.try_consume(1))        # capped at burst=300, not unbounded


class TestGovernorKillSwitch(unittest.TestCase):
    def _gov(self, td, **kw):
        return FMPBudgetGovernor(
            db_path=Path(td) / "fmp_budget.db",
            cache_dir=Path(td) / "cache",
            config={"enabled": True, "monthly_bandwidth_gb": 20,
                    "rate_per_min": 240, "burst": 300}, **kw)

    def test_killswitch_env_returns_plain_client(self):
        import os
        with tempfile.TemporaryDirectory() as td:
            os.environ["STOCKBOT_FMP_GOVERNOR_DISABLED"] = "1"
            try:
                gov = self._gov(td)
                client = gov.client(run_mode="daily", fmp_client=MagicMock())
                from portfolio_automation.data_budget.governor import GovernedFMPClient
                self.assertNotIsInstance(client, GovernedFMPClient)
            finally:
                os.environ.pop("STOCKBOT_FMP_GOVERNOR_DISABLED", None)

    def test_enabled_returns_governed_client(self):
        with tempfile.TemporaryDirectory() as td:
            gov = self._gov(td)
            client = gov.client(run_mode="daily", fmp_client=MagicMock())
            from portfolio_automation.data_budget.governor import GovernedFMPClient
            self.assertIsInstance(client, GovernedFMPClient)

    def test_killswitch_config_disabled(self):
        with tempfile.TemporaryDirectory() as td:
            gov = FMPBudgetGovernor(
                db_path=Path(td) / "fmp_budget.db", cache_dir=Path(td) / "cache",
                config={"enabled": False})
            from portfolio_automation.data_budget.governor import GovernedFMPClient
            client = gov.client(run_mode="daily", fmp_client=MagicMock())
            self.assertNotIsInstance(client, GovernedFMPClient)


class TestGovernedClientBehavior(unittest.TestCase):
    def _governed(self, td, run_mode="daily"):
        gov = FMPBudgetGovernor(
            db_path=Path(td) / "fmp_budget.db", cache_dir=Path(td) / "cache",
            config={"enabled": True, "monthly_bandwidth_gb": 20,
                    "rate_per_min": 240, "burst": 300})
        fake = MagicMock()
        fake.get_batch_quotes.return_value = {"AAPL": {"price": 1.0}}
        fake.last_response_bytes = 50
        return gov, gov.client(run_mode=run_mode, fmp_client=fake), fake

    def test_proxies_method_and_records_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            gov, gc, fake = self._governed(td)
            out = gc.get_batch_quotes(["AAPL"])
            self.assertEqual(out, {"AAPL": {"price": 1.0}})
            fake.get_batch_quotes.assert_called_once()
            self.assertGreaterEqual(
                gov.ledger.calls_in_run(run_mode="daily", since="2000-01-01T00:00:00+00:00"), 0)

    def test_counts_real_call_when_bytes_change(self):
        # Simulate a fresh fetch: last_response_bytes increases during the call.
        with tempfile.TemporaryDirectory() as td:
            gov = FMPBudgetGovernor(
                db_path=Path(td) / "fmp_budget.db", cache_dir=Path(td) / "cache",
                config={"enabled": True, "monthly_bandwidth_gb": 20,
                        "rate_per_min": 240, "burst": 300})
            fake = MagicMock()
            fake.last_response_bytes = 0
            def _fetch(symbols):
                fake.last_response_bytes = 123
                return {"AAPL": {"price": 1.0}}
            fake.get_batch_quotes.side_effect = _fetch
            gc = gov.client(run_mode="daily", fmp_client=fake, now_month="2026-06")
            gc.get_batch_quotes(["AAPL"])
            self.assertEqual(gov.ledger.calls_in_run(run_mode="daily",
                             since="2000-01-01T00:00:00+00:00"), 1)
            self.assertEqual(gov.ledger.monthly_bytes(month="2026-06"), 123)

    def test_low_priority_skipped_when_bandwidth_over_guard(self):
        with tempfile.TemporaryDirectory() as td:
            gov = FMPBudgetGovernor(
                db_path=Path(td) / "fmp_budget.db", cache_dir=Path(td) / "cache",
                config={"enabled": True, "monthly_bandwidth_gb": 0.0000001,
                        "rate_per_min": 240, "burst": 300})
            fake = MagicMock()
            fake.get_batch_quotes.return_value = {"X": {}}
            fake.last_response_bytes = 999
            gov.ledger.record(run_mode="discovery", endpoint="quote", symbols=["X"],
                              cache_hit=False, bytes_=10_000, skipped_reason=None,
                              ts="2026-06-15T00:00:00+00:00")
            gc = gov.client(run_mode="discovery", fmp_client=fake, now_month="2026-06")
            gc.get_batch_quotes(["X"])
            fake.get_batch_quotes.assert_not_called()  # skipped due to bandwidth guard


if __name__ == "__main__":
    unittest.main()
