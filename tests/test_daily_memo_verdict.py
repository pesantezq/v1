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

    def _summary(self, generated_at: str = "2026-05-19T01:00:00") -> dict:
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
