"""Tests for applied_fix_verifier — the daily-tool-analysis consumer that
re-checks fixes recorded in daily_check_state.json:applied_fixes against the
next run's artifacts.

Each fix carries a machine-checkable `verify` spec; the verifier classifies it
confirmed / regressed / pending / manual. Healthy fixtures must yield confirmed;
degraded fixtures (old symptom back) must yield regressed.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from portfolio_automation.applied_fix_verifier import (
    CONFIRMED,
    MANUAL,
    PENDING,
    REGRESSED,
    drop_resolved,
    summarize,
    verify_applied_fixes,
)


def _state(*fixes, applied_at=None):
    batch = {"date": "2026-05-30", "commit": "abc123", "fixes": list(fixes)}
    if applied_at is not None:
        batch["applied_at"] = applied_at
    return {"applied_fixes": [batch]}


class _Root:
    """Builds a throwaway artifacts root with outputs/latest/ files."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "outputs" / "latest").mkdir(parents=True)

    def write(self, rel, payload):
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload), encoding="utf-8")


class TestLivenessRowNotWarn(unittest.TestCase):
    def setUp(self):
        self.root = _Root()
        self.fix = {
            "id": "pulse_last_run_age_sla",
            "verify": {
                "kind": "liveness_row_not_warn",
                "row": "discovery_pulse.last_run_age",
                "regression_below_observed": 840,
            },
        }

    def test_ok_row_is_confirmed(self):
        self.root.write("outputs/latest/daily_run_status.json", {
            "content_liveness": [
                {"name": "discovery_pulse.last_run_age", "status": "ok", "observed": 600},
            ],
        })
        v = verify_applied_fixes(_state(self.fix), self.root.dir)
        self.assertEqual(v[0]["status"], CONFIRMED)

    def test_warn_below_new_threshold_is_regressed(self):
        # 600min < 840min but the row warns → the old 6h(360) threshold is back.
        self.root.write("outputs/latest/daily_run_status.json", {
            "content_liveness": [
                {"name": "discovery_pulse.last_run_age", "status": "warn", "observed": 600},
            ],
        })
        v = verify_applied_fixes(_state(self.fix), self.root.dir)
        self.assertEqual(v[0]["status"], REGRESSED)

    def test_warn_above_new_threshold_is_pending(self):
        # 900min > 840min → a genuinely missed cycle, not a regression of the fix.
        self.root.write("outputs/latest/daily_run_status.json", {
            "content_liveness": [
                {"name": "discovery_pulse.last_run_age", "status": "warn", "observed": 900},
            ],
        })
        v = verify_applied_fixes(_state(self.fix), self.root.dir)
        self.assertEqual(v[0]["status"], PENDING)

    def test_missing_row_is_pending(self):
        self.root.write("outputs/latest/daily_run_status.json", {"content_liveness": []})
        v = verify_applied_fixes(_state(self.fix), self.root.dir)
        self.assertEqual(v[0]["status"], PENDING)

    def test_missing_artifact_is_pending(self):
        v = verify_applied_fixes(_state(self.fix), self.root.dir)
        self.assertEqual(v[0]["status"], PENDING)


class TestArtifactMaxFieldGt(unittest.TestCase):
    def setUp(self):
        self.root = _Root()
        self.fix = {
            "id": "persistence_7d_daily_mode",
            "verify": {
                "kind": "artifact_max_field_gt",
                "artifact": "outputs/latest/theme_signals.json",
                "list_path": "themes",
                "field": "persistence_7d",
                "threshold": 0,
            },
        }

    def test_max_above_threshold_is_confirmed(self):
        self.root.write("outputs/latest/theme_signals.json", {
            "themes": [
                {"name": "Defense", "persistence_7d": 2},
                {"name": "Energy", "persistence_7d": 0},
            ],
        })
        v = verify_applied_fixes(_state(self.fix), self.root.dir)
        self.assertEqual(v[0]["status"], CONFIRMED)

    def test_all_zero_is_pending_not_regressed(self):
        # A genuine first-day-of-data reads 0 too, so 0 cannot prove regression.
        self.root.write("outputs/latest/theme_signals.json", {
            "themes": [{"name": "Defense", "persistence_7d": 0}],
        })
        v = verify_applied_fixes(_state(self.fix), self.root.dir)
        self.assertEqual(v[0]["status"], PENDING)

    def test_empty_list_is_pending(self):
        self.root.write("outputs/latest/theme_signals.json", {"themes": []})
        v = verify_applied_fixes(_state(self.fix), self.root.dir)
        self.assertEqual(v[0]["status"], PENDING)


