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


class _EmptyFallbackWatchlist:
    def __init__(self, *_args, **_kwargs):
        self.enabled = True

    def build(self, *args, **kwargs):
        return []

    def save(self, *_args, **_kwargs):
        return None


class TestMainDegradedMode(unittest.TestCase):
    def _seed_prices(self, holdings, _market_client):
        for holding in holdings:
            holding.current_price = 100.0
            holding.market_value = holding.shares * 100.0
        return holdings, []

    def _base_config(self):
        config = load_config("config.json")
        config.theme_engine["enabled"] = False
        config.watchlist_scanner["enabled"] = False
        return config

    def test_circuit_breaker_sets_degraded_mode_metadata(self):
        config = self._base_config()
        config.speculative_sleeve["enabled"] = False

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("main.create_market_client", return_value=object()), patch(
                "main.update_holdings_with_prices", side_effect=self._seed_prices
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
                    logger=logging.getLogger("test.main.degraded"),
                    store=_FakeStore(),
                )

        self.assertTrue(result["success"])
        self.assertTrue(result["degraded_mode"])
        self.assertEqual(result["degraded_reason"], "circuit_breaker")
        self.assertEqual(result["data_mode"], "fallback")
        self.assertTrue(result["scanner"]["meta"]["data_fallback_triggered"])
        self.assertEqual(result["scanner"]["meta"]["run_degraded_reason"], "circuit_breaker")

    def test_degraded_empty_dataset_enters_safe_mode_and_skips_sleeve_plan(self):
        config = self._base_config()
        config.speculative_sleeve["enabled"] = True

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("main.create_market_client", return_value=object()), patch(
                "main.update_holdings_with_prices", side_effect=self._seed_prices
            ), patch(
                "main.load_retirement_data", return_value=SimpleNamespace(total_balance=0.0)
            ), patch(
                "main.DrawdownTracker", _FakeDrawdownTracker
            ), patch(
                "scanner.fallback_watchlist.FallbackWatchlist", _EmptyFallbackWatchlist
            ), patch(
                "builtins.print"
            ):
                result = run_portfolio_update(
                    config=config,
                    dry_run=True,
                    skip_email=True,
                    run_mode="monthly",
                    output_dir=Path(tmpdir),
                    logger=logging.getLogger("test.main.safe_mode"),
                    store=_FakeStore(),
                )

        self.assertTrue(result["success"])
        self.assertTrue(result["scanner"]["safe_mode"])
        self.assertIn("empty_dataset", result["scanner"]["safe_mode_reasons"])
        self.assertEqual(result["scanner"]["sleeve_plan"], [])
        self.assertTrue(
            any("SCANNER SAFE MODE" in warning for warning in result["warnings"])
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
