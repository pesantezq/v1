"""Phase 2B tests: context loader, advisory enricher, GUI loader, guardrails.

Artifact-only, observe-only, context-only. No FMP/HTTP/governor; no decision impact.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio_automation.crowd_intelligence.context_loader import load_crowd_context
from portfolio_automation.crowd_intelligence import advisory_context_enricher as enr
from gui_v2.data.dash_crowd_context import crowd_context_for

_ROOT = Path(__file__).resolve().parent.parent


def _write_artifacts(root: Path, *, generated_at: str, symbols: list[dict]):
    latest = root / "outputs" / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    (latest / "crowd_intelligence.json").write_text(json.dumps({
        "observe_only": True, "generated_at": generated_at, "symbols": symbols}))
    (latest / "crowd_intelligence_status.json").write_text(json.dumps({
        "observe_only": True, "disabled_categories": ["social_sentiment"]}))


def _sig(symbol, composite=0.0, conf=0.7, attention=0.0, analyst=0.0, recs=10):
    return {"symbol": symbol, "composite_crowd_score": composite, "confidence": conf,
            "category_scores": {"attention": attention, "analyst": analyst,
                                "news": 0.0, "insider": 0.0, "congress": 0.0,
                                "social_sentiment": 0.0},
            "enabled_sources": ["stock_grades"], "disabled_sources": ["social_sentiment_legacy"],
            "data_freshness": 0.9, "top_reasons": ["analyst: consensus +0.5"],
            "warnings": [], "source_records_count": recs}


class TestContextLoader(unittest.TestCase):
    def test_missing_artifact_safe(self):
        with tempfile.TemporaryDirectory() as td:
            c = load_crowd_context(td)
            self.assertFalse(c["available"])
            self.assertEqual(c["missing_reason"], "not_generated")
            self.assertEqual(c["by_symbol"], {})

    def test_unreadable_artifact_safe(self):
        with tempfile.TemporaryDirectory() as td:
            latest = Path(td) / "outputs" / "latest"
            latest.mkdir(parents=True)
            (latest / "crowd_intelligence.json").write_text("{ not json")
            c = load_crowd_context(td)
            self.assertFalse(c["available"])
            self.assertEqual(c["missing_reason"], "unreadable")

    def test_stale_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
            _write_artifacts(Path(td), generated_at=old, symbols=[_sig("AAPL")])
            c = load_crowd_context(td)
            self.assertTrue(c["available"])
            self.assertTrue(c["stale"])

    def test_fresh_ok_and_symbol_present(self):
        with tempfile.TemporaryDirectory() as td:
            now = datetime.now(timezone.utc).isoformat()
            _write_artifacts(Path(td), generated_at=now, symbols=[_sig("AAPL", composite=0.3)])
            c = load_crowd_context(td)
            self.assertTrue(c["available"])
            self.assertFalse(c["stale"])
            self.assertIn("AAPL", c["by_symbol"])


class TestEnricher(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(enr.context_label(None), "Insufficient Data")
        self.assertEqual(enr.context_label(_sig("X", conf=0.1)), "Insufficient Data")
        self.assertEqual(enr.context_label(_sig("X", recs=0)), "Insufficient Data")
        self.assertEqual(enr.context_label(_sig("X", attention=0.7)), "High Attention")
        self.assertEqual(enr.context_label(_sig("X", composite=0.5)), "Supportive")
        self.assertEqual(enr.context_label(_sig("X", composite=-0.5)), "Caution")
        self.assertEqual(enr.context_label(_sig("X", composite=0.0)), "Neutral")

    def test_all_lines_pass_forbidden_guard_over_fuzz(self):
        import itertools
        for comp, att, ana, sd in itertools.product(
                (-0.9, -0.2, 0.0, 0.2, 0.9), (0.0, 0.7), (-0.5, 0.0, 0.5), (True, False)):
            s = _sig("X", composite=comp, attention=att, analyst=ana)
            label = enr.context_label(s)
            for line in enr.enrich(s, label, social_disabled=sd):
                enr.assert_safe(line)  # raises ForbiddenPhraseError if unsafe

    def test_social_disabled_line_present(self):
        s = _sig("X", composite=0.3)
        lines = enr.enrich(s, "Supportive", social_disabled=True)
        self.assertTrue(any("social sentiment is unavailable" in ln.lower() for ln in lines))

    def test_labels_avoid_trade_words(self):
        bad = ("bullish", "bearish", "buy", "sell", "strong")
        for label in enr.LABELS:
            self.assertFalse(any(b in label.lower() for b in bad), label)

    def test_forbidden_guard_raises(self):
        with self.assertRaises(enr.ForbiddenPhraseError):
            enr.assert_safe("this is a strong buy signal")


class TestGuiLoader(unittest.TestCase):
    def test_missing_artifact_banner_and_safe_state(self):
        with tempfile.TemporaryDirectory() as td:
            cc = crowd_context_for(td, ["AAPL"])
            self.assertFalse(cc["status"]["available"])
            self.assertIn("not generated", cc["status"]["banner"])
            self.assertFalse(cc["by_symbol"]["AAPL"]["present"])
            self.assertEqual(cc["by_symbol"]["AAPL"]["label"], "Insufficient Data")

    def test_symbol_absent_message(self):
        with tempfile.TemporaryDirectory() as td:
            now = datetime.now(timezone.utc).isoformat()
            _write_artifacts(Path(td), generated_at=now, symbols=[_sig("AAPL")])
            cc = crowd_context_for(td, ["TSLA"])
            self.assertFalse(cc["by_symbol"]["TSLA"]["present"])
            self.assertTrue(any("No crowd context available" in ln
                                for ln in cc["by_symbol"]["TSLA"]["lines"]))

    def test_present_symbol_has_label_and_lines(self):
        with tempfile.TemporaryDirectory() as td:
            now = datetime.now(timezone.utc).isoformat()
            _write_artifacts(Path(td), generated_at=now, symbols=[_sig("AAPL", composite=0.4, analyst=0.5)])
            cc = crowd_context_for(td, ["AAPL"])
            row = cc["by_symbol"]["AAPL"]
            self.assertTrue(row["present"])
            self.assertEqual(row["label"], "Supportive")
            self.assertTrue(row["lines"])


class TestNoFmpAndNoDecisionImpact(unittest.TestCase):
    _2B_MODULES = [
        "portfolio_automation/crowd_intelligence/context_loader.py",
        "portfolio_automation/crowd_intelligence/advisory_context_enricher.py",
        "gui_v2/data/dash_crowd_context.py",
    ]

    def test_no_fmp_http_or_governor_in_2b_modules(self):
        for rel in self._2B_MODULES:
            src = (_ROOT / rel).read_text()
            for forbidden in ("FMPClient", "governed_client", "import urllib",
                              "import requests", "requests.", "get_json"):
                self.assertNotIn(forbidden, src, f"{rel} references {forbidden}")
            self.assertNotIn("decision_plan", src, f"{rel} references decision_plan")

    def test_enrichment_preserves_action_and_ticker(self):
        with tempfile.TemporaryDirectory() as td:
            now = datetime.now(timezone.utc).isoformat()
            _write_artifacts(Path(td), generated_at=now, symbols=[_sig("AAPL", composite=0.9)])
            decisions = [{"ticker": "AAPL", "action": "HOLD"}, {"ticker": "TSLA", "action": "BUY"}]
            cc = crowd_context_for(td, [d["ticker"] for d in decisions])
            for d in decisions:
                d["crowd_context"] = cc["by_symbol"].get(d["ticker"])
            self.assertEqual(decisions[0]["action"], "HOLD")  # crowd Supportive did NOT flip it
            self.assertEqual(decisions[1]["action"], "BUY")
            self.assertEqual(decisions[0]["ticker"], "AAPL")


class TestDailyWiringNonFatal(unittest.TestCase):
    def test_run_returns_status_dict_not_raises_on_bad_root(self):
        from portfolio_automation.crowd_intelligence.artifact_writer import run
        with tempfile.TemporaryDirectory() as td:
            out = run(td)  # no config.json -> empty universe, still returns a dict
            self.assertIsInstance(out, dict)
            self.assertTrue(out.get("observe_only"))


if __name__ == "__main__":
    unittest.main()
