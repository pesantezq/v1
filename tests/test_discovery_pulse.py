"""
Tests for portfolio_automation/discovery_pulse.py.

Covers:
  - State file lifecycle (init, monthly rollover, daily counter rollover)
  - Cap evaluation (each cap acts as a trip-wire; both must clear to allow)
  - Skip path writes telemetry + increments skipped counter
  - Pure-function helpers don't require live LLM / FMP / scraped_intel calls
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_automation.discovery_pulse import (
    _DEFAULT_CAPS,
    _current_date_key,
    _current_month_key,
    _empty_state,
    evaluate_caps,
    load_state,
    run_discovery_pulse,
)


class TestStateLifecycle(unittest.TestCase):
    def test_empty_state_has_all_required_fields(self):
        s = _empty_state()
        for key in (
            "month",
            "openai_cost_usd_month",
            "fmp_calls_month",
            "theme_runs_today",
            "theme_runs_date",
            "scraped_intel_runs_today",
            "scraped_intel_runs_date",
            "total_runs_month",
            "skipped_runs_month",
            "last_run_at",
            "last_skip_reason",
            "caps",
        ):
            self.assertIn(key, s)
        self.assertEqual(s["openai_cost_usd_month"], 0.0)
        self.assertEqual(s["fmp_calls_month"], 0)

    def test_load_state_initializes_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            s = load_state(Path(td))
            self.assertEqual(s["month"], _current_month_key())
            self.assertEqual(s["theme_runs_today"], 0)
            self.assertIn("openai_cost_usd_max", s["caps"])

    def test_monthly_rollover_resets_counters_preserves_caps(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_path = root / "data" / "discovery_pulse_state.json"
            state_path.parent.mkdir(parents=True)
            # Stale state from a prior month
            stale = _empty_state()
            stale["month"] = "2025-01"
            stale["openai_cost_usd_month"] = 999.0
            stale["fmp_calls_month"] = 99999
            stale["caps"]["openai_cost_usd_max"] = 25.0  # custom cap
            state_path.write_text(json.dumps(stale))
            s = load_state(root)
            self.assertEqual(s["month"], _current_month_key())
            self.assertEqual(s["openai_cost_usd_month"], 0.0)
            self.assertEqual(s["fmp_calls_month"], 0)
            # Custom cap preserved
            self.assertEqual(s["caps"]["openai_cost_usd_max"], 25.0)

    def test_daily_rollover_resets_per_day_counters(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_path = root / "data" / "discovery_pulse_state.json"
            state_path.parent.mkdir(parents=True)
            today = _current_date_key()
            stale = _empty_state()
            stale["theme_runs_today"] = 5
            stale["theme_runs_date"] = "2025-01-01"   # stale
            stale["scraped_intel_runs_today"] = 4
            stale["scraped_intel_runs_date"] = "2025-01-01"
            state_path.write_text(json.dumps(stale))
            s = load_state(root)
            self.assertEqual(s["theme_runs_today"], 0)
            self.assertEqual(s["theme_runs_date"], today)
            self.assertEqual(s["scraped_intel_runs_today"], 0)
            self.assertEqual(s["scraped_intel_runs_date"], today)


class TestCapEvaluation(unittest.TestCase):
    def test_fresh_state_allows_run(self):
        s = _empty_state()
        allowed, reason = evaluate_caps(s)
        self.assertTrue(allowed)
        self.assertIsNone(reason)

    def test_openai_cap_trips(self):
        s = _empty_state()
        s["openai_cost_usd_month"] = s["caps"]["openai_cost_usd_max"]
        allowed, reason = evaluate_caps(s)
        self.assertFalse(allowed)
        self.assertIn("openai_cap", reason)

    def test_fmp_cap_trips(self):
        s = _empty_state()
        s["fmp_calls_month"] = s["caps"]["fmp_calls_max"]
        allowed, reason = evaluate_caps(s)
        self.assertFalse(allowed)
        self.assertIn("fmp_cap", reason)

    def test_daily_theme_cap_trips(self):
        s = _empty_state()
        s["theme_runs_today"] = s["caps"]["theme_runs_per_day_max"]
        allowed, reason = evaluate_caps(s)
        self.assertFalse(allowed)
        self.assertIn("theme_cap", reason)

    def test_daily_scraped_intel_cap_trips(self):
        s = _empty_state()
        s["scraped_intel_runs_today"] = s["caps"]["scraped_intel_runs_per_day_max"]
        allowed, reason = evaluate_caps(s)
        self.assertFalse(allowed)
        self.assertIn("scraped_intel_cap", reason)

    def test_either_cap_blocks_independently(self):
        # OpenAI maxed, FMP healthy → blocked
        s = _empty_state()
        s["openai_cost_usd_month"] = s["caps"]["openai_cost_usd_max"]
        s["fmp_calls_month"] = 100  # well under cap
        allowed, _ = evaluate_caps(s)
        self.assertFalse(allowed)

        # FMP maxed, OpenAI healthy → blocked
        s = _empty_state()
        s["openai_cost_usd_month"] = 0.01
        s["fmp_calls_month"] = s["caps"]["fmp_calls_max"]
        allowed, _ = evaluate_caps(s)
        self.assertFalse(allowed)


class TestSkipPath(unittest.TestCase):
    """When caps are reached, run_discovery_pulse skips cleanly and writes telemetry."""

    def test_skip_writes_state_and_increments_skipped_counter(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_path = root / "data" / "discovery_pulse_state.json"
            state_path.parent.mkdir(parents=True)
            # Force OpenAI cap reached
            s = _empty_state()
            s["openai_cost_usd_month"] = s["caps"]["openai_cost_usd_max"]
            state_path.write_text(json.dumps(s))

            payload = run_discovery_pulse(root=root, write_files=True, dry_run=False)
            self.assertTrue(payload["skipped"])
            self.assertIsNotNone(payload["skip_reason"])
            self.assertIsNone(payload["tier_a"])
            self.assertIsNone(payload["tier_b"])

            # State persisted with incremented skipped counter
            new_state = json.loads(state_path.read_text())
            self.assertEqual(new_state["skipped_runs_month"], 1)
            self.assertEqual(new_state["last_skip_reason"], payload["skip_reason"])

            # Telemetry artifact written
            artifact = root / "outputs" / "latest" / "discovery_pulse_status.json"
            self.assertTrue(artifact.exists())

    def test_dry_run_skips_writes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = run_discovery_pulse(root=root, write_files=True, dry_run=True)
            # Dry run still skips file writes
            self.assertFalse((root / "data" / "discovery_pulse_state.json").exists())
            self.assertFalse((root / "outputs" / "latest" / "discovery_pulse_status.json").exists())


class TestCustomCaps(unittest.TestCase):
    def test_custom_caps_loaded_from_state_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_path = root / "data" / "discovery_pulse_state.json"
            state_path.parent.mkdir(parents=True)
            s = _empty_state()
            s["caps"]["openai_cost_usd_max"] = 1.0  # very tight
            state_path.write_text(json.dumps(s))

            loaded = load_state(root)
            self.assertEqual(loaded["caps"]["openai_cost_usd_max"], 1.0)
            # Defaults backfilled
            self.assertEqual(loaded["caps"]["fmp_calls_max"], _DEFAULT_CAPS["fmp_calls_max"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
