"""Tests for the structured config loader and legacy compatibility path."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from config.loader import load_runtime_config_dict
from config.schema import ConfigValidationError
from utils import load_config


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _legacy_config() -> dict:
    return {
        "investor": {
            "name": "Test Investor",
            "age": 30,
            "birthdate": "01-01",
            "annual_income": 100000,
            "monthly_expenses": 3000,
            "investment_horizon_years": 20,
            "risk_tolerance": "high",
            "strategy": "buy-and-hold",
        },
        "portfolio": {
            "holdings": [
                {
                    "symbol": "qqq",
                    "shares": 5,
                    "target_weight": 0.45,
                    "asset_class": "us_equity",
                    "is_leveraged": False,
                    "leverage_factor": 1,
                },
                {
                    "symbol": "gld",
                    "shares": 2,
                    "target_weight": 0.30,
                    "asset_class": "commodity",
                    "is_leveraged": False,
                    "leverage_factor": 1,
                },
            ],
            "cash_available": 500.0,
            "target_cash_weight": 0.25,
            "monthly_contribution": 1000,
            "has_regular_contributions": True,
            "is_taxable_account": True,
        },
        "rebalance_rules": {
            "band_threshold": 0.12,
            "use_cash_before_selling": True,
            "direct_contributions_first": True,
            "trim_leverage_before_core": True,
            "avoid_taxable_sales": True,
            "panic_sell_protection": True,
        },
        "growth_mode": {
            "mode": "accumulation_aggressive",
            "concentration_cap": 0.40,
            "leverage_cap": 0.15,
            "target_cagr": 0.09,
        },
        "retirement_401k": {"enabled": False},
        "market_data": {},
        "email": {"enabled": False},
        "schedule": {},
        "output": {},
        "scanner": {"enabled": False},
        "api_limits": {"fmp_daily_calls_budget": 230},
        "watchlist_scanner": {
            "enabled": True,
            "watchlist": ["aapl", " msft ", "AAPL"],
            "max_daily_calls": 20,
            "price_change_alert_pct": 3.0,
            "volume_spike_factor": 1.5,
            "theme_score_threshold": 0.4,
            "min_signal_score": 0.5,
        },
        "theme_engine": {"enabled": False},
    }


def _structured_base() -> dict:
    data = _legacy_config()
    data.update(
        {
            "signals": {
                "min_signal_score": 0.5,
                "min_confidence_score": 0.55,
                "theme_score_threshold": 0.4,
                "price_change_alert_pct": 3.0,
                "volume_spike_factor": 1.5,
                "cooldown_hours": 72,
                "min_evidence_count": 2,
            },
            "execution": {
                "recommend_only": True,
                "max_new_positions_per_day": 1,
                "max_capital_per_day": 1000,
            },
            "data": {
                "max_daily_calls": 20,
            },
            "watchlist": {
                "core": ["AAPL", "MSFT"],
                "tactical": ["SMCI"],
                "speculative": ["coin"],
            },
        }
    )
    data["portfolio"]["cash_reserve_pct"] = 0.25
    data["portfolio"]["max_position_pct"] = 0.40
    data["portfolio"]["rebalance_band_pct"] = 0.12
    data["theme_engine"].update(
        {
            "llm_provider": "openai",
            "openai_base_url": "https://api.openai.com/v1",
            "openai_model": "gpt-4o-mini",
            "task_providers": {"daily": "openai"},
        }
    )
    data["agent"] = {
        "task_providers": {
            "standalone": "openai",
            "weekly": "openai",
            "monthly": "anthropic",
            "maintainer": "anthropic",
        }
    }
    return data


class TestConfigLoader(unittest.TestCase):

    def test_legacy_config_file_gets_structured_sections(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "config.json"
            _write_json(path, _legacy_config())

            resolved = load_runtime_config_dict(str(path), record_history=False)

            self.assertEqual(resolved["watchlist"]["core"], ["AAPL", "MSFT"])
            self.assertEqual(resolved["watchlist_scanner"]["watchlist"], ["AAPL", "MSFT"])
            self.assertEqual(resolved["portfolio"]["cash_reserve_pct"], 0.25)
            self.assertEqual(resolved["signals"]["min_signal_score"], 0.5)
            self.assertEqual(resolved["data"]["max_daily_calls"], 20)
            self.assertEqual(resolved["signals"]["confidence_tiers"]["high"], 0.8)
            self.assertEqual(resolved["signals"]["cooldown"]["medium"], 24)
            self.assertAlmostEqual(resolved["ranking"]["signal_weight"], 0.45)
            self.assertEqual(resolved["config_runtime"]["source_mode"], "legacy_file")

    def test_structured_dir_profile_overlay_syncs_legacy_fields_and_history(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            config_dir = root / "config"
            _write_json(config_dir / "base.json", _structured_base())
            _write_json(
                config_dir / "profiles" / "growth.json",
                {
                    "portfolio": {
                        "cash_reserve_pct": 0.10,
                        "rebalance_band_pct": 0.08,
                    },
                    "signals": {
                        "min_signal_score": 0.65,
                        "cooldown_hours": 36,
                    },
                    "data": {
                        "max_daily_calls": 25,
                    },
                    "watchlist": {
                        "tactical": ["tsla", "smci"],
                    },
                },
            )

            resolved = load_runtime_config_dict(str(config_dir), profile="growth", record_history=True)

            self.assertEqual(resolved["portfolio"]["target_cash_weight"], 0.10)
            self.assertEqual(resolved["rebalance_rules"]["band_threshold"], 0.08)
            self.assertEqual(resolved["watchlist_scanner"]["min_signal_score"], 0.65)
            self.assertEqual(resolved["watchlist_scanner"]["max_daily_calls"], 25)
            self.assertEqual(
                resolved["watchlist_scanner"]["watchlist"],
                ["AAPL", "MSFT", "TSLA", "SMCI", "COIN"],
            )
            history_path = resolved["config_runtime"]["history_snapshot"]
            self.assertTrue(history_path)
            self.assertTrue(Path(history_path).exists())

            load_runtime_config_dict(str(config_dir), profile="growth", record_history=True)
            history_files = list((config_dir / "history").glob("*.json"))
            self.assertEqual(len(history_files), 1)

    def test_structured_validation_rejects_bad_values(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_dir = Path(tmp_dir) / "config"
            bad = _structured_base()
            bad["signals"]["min_signal_score"] = 1.5
            _write_json(config_dir / "base.json", bad)

            with self.assertRaises(ConfigValidationError):
                load_runtime_config_dict(str(config_dir), record_history=False)

    def test_structured_validation_rejects_invalid_tiers_and_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_dir = Path(tmp_dir) / "config"
            bad = _structured_base()
            bad["signals"]["confidence_tiers"] = {"high": 0.6, "medium": 0.7, "low": 0.5}
            bad["signals"]["cooldown"] = {"high": 24, "medium": 6, "low": 72}
            _write_json(config_dir / "base.json", bad)

            with self.assertRaises(ConfigValidationError):
                load_runtime_config_dict(str(config_dir), record_history=False)

    def test_utils_load_config_supports_profile(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_dir = Path(tmp_dir) / "config"
            _write_json(config_dir / "base.json", _structured_base())
            _write_json(
                config_dir / "profiles" / "conservative.json",
                {
                    "portfolio": {"cash_reserve_pct": 0.15},
                    "signals": {"min_signal_score": 0.7},
                },
            )

            config = load_config(str(config_dir), profile="conservative", record_history=False)

            self.assertAlmostEqual(config.target_cash_weight, 0.15)
            self.assertAlmostEqual(config.rebalance_rules.band_threshold, 0.12)
            self.assertEqual(config.watchlist_scanner["min_signal_score"], 0.7)

    def test_theme_engine_llm_fields_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "config.json"
            _write_json(path, _structured_base())

            resolved = load_runtime_config_dict(str(path), record_history=False)

            self.assertEqual(resolved["theme_engine"]["llm_provider"], "openai")
            self.assertEqual(resolved["theme_engine"]["openai_base_url"], "https://api.openai.com/v1")
            self.assertEqual(resolved["theme_engine"]["openai_model"], "gpt-4o-mini")
            self.assertEqual(resolved["theme_engine"]["task_providers"]["daily"], "openai")
            self.assertEqual(resolved["agent"]["task_providers"]["monthly"], "anthropic")


if __name__ == "__main__":
    unittest.main(verbosity=2)
