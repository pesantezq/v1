"""
Tests for portfolio_automation/pattern_learning.py.

Covers:
  - Wilson 95% CI math
  - Snapshot loading degrades safely when history is missing
  - Outcome matching by (ticker, date) prefix
  - Per-tag aggregation produces correct counts + hit-rates
  - Significance classification respects min-n threshold and Δ thresholds
  - Yearly partition adds gauge_fingerprint × volatility_regime breakdown
  - run_pattern_learning orchestrator writes both artifacts
"""
from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_automation.pattern_learning import (
    _MIN_N_SIGNIFICANT,
    _STRONG_DELTA_PP,
    _WINNER_DELTA_PP,
    _classify,
    _match_outcome,
    _new_bucket,
    build_pattern_efficacy,
    run_pattern_learning,
    wilson_ci_95,
)


class TestWilsonCI(unittest.TestCase):
    def test_zero_n_returns_zero_zero(self):
        lo, hi = wilson_ci_95(0, 0)
        self.assertEqual((lo, hi), (0.0, 0.0))

    def test_all_hits_high_ci(self):
        lo, hi = wilson_ci_95(10, 10)
        self.assertGreater(lo, 0.5)
        self.assertEqual(hi, 1.0)

    def test_50_50_at_30_samples_brackets_half(self):
        lo, hi = wilson_ci_95(15, 30)
        self.assertLess(lo, 0.5)
        self.assertGreater(hi, 0.5)

    def test_ci_narrows_as_n_grows(self):
        _, hi_small = wilson_ci_95(5, 10)
        _, hi_large = wilson_ci_95(500, 1000)
        # Half-width is smaller for larger n at the same proportion
        self.assertLess(hi_large - 0.5, hi_small - 0.5)


class TestClassify(unittest.TestCase):
    def test_insufficient_sample_below_min(self):
        stats = {"n_samples": _MIN_N_SIGNIFICANT - 1, "hit_rate_1d": 0.8}
        self.assertEqual(_classify(stats, 0.5), "insufficient_sample")

    def test_strong_winner(self):
        stats = {"n_samples": 100, "hit_rate_1d": 0.5 + (_STRONG_DELTA_PP / 100) + 0.01}
        self.assertEqual(_classify(stats, 0.5), "strong_winner")

    def test_winner(self):
        stats = {"n_samples": 100, "hit_rate_1d": 0.5 + (_WINNER_DELTA_PP / 100) + 0.01}
        self.assertEqual(_classify(stats, 0.5), "winner")

    def test_neutral_when_close_to_baseline(self):
        stats = {"n_samples": 100, "hit_rate_1d": 0.52}
        self.assertEqual(_classify(stats, 0.50), "neutral")

    def test_strong_loser(self):
        stats = {"n_samples": 100, "hit_rate_1d": 0.5 - (_STRONG_DELTA_PP / 100) - 0.01}
        self.assertEqual(_classify(stats, 0.5), "strong_loser")


class TestOutcomeMatching(unittest.TestCase):
    def test_match_same_day(self):
        outcomes = {
            "NVDA": [
                {"signal_time": "2026-05-28T01:00:00",
                 "outcome_return_1d": 0.03, "direction_correct_1d": 1},
            ],
        }
        m = _match_outcome("2026-05-28", "NVDA", outcomes)
        self.assertIsNotNone(m)
        self.assertEqual(m["outcome_return_1d"], 0.03)

    def test_no_match_returns_none(self):
        outcomes = {"NVDA": [{"signal_time": "2026-05-20T00:00:00"}]}
        m = _match_outcome("2026-05-28", "NVDA", outcomes)
        self.assertIsNone(m)  # 05-20 is before 05-28

    def test_picks_earliest_same_day(self):
        outcomes = {
            "NVDA": [
                {"signal_time": "2026-05-28T13:00:00", "outcome_return_1d": 0.01},
                {"signal_time": "2026-05-28T09:00:00", "outcome_return_1d": 0.02},
            ],
        }
        m = _match_outcome("2026-05-28", "NVDA", outcomes)
        self.assertEqual(m["outcome_return_1d"], 0.02)


