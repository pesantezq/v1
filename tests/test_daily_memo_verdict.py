"""
Tests for watchlist_scanner.daily_memo._build_verdict.

Covers the mood-priority ladder + body composition. The verdict is a
single-line synthesis intended for the top of the memo, so the test
asserts each mood label maps to the expected output substring.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watchlist_scanner.daily_memo import _build_verdict


def _decisions(*specs):
    """Build a list of decision rows from (urgency, source) tuples."""
    return [{"urgency": u, "source": s, "decision": "SELL", "symbol": "ABC"}
            for u, s in specs]


class TestBuildVerdict(unittest.TestCase):

    def _summary(self, generated_at: str | None = None) -> dict:
        # Default to "right now" so the test isn't fragile to wall-clock time —
        # _freshness_banner fires at >=2 days old, which would silently flip
        # every verdict to "Stale" once the calendar advanced past a hardcoded
        # date by 2 days. (Pre-existing fragility caught 2026-05-21.)
        if generated_at is None:
            from datetime import datetime
            generated_at = datetime.now().isoformat(timespec="seconds")
        return {"generated_at": generated_at}

    def test_steady_when_no_urgent_actions(self):
        with tempfile.TemporaryDirectory() as td:
            verdict = _build_verdict(
                self._summary(),
                decision_rows=_decisions(("low", "portfolio")),
                capital_counts={"SELL": 0, "SCALE": 1, "BUY": 0},
                root=Path(td),
            )
            self.assertIn("Steady", verdict)
            self.assertIn("1 advisory action", verdict)

    def test_action_required_when_critical_urgency_present(self):
        with tempfile.TemporaryDirectory() as td:
            verdict = _build_verdict(
                self._summary(),
                decision_rows=_decisions(("critical", "structural")),
                capital_counts={"SELL": 1, "SCALE": 0, "BUY": 0},
                root=Path(td),
            )
            # critical-urgency wins over the structural-risk demotion path
            # because action_required has higher priority than structural_risk.
            self.assertIn("Action required", verdict)

    def test_structural_risk_when_structural_source_no_urgent(self):
        with tempfile.TemporaryDirectory() as td:
            verdict = _build_verdict(
                self._summary(),
                decision_rows=_decisions(("low", "structural")),
                capital_counts={"SELL": 0, "SCALE": 0, "BUY": 0},
                root=Path(td),
            )
            self.assertIn("Structural risk", verdict)

    def test_cautious_when_medium_urgency_present(self):
        with tempfile.TemporaryDirectory() as td:
            verdict = _build_verdict(
                self._summary(),
                decision_rows=_decisions(("medium", "market"), ("low", "portfolio")),
                capital_counts={"SELL": 0, "SCALE": 1, "BUY": 0},
                root=Path(td),
            )
            self.assertIn("Cautious", verdict)

    def test_stale_when_summary_is_old(self):
        # _freshness_banner fires at >= 2 days old.
        with tempfile.TemporaryDirectory() as td:
            verdict = _build_verdict(
                {"generated_at": "2026-04-01T00:00:00"},
                decision_rows=_decisions(("critical", "structural")),
                capital_counts={"SELL": 5, "SCALE": 0, "BUY": 0},
                root=Path(td),
            )
            self.assertIn("Stale", verdict)

    def test_risk_delta_near_cap_promotes_to_cautious(self):
        # Even with no medium-urgency decisions, a near_cap risk_delta
        # status pushes the verdict to cautious.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "outputs" / "latest").mkdir(parents=True)
            (root / "outputs" / "latest" / "risk_delta.json").write_text(
                '{"overall_status": "near_cap"}'
            )
            verdict = _build_verdict(
                self._summary(),
                decision_rows=_decisions(("low", "portfolio")),
                capital_counts={"SELL": 0, "SCALE": 0, "BUY": 0},
                root=root,
            )
            self.assertIn("Cautious", verdict)
            self.assertIn("near a cap", verdict)

    def test_retune_milestone_appended_when_n_ge_30_and_delta_ge_10pp(self):
        # When current-fp has crossed n=30 and the hit-rate delta exceeds
        # 10pp, the verdict gets a trailing "Retune validated" callout
        # regardless of mood (except stale).
        import json
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "outputs" / "latest").mkdir(parents=True)
            (root / "outputs" / "latest" / "retune_impact.json").write_text(json.dumps({
                "current_fingerprint": "fp_new",
                "outcome_attribution": {
                    "available": True,
                    "pre_tracker_label": "pre_tracker_unknown",
                    "by_fingerprint": {
                        "fp_new": {"resolved_1d": 44, "hit_rate_1d": 0.795},
                        "pre_tracker_unknown": {"resolved_1d": 352, "hit_rate_1d": 0.406},
                    },
                },
            }))
            verdict = _build_verdict(
                self._summary(),
                decision_rows=_decisions(("low", "portfolio")),
                capital_counts={"SELL": 0, "SCALE": 0, "BUY": 0},
                root=root,
            )
            self.assertIn("Steady", verdict)
            self.assertIn("Retune validated", verdict)
            self.assertIn("n=44", verdict)

    def test_retune_milestone_underperforming_when_negative_delta(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "outputs" / "latest").mkdir(parents=True)
            (root / "outputs" / "latest" / "retune_impact.json").write_text(json.dumps({
                "current_fingerprint": "fp_new",
                "outcome_attribution": {
                    "available": True,
                    "pre_tracker_label": "pre_tracker_unknown",
                    "by_fingerprint": {
                        "fp_new": {"resolved_1d": 35, "hit_rate_1d": 0.20},
                        "pre_tracker_unknown": {"resolved_1d": 352, "hit_rate_1d": 0.40},
                    },
                },
            }))
            verdict = _build_verdict(
                self._summary(),
                decision_rows=_decisions(("low", "portfolio")),
                capital_counts={"SELL": 0, "SCALE": 0, "BUY": 0},
                root=root,
            )
            self.assertIn("Retune underperforming", verdict)

    def test_retune_milestone_suppressed_when_n_below_30(self):
        import json
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "outputs" / "latest").mkdir(parents=True)
            (root / "outputs" / "latest" / "retune_impact.json").write_text(json.dumps({
                "current_fingerprint": "fp_new",
                "outcome_attribution": {
                    "available": True,
                    "pre_tracker_label": "pre_tracker_unknown",
                    "by_fingerprint": {
                        "fp_new": {"resolved_1d": 22, "hit_rate_1d": 0.795},
                        "pre_tracker_unknown": {"resolved_1d": 352, "hit_rate_1d": 0.406},
                    },
                },
            }))
            verdict = _build_verdict(
                self._summary(),
                decision_rows=_decisions(("low", "portfolio")),
                capital_counts={"SELL": 0, "SCALE": 0, "BUY": 0},
                root=root,
            )
            self.assertNotIn("Retune validated", verdict)
            self.assertNotIn("Retune underperforming", verdict)

    def test_risk_delta_breach_promotes_to_structural_risk(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "outputs" / "latest").mkdir(parents=True)
            (root / "outputs" / "latest" / "risk_delta.json").write_text(
                '{"overall_status": "breach"}'
            )
            verdict = _build_verdict(
                self._summary(),
                decision_rows=_decisions(("low", "portfolio")),
                capital_counts={"SELL": 0, "SCALE": 0, "BUY": 0},
                root=root,
            )
            self.assertIn("Structural risk", verdict)


if __name__ == "__main__":
    unittest.main(verbosity=2)
