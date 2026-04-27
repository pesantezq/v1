"""
Tests for the upgraded theme_discovery package.

Covers:
  A. extractor — empty input, phrase extraction, stopword/junk filtering
  B. scorer    — stable sort, persistence lift, acceleration, single-source penalty
  C. history   — missing file, malformed file, max_runs cap
  D. CLI/main  — dry-run no writes, full run writes both files, empty-input JSON

All tests are offline (no network, no feedparser, no Streamlit).
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from theme_discovery.models import Article, ArticleSignal, ExtractResult, ThemeOpportunity
from theme_discovery.extractor import (
    extract, _normalize, _tokenize, _keep_phrase, _extract_tickers,
    _extract_phrase_frequencies, _phrase_in_normalized,
)
from theme_discovery.scorer import score, _age_hours, _recency_weight, _score_group
from theme_discovery.history import (
    load_theme_history, update_theme_history, compute_history_metrics,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _article(
    title: str,
    summary: str = "",
    domain: str = "marketwatch.com",
    age_hours: float = 1.0,
) -> Article:
    published = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()
    return Article(
        title=title,
        summary=summary,
        link=f"https://{domain}/article",
        published=published,
        source_domain=domain,
        item_hash=title[:16],
    )


def _empty_history() -> dict:
    return {"runs": []}


def _history_with_runs(themes_per_run: list[list[dict]]) -> dict:
    runs = []
    for i, themes in enumerate(themes_per_run):
        ts = (datetime.now(timezone.utc) - timedelta(hours=(len(themes_per_run) - i))).isoformat()
        runs.append({"generated_at": ts, "themes": themes})
    return {"runs": runs}


def _opp(name: str, theme_type: str = "classified", mention_count: int = 3) -> ThemeOpportunity:
    return ThemeOpportunity(
        name=name, theme_type=theme_type, score=0.5, confidence=0.4,
        mention_count=mention_count, unique_ticker_count=1, tickers=["NVDA"],
        evidence=["headline"], source_count=2, persistence_score=0.5,
        acceleration_score=0.5, recency_score=0.8, diversity_score=0.5,
        history_runs_seen=3, first_seen=None, last_seen=None,
    )


# ===========================================================================
# A. Extractor tests
# ===========================================================================

class TestExtractorEmpty(unittest.TestCase):
    def test_empty_articles_returns_empty_result(self):
        result = extract([])
        self.assertEqual(result.classified, {})
        self.assertEqual(result.emerging, {})

    def test_extract_result_type(self):
        result = extract([])
        self.assertIsInstance(result, ExtractResult)


class TestExtractorPhraseExtraction(unittest.TestCase):
    def _articles_with_phrase(self, phrase: str, n: int = 3) -> list[Article]:
        return [_article(f"{phrase} drives gains") for _ in range(n)]

    def test_repeated_phrase_appears_in_emerging(self):
        arts = self._articles_with_phrase("ai infrastructure", n=3)
        result = extract(arts, min_phrase_freq=2)
        self.assertIn("ai infrastructure", result.emerging)

    def test_single_occurrence_below_threshold_excluded(self):
        arts = [_article("ai infrastructure drives gains")] + [_article("unrelated news") for _ in range(3)]
        result = extract(arts, min_phrase_freq=2)
        self.assertNotIn("ai infrastructure", result.emerging)

    def test_phrase_group_contains_correct_articles(self):
        phrase_arts = [_article("data center demand rises") for _ in range(3)]
        other = _article("oil prices surge today")
        result = extract(phrase_arts + [other], min_phrase_freq=2)
        group = result.emerging.get("data center")
        self.assertIsNotNone(group)
        # Only the 3 phrase articles should be in the group, not the "oil" one
        titles = {sig.article.title for sig in group}
        self.assertNotIn(other.title, titles)

    def test_trigram_extraction(self):
        arts = [_article("nuclear power plant expansion plans") for _ in range(3)]
        result = extract(arts, min_phrase_freq=2)
        # "nuclear power plant" or "power plant expansion" should appear
        found = any("nuclear" in p or "power plant" in p for p in result.emerging)
        self.assertTrue(found, f"expected nuclear/power phrase in {list(result.emerging)}")

    def test_multi_source_tickers_captured(self):
        arts = [
            _article("NVDA leads ai infrastructure rally", domain="marketwatch.com"),
            _article("NVDA and AMD ai infrastructure gains", domain="finance.yahoo.com"),
            _article("ai infrastructure build out continues with NVDA", domain="reuters.com"),
        ]
        result = extract(arts, min_phrase_freq=2)
        group = result.emerging.get("ai infrastructure", [])
        tickers = {t for sig in group for t in sig.tickers_found}
        self.assertIn("NVDA", tickers)


class TestExtractorFiltering(unittest.TestCase):
    def test_stopword_only_bigram_excluded(self):
        arts = [_article("and the market moves on") for _ in range(3)]
        result = extract(arts, min_phrase_freq=2)
        # "and the", "the market" are stopword-dominated or blocked
        self.assertNotIn("and the", result.emerging)

    def test_blocked_phrase_excluded(self):
        arts = [_article("stock market rally continues today") for _ in range(3)]
        result = extract(arts, min_phrase_freq=2)
        self.assertNotIn("stock market", result.emerging)

    def test_single_char_token_phrase_excluded(self):
        # "a" in a phrase should cause it to be dropped
        self.assertFalse(_keep_phrase(["a", "market"]))

    def test_all_numeric_non_stop_excluded(self):
        self.assertFalse(_keep_phrase(["10", "20"]))

    def test_good_phrase_passes(self):
        self.assertTrue(_keep_phrase(["ai", "infrastructure"]))
        self.assertTrue(_keep_phrase(["data", "center"]))
        self.assertTrue(_keep_phrase(["nuclear", "restart"]))
        self.assertTrue(_keep_phrase(["grid", "expansion"]))

    def test_normalize_strips_punctuation(self):
        self.assertEqual(_normalize("Hello, World!"), "hello world")
        self.assertEqual(_normalize("AI-driven data-center"), "ai driven data center")

    def test_ticker_extraction_filters_non_universe(self):
        tickers = _extract_tickers("The CEO of NVDA and a CEO from AAPL spoke")
        self.assertIn("NVDA", tickers)
        self.assertIn("AAPL", tickers)
        self.assertNotIn("CEO", tickers)

    def test_classified_path_still_works(self):
        arts = [
            _article("Federal Reserve rate cut signals inflation concern"),
            _article("Fed monetary policy fomc meeting yield curve"),
        ]
        result = extract(arts)
        # "Inflation" and "Interest Rates" themes should fire
        self.assertTrue(
            any(k in result.classified for k in ("Inflation", "Interest Rates")),
            f"expected inflation/rate theme in {list(result.classified)}",
        )


# ===========================================================================
# B. Scorer tests
# ===========================================================================

class TestScorerBasic(unittest.TestCase):
    def _make_result(self, classified=None, emerging=None) -> ExtractResult:
        return ExtractResult(
            classified=classified or {},
            emerging=emerging or {},
        )

    def _make_signals(self, n: int, domain_prefix: str = "site", age_hours: float = 2.0) -> list[ArticleSignal]:
        return [
            ArticleSignal(
                article=_article(f"headline {i}", domain=f"{domain_prefix}{i}.com", age_hours=age_hours),
                theme_score=0.8,
                tickers_found=["NVDA"] if i == 0 else [],
            )
            for i in range(n)
        ]

    def test_empty_returns_empty(self):
        result = score(self._make_result(), _empty_history())
        self.assertEqual(result, [])

    def test_output_sorted_by_score_desc(self):
        classified = {
            "AI": self._make_signals(8, domain_prefix="a", age_hours=1.0),
            "Crypto": self._make_signals(2, domain_prefix="b", age_hours=60.0),
        }
        opps = score(self._make_result(classified=classified), _empty_history(), top_n=10)
        scores = [o.score for o in opps]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_scores_clamped_to_0_1(self):
        classified = {"AI": self._make_signals(20, age_hours=0.1)}
        opps = score(self._make_result(classified=classified), _empty_history())
        for o in opps:
            self.assertGreaterEqual(o.score, 0.0)
            self.assertLessEqual(o.score, 1.0)
            self.assertGreaterEqual(o.confidence, 0.0)
            self.assertLessEqual(o.confidence, 1.0)

    def test_top_n_respected(self):
        classified = {f"Theme{i}": self._make_signals(3) for i in range(10)}
        opps = score(self._make_result(classified=classified), _empty_history(), top_n=3)
        self.assertLessEqual(len(opps), 3)

    def test_to_dict_keys_present(self):
        classified = {"AI": self._make_signals(3)}
        opps = score(self._make_result(classified=classified), _empty_history())
        self.assertTrue(opps)
        d = opps[0].to_dict()
        for key in ("name", "theme_type", "score", "confidence", "mention_count",
                    "tickers", "evidence", "source_count", "persistence_score",
                    "acceleration_score", "recency_score", "diversity_score",
                    "history_runs_seen", "first_seen", "last_seen", "theme"):
            self.assertIn(key, d, f"missing key {key!r} in to_dict()")

    def test_backward_compat_theme_key(self):
        classified = {"Semiconductors": self._make_signals(3)}
        opps = score(self._make_result(classified=classified), _empty_history())
        self.assertEqual(opps[0].to_dict()["theme"], opps[0].to_dict()["name"])

    def test_evidence_deduplicated(self):
        # All signals point to the same title
        sigs = [
            ArticleSignal(article=_article("same title"), theme_score=0.9)
            for _ in range(5)
        ]
        classified = {"AI": sigs}
        opps = score(self._make_result(classified=classified), _empty_history())
        evidence = opps[0].evidence
        self.assertEqual(len(evidence), len(set(evidence)))

    def test_only_classified_present(self):
        classified = {"Defense": self._make_signals(3)}
        result = score(self._make_result(classified=classified), _empty_history())
        self.assertTrue(result)
        self.assertEqual(result[0].theme_type, "classified")

    def test_only_emerging_present(self):
        emerging = {"ai chip": self._make_signals(3)}
        result = score(self._make_result(emerging=emerging), _empty_history())
        self.assertTrue(result)
        self.assertEqual(result[0].theme_type, "emerging")


class TestScorerPersistence(unittest.TestCase):
    def _signals(self, n: int = 4) -> list[ArticleSignal]:
        return [
            ArticleSignal(article=_article(f"h{i}", domain=f"d{i}.com"), theme_score=0.8)
            for i in range(n)
        ]

    def test_persistent_theme_scores_higher(self):
        # theme_a appeared in 8 of last 10 runs; theme_b appeared in 1
        history_a = _history_with_runs(
            [
                [{"name": "theme_a", "theme_type": "classified",
                  "score": 0.7, "mention_count": 5, "source_count": 2}]
                if i < 8 else []
                for i in range(10)
            ]
        )
        history_b = _history_with_runs(
            [
                [{"name": "theme_b", "theme_type": "classified",
                  "score": 0.7, "mention_count": 5, "source_count": 2}]
                if i == 0 else []
                for i in range(10)
            ]
        )
        from theme_discovery.history import compute_history_metrics
        ma = compute_history_metrics("theme_a", "classified", history_a)
        mb = compute_history_metrics("theme_b", "classified", history_b)
        self.assertGreater(ma["persistence_score"], mb["persistence_score"])

    def test_persistence_score_in_opportunity(self):
        history = _history_with_runs(
            [
                [{"name": "AI", "theme_type": "classified",
                  "score": 0.7, "mention_count": 5, "source_count": 2}]
                for _ in range(7)
            ] + [[] for _ in range(3)]
        )
        classified = {"AI": self._signals()}
        opps = score(ExtractResult(classified=classified, emerging={}), history)
        self.assertGreater(opps[0].persistence_score, 0.0)


class TestScorerAcceleration(unittest.TestCase):
    def _signals(self, n: int = 3) -> list[ArticleSignal]:
        return [
            ArticleSignal(article=_article(f"h{i}", domain=f"d{i}.com"), theme_score=0.9)
            for i in range(n)
        ]

    def test_accelerating_theme_higher_than_decelerating(self):
        def _run(cnt):
            return [{"name": "T", "theme_type": "classified",
                     "score": 0.4, "mention_count": cnt, "source_count": 1}]

        # accelerating: low prior (5 runs), high recent (3 runs)
        history_accel = _history_with_runs(
            [_run(2), _run(2), _run(2), _run(2), _run(2), _run(8), _run(10), _run(12)]
        )
        # decelerating: high prior (5 runs), low recent (3 runs)
        history_decel = _history_with_runs(
            [_run(8), _run(10), _run(12), _run(10), _run(8), _run(2), _run(2), _run(2)]
        )
        from theme_discovery.history import compute_history_metrics
        ma = compute_history_metrics("T", "classified", history_accel)
        md = compute_history_metrics("T", "classified", history_decel)
        self.assertGreater(ma["acceleration_score"], md["acceleration_score"])


class TestScorerSingleSourcePenalty(unittest.TestCase):
    def _single_source_emerging(self) -> ExtractResult:
        sigs = [
            ArticleSignal(article=_article("ai chip demand", domain="singlesite.com"),
                          theme_score=1.0)
            for _ in range(2)
        ]
        return ExtractResult(classified={}, emerging={"ai chip": sigs})

    def _multi_source_emerging(self) -> ExtractResult:
        sigs = [
            ArticleSignal(article=_article("ai chip demand", domain=f"site{i}.com"),
                          theme_score=1.0)
            for i in range(3)
        ]
        return ExtractResult(classified={}, emerging={"ai chip": sigs})

    def test_single_source_has_lower_confidence(self):
        single_opps = score(self._single_source_emerging(), _empty_history())
        multi_opps = score(self._multi_source_emerging(), _empty_history())
        self.assertLess(single_opps[0].confidence, multi_opps[0].confidence)


# ===========================================================================
# C. History tests
# ===========================================================================

class TestHistoryLoad(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "nonexistent.json"
            h = load_theme_history(path)
            self.assertEqual(h, {"runs": []})

    def test_malformed_json_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.json"
            path.write_text("{ this is not json", encoding="utf-8")
            h = load_theme_history(path)
            self.assertEqual(h, {"runs": []})

    def test_wrong_schema_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "wrong.json"
            path.write_text(json.dumps({"data": [1, 2, 3]}), encoding="utf-8")
            h = load_theme_history(path)
            self.assertEqual(h, {"runs": []})

    def test_valid_history_loaded_correctly(self):
        data = {"runs": [{"generated_at": "2026-01-01T00:00:00", "themes": []}]}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "history.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            h = load_theme_history(path)
            self.assertEqual(len(h["runs"]), 1)


class TestHistoryUpdate(unittest.TestCase):
    def test_appends_run(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "history.json"
            opp = _opp("AI", mention_count=5)
            update_theme_history(path, "2026-01-01T00:00:00", [opp])
            h = load_theme_history(path)
            self.assertEqual(len(h["runs"]), 1)
            self.assertEqual(h["runs"][0]["themes"][0]["name"], "AI")

    def test_max_runs_cap(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "history.json"
            for i in range(5):
                update_theme_history(path, f"2026-01-0{i+1}T00:00:00", [_opp("AI")], max_runs=3)
            h = load_theme_history(path)
            self.assertLessEqual(len(h["runs"]), 3)

    def test_snapshot_structure(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "history.json"
            opp = _opp("Semiconductors", theme_type="classified", mention_count=7)
            opp.source_count = 3
            update_theme_history(path, "2026-04-23T12:00:00", [opp])
            h = load_theme_history(path)
            t = h["runs"][0]["themes"][0]
            self.assertEqual(t["name"], "Semiconductors")
            self.assertEqual(t["theme_type"], "classified")
            self.assertEqual(t["mention_count"], 7)


class TestHistoryMetrics(unittest.TestCase):
    def _runs(self, present_flags: list[bool], mentions: list[int] | None = None) -> dict:
        runs = []
        for i, present in enumerate(present_flags):
            ts = f"2026-01-{i+1:02d}T00:00:00"
            themes = []
            if present:
                count = mentions[i] if mentions else 5
                themes = [{"name": "AI", "theme_type": "classified",
                           "score": 0.7, "mention_count": count, "source_count": 2}]
            runs.append({"generated_at": ts, "themes": themes})
        return {"runs": runs}

    def test_empty_history_returns_neutral(self):
        m = compute_history_metrics("AI", "classified", _empty_history())
        self.assertEqual(m["persistence_score"], 0.0)
        self.assertEqual(m["acceleration_score"], 0.5)
        self.assertIsNone(m["first_seen"])

    def test_persistent_theme_high_score(self):
        h = self._runs([True] * 10)
        m = compute_history_metrics("AI", "classified", h)
        self.assertAlmostEqual(m["persistence_score"], 1.0)

    def test_one_of_ten_low_score(self):
        flags = [True] + [False] * 9
        h = self._runs(flags)
        m = compute_history_metrics("AI", "classified", h)
        self.assertLessEqual(m["persistence_score"], 0.2)

    def test_first_last_seen_populated(self):
        h = self._runs([False, True, True, False, True])
        m = compute_history_metrics("AI", "classified", h)
        self.assertIsNotNone(m["first_seen"])
        self.assertIsNotNone(m["last_seen"])
        self.assertLess(m["first_seen"], m["last_seen"])

    def test_history_runs_seen(self):
        h = self._runs([True, False, True, True, False])
        m = compute_history_metrics("AI", "classified", h)
        self.assertEqual(m["history_runs_seen"], 3)

    def test_acceleration_neutral_no_prior(self):
        # Only 2 runs — not enough for a prior window of 5
        h = self._runs([True, True])
        m = compute_history_metrics("AI", "classified", h, recent_window=3, prior_window=5)
        self.assertEqual(m["acceleration_score"], 0.5)

    def test_acceleration_rising(self):
        # prior (5 runs): 2 mentions each; recent (3 runs): 10 mentions each
        flags = [True] * 8
        counts = [2, 2, 2, 2, 2, 10, 10, 10]
        h = self._runs(flags, mentions=counts)
        m = compute_history_metrics("AI", "classified", h, recent_window=3, prior_window=5)
        self.assertGreater(m["acceleration_score"], 0.5)

    def test_acceleration_falling(self):
        flags = [True] * 8
        counts = [10, 10, 10, 10, 10, 2, 2, 2]
        h = self._runs(flags, mentions=counts)
        m = compute_history_metrics("AI", "classified", h, recent_window=3, prior_window=5)
        self.assertLess(m["acceleration_score"], 0.5)

    def test_brand_new_theme_neutral_acceleration(self):
        # Theme not in history at all
        h = self._runs([True, True, True])  # different theme "X"
        m = compute_history_metrics("BRAND_NEW", "emerging", h)
        self.assertEqual(m["acceleration_score"], 0.5)
        self.assertEqual(m["history_runs_seen"], 0)


# ===========================================================================
# D. CLI / main tests
# ===========================================================================

class TestCLIMain(unittest.TestCase):
    def _empty_collect_patch(self):
        return patch("theme_discovery.__main__.collect_articles", return_value=[])

    def test_dry_run_empty_input_valid_json(self):
        import io
        from theme_discovery.__main__ import main

        with self._empty_collect_patch():
            buf = io.StringIO()
            with patch("builtins.print") as mock_print:
                ret = main(["--dry-run"])
            self.assertEqual(ret, 0)
            # Capture what was printed
            printed = mock_print.call_args[0][0]
            data = json.loads(printed)
            self.assertIn("themes", data)
            self.assertIn("generated_at", data)
            self.assertIn("theme_count", data)
            self.assertEqual(data["themes"], [])

    def test_dry_run_does_not_write_history(self):
        from theme_discovery.__main__ import main

        with tempfile.TemporaryDirectory() as d:
            hist_path = Path(d) / "history.json"
            with self._empty_collect_patch(), \
                 patch("theme_discovery.__main__._HISTORY_PATH", hist_path):
                main(["--dry-run"])
            self.assertFalse(hist_path.exists(), "dry-run must not write history")

    def test_dry_run_does_not_write_output(self):
        from theme_discovery.__main__ import main

        with tempfile.TemporaryDirectory() as d:
            out_path = Path(d) / "theme_opportunities.json"
            with self._empty_collect_patch(), \
                 patch("theme_discovery.__main__._OUTPUT_PATH", out_path):
                main(["--dry-run"])
            self.assertFalse(out_path.exists(), "dry-run must not write output")

    def test_full_run_writes_output_file(self):
        from theme_discovery.__main__ import main

        with tempfile.TemporaryDirectory() as d:
            out_path = Path(d) / "latest" / "theme_opportunities.json"
            hist_path = Path(d) / "history" / "theme_history.json"
            with self._empty_collect_patch(), \
                 patch("theme_discovery.__main__._OUTPUT_PATH", out_path), \
                 patch("theme_discovery.__main__._HISTORY_PATH", hist_path):
                ret = main([])
            self.assertEqual(ret, 0)
            self.assertTrue(out_path.exists())
            data = json.loads(out_path.read_text())
            self.assertIn("themes", data)
            self.assertIn("theme_count", data)

    def test_full_run_writes_history_file(self):
        from theme_discovery.__main__ import main

        with tempfile.TemporaryDirectory() as d:
            out_path = Path(d) / "latest" / "theme_opportunities.json"
            hist_path = Path(d) / "history" / "theme_history.json"
            with self._empty_collect_patch(), \
                 patch("theme_discovery.__main__._OUTPUT_PATH", out_path), \
                 patch("theme_discovery.__main__._HISTORY_PATH", hist_path):
                main([])
            self.assertTrue(hist_path.exists())
            h = json.loads(hist_path.read_text())
            self.assertIn("runs", h)

    def test_full_run_with_articles_produces_themes(self):
        from theme_discovery.__main__ import main

        arts = [
            _article("NVIDIA AI chip demand surges on data center build", age_hours=1),
            _article("AI chip shortage affects hyperscaler plans", domain="reuters.com", age_hours=2),
            _article("Federal Reserve signals rate cut amid inflation data", age_hours=1),
        ]
        with tempfile.TemporaryDirectory() as d:
            out_path = Path(d) / "latest" / "theme_opportunities.json"
            hist_path = Path(d) / "history" / "theme_history.json"
            with patch("theme_discovery.__main__.collect_articles", return_value=arts), \
                 patch("theme_discovery.__main__._OUTPUT_PATH", out_path), \
                 patch("theme_discovery.__main__._HISTORY_PATH", hist_path):
                ret = main([])
            self.assertEqual(ret, 0)
            data = json.loads(out_path.read_text())
            self.assertGreater(data["theme_count"], 0)
            theme_names = {t["name"] for t in data["themes"]}
            # At minimum, AI or Inflation should fire
            self.assertTrue(
                theme_names & {"AI", "Inflation", "Interest Rates", "Semiconductors"},
                f"expected at least one known theme, got {theme_names}",
            )


if __name__ == "__main__":
    unittest.main()
