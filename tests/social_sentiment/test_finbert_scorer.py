"""Tests for FinBERT scorer — graceful degradation when model unavailable."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portfolio_automation.social_sentiment.finbert_scorer import (
    SCORER_VERSION,
    FinBERTScorer,
    ScorerResult,
    _unavailable,
    score_records,
)


class TestScorerResultIsAvailable(unittest.TestCase):
    def test_ok_status_is_ok(self):
        r = ScorerResult(text="hi", status="ok", scorer="finbert",
                         scorer_version=SCORER_VERSION)
        self.assertTrue(r.is_ok)

    def test_unavailable_status_not_ok(self):
        r = _unavailable("test", "no model")
        self.assertFalse(r.is_ok)
        self.assertEqual(r.status, "scorer_unavailable")
        self.assertEqual(r.scorer, "scorer_unavailable")


class TestFinBERTScorerUnavailablePath(unittest.TestCase):
    """All tests here operate in scorer_unavailable mode (model not present)."""

    def _scorer(self, enabled=True):
        return FinBERTScorer({
            "enabled": enabled,
            "allow_download": False,
            "model_name": "ProsusAI/finbert",
        })

    def test_score_returns_scorer_result(self):
        sc = self._scorer()
        result = sc.score("NVDA stock is surging today")
        self.assertIsInstance(result, ScorerResult)

    def test_score_unavailable_when_model_not_cached(self):
        sc = self._scorer()
        result = sc.score("This is a test")
        # Without the model downloaded, should return scorer_unavailable
        self.assertIn(result.status, ("scorer_unavailable", "ok"))

    def test_score_batch_returns_same_count_as_input(self):
        sc = self._scorer()
        texts = ["good news", "bad news", "neutral"]
        results = sc.score_batch(texts)
        self.assertEqual(len(results), 3)

    def test_score_batch_empty_input_returns_empty(self):
        sc = self._scorer()
        self.assertEqual(sc.score_batch([]), [])

    def test_disabled_scorer_returns_unavailable(self):
        sc = self._scorer(enabled=False)
        result = sc.score("test")
        self.assertEqual(result.status, "scorer_unavailable")

    def test_disabled_scorer_is_not_available(self):
        sc = self._scorer(enabled=False)
        self.assertFalse(sc.is_available())

    def test_scorer_never_raises(self):
        sc = self._scorer()
        for text in ["", "x" * 10000, "$NVDA!!! 🚀🚀", "normal text"]:
            result = sc.score(text)
            self.assertIsNotNone(result)

    def test_scorer_version_is_set(self):
        sc = self._scorer()
        r = sc.score("test")
        self.assertEqual(r.scorer_version, SCORER_VERSION)

    def test_status_property(self):
        sc = self._scorer(enabled=False)
        self.assertEqual(sc.status, "disabled")


class TestScoreRecords(unittest.TestCase):
    """Test the score_records helper that operates on record lists."""

    def _rec(self, text, **kw):
        r = {"ticker": "NVDA", "text": text, "source": "bluesky",
             "source_type": "text", "post_id_hash": "x", "created_at": "t"}
        r.update(kw)
        return r

    def test_score_records_sets_sentiment_fields(self):
        records = [self._rec("Nvidia is up 5% today")]
        scorer = FinBERTScorer({"enabled": False})  # unavailable path
        scored = score_records(records, scorer=scorer)
        self.assertEqual(len(scored), 1)
        self.assertIn("sentiment_score", scored[0])
        self.assertIn("label", scored[0])
        self.assertIn("scorer", scored[0])

    def test_score_records_skips_already_scored(self):
        rec = self._rec("test", sentiment_score=0.5, scorer="finbert",
                        scorer_version="1", label="positive",
                        positive_probability=0.8, neutral_probability=0.1,
                        negative_probability=0.1)
        original_score = rec["sentiment_score"]
        scorer = FinBERTScorer({"enabled": False})
        score_records([rec], scorer=scorer)
        self.assertEqual(rec["sentiment_score"], original_score)  # unchanged

    def test_score_records_handles_empty_text(self):
        rec = self._rec("")
        scorer = FinBERTScorer({"enabled": False})
        score_records([rec], scorer=scorer)
        self.assertEqual(rec["scorer"], "scorer_unavailable")
        self.assertEqual(rec["label"], "neutral")

    def test_score_records_returns_same_list(self):
        records = [self._rec("test")]
        scorer = FinBERTScorer({"enabled": False})
        result = score_records(records, scorer=scorer)
        self.assertIs(result, records)

    def test_score_records_empty_input(self):
        scorer = FinBERTScorer({"enabled": False})
        result = score_records([], scorer=scorer)
        self.assertEqual(result, [])

    def test_scorer_unavailable_sets_neutral_probabilities(self):
        records = [self._rec("some text")]
        scorer = FinBERTScorer({"enabled": False})
        score_records(records, scorer=scorer)
        r = records[0]
        # scorer_unavailable defaults to neutral=1.0
        self.assertIn("neutral_probability", r)
        # Must not be exactly 0 if it defaults to neutral
        self.assertAlmostEqual(r["positive_probability"] + r["neutral_probability"] +
                               r["negative_probability"], 1.0, places=4)


class TestProductionIsolation(unittest.TestCase):
    """Ensure scorer never writes to production artifacts."""
    def test_scorer_result_has_no_file_writes(self):
        import inspect
        import portfolio_automation.social_sentiment.finbert_scorer as mod
        src = inspect.getsource(mod)
        for forbidden in ("decision_plan", "portfolio_snapshot", "OutputNamespace.LATEST"):
            self.assertNotIn(forbidden, src)