class TestArtifactPredatesFix(unittest.TestCase):
    """A fix can only be judged against an artifact generated AFTER the fix
    went live. An older artifact (still reflecting the pre-fix code) must read
    pending, never regressed/confirmed — otherwise every fix false-alarms on
    its first run until the next pipeline regenerates artifacts."""

    def setUp(self):
        self.root = _Root()
        self.fix = {
            "id": "pulse_last_run_age_sla",
            "verify": {
                "kind": "liveness_row_not_warn",
                "row": "discovery_pulse.last_run_age",
                "regression_below_observed": 840,
            },
        }
        # Artifact generated BEFORE the fix was applied.
        self.root.write("outputs/latest/daily_run_status.json", {
            "generated_at": "2026-05-30T09:03:00+00:00",
            "content_liveness": [
                {"name": "discovery_pulse.last_run_age", "status": "warn", "observed": 603},
            ],
        })

    def test_stale_artifact_is_pending_not_regressed(self):
        v = verify_applied_fixes(_state(self.fix, applied_at="2026-05-30T17:00:00+00:00"), self.root.dir)
        self.assertEqual(v[0]["status"], PENDING)
        self.assertIn("predates", v[0]["detail"])

    def test_fresh_artifact_is_judged_normally(self):
        # Same artifact, but applied_at is BEFORE generated_at → judge it.
        v = verify_applied_fixes(_state(self.fix, applied_at="2026-05-30T06:00:00+00:00"), self.root.dir)
        self.assertEqual(v[0]["status"], REGRESSED)

    def test_no_applied_at_skips_staleness_guard(self):
        # Backward compatible: without applied_at, judge the artifact as-is.
        v = verify_applied_fixes(_state(self.fix), self.root.dir)
        self.assertEqual(v[0]["status"], REGRESSED)


class TestManualAndAggregates(unittest.TestCase):
    def setUp(self):
        self.root = _Root()

    def test_unknown_kind_is_manual(self):
        fix = {"id": "extended_watchlist_cross_day_gate", "verify": {"kind": "operator_eyeball"}}
        v = verify_applied_fixes(_state(fix), self.root.dir)
        self.assertEqual(v[0]["status"], MANUAL)

    def test_no_verify_block_is_manual(self):
        fix = {"id": "something"}
        v = verify_applied_fixes(_state(fix), self.root.dir)
        self.assertEqual(v[0]["status"], MANUAL)

    def test_empty_state_returns_empty(self):
        self.assertEqual(verify_applied_fixes({}, self.root.dir), [])

    def test_summarize_counts_by_status(self):
        verdicts = [
            {"id": "a", "status": CONFIRMED},
            {"id": "b", "status": PENDING},
            {"id": "c", "status": REGRESSED},
            {"id": "d", "status": MANUAL},
            {"id": "e", "status": CONFIRMED},
        ]
        s = summarize(verdicts)
        self.assertEqual(s[CONFIRMED], 2)
        self.assertEqual(s[PENDING], 1)
        self.assertEqual(s[REGRESSED], 1)
        self.assertEqual(s[MANUAL], 1)
        self.assertTrue(s["has_regression"])

    def test_drop_resolved_removes_confirmed_keeps_rest(self):
        state = _state(
            {"id": "a", "verify": {"kind": "x"}},
            {"id": "b", "verify": {"kind": "y"}},
            {"id": "c", "verify": {"kind": "z"}},
        )
        verdicts = [
            {"id": "a", "status": CONFIRMED},
            {"id": "b", "status": PENDING},
            {"id": "c", "status": REGRESSED},
        ]
        pruned = drop_resolved(state, verdicts)
        kept = {f["id"] for batch in pruned.get("applied_fixes", []) for f in batch.get("fixes", [])}
        self.assertEqual(kept, {"b", "c"})

    def test_drop_resolved_removes_empty_batches(self):
        state = _state({"id": "a", "verify": {"kind": "x"}})
        pruned = drop_resolved(state, [{"id": "a", "status": CONFIRMED}])
        self.assertEqual(pruned.get("applied_fixes"), [])


if __name__ == "__main__":
    unittest.main()
