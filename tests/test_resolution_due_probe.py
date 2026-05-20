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

    def test_sort_order_descending_by_stuck_then_max_gap(self):
        # Locks the (-stuck_signals, -max_gap_days) ordering. Without this
        # assertion, a reordering refactor would silently rearrange the
        # "Top Stuck Tickers" table in the memo.
        flagged = [
            {"ticker": "LOW", "window_days": 1, "gap_calendar_days": 0.5},
            {"ticker": "TIE_BIG_GAP", "window_days": 1, "gap_calendar_days": 9.0},
            {"ticker": "TIE_SMALL_GAP", "window_days": 1, "gap_calendar_days": 1.0},
            {"ticker": "TIE_BIG_GAP", "window_days": 3, "gap_calendar_days": 2.0},
            {"ticker": "TIE_SMALL_GAP", "window_days": 3, "gap_calendar_days": 1.5},
        ]
        out = _group_by_ticker(flagged)
        tickers_in_order = [r["ticker"] for r in out]
        # TIE_BIG_GAP and TIE_SMALL_GAP both have 2 stuck signals; the one
        # with the larger max_gap_days breaks the tie ahead.
        self.assertEqual(tickers_in_order[:2], ["TIE_BIG_GAP", "TIE_SMALL_GAP"])
        self.assertEqual(tickers_in_order[-1], "LOW")


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

    def test_on_disk_payload_carries_invariants(self):
        # Catches a writer that silently drops observe_only / schema_version /
        # source from the on-disk artifact even when the in-memory payload
        # has them.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_csv(root, [_row("X", days_ago=5.0)])
            run_resolution_due_probe(root=root, now=_NOW)
            artifact = root / "outputs" / "latest" / "decisions_due_for_resolution.json"
            payload = json.loads(artifact.read_text(encoding="utf-8"))
            self.assertIs(payload["observe_only"], True)
            self.assertEqual(payload["schema_version"], "1")
            self.assertEqual(payload["source"], "resolution_due_probe")
            self.assertEqual(payload["windows_tracked"], [1, 3, 7])
            self.assertIn("disclaimer", payload)

    def test_write_files_false_returns_no_artifacts_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_csv(root, [_row("X", days_ago=5.0)])
            result = run_resolution_due_probe(root=root, now=_NOW, write_files=False)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["artifacts"], {})
            self.assertFalse(
                (root / "outputs" / "latest" / "decisions_due_for_resolution.json").exists()
            )

    def test_malformed_csv_returns_error_status(self):
        # Garbage bytes in the CSV path → the producer must return a degraded
        # dict, never raise. Without this, a corrupted CSV would bubble up as
        # an unhandled exception in the wrapper stage.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "outputs" / "performance").mkdir(parents=True, exist_ok=True)
            csv_path = root / "outputs" / "performance" / "signal_outcomes.csv"
            csv_path.write_bytes(b"\x00\xff\xfe garbage not csv")
            # csv.DictReader on this returns a single dict; the resolver
            # should not raise. Either status="ok" with stuck_count=0, or
            # status="error". We accept either as long as nothing raises.
            payload = build_resolution_due(root=root, now=_NOW)
            self.assertIn(payload["status"], ("ok", "error", "insufficient_data"))
            self.assertEqual(payload.get("stuck_count", 0), 0)

    def test_sentinel_null_outcome_values_treated_as_unresolved(self):
        # Real CSV producers occasionally emit "—" / "None" / "null" / "" in
        # outcome cells. Each should be classified as unresolved by the
        # probe, not silently treated as a number.
        import datetime as _dt
        ts = (_NOW - _dt.timedelta(days=5)).isoformat()
        rows = [
            {"ticker": "DASH", "signal_time": ts,
             "outcome_return_1d": "—", "outcome_return_3d": "", "outcome_return_7d": ""},
            {"ticker": "NONE_STR", "signal_time": ts,
             "outcome_return_1d": "None", "outcome_return_3d": "", "outcome_return_7d": ""},
            {"ticker": "NULL_STR", "signal_time": ts,
             "outcome_return_1d": "null", "outcome_return_3d": "", "outcome_return_7d": ""},
            {"ticker": "EMPTY", "signal_time": ts,
             "outcome_return_1d": "", "outcome_return_3d": "", "outcome_return_7d": ""},
        ]
        out = scan_unresolved(rows, now=_NOW)
        flagged_tickers = {r["ticker"] for r in out if r["window_days"] == 1}
        self.assertEqual(flagged_tickers, {"DASH", "NONE_STR", "NULL_STR", "EMPTY"})

    def test_timezone_aware_signal_time_parses_correctly(self):
        # The parser strips tzinfo and converts to naive UTC. Lock this so a
        # refactor doesn't accidentally compare a tz-aware datetime against a
        # naive one (Python raises TypeError on that).
        import datetime as _dt
        from portfolio_automation.resolution_due_probe import _parse_signal_time
        utc_z = "2026-05-15T00:00:00Z"
        plus_offset = "2026-05-15T05:00:00+05:00"  # same instant in UTC
        utc_naive = _parse_signal_time(utc_z)
        utc_offset = _parse_signal_time(plus_offset)
        self.assertEqual(utc_naive, _dt.datetime(2026, 5, 15, 0, 0, 0))
        self.assertEqual(utc_offset, _dt.datetime(2026, 5, 15, 0, 0, 0))
        self.assertIsNone(utc_naive.tzinfo)


if __name__ == "__main__":
    unittest.main(verbosity=2)