class TestAggregationOnFixtures(unittest.TestCase):
    """End-to-end via a controlled fixture: one snapshot with two tickers,
    one a winner, one a loser, and a third with no outcome."""

    def _write_snapshot(self, root: Path, date_iso: str, rows: list[dict]) -> None:
        if date_iso == datetime.now(timezone.utc).date().isoformat():
            p = root / "outputs" / "latest" / "top100_daily.json"
        else:
            p = root / "outputs" / "history" / date_iso / "top100_daily.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"candidates": rows}))

    def _write_outcomes(self, root: Path, rows: list[dict]) -> None:
        p = root / "outputs" / "performance" / "signal_outcomes.csv"
        p.parent.mkdir(parents=True, exist_ok=True)
        cols = [
            "ticker", "signal_time", "regime_label",
            "outcome_return_1d", "direction_correct_1d",
            "outcome_return_3d", "direction_correct_3d",
            "outcome_return_7d", "direction_correct_7d",
        ]
        with p.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)

    def test_winner_tag_outperforms_baseline(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            today = datetime.now(timezone.utc).date().isoformat()
            self._write_snapshot(root, today, [
                {"symbol": "WIN1", "rationale_tags": ["good", "common"]},
                {"symbol": "WIN2", "rationale_tags": ["good", "common"]},
                {"symbol": "LOSS1", "rationale_tags": ["bad", "common"]},
                {"symbol": "LOSS2", "rationale_tags": ["bad", "common"]},
            ])
            today_iso = f"{today}T09:00:00"
            self._write_outcomes(root, [
                {"ticker": "WIN1", "signal_time": today_iso, "regime_label": "risk_on",
                 "outcome_return_1d": "0.04", "direction_correct_1d": "1"},
                {"ticker": "WIN2", "signal_time": today_iso, "regime_label": "risk_on",
                 "outcome_return_1d": "0.03", "direction_correct_1d": "1"},
                {"ticker": "LOSS1", "signal_time": today_iso, "regime_label": "risk_on",
                 "outcome_return_1d": "-0.02", "direction_correct_1d": "0"},
                {"ticker": "LOSS2", "signal_time": today_iso, "regime_label": "risk_on",
                 "outcome_return_1d": "-0.01", "direction_correct_1d": "0"},
            ])
            payload = build_pattern_efficacy(root=root, lookback_days=1)
            tags = payload["by_tag"]
            self.assertEqual(tags["good"]["hit_rate_1d"], 1.0)
            self.assertEqual(tags["bad"]["hit_rate_1d"], 0.0)
            self.assertEqual(tags["common"]["hit_rate_1d"], 0.5)

    def test_unmatched_rows_dont_inflate_counts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            today = datetime.now(timezone.utc).date().isoformat()
            self._write_snapshot(root, today, [
                {"symbol": "X", "rationale_tags": ["t"]},
                {"symbol": "ORPHAN", "rationale_tags": ["t"]},
            ])
            today_iso = f"{today}T09:00:00"
            self._write_outcomes(root, [
                {"ticker": "X", "signal_time": today_iso,
                 "outcome_return_1d": "0.02", "direction_correct_1d": "1"},
            ])
            payload = build_pattern_efficacy(root=root, lookback_days=1)
            # Only X matched; ORPHAN has no outcome row
            self.assertEqual(payload["by_tag"]["t"]["n_samples"], 1)
            self.assertEqual(payload["rows_matched_to_outcomes"], 1)


class TestYearlyPartition(unittest.TestCase):
    def test_partition_off_by_default_in_weekly(self):
        with tempfile.TemporaryDirectory() as td:
            r = run_pattern_learning(root=Path(td), cadence="weekly")
            self.assertEqual(r["status"], "ok")
            # No partition section emitted for weekly
            p = Path(td) / "outputs" / "latest" / "pattern_efficacy_weekly.json"
            payload = json.loads(p.read_text())
            self.assertIsNone(payload.get("partitioned_by_fingerprint_regime"))

    def test_partition_on_for_yearly(self):
        with tempfile.TemporaryDirectory() as td:
            r = run_pattern_learning(root=Path(td), cadence="yearly")
            self.assertEqual(r["status"], "ok")
            p = Path(td) / "outputs" / "latest" / "pattern_efficacy_yearly.json"
            payload = json.loads(p.read_text())
            # Empty list (no data) but the field is present (not None)
            self.assertIsNotNone(payload.get("partitioned_by_fingerprint_regime"))


class TestOrchestrator(unittest.TestCase):
    def test_run_writes_json_and_md(self):
        with tempfile.TemporaryDirectory() as td:
            r = run_pattern_learning(root=Path(td), cadence="weekly")
            self.assertEqual(r["status"], "ok")
            self.assertTrue((Path(td) / "outputs" / "latest" / "pattern_efficacy_weekly.json").exists())
            self.assertTrue((Path(td) / "outputs" / "latest" / "pattern_efficacy_weekly.md").exists())

    def test_unknown_cadence_returns_error(self):
        with tempfile.TemporaryDirectory() as td:
            r = run_pattern_learning(root=Path(td), cadence="hourly")
            self.assertEqual(r["status"], "error")
            self.assertIn("unknown_cadence", r["error"])

    def test_empty_inputs_no_crash(self):
        with tempfile.TemporaryDirectory() as td:
            r = run_pattern_learning(root=Path(td), cadence="weekly")
            self.assertEqual(r["status"], "ok")
            self.assertEqual(r["snapshots_consumed"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
