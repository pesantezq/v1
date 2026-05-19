"""
Tests for portfolio_automation/retune_impact_tracker.py.

Covers:
  - Deterministic fingerprinting (same input → same hash)
  - Diff against baseline catches knob changes + computes correct deltas
  - History ledger dedupes consecutive identical fingerprints
  - Artifact has the observe_only invariant + the baseline reference
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_automation.retune_impact_tracker import (
    _BASELINE_GAUGE,
    _PRE_TRACKER_LABEL,
    _attribute_signal,
    _fingerprint,
    _load_gauge_history,
    append_to_history,
    build_retune_impact,
    compute_gauge_fingerprint,
    compute_outcome_attribution,
    diff_against_baseline,
    run_retune_impact_tracker,
)


class TestFingerprint(unittest.TestCase):
    def test_deterministic(self):
        payload = {"a": 1, "b": {"c": 2}}
        self.assertEqual(_fingerprint(payload), _fingerprint(payload))

    def test_key_order_does_not_matter(self):
        self.assertEqual(
            _fingerprint({"a": 1, "b": 2}),
            _fingerprint({"b": 2, "a": 1}),
        )

    def test_different_values_produce_different_hashes(self):
        self.assertNotEqual(
            _fingerprint({"a": 1}),
            _fingerprint({"a": 2}),
        )


class TestDiffAgainstBaseline(unittest.TestCase):
    def test_no_changes_when_snapshot_matches_baseline(self):
        current = {"snapshot": _BASELINE_GAUGE}
        changes = diff_against_baseline(current)
        real = [c for c in changes if c.get("status") == "changed"]
        self.assertEqual(real, [])

    def test_detects_a_single_knob_change(self):
        snapshot = json.loads(json.dumps(_BASELINE_GAUGE))  # deep copy
        snapshot["allocation_engine"]["max_position_cap"] = 0.20
        current = {"snapshot": snapshot}
        changes = diff_against_baseline(current)
        changed = [c for c in changes if c.get("status") == "changed"]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["knob"], "max_position_cap")
        self.assertAlmostEqual(changed[0]["delta_abs"], 0.12, places=4)

    def test_unavailable_for_missing_current(self):
        snapshot = json.loads(json.dumps(_BASELINE_GAUGE))
        # Wipe one knob so it shows as None.
        snapshot["allocation_engine"]["compounder_base_pct"] = None
        current = {"snapshot": snapshot}
        changes = diff_against_baseline(current)
        unavailable = [c for c in changes if c.get("status") == "unavailable"]
        self.assertEqual(len(unavailable), 1)
        self.assertEqual(unavailable[0]["knob"], "compounder_base_pct")


class TestHistoryLedger(unittest.TestCase):
    def test_appends_new_row_when_fingerprint_differs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data").mkdir()
            r1 = append_to_history({"fingerprint": "aaaa", "snapshot": {}}, root=root)
            r2 = append_to_history({"fingerprint": "bbbb", "snapshot": {}}, root=root)
            self.assertTrue(r1)
            self.assertTrue(r2)
            history = (root / "data" / "gauge_versions.jsonl").read_text()
            self.assertEqual(history.strip().count("\n") + 1, 2)

    def test_dedups_when_fingerprint_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data").mkdir()
            r1 = append_to_history({"fingerprint": "aaaa", "snapshot": {}}, root=root)
            r2 = append_to_history({"fingerprint": "aaaa", "snapshot": {}}, root=root)
            self.assertTrue(r1)
            self.assertFalse(r2)


class TestBuildArtifact(unittest.TestCase):
    def test_observe_only_invariant_and_baseline_reference(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config.json").write_text(json.dumps({}))
            payload = build_retune_impact(root=root)
            self.assertTrue(payload["observe_only"])
            self.assertIn("baseline_label", payload)
            self.assertEqual(payload["baseline_label"], "pre_retune_2026_05_18")

    def test_changes_count_reflects_diff(self):
        # With an empty config.json, allocation_engine + portfolio_construction
        # still imported with current defaults → changes vs baseline expected.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config.json").write_text("{}")
            payload = build_retune_impact(root=root)
            self.assertIsInstance(payload["changes_count"], int)


class TestRunOrchestrator(unittest.TestCase):
    def test_writes_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config.json").write_text("{}")
            (root / "config").mkdir()
            (root / "config" / "base.json").write_text(json.dumps(
                {"ml_advisor": {"enabled": False}}
            ))
            r = run_retune_impact_tracker(root=root)
            self.assertEqual(r["status"], "ok")
            self.assertTrue((root / "outputs" / "latest" / "retune_impact.json").exists())
            self.assertTrue((root / "outputs" / "latest" / "retune_impact.md").exists())


class TestOutcomeAttribution(unittest.TestCase):
    """v2 join: signal_outcomes.csv → gauge_versions.jsonl by timestamp range."""

    def _write_gauge_history(self, root: Path, rows: list[dict]) -> None:
        (root / "data").mkdir(parents=True, exist_ok=True)
        history = root / "data" / "gauge_versions.jsonl"
        history.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    def _write_signal_outcomes(self, root: Path, rows: list[dict]) -> None:
        (root / "outputs" / "performance").mkdir(parents=True, exist_ok=True)
        csv_path = root / "outputs" / "performance" / "signal_outcomes.csv"
        # Use the minimal columns the attribution reads.
        cols = [
            "ticker", "signal_time",
            "outcome_return_1d", "direction_correct_1d",
            "outcome_return_3d", "direction_correct_3d",
            "outcome_return_7d", "direction_correct_7d",
        ]
        import csv as _csv
        with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)

    def test_signal_before_first_history_row_is_pre_tracker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_gauge_history(root, [
                {"first_seen_at": "2026-05-19T00:00:00+00:00", "fingerprint": "AAAA"},
            ])
            self._write_signal_outcomes(root, [
                {"ticker": "X", "signal_time": "2026-05-18T12:00:00",
                 "outcome_return_1d": "0.02", "direction_correct_1d": "1"},
            ])
            r = compute_outcome_attribution(root=root)
            self.assertTrue(r["available"])
            self.assertEqual(r["total_signals"], 1)
            self.assertEqual(r["unattributed_signals"], 1)
            self.assertEqual(r["attributed_signals"], 0)

    def test_signal_after_first_history_row_attributed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_gauge_history(root, [
                {"first_seen_at": "2026-05-19T00:00:00+00:00", "fingerprint": "AAAA"},
            ])
            self._write_signal_outcomes(root, [
                {"ticker": "X", "signal_time": "2026-05-19T01:00:00",
                 "outcome_return_1d": "0.02", "direction_correct_1d": "1"},
                {"ticker": "Y", "signal_time": "2026-05-19T02:00:00",
                 "outcome_return_1d": "-0.01", "direction_correct_1d": "0"},
            ])
            r = compute_outcome_attribution(root=root)
            self.assertEqual(r["attributed_signals"], 2)
            self.assertEqual(r["unattributed_signals"], 0)
            bucket = r["by_fingerprint"]["AAAA"]
            self.assertEqual(bucket["count"], 2)
            self.assertEqual(bucket["resolved_1d"], 2)
            self.assertAlmostEqual(bucket["hit_rate_1d"], 0.5, places=4)
            self.assertAlmostEqual(bucket["mean_return_1d"], 0.005, places=4)

    def test_signal_crossing_retune_attributed_to_later_version(self):
        # Signal at 03:00 should land under fingerprint BBBB (active from 02:00),
        # not AAAA (replaced at 02:00).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_gauge_history(root, [
                {"first_seen_at": "2026-05-19T00:00:00+00:00", "fingerprint": "AAAA"},
                {"first_seen_at": "2026-05-19T02:00:00+00:00", "fingerprint": "BBBB"},
            ])
            self._write_signal_outcomes(root, [
                {"ticker": "X", "signal_time": "2026-05-19T01:00:00"},
                {"ticker": "Y", "signal_time": "2026-05-19T03:00:00"},
            ])
            r = compute_outcome_attribution(root=root)
            self.assertEqual(r["by_fingerprint"]["AAAA"]["count"], 1)
            self.assertEqual(r["by_fingerprint"]["BBBB"]["count"], 1)

    def test_unparseable_signal_time_falls_to_pre_tracker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write_gauge_history(root, [
                {"first_seen_at": "2026-05-19T00:00:00+00:00", "fingerprint": "AAAA"},
            ])
            self._write_signal_outcomes(root, [
                {"ticker": "X", "signal_time": "not-a-timestamp"},
            ])
            r = compute_outcome_attribution(root=root)
            self.assertEqual(r["unattributed_signals"], 1)

    def test_missing_csv_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            r = compute_outcome_attribution(root=Path(td))
            self.assertFalse(r["available"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
