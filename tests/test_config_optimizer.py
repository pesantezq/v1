from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from config.loader import load_runtime_config_dict
from watchlist_scanner.config_optimizer import (
    analyze_outcomes_and_suggest_config,
    write_config_suggestions,
)


def _resolved_row(
    *,
    alert_tier: str,
    priority_score: float,
    evidence_count: int,
    outcome_label: str,
    return_pct: float,
) -> dict:
    return {
        "outcome_pending": 0,
        "alert_tier": alert_tier,
        "priority_score": priority_score,
        "evidence_count": evidence_count,
        "evidence_breadth": evidence_count,
        "outcome_label": outcome_label,
        "return_pct": return_pct,
    }


class TestConfigOptimizer(unittest.TestCase):

    def test_no_resolved_rows_returns_empty_suggestions(self):
        config = load_runtime_config_dict("config", record_history=False)
        result = analyze_outcomes_and_suggest_config([], config)
        self.assertEqual(result["sample_size"], 0)
        self.assertEqual(result["suggestions"], [])

    def test_underperforming_medium_and_low_rows_generate_conservative_suggestions(self):
        rows = []
        for _ in range(10):
            rows.append(_resolved_row(alert_tier="medium", priority_score=0.62, evidence_count=1, outcome_label="negative", return_pct=-1.5))
        for _ in range(10):
            rows.append(_resolved_row(alert_tier="high", priority_score=0.86, evidence_count=3, outcome_label="positive", return_pct=2.2))
        for _ in range(10):
            rows.append(_resolved_row(alert_tier="low", priority_score=0.44, evidence_count=1, outcome_label="negative", return_pct=-0.8))
        for _ in range(10):
            rows.append(_resolved_row(alert_tier="medium", priority_score=0.55, evidence_count=2, outcome_label="flat", return_pct=0.2))

        config = load_runtime_config_dict("config", profile="growth", record_history=False)
        result = analyze_outcomes_and_suggest_config(rows, config)
        fields = {item["field"] for item in result["suggestions"]}

        self.assertIn("signals.min_evidence_count", fields)
        self.assertIn("ranking.confidence_weight", fields)
        self.assertIn("signals.confidence_tiers.low", fields)

    def test_priority_bucket_underperformance_can_raise_min_signal_score(self):
        rows = []
        for _ in range(10):
            rows.append(_resolved_row(alert_tier="medium", priority_score=0.42, evidence_count=2, outcome_label="negative", return_pct=-1.1))
        for _ in range(10):
            rows.append(_resolved_row(alert_tier="high", priority_score=0.68, evidence_count=3, outcome_label="positive", return_pct=1.4))
        for _ in range(10):
            rows.append(_resolved_row(alert_tier="medium", priority_score=0.56, evidence_count=2, outcome_label="flat", return_pct=0.2))

        config = load_runtime_config_dict("config", record_history=False)
        result = analyze_outcomes_and_suggest_config(rows, config)
        fields = {item["field"] for item in result["suggestions"]}
        self.assertIn("signals.min_signal_score", fields)

    def test_write_config_suggestions_writes_into_history_for_structured_config(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_dir = Path(tmp_dir) / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "history").mkdir(parents=True, exist_ok=True)
            (config_dir / "base.json").write_text(json.dumps({
                "portfolio": {"cash_reserve_pct": 0.05, "max_position_pct": 0.4, "rebalance_band_pct": 0.12, "holdings": []},
                "signals": {"min_signal_score": 0.5, "min_confidence_score": 0.5, "theme_score_threshold": 0.4, "price_change_alert_pct": 3.0, "volume_spike_factor": 1.5, "cooldown_hours": 72, "min_evidence_count": 2},
                "execution": {"recommend_only": True, "max_new_positions_per_day": 1, "max_capital_per_day": 1000},
                "data": {"max_daily_calls": 20},
                "watchlist": {"core": [], "tactical": [], "speculative": []},
                "investor": {"name": "Test", "age": 30, "birthdate": "01-01", "annual_income": 100000, "monthly_expenses": 3000, "investment_horizon_years": 20, "risk_tolerance": "high", "strategy": "buy-and-hold"},
                "rebalance_rules": {"band_threshold": 0.12, "use_cash_before_selling": True, "direct_contributions_first": True, "trim_leverage_before_core": True, "avoid_taxable_sales": True, "panic_sell_protection": True},
            }), encoding="utf-8")

            payload = {"profile": "base", "suggestions": [{"field": "signals.min_signal_score", "current": 0.5, "suggested": 0.55, "reason": "test", "sample_size": 12}]}
            path = write_config_suggestions(payload, config_path=config_dir)

            self.assertIsNotNone(path)
            self.assertTrue(Path(path).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
