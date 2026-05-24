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
    _TRADING_DAY_MULTIPLIER,
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
        # 1d window needs 2 trading days; row only 1 cal day old → skip.
        # _NOW is Tue 12:00; days_ago=1.0 lands on Mon 12:00 → ~1.0 trading day.
        rows = [_row("X", days_ago=1.0)]
        out = scan_unresolved(rows, now=_NOW)
        self.assertEqual(out, [])

    def test_elapsed_and_null_outcome_flagged(self):
        # _NOW is Tue 12:00; days_ago=5.0 lands on Thu 12:00 prior week.
        # Trading days elapsed = 0.5 (Thu PM) + 1 (Fri) + 1 (Mon) + 0.5 (Tue AM) = 3.0.
        # 3.0 >= 2.0 (1d threshold) → flag 1d. 3.0 < 6.0 (3d threshold) → no flag 3d.
        rows = [_row("X", days_ago=5.0)]
        out = scan_unresolved(rows, now=_NOW)
        names = {r["window_days"] for r in out}
        self.assertIn(1, names)
        self.assertNotIn(3, names)
        self.assertNotIn(7, names)  # 7-day window needs 14 trading days

    def test_resolved_outcome_not_flagged(self):
        rows = [_row("X", days_ago=10.0,
                     outcomes={"outcome_return_1d": "0.02",
                               "outcome_return_3d": "0.04"})]
        out = scan_unresolved(rows, now=_NOW)
        # Only 7d should still be flagged (10 cal days < 14 cal day threshold? no — 14 means not yet)
        # 10 < 14 so 7d not elapsed → no flags at all.
        self.assertEqual(out, [])

    def test_partial_resolution_only_flags_missing_windows(self):
        # 22 cal days old → ~16 trading days. 1d resolved but 3d + 7d still null.
        # 16 >= 14 (7d threshold) so 7d also flagged; 1d cell carries a value
        # so it is excluded.
        rows = [_row("X", days_ago=22.0,
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
            {"ticker": "A", "window_days": 1, "gap_trading_days": 5.0},
            {"ticker": "A", "window_days": 3, "gap_trading_days": 2.0},
            {"ticker": "B", "window_days": 1, "gap_trading_days": 1.0},
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
            {"ticker": "LOW", "window_days": 1, "gap_trading_days": 0.5},
            {"ticker": "TIE_BIG_GAP", "window_days": 1, "gap_trading_days": 9.0},
            {"ticker": "TIE_SMALL_GAP", "window_days": 1, "gap_trading_days": 1.0},
            {"ticker": "TIE_BIG_GAP", "window_days": 3, "gap_trading_days": 2.0},
            {"ticker": "TIE_SMALL_GAP", "window_days": 3, "gap_trading_days": 1.5},
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
            self.assertEqual(payload["schema_version"], "2")
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


class TestTradingDayCalendar(unittest.TestCase):
    """Trading-day awareness — signals emitted Friday should NOT be flagged
    stuck on Sunday because no trading sessions have elapsed."""

    def test_trading_days_elapsed_excludes_weekend(self):
        # Fri 12:00 -> Mon 12:00 = 0.5 (Fri PM) + 0 (Sat) + 0 (Sun) + 0.5 (Mon AM)
        from portfolio_automation.resolution_due_probe import _trading_days_elapsed
        fri_noon = datetime(2026, 5, 22, 12, 0, 0)
        mon_noon = datetime(2026, 5, 25, 12, 0, 0)
        self.assertAlmostEqual(
            _trading_days_elapsed(fri_noon, mon_noon), 1.0, places=5
        )

    def test_trading_days_elapsed_zero_over_weekend_only(self):
        # Sat 00:00 -> Sun 23:59 = 0 trading days (both weekend).
        from portfolio_automation.resolution_due_probe import _trading_days_elapsed
        sat = datetime(2026, 5, 23, 0, 0, 0)
        sun_end = datetime(2026, 5, 24, 23, 59, 59)
        self.assertAlmostEqual(
            _trading_days_elapsed(sat, sun_end), 0.0, places=3
        )

    def test_trading_days_elapsed_handles_reverse_or_equal(self):
        from portfolio_automation.resolution_due_probe import _trading_days_elapsed
        same = datetime(2026, 5, 22, 12, 0, 0)
        self.assertEqual(_trading_days_elapsed(same, same), 0.0)
        # If end < start, return 0 rather than negative.
        later = datetime(2026, 5, 22, 13, 0, 0)
        self.assertEqual(_trading_days_elapsed(later, same), 0.0)

    def test_friday_signal_not_flagged_on_sunday_after_1d_window(self):
        # Production bug from 2026-05-24: Friday 09:02 signals checked on
        # Sunday 09:03. Calendar age = 2.0d -> previously flagged. Trading
        # age = ~0.62d (Fri PM only) -> must NOT be flagged.
        fri = datetime(2026, 5, 22, 9, 2, 0)
        sun = datetime(2026, 5, 24, 9, 3, 0)
        rows = [{
            "ticker": "QQQ", "signal_time": fri.isoformat(),
            "outcome_return_1d": "", "outcome_return_3d": "",
            "outcome_return_7d": "",
        }]
        self.assertEqual(scan_unresolved(rows, now=sun), [])

    def test_friday_signal_not_flagged_on_monday_morning(self):
        # Mon 09:03 after Fri 09:02 = ~1.0 trading days; threshold for 1d
        # window is 2.0 -> still NOT flagged. Gives the Monday cron its full
        # cycle to populate outcomes.
        fri = datetime(2026, 5, 22, 9, 2, 0)
        mon = datetime(2026, 5, 25, 9, 3, 0)
        rows = [{
            "ticker": "QQQ", "signal_time": fri.isoformat(),
            "outcome_return_1d": "", "outcome_return_3d": "",
            "outcome_return_7d": "",
        }]
        self.assertEqual(scan_unresolved(rows, now=mon), [])

    def test_friday_signal_flagged_on_tuesday_if_outcome_still_null(self):
        # Tue 09:03 after Fri 09:02 = ~2.0 trading days; threshold met.
        # Two cron cycles (Mon, Tue) have run; if outcome is still null the
        # row is genuinely stuck and must be flagged.
        fri = datetime(2026, 5, 22, 9, 2, 0)
        tue = datetime(2026, 5, 26, 9, 3, 0)
        rows = [{
            "ticker": "QQQ", "signal_time": fri.isoformat(),
            "outcome_return_1d": "", "outcome_return_3d": "",
            "outcome_return_7d": "",
        }]
        out = scan_unresolved(rows, now=tue)
        flagged_windows = {r["window_days"] for r in out}
        self.assertIn(1, flagged_windows)
        self.assertNotIn(3, flagged_windows)
        self.assertNotIn(7, flagged_windows)

    def test_payload_uses_trading_day_field_names(self):
        # Stuck-row payload should expose `age_trading_days`,
        # `expected_trading_days`, `gap_trading_days` (renamed from the
        # earlier *_calendar_days variants). Base payload should expose
        # `trading_day_multiplier` and schema_version "2".
        fri = datetime(2026, 5, 22, 9, 2, 0)
        next_fri = datetime(2026, 5, 29, 9, 3, 0)  # ~5 trading days later
        rows = [{
            "ticker": "X", "signal_time": fri.isoformat(),
            "outcome_return_1d": "", "outcome_return_3d": "",
            "outcome_return_7d": "",
        }]
        out = scan_unresolved(rows, now=next_fri)
        self.assertGreaterEqual(len(out), 1)
        row = out[0]
        self.assertIn("age_trading_days", row)
        self.assertIn("expected_trading_days", row)
        self.assertIn("gap_trading_days", row)
        self.assertNotIn("age_calendar_days", row)

    def test_base_payload_carries_trading_day_multiplier_and_schema_v2(self):
        with tempfile.TemporaryDirectory() as td:
            payload = build_resolution_due(root=Path(td), now=_NOW)
            self.assertEqual(payload["schema_version"], "2")
            self.assertIn("trading_day_multiplier", payload)
            self.assertNotIn("cal_day_multiplier", payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
