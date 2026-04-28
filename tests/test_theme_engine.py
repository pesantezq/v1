"""
Offline tests for the theme engine.

All tests run without Ollama and without any network calls.
Integration test (TestOllamaIntegration) is skipped unless
STOCKBOT_ENABLE_OLLAMA_TEST=1 is set in the environment.

Test classes:
    TestRSSCollector         — feed parsing, dedup, summary truncation
    TestThemeDetector        — testing_mode mock, JSON parsing, retry
    TestThemeMapper          — catalog match, synonym match, direct mentions
    TestThemeStore           — SQLite persistence, JSON output files
    TestApplyThemeBoosts     — boost calculation, caps, low-confidence gate
    TestScannerIntegration   — scanner stability without theme signals
    TestOllamaIntegration    — real Ollama call (gated by env var)
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure project root is on sys.path
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from theme_engine.rss_collector import RSSCollector, _item_hash, _truncate
from theme_engine.__main__ import _resolve_theme_task_context
from theme_engine.theme_detector import ThemeDetector, MOCK_THEMES
from theme_engine.theme_mapper import ThemeMapper, _normalize
from theme_engine.theme_store import ThemeStore
from scanner.candidate_scanner import apply_theme_boosts


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _candidate(symbol: str, score: float = 60.0, sector: str = "Technology") -> dict:
    return {
        "symbol": symbol,
        "score": score,
        "sector": sector,
        "mkt_cap": 1e11,
        "rev_growth": 0.20,
        "fcf_yield": 0.03,
        "roe": 0.25,
        "pe": 25.0,
        "price": 300.0,
        "price_200dma": 280.0,
        "above_200dma": True,
        "reasons": "RevGrowth 20%",
        "scanned_at": "2026-03-03T12:00:00",
        "theme_boost": 0,
        "theme_names": "",
    }


def _fake_entry(title: str, link: str, summary: str = "", published: str = "Mon, 03 Mar 2026") -> MagicMock:
    """Create a feedparser-like entry mock."""
    entry = MagicMock()
    entry.title = title
    entry.link = link
    entry.summary = summary
    entry.description = summary
    entry.published = published
    return entry


# ---------------------------------------------------------------------------
# TestRSSCollector
# ---------------------------------------------------------------------------

class TestRSSCollector(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cache = self.tmp / "rss_seen.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_collector(self, feeds=None):
        return RSSCollector(
            feeds=feeds or ["http://fake-feed/rss"],
            max_items=10,
            cache_path=str(self.cache),
        )

    def _mock_parse(self, entries):
        """Patch _parse_feed to return given entries."""
        return patch("theme_engine.rss_collector._parse_feed", return_value=entries)

    def test_collect_returns_new_items(self):
        entries = [
            _fake_entry("Headline A", "http://example.com/a", "Summary A"),
            _fake_entry("Headline B", "http://example.com/b", "Summary B"),
        ]
        with self._mock_parse(entries):
            collector = self._make_collector()
            results = collector.collect()
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["title"], "Headline A")

    def test_deduplication_skips_seen_items(self):
        entry = _fake_entry("Dup Headline", "http://example.com/dup")
        with self._mock_parse([entry]):
            c1 = self._make_collector()
            first = c1.collect()
        self.assertEqual(len(first), 1)


        # Second collect with same item — should return 0
        with self._mock_parse([entry]):
            c2 = self._make_collector()  # loads cache from disk
            second = c2.collect()
        self.assertEqual(len(second), 0)

    def test_summary_truncated_to_280(self):
        long_summary = "X" * 500
        entry = _fake_entry("Short Title", "http://example.com/long", long_summary)
        with self._mock_parse([entry]):
            c = self._make_collector()
            results = c.collect()
        self.assertLessEqual(len(results[0]["summary"]), 281)  # 280 chars + ellipsis

    def test_truncate_short_text_unchanged(self):
        self.assertEqual(_truncate("hello"), "hello")

    def test_item_hash_stable(self):
        h1 = _item_hash("http://a.com/1", "Title X")
        h2 = _item_hash("http://a.com/1", "Title X")
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 16)

    def test_max_items_respected(self):
        entries = [
            _fake_entry(f"H{i}", f"http://example.com/{i}") for i in range(20)
        ]
        with self._mock_parse(entries):
            c = RSSCollector(feeds=["http://f/rss"], max_items=5, cache_path=str(self.cache))
            results = c.collect()
        self.assertLessEqual(len(results), 5)

    def test_missing_title_or_link_skipped(self):
        bad = MagicMock()
        bad.title = ""
        bad.link = "http://example.com/ok"
        bad.summary = ""
        bad.description = ""
        bad.published = "Mon, 03 Mar 2026"
        with self._mock_parse([bad]):
            c = self._make_collector()
            results = c.collect()
        self.assertEqual(results, [])

    def test_seen_cache_saved_to_disk(self):
        entry = _fake_entry("Cache Test", "http://example.com/cache")
        with self._mock_parse([entry]):
            c = self._make_collector()
            c.collect()
        self.assertTrue(self.cache.exists())
        data = json.loads(self.cache.read_text())
        self.assertIn("hashes", data)
        self.assertTrue(len(data["hashes"]) > 0)


class TestThemeCliEncoding(unittest.TestCase):

    def test_configure_stdio_utf8_reconfigures_supported_streams(self):
        import theme_engine.__main__ as theme_main

        stdout = MagicMock()
        stderr = MagicMock()

        with patch.object(theme_main.sys, "stdout", stdout), patch.object(theme_main.sys, "stderr", stderr):
            theme_main._configure_stdio_utf8()

        stdout.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")
        stderr.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# TestThemeDetector
# ---------------------------------------------------------------------------

class TestThemeDetector(unittest.TestCase):

    def test_testing_mode_returns_mock_themes(self):
        detector = ThemeDetector(testing_mode=True)
        result = detector.detect([{"title": "Any headline"}])
        self.assertEqual(result, list(MOCK_THEMES))

    def test_testing_mode_makes_no_network_call(self):
        with patch("urllib.request.urlopen") as mock_url:
            detector = ThemeDetector(testing_mode=True)
            detector.detect([{"title": "Any"}])
            mock_url.assert_not_called()

    def test_empty_headlines_returns_empty_list(self):
        detector = ThemeDetector(testing_mode=False)
        with patch.object(detector, "_call_ollama", return_value=None):
            result = detector.detect([])
        self.assertEqual(result, [])

    def test_valid_json_parsed_correctly(self):
        raw_json = json.dumps({
            "themes": [
                {
                    "name": "AI Infrastructure",
                    "confidence": 0.85,
                    "rationale": "GPU demand surge",
                    "evidence_items": ["Nvidia earnings beat"],
                    "direct_mentions": ["NVDA"],
                }
            ]
        })
        detector = ThemeDetector(testing_mode=False)
        with patch.object(detector, "_call_ollama", return_value=raw_json):
            result = detector.detect([{"title": "Nvidia earnings"}])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "AI Infrastructure")
        self.assertAlmostEqual(result[0]["confidence"], 0.85)

    def test_markdown_fenced_json_parsed(self):
        raw = "```json\n" + json.dumps({"themes": [{"name": "Cybersecurity", "confidence": 0.7, "rationale": "Breaches", "evidence_items": [], "direct_mentions": []}]}) + "\n```"
        detector = ThemeDetector(testing_mode=False)
        with patch.object(detector, "_call_ollama", return_value=raw):
            result = detector.detect([{"title": "Security"}])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Cybersecurity")

    def test_invalid_json_triggers_retry(self):
        valid_json = json.dumps({"themes": [{"name": "Payments", "confidence": 0.6, "rationale": "Fintech", "evidence_items": [], "direct_mentions": []}]})
        call_count = [0]

        def _side_effect(prompt: str):
            call_count[0] += 1
            if call_count[0] == 1:
                return "NOT VALID JSON {{{"
            return valid_json

        detector = ThemeDetector(testing_mode=False)
        with patch.object(detector, "_call_ollama", side_effect=_side_effect):
            result = detector.detect([{"title": "Payments growth"}])
        self.assertEqual(call_count[0], 2)
        self.assertEqual(len(result), 1)

    def test_confidence_clamped_0_to_1(self):
        raw_json = json.dumps({
            "themes": [{"name": "X", "confidence": 1.5, "rationale": "", "evidence_items": [], "direct_mentions": []}]
        })
        detector = ThemeDetector(testing_mode=False)
        with patch.object(detector, "_call_ollama", return_value=raw_json):
            result = detector.detect([{"title": "X"}])
        self.assertEqual(result[0]["confidence"], 1.0)

    def test_max_5_themes_enforced(self):
        themes = [
            {"name": f"Theme {i}", "confidence": 0.7, "rationale": "r", "evidence_items": [], "direct_mentions": []}
            for i in range(10)
        ]
        raw_json = json.dumps({"themes": themes})
        detector = ThemeDetector(testing_mode=False)
        with patch.object(detector, "_call_ollama", return_value=raw_json):
            result = detector.detect([{"title": "Anything"}])
        self.assertLessEqual(len(result), 5)

    def test_env_stockbot_testing_activates_mock(self):
        with patch.dict(os.environ, {"STOCKBOT_TESTING": "1"}):
            detector = ThemeDetector(testing_mode=False)
            self.assertTrue(detector.testing_mode)

    def test_selected_provider_routes_through_shared_adapter(self):
        raw_json = json.dumps({
            "themes": [
                {
                    "name": "AI Infrastructure",
                    "confidence": 0.85,
                    "rationale": "GPU demand",
                    "evidence_items": ["Headline"],
                    "direct_mentions": ["NVDA"],
                }
            ]
        })
        with patch("theme_engine.theme_detector.call_provider", return_value=raw_json) as mock_call:
            detector = ThemeDetector(
                provider="openai",
                model="gpt-4o-mini",
                testing_mode=False,
            )
            result = detector.detect([{"title": "Nvidia expands AI infrastructure"}])
        self.assertEqual(len(result), 1)
        self.assertEqual(mock_call.call_args.kwargs["provider"], "openai")
        self.assertEqual(mock_call.call_args.kwargs["model"], "gpt-4o-mini")

    def test_legacy_ollama_endpoint_normalized_to_v1(self):
        detector = ThemeDetector(
            provider="ollama",
            endpoint="http://localhost:11434/api/generate",
            testing_mode=False,
        )
        self.assertEqual(detector.base_url, "http://localhost:11434/v1")


class TestThemeProviderRouting(unittest.TestCase):

    def test_global_override_beats_task_provider(self):
        config = {
            "task_providers": {"daily": "ollama"},
            "ollama_model": "gemma3:4b",
            "openai_model": "gpt-4o-mini",
        }
        with patch.dict(os.environ, {"STOCKBOT_LLM_PROVIDER": "openai"}, clear=False):
            context = _resolve_theme_task_context(mode="daily", config=config)
        self.assertEqual(context["provider"], "openai")

    def test_task_provider_beats_default_routing(self):
        config = {
            "task_providers": {"daily": "anthropic"},
            "anthropic_model": "claude-haiku-4-5-20251001",
        }
        with patch.dict(os.environ, {"STOCKBOT_LLM_PROVIDER": ""}, clear=False):
            context = _resolve_theme_task_context(mode="daily", config=config)
        self.assertEqual(context["provider"], "anthropic")

    def test_no_task_provider_preserves_default_routing(self):
        config = {"ollama_model": "gemma3:4b"}
        with patch.dict(os.environ, {"STOCKBOT_LLM_PROVIDER": ""}, clear=False):
            context = _resolve_theme_task_context(mode="daily", config=config)
        self.assertEqual(context["provider"], "ollama")

    def test_run_writes_llm_metadata_for_llm_backed_execution(self):
        from theme_engine.__main__ import run

        tmp = Path(tempfile.mkdtemp())

        class _FakeCollector:
            def __init__(self, *args, **kwargs):
                pass

            def collect(self):
                return [{"title": "AI infrastructure demand rises"}]

        class _FakeMapper:
            def __init__(self, *args, **kwargs):
                pass

            def map_themes(self, raw_themes):
                return raw_themes, [{"ticker": "NVDA", "confidence": 0.8}]

        class _FakeStore:
            def __init__(self, *args, **kwargs):
                pass

            def get_recent_signals(self, days=7):
                return []

            def save_signals(self, enriched_themes, watch_candidates, run_date=None, metadata=None):
                return None

        config = {
            "rss_feeds": [],
            "output_dir": "outputs/latest",
            "task_providers": {"daily": "openai"},
            "openai_model": "gpt-4o-mini",
            "testing_mode": False,
        }
        try:
            with patch("theme_engine.rss_collector.RSSCollector", _FakeCollector):
                with patch("theme_engine.theme_detector.ThemeDetector.detect", return_value=[{"name": "AI Infrastructure", "confidence": 0.8, "tickers": ["NVDA"]}]):
                    with patch("theme_engine.theme_mapper.ThemeMapper", _FakeMapper):
                        with patch("theme_engine.theme_store.ThemeStore", _FakeStore):
                            with patch.dict(os.environ, {"STOCKBOT_LLM_PROVIDER": ""}, clear=False):
                                with self.assertLogs("theme_engine.__main__", level="INFO") as captured:
                                    result = run(mode="daily", config=config, dry_run=False, root=str(tmp))

            self.assertEqual(result["llm_metadata"]["resolved_provider"], "openai")
            self.assertEqual(result["llm_metadata"]["model"], "gpt-4o-mini")
            self.assertIn("run_id", result["llm_metadata"])
            self.assertIn("started_at", result["llm_metadata"])
            self.assertIn("completed_at", result["llm_metadata"])
            self.assertIn("latency_ms", result["llm_metadata"])
            self.assertIn("success", result["llm_metadata"])
            self.assertIn("error_type", result["llm_metadata"])
            self.assertIn("fallback_reason", result["llm_metadata"])
            self.assertTrue(result["llm_metadata"]["success"])
            self.assertIsNone(result["llm_metadata"]["error_type"])
            self.assertIsNone(result["llm_metadata"]["fallback_reason"])
            self.assertFalse(result["llm_metadata"]["fallback_triggered"])
            self.assertTrue(
                any("provider=openai" in message for message in captured.output)
            )
            self.assertTrue(
                any(
                    "Theme engine LLM summary: task=theme_engine.daily resolved=openai actual=openai model=gpt-4o-mini fallback=no"
                    in message
                    for message in captured.output
                )
            )
            metadata_path = tmp / "outputs" / "latest" / "theme_engine_llm_metadata.json"
            self.assertTrue(metadata_path.exists())
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertIn("run_id", metadata)
            self.assertIn("started_at", metadata)
            self.assertIn("completed_at", metadata)
            self.assertIn("git_commit", metadata)
            self.assertEqual(metadata["llm_metadata"]["resolved_provider"], "openai")
            self.assertEqual(metadata["llm_metadata"]["actual_provider"], "openai")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# TestThemeMapper
# ---------------------------------------------------------------------------

class TestThemeMapper(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.catalog_path = self.tmp / "themes_catalog.json"
        catalog = {
            "AI Infrastructure": {
                "tickers": ["NVDA", "MSFT"],
                "synonyms": ["artificial intelligence", "gpu", "large language model"],
            },
            "Cybersecurity": {
                "tickers": ["PANW", "CRWD"],
                "synonyms": ["cyber security", "zero trust"],
            },
        }
        _write_json(self.catalog_path, catalog)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mapper(self, sp500=None):
        return ThemeMapper(
            catalog_path=str(self.catalog_path),
            sp500_symbols=sp500,
        )

    def test_exact_name_match(self):
        m = self._mapper()
        result = m._match_catalog("AI Infrastructure")
        self.assertEqual(result, "AI Infrastructure")

    def test_synonym_match(self):
        m = self._mapper()
        result = m._match_catalog("gpu demand surge")
        self.assertEqual(result, "AI Infrastructure")

    def test_case_insensitive_match(self):
        m = self._mapper()
        result = m._match_catalog("CYBER SECURITY threats")
        self.assertEqual(result, "Cybersecurity")

    def test_no_match_returns_none(self):
        m = self._mapper()
        result = m._match_catalog("Random unrelated topic")
        self.assertIsNone(result)

    def test_map_themes_returns_tickers(self):
        m = self._mapper()
        detected = [{"name": "AI Infrastructure", "confidence": 0.85, "rationale": "GPU", "evidence_items": [], "direct_mentions": []}]
        enriched, candidates = m.map_themes(detected)
        self.assertEqual(enriched[0]["tickers"], ["NVDA", "MSFT"])

    def test_direct_mentions_filtered_by_sp500(self):
        m = self._mapper(sp500=["NVDA", "AMZN"])
        detected = [{"name": "Cloud Infrastructure", "confidence": 0.7, "rationale": "Cloud", "evidence_items": [], "direct_mentions": ["NVDA", "RANDOM_TICKER", "AMZN"]}]
        enriched, candidates = m.map_themes(detected)
        ticker_syms = {c["ticker"] for c in candidates}
        self.assertIn("NVDA", ticker_syms)
        self.assertIn("AMZN", ticker_syms)
        self.assertNotIn("RANDOM_TICKER", ticker_syms)

    def test_direct_mentions_all_pass_when_no_sp500_filter(self):
        m = self._mapper(sp500=None)  # no filter
        detected = [{"name": "Unknown Theme", "confidence": 0.7, "rationale": "", "evidence_items": [], "direct_mentions": ["XYZ", "ABC"]}]
        _, candidates = m.map_themes(detected)
        ticker_syms = {c["ticker"] for c in candidates}
        self.assertIn("XYZ", ticker_syms)

    def test_max_confidence_taken_across_themes(self):
        m = self._mapper()
        detected = [
            {"name": "AI Infrastructure", "confidence": 0.6, "rationale": "", "evidence_items": [], "direct_mentions": ["NVDA"]},
            {"name": "AI Infrastructure", "confidence": 0.9, "rationale": "", "evidence_items": [], "direct_mentions": ["NVDA"]},
        ]
        _, candidates = m.map_themes(detected)
        nvda_candidate = next(c for c in candidates if c["ticker"] == "NVDA")
        self.assertAlmostEqual(nvda_candidate["confidence"], 0.9)

    def test_normalize_helper(self):
        self.assertEqual(_normalize("  AI-Infrastructure! "), "ai infrastructure")

    def test_missing_catalog_returns_empty_tickers(self):
        m = ThemeMapper(catalog_path=str(self.tmp / "nonexistent.json"))
        detected = [{"name": "AI Infrastructure", "confidence": 0.8, "rationale": "", "evidence_items": [], "direct_mentions": []}]
        enriched, candidates = m.map_themes(detected)
        self.assertEqual(enriched[0]["tickers"], [])


# ---------------------------------------------------------------------------
# TestThemeStore
# ---------------------------------------------------------------------------

class TestThemeStore(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = self.tmp / "test.db"
        self.output_dir = self.tmp / "outputs" / "latest"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _store(self):
        return ThemeStore(db_path=str(self.db_path), output_dir=str(self.output_dir))

    def _sample_themes(self):
        return [
            {
                "name": "AI Infrastructure",
                "confidence": 0.85,
                "rationale": "GPU demand",
                "evidence_items": ["Nvidia record revenue"],
                "direct_mentions": ["NVDA"],
                "tickers": ["NVDA", "MSFT"],
                "persistence_7d": 0,
            }
        ]

    def _sample_candidates(self):
        return [{"ticker": "NVDA", "sources": ["theme"], "themes": ["AI Infrastructure"], "confidence": 0.85, "rationale": "GPU", "timestamp": "2026-03-03T00:00:00+00:00"}]

    def test_table_created_on_init(self):
        _ = self._store()
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='theme_signals'")
            self.assertIsNotNone(cursor.fetchone())
        finally:
            conn.close()

    def test_save_signals_writes_json_files(self):
        store = self._store()
        store.save_signals(self._sample_themes(), self._sample_candidates(), "2026-03-03")
        self.assertTrue((self.output_dir / "theme_signals.json").exists())
        self.assertTrue((self.output_dir / "watch_candidates.json").exists())

    def test_theme_signals_json_format(self):
        store = self._store()
        store.save_signals(self._sample_themes(), self._sample_candidates(), "2026-03-03")
        data = json.loads((self.output_dir / "theme_signals.json").read_text())
        self.assertIn("generated_at", data)
        self.assertIn("run_date", data)
        self.assertIn("themes", data)
        self.assertIn("data_mode", data)
        self.assertIn("degraded_mode", data)
        self.assertEqual(data["run_date"], "2026-03-03")
        self.assertEqual(len(data["themes"]), 1)

    def test_watch_candidates_json_format(self):
        store = self._store()
        store.save_signals(self._sample_themes(), self._sample_candidates(), "2026-03-03")
        data = json.loads((self.output_dir / "watch_candidates.json").read_text())
        self.assertIn("data_mode", data)
        self.assertIn("degraded_mode", data)
        self.assertIn("watch_candidates", data)
        self.assertEqual(len(data["watch_candidates"]), 1)
        self.assertEqual(data["watch_candidates"][0]["ticker"], "NVDA")

    def test_save_signals_persists_degraded_mode_metadata(self):
        store = self._store()
        store.save_signals(
            self._sample_themes(),
            self._sample_candidates(),
            "2026-03-03",
            metadata={
                "data_mode": "fallback",
                "degraded_mode": True,
                "degraded_reason": "cache_only",
                "data_sources_used": ["cache"],
            },
        )
        data = json.loads((self.output_dir / "theme_signals.json").read_text())
        self.assertEqual(data["data_mode"], "fallback")
        self.assertTrue(data["degraded_mode"])
        self.assertEqual(data["degraded_reason"], "cache_only")
        self.assertEqual(data["data_sources_used"], ["cache"])

    def test_empty_theme_run_retains_previous_theme_snapshot(self):
        store = self._store()
        store.save_signals(self._sample_themes(), self._sample_candidates(), "2026-03-03")

        store.save_signals([], [], "2026-03-04")

        theme_data = json.loads((self.output_dir / "theme_signals.json").read_text())
        candidate_data = json.loads((self.output_dir / "watch_candidates.json").read_text())
        self.assertEqual(theme_data["run_date"], "2026-03-04")
        self.assertEqual(theme_data["theme_source"], "stale")
        self.assertTrue(theme_data["no_update"])
        self.assertEqual(theme_data["themes"][0]["name"], "AI Infrastructure")
        self.assertEqual(candidate_data["theme_source"], "stale")
        self.assertTrue(candidate_data["no_update"])
        self.assertEqual(candidate_data["watch_candidates"][0]["ticker"], "NVDA")

    def test_get_recent_signals_returns_rows(self):
        from datetime import date
        today = date.today().isoformat()
        store = self._store()
        store.save_signals(self._sample_themes(), [], today)
        rows = store.get_recent_signals(days=7)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["theme_name"], "AI Infrastructure")

    def test_compute_persistence_counts_distinct_days(self):
        from datetime import date, timedelta
        today = date.today().isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        store = self._store()
        # Save same theme on two different dates
        store.save_signals(self._sample_themes(), [], yesterday)
        store.save_signals(self._sample_themes(), [], today)
        persistence = store.compute_persistence("AI Infrastructure", days=7)
        self.assertEqual(persistence, 2)

    def test_compute_persistence_zero_for_new_theme(self):
        store = self._store()
        persistence = store.compute_persistence("Non Existent Theme", days=7)
        self.assertEqual(persistence, 0)


# ---------------------------------------------------------------------------
# TestApplyThemeBoosts
# ---------------------------------------------------------------------------

class TestApplyThemeBoosts(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.signals_path = self.tmp / "theme_signals.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_signals(self, themes: list) -> None:
        _write_json(self.signals_path, {"generated_at": "2026-03-03T00:00:00", "run_date": "2026-03-03", "themes": themes})

    def _config(self, max_boost=10, min_conf=0.6):
        return {"max_theme_boost_points": max_boost, "min_confidence": min_conf}

    def test_boost_applied_to_matching_ticker(self):
        self._write_signals([{"name": "AI Infrastructure", "confidence": 0.8, "rationale": "", "evidence_items": [], "direct_mentions": [], "tickers": ["NVDA"]}])
        candidates = [_candidate("NVDA", score=60.0)]
        result = apply_theme_boosts(candidates, str(self.signals_path), self._config())
        nvda = next(c for c in result if c["symbol"] == "NVDA")
        self.assertEqual(nvda["theme_boost"], 8)  # round(0.8 * 10) = 8
        self.assertAlmostEqual(nvda["score"], 68.0)

    def test_boost_capped_at_max_boost_points(self):
        self._write_signals([{"name": "AI Infrastructure", "confidence": 1.0, "rationale": "", "evidence_items": [], "direct_mentions": [], "tickers": ["NVDA"]}])
        candidates = [_candidate("NVDA", score=95.0)]
        result = apply_theme_boosts(candidates, str(self.signals_path), self._config(max_boost=10))
        nvda = next(c for c in result if c["symbol"] == "NVDA")
        self.assertEqual(nvda["theme_boost"], 10)  # max capped

    def test_final_score_capped_at_100(self):
        self._write_signals([{"name": "AI Infrastructure", "confidence": 1.0, "rationale": "", "evidence_items": [], "direct_mentions": [], "tickers": ["NVDA"]}])
        candidates = [_candidate("NVDA", score=97.0)]
        result = apply_theme_boosts(candidates, str(self.signals_path), self._config())
        nvda = next(c for c in result if c["symbol"] == "NVDA")
        self.assertLessEqual(nvda["score"], 100.0)

    def test_low_confidence_below_threshold_not_boosted(self):
        self._write_signals([{"name": "AI Infrastructure", "confidence": 0.4, "rationale": "", "evidence_items": [], "direct_mentions": [], "tickers": ["NVDA"]}])
        candidates = [_candidate("NVDA", score=60.0)]
        result = apply_theme_boosts(candidates, str(self.signals_path), self._config(min_conf=0.6))
        nvda = next(c for c in result if c["symbol"] == "NVDA")
        self.assertEqual(nvda["theme_boost"], 0)
        self.assertAlmostEqual(nvda["score"], 60.0)

    def test_non_matched_ticker_gets_zero_boost(self):
        self._write_signals([{"name": "AI Infrastructure", "confidence": 0.9, "rationale": "", "evidence_items": [], "direct_mentions": [], "tickers": ["NVDA"]}])
        candidates = [_candidate("GLD", score=50.0)]
        result = apply_theme_boosts(candidates, str(self.signals_path), self._config())
        gld = next(c for c in result if c["symbol"] == "GLD")
        self.assertEqual(gld["theme_boost"], 0)
        self.assertEqual(gld["theme_names"], "")

    def test_no_signals_file_returns_candidates_unchanged(self):
        candidates = [_candidate("MSFT", score=70.0)]
        result = apply_theme_boosts(candidates, str(self.signals_path), self._config())
        msft = next(c for c in result if c["symbol"] == "MSFT")
        self.assertAlmostEqual(msft["score"], 70.0)
        self.assertEqual(msft["theme_boost"], 0)

    def test_theme_names_populated(self):
        self._write_signals([{"name": "AI Infrastructure", "confidence": 0.8, "rationale": "", "evidence_items": [], "direct_mentions": [], "tickers": ["NVDA"]}])
        candidates = [_candidate("NVDA", score=60.0)]
        result = apply_theme_boosts(candidates, str(self.signals_path), self._config())
        nvda = next(c for c in result if c["symbol"] == "NVDA")
        self.assertIn("AI Infrastructure", nvda["theme_names"])

    def test_max_confidence_used_when_ticker_in_multiple_themes(self):
        self._write_signals([
            {"name": "AI Infrastructure", "confidence": 0.6, "rationale": "", "evidence_items": [], "direct_mentions": [], "tickers": ["NVDA"]},
            {"name": "Semicap Equipment", "confidence": 0.9, "rationale": "", "evidence_items": [], "direct_mentions": [], "tickers": ["NVDA"]},
        ])
        candidates = [_candidate("NVDA", score=60.0)]
        result = apply_theme_boosts(candidates, str(self.signals_path), self._config())
        nvda = next(c for c in result if c["symbol"] == "NVDA")
        self.assertEqual(nvda["theme_boost"], 9)  # round(0.9 * 10)

    def test_output_sorted_by_score_descending(self):
        self._write_signals([{"name": "AI Infrastructure", "confidence": 1.0, "rationale": "", "evidence_items": [], "direct_mentions": [], "tickers": ["NVDA"]}])
        candidates = [_candidate("NVDA", score=60.0), _candidate("MSFT", score=80.0)]
        result = apply_theme_boosts(candidates, str(self.signals_path), self._config())
        scores = [c["score"] for c in result]
        self.assertEqual(scores, sorted(scores, reverse=True))


# ---------------------------------------------------------------------------
# TestScannerIntegration
# ---------------------------------------------------------------------------

class TestScannerIntegration(unittest.TestCase):
    """Verify scanner output stability when theme engine is inactive."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_candidates_stable_without_signals_file(self):
        """No theme_signals.json → candidates unchanged, theme_boost=0."""
        signals_path = str(self.tmp / "theme_signals.json")
        candidates = [
            _candidate("NVDA", score=80.0),
            _candidate("MSFT", score=70.0),
            _candidate("AMZN", score=60.0),
        ]
        original_scores = [c["score"] for c in candidates]
        result = apply_theme_boosts(candidates, signals_path, {"max_theme_boost_points": 10, "min_confidence": 0.6})
        result_scores = [c["score"] for c in result]
        self.assertEqual(original_scores, sorted(result_scores, reverse=True))
        for c in result:
            self.assertEqual(c["theme_boost"], 0)

    def test_low_confidence_theme_does_not_reorder_candidates(self):
        """A low-confidence theme (below min_confidence) must not change ordering."""
        signals_path = self.tmp / "theme_signals.json"
        # Theme has confidence=0.4 which is below default min_confidence=0.6
        _write_json(signals_path, {
            "generated_at": "2026-03-03T00:00:00",
            "run_date": "2026-03-03",
            "themes": [{"name": "AI Infrastructure", "confidence": 0.4, "rationale": "", "evidence_items": [], "direct_mentions": [], "tickers": ["AMZN"]}]
        })
        candidates = [
            _candidate("NVDA", score=80.0),
            _candidate("MSFT", score=70.0),
            _candidate("AMZN", score=60.0),
        ]
        result = apply_theme_boosts(candidates, str(signals_path), {"max_theme_boost_points": 10, "min_confidence": 0.6})
        symbols_in_order = [c["symbol"] for c in result]
        # AMZN should not have jumped above MSFT or NVDA
        self.assertEqual(symbols_in_order, ["NVDA", "MSFT", "AMZN"])

    def test_theme_boost_fields_always_present(self):
        """theme_boost and theme_names must exist on every candidate regardless."""
        signals_path = str(self.tmp / "nonexistent.json")
        candidates = [_candidate("GLD")]
        result = apply_theme_boosts(candidates, signals_path, {})
        self.assertIn("theme_boost", result[0])
        self.assertIn("theme_names", result[0])


# ---------------------------------------------------------------------------
# TestOllamaIntegration (gated — real network call)
# ---------------------------------------------------------------------------

@unittest.skipUnless(
    os.getenv("STOCKBOT_ENABLE_OLLAMA_TEST") == "1",
    "Set STOCKBOT_ENABLE_OLLAMA_TEST=1 to run Ollama integration tests",
)
class TestOllamaIntegration(unittest.TestCase):

    def test_ollama_detect_returns_list(self):
        """Real Ollama call — requires local Ollama running with gemma3:4b."""
        detector = ThemeDetector(
            model="gemma3:4b",
            testing_mode=False,
            timeout=90,
        )
        headlines = [
            {"title": "Nvidia posts record revenue on AI chip demand"},
            {"title": "Microsoft Azure growth accelerates cloud infrastructure spending"},
            {"title": "CrowdStrike expands cybersecurity platform with AI features"},
        ]
        result = detector.detect(headlines)
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)
        for theme in result:
            self.assertIn("name", theme)
            self.assertIn("confidence", theme)
            self.assertGreaterEqual(theme["confidence"], 0.0)
            self.assertLessEqual(theme["confidence"], 1.0)


if __name__ == "__main__":
    unittest.main()
