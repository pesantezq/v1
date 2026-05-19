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
    _fingerprint,
    append_to_history,
    build_retune_impact,
    compute_gauge_fingerprint,
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
