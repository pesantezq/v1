from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from gui_insight_cards import build_insight_card_models, render_insight_cards


def _structured_row(**overrides) -> dict:
    base = {
        "symbol": "NVDA",
        "decision": "BUY",
        "priority": 0.5623,
        "priority_score": 0.865,
        "recommended_allocation_pct": 0.04,
        "recommended_amount": 2000.0,
        "reason": "Legacy reason string.",
        "decision_reason": "Structured fallback string.",
        "risk_flags": [],
        "override_flags": [],
        "decision_reason_structured": {
            "decision": "BUY",
            "decision_type": "BUY",
            "band": "high_conviction",
            "strategy": "compounder",
            "drivers": {
                "conviction_score": 0.88,
                "signal_score": 0.82,
                "confidence_score": 0.91,
                "effective_score": 0.85,
                "priority_score": 0.865,
            },
            "allocation": {
                "recommended_allocation_pct": 0.04,
                "recommended_amount": 2000.0,
                "current_position_pct": 0.0,
                "available_cash_pct": 0.10,
            },
            "risk_flags": ["degraded_data"],
            "override_flags": ["low_confidence"],
            "why": ["Conviction band is high_conviction.", "Open NVDA position."],
            "what_would_change": ["Fresh live data would remove the degraded-data cap."],
            "watch_next": ["Recheck conviction and confidence on the next scan."],
        },
    }
    base.update(overrides)
    return base


class _FakeContainer:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeStreamlit:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def container(self):
        self.calls.append(("container", ""))
        return _FakeContainer()

    def markdown(self, text, **kwargs):
        self.calls.append(("markdown", str(text)))

    def caption(self, text):
        self.calls.append(("caption", str(text)))

    def write(self, text):
        self.calls.append(("write", str(text)))

    def divider(self):
        self.calls.append(("divider", ""))


class TestGuiInsightCards(unittest.TestCase):
    def test_build_models_runs_for_structured_rows(self):
        cards = build_insight_card_models([_structured_row()])
        self.assertEqual(1, len(cards))
        self.assertEqual("NVDA", cards[0]["symbol"])
        self.assertEqual("BUY", cards[0]["decision"])
        self.assertEqual("compounder", cards[0]["strategy"])

    def test_handles_missing_structured_reason(self):
        row = _structured_row(decision_reason_structured=None, decision_reason="Fallback reason.")
        cards = build_insight_card_models([row])
        self.assertEqual(["Fallback reason."], cards[0]["why"])
        self.assertEqual("unknown", cards[0]["strategy"])
        self.assertEqual("unknown", cards[0]["band"])

    def test_handles_empty_list(self):
        self.assertEqual([], build_insight_card_models([]))

    def test_does_not_mutate_input(self):
        rows = [_structured_row()]
        before = copy.deepcopy(rows)
        build_insight_card_models(rows)
        self.assertEqual(before, rows)

    def test_renders_without_error(self):
        fake_st = _FakeStreamlit()
        with patch("gui_insight_cards.st", fake_st):
            render_insight_cards([_structured_row()])
        joined = " ".join(text for _, text in fake_st.calls)
        self.assertIn("NVDA BUY", joined)
        self.assertIn("Why", joined)

    def test_render_handles_missing_structured_reason(self):
        fake_st = _FakeStreamlit()
        row = _structured_row(decision_reason_structured=None, decision_reason="Fallback reason.")
        with patch("gui_insight_cards.st", fake_st):
            render_insight_cards([row])
        joined = " ".join(text for _, text in fake_st.calls)
        self.assertIn("Fallback reason.", joined)

    def test_render_empty_list_does_not_crash(self):
        fake_st = _FakeStreamlit()
        with patch("gui_insight_cards.st", fake_st):
            render_insight_cards([])
        joined = " ".join(text for _, text in fake_st.calls)
        self.assertIn("No insight cards available.", joined)


if __name__ == "__main__":
    unittest.main()
