"""
Tests for portfolio_automation/resolution_due_probe.py.

Covers:
  - scan_unresolved correctly flags rows past their elapsed window
  - Window-not-yet-elapsed rows are NOT flagged
  - Already-resolved rows are NOT flagged
  - Unparseable signal_time is skipped, not raised
  - Group-by-ticker aggregates max gap correctly
  - End-to-end run() writes both artifacts
"""
from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_automation.resolution_due_probe import (
    _CAL_DAY_MULTIPLIER,
    _WINDOWS,
    _group_by_ticker,
    build_resolution_due,
    run_resolution_due_probe,
    scan_unresolved,
)


_NOW = datetime(2026, 5, 19, 12, 0, 0)  # naive UTC reference


def _row(ticker: str, days_ago: float, *, outcomes: dict[str, str] | None = None) -> dict:
    ts = _NOW.replace(microsecond=0) - __import__("datetime").timedelta(days=days_ago)
    row = {"ticker": ticker, "signal_time": ts.isoformat()}
    for w in _WINDOWS:
        row[f"outcome_return_{w}d"] = ""
    if outcomes:
        row.update(outcomes)
    return row


class TestScanUnresolved(unittest.TestCase):
    def test_window_not_yet_elapsed_not_flagged(self):
        # 1d window needs 2 cal days; row only 1 cal day old → skip.
        rows = [_row("X", days_ago=1.0)]
        out = scan_unresolved(rows, now=_NOW)
        # No window has elapsed yet for a 1-day-old signal under 2x multiplier.
        self.assertEqual(out, [])

    def test_elapsed_and_null_outcome_flagged(self):
        # 1d window elapsed (3 days > 2); outcome still null → flag.
        rows = [_row("X", days_ago=3.0)]
        out = scan_unresolved(rows, now=_NOW)
        names = {r["window_days"] for r in out}
        self.assertIn(1, names)
        self.assertNotIn(7, names)  # 7-day window needs 14 cal days

    def test_resolved_outcome_not_flagged(self):
        rows = [_row("X", days_ago=10.0,
                     outcomes={"outcome_return_1d": "0.02",
                               "outcome_return_3d": "0.04"})]
        out = scan_unresolved(rows, now=_NOW)
        # Only 7d should still be flagged (10 cal days < 14 cal day threshold? no — 14 means not yet)
        # 10 < 14 so 7d not elapsed → no flags at all.
        self.assertEqual(out, [])

    def test_partial_resolution_only_flags_missing_windows(self):
        # 15 cal days old, 1d resolved but 3d + 7d still null.
        rows = [_row("X", days_ago=15.0,
                     outcomes={"outcome_return_1d": "0.02"})]
        out = scan_unresolved(rows, now=_NOW)
        flagged_windows = {r["window_days"] for r in out}
        self.assertNotIn(1, flagged_windows)
        self.assertIn(3, flagged_windows)
        self.assertIn(7, flagged_windows)

    def test_unparseable_timestamp_skipped(self):
        rows = [{"ticker": "X", "signal_time": "not-a-date"}]
        out = scan_unresolved(rows, now=_NOW)
        self.assertEqual(out, [])


class TestGroupByTicker(unittest.TestCase):
    def test_aggregates_max_gap_and_window_set(self):
        flagged = [
            {"ticker": "A", "window_days": 1, "gap_calendar_days": 5.0},
            {"ticker": "A", "window_days": 3, "gap_calendar_days": 2.0},
            {"ticker": "B", "window_days": 1, "gap_calendar_days": 1.0},
        ]
        out = _group_by_ticker(flagged)
        a = next(r for r in out if r["ticker"] == "A")
        b = next(r for r in out if r["ticker"] == "B")
        self.assertEqual(a["stuck_signals"], 2)
        self.assertEqual(a["windows_stuck"], [1, 3])
        self.assertEqual(a["max_gap_days"], 5.0)
        self.assertEqual(b["stuck_signals"], 1)


class TestBuildAndRun(unittest.TestCase):
    def _write_csv(self, root: Path, rows: list[dict]) -> None:
        (root / "outputs" / "performance").mkdir(parents=True, exist_ok=True)
        path = root / "outputs" / "performance" / "signal_outcomes.csv"
        cols = list(rows[0].keys()) if rows else ["ticker", "signal_time"]
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)

    def test_run_writes_both_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_csv(root, [_row("X", days_ago=5.0)])
            result = run_resolution_due_probe(root=root, now=_NOW)
            self.assertEqual(result["status"], "ok")
            self.assertGreaterEqual(result["stuck_count"], 1)
            self.assertTrue((root / "outputs" / "latest" / "decisions_due_for_resolution.json").exists())
            self.assertTrue((root / "outputs" / "latest" / "decisions_due_for_resolution.md").exists())

    def test_missing_csv_status_insufficient(self):
        with tempfile.TemporaryDirectory() as td:
            payload = build_resolution_due(root=Path(td), now=_NOW)
            self.assertEqual(payload["status"], "insufficient_data")
            self.assertEqual(payload["stuck_count"], 0)

    def test_observe_only_invariant(self):
        with tempfile.TemporaryDirectory() as td:
            payload = build_resolution_due(root=Path(td), now=_NOW)
            self.assertIs(payload["observe_only"], True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
