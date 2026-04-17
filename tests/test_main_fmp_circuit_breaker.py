import logging
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from main import run_portfolio_update
from utils import load_config


class _FakeStore:
    def get_cash_balance(self):
        return 898.11

    def upsert_peak(self, *_args, **_kwargs):
        return None

    def is_subsystem_disabled(self, subsystem):
        return subsystem == "fmp"

    def get_subsystem_health(self, subsystem):
        if subsystem != "fmp":
            return None
        return {
            "disabled_until": "2026-04-15T14:51:17",
            "last_error": "FMP authentication failed (HTTP 403)",
        }


class _FakeDrawdownTracker:
    def __init__(self, *_args, **_kwargs):
        pass

    def update(self, _value):
        return SimpleNamespace(
            all_time_high=7257.71,
            rolling_12m_high=7257.71,
            drawdown_from_12m_high=0.0,
        )

    def get_regime(self, _thresholds):
        return "normal"

    def should_suppress_sells(self):
        return False

    def format_summary(self, _thresholds):
        return "Drawdown: 0.0% from 12m-high | 0.0% from ATH $7,258 | Regime: normal"


class TestMainFmpCircuitBreaker(unittest.TestCase):
    def test_circuit_breaker_falls_back_without_crashing(self):
        config = load_config("config.json")
        config.theme_engine["enabled"] = False
        config.watchlist_scanner["enabled"] = False
        config.speculative_sleeve["enabled"] = False

        def _seed_prices(holdings, _market_client):
            for holding in holdings:
                holding.current_price = 100.0
                holding.market_value = holding.shares * 100.0
            return holdings, []

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("main.create_market_client", return_value=object()), patch(
                "main.update_holdings_with_prices", side_effect=_seed_prices
            ), patch(
                "main.load_retirement_data", return_value=SimpleNamespace(total_balance=0.0)
            ), patch(
                "main.DrawdownTracker", _FakeDrawdownTracker
            ), patch(
                "builtins.print"
            ):
                result = run_portfolio_update(
                    config=config,
                    dry_run=True,
                    skip_email=True,
                    run_mode="monthly",
                    output_dir=Path(tmpdir),
                    logger=logging.getLogger("test.main.circuit_breaker"),
                    store=_FakeStore(),
                )

        self.assertTrue(result["success"])
        self.assertEqual(result["scanner"]["meta"]["fmp_attempted"], False)
        self.assertTrue(result["scanner"]["meta"]["fallback_used"])
        self.assertEqual(result["scanner"]["meta"]["watchlist_source"], "fallback")
        self.assertIn("FMP circuit breaker open", result["scanner"]["meta"]["fmp_error"])
        self.assertEqual(len(result["scanner"]["candidates"]), 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
