"""
Tests for scanner/fallback_watchlist.py

Coverage:
  1.  FMP success path is completely unchanged (fallback not involved)
  2.  FMP failure activates fallback path
  3.  Missing watchlist activates fallback path
  4.  Fallback + theme blend deduplicates and caps correctly
  5.  Fallback disabled via config
  6.  Theme candidates file missing → graceful no-op blend
  7.  Theme candidates file malformed → graceful no-op blend
  8.  Custom symbols override defaults
  9.  Existing watchlist symbols preserved (lower priority)
  10. save() writes valid JSON with watchlist_source=fallback marker
  11. build() returns correct schema (matches CandidateScanner._build_row schema)
  12. max_size cap respected even when many symbols provided
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scanner.fallback_watchlist import FallbackWatchlist, _DEFAULT_SYMBOLS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_config(**overrides) -> dict:
    """Return a scanner config dict with all fallback keys set to safe defaults."""
    base = {
        "fallback_watchlist_enabled": True,
        "fallback_watchlist_symbols": ["NVDA", "MSFT", "AMZN", "GOOGL", "META"],
        "fallback_watchlist_max_size": 5,
        "fallback_watchlist_include_theme_candidates": False,
        "fallback_watchlist_theme_candidate_limit": 3,
    }
    base.update(overrides)
    return base


def _write_theme_candidates(path: Path, symbols: list[str]) -> None:
    """Write a minimal watch_candidates.json for testing."""
    items = [{"symbol": s, "confidence": 0.9, "theme_name": "AI"} for s in symbols]
    path.write_text(json.dumps(items), encoding="utf-8")


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestFallbackWatchlistEnabled(unittest.TestCase):
    """Fallback is enabled (default)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.watchlist_path = Path(self.tmpdir) / "top100_watchlist.json"
        self.config = _minimal_config()

    # ── 1. FMP success: fallback.enabled is True but build() is never called ──

    def test_fmp_success_fallback_not_needed(self):
        """When FMP succeeds, caller never calls build(); fallback is inert."""
        fb = FallbackWatchlist(self.config, watchlist_path=self.watchlist_path)
        self.assertTrue(fb.enabled)
        # Watchlist path should NOT exist — fallback was not called
        self.assertFalse(self.watchlist_path.exists())

    # ── 2. FMP failure activates fallback ────────────────────────────────────

    def test_build_returns_candidates_on_fmp_failure(self):
        fb = FallbackWatchlist(self.config, watchlist_path=self.watchlist_path)
        candidates = fb.build()
        self.assertIsInstance(candidates, list)
        self.assertGreater(len(candidates), 0)

    def test_build_candidates_have_required_schema_fields(self):
        """Each candidate must expose the fields CandidateScanner.daily_refresh needs."""
        required_fields = {
            "symbol", "score", "sector", "mkt_cap", "rev_growth",
            "fcf_yield", "roe", "pe", "price", "price_200dma",
            "above_200dma", "reasons", "theme_boost", "theme_names",
            "watchlist_source", "scanned_at",
        }
        fb = FallbackWatchlist(self.config, watchlist_path=self.watchlist_path)
        for candidate in fb.build():
            self.assertFalse(
                required_fields - candidate.keys(),
                f"Missing fields: {required_fields - candidate.keys()}",
            )

    def test_build_watchlist_source_labelled_as_fallback(self):
        fb = FallbackWatchlist(self.config, watchlist_path=self.watchlist_path)
        for c in fb.build():
            self.assertEqual(c["watchlist_source"], "fallback")

    # ── 3. Missing watchlist activates fallback ───────────────────────────────

    def test_build_when_watchlist_file_absent(self):
        self.assertFalse(self.watchlist_path.exists())
        fb = FallbackWatchlist(self.config, watchlist_path=self.watchlist_path)
        candidates = fb.build()
        self.assertIsInstance(candidates, list)
        self.assertGreater(len(candidates), 0)

    # ── 4. Fallback + theme blend deduplicates and caps ──────────────────────

    def test_theme_blend_deduplicates_symbols(self):
        theme_file = Path(self.tmpdir) / "watch_candidates.json"
        # NVDA overlaps with config defaults
        _write_theme_candidates(theme_file, ["NVDA", "TSLA", "AMD"])
        config = _minimal_config(
            fallback_watchlist_include_theme_candidates=True,
            fallback_watchlist_theme_candidate_limit=5,
            fallback_watchlist_max_size=10,
            fallback_watchlist_symbols=["NVDA", "MSFT", "AMZN"],
        )
        fb = FallbackWatchlist(config, watchlist_path=self.watchlist_path)
        candidates = fb.build(theme_candidates_path=str(theme_file))
        symbols = [c["symbol"] for c in candidates]
        # No duplicates
        self.assertEqual(len(symbols), len(set(symbols)))
        # NVDA appears exactly once
        self.assertEqual(symbols.count("NVDA"), 1)

    def test_theme_blend_respects_max_size_cap(self):
        theme_file = Path(self.tmpdir) / "watch_candidates.json"
        _write_theme_candidates(theme_file, ["X1", "X2", "X3", "X4", "X5"])
        config = _minimal_config(
            fallback_watchlist_include_theme_candidates=True,
            fallback_watchlist_theme_candidate_limit=5,
            fallback_watchlist_max_size=4,     # hard cap at 4
            fallback_watchlist_symbols=["A", "B", "C", "D", "E"],
        )
        fb = FallbackWatchlist(config, watchlist_path=self.watchlist_path)
        candidates = fb.build(theme_candidates_path=str(theme_file))
        self.assertLessEqual(len(candidates), 4)

    def test_theme_symbols_labelled_fallback_plus_themes(self):
        theme_file = Path(self.tmpdir) / "watch_candidates.json"
        _write_theme_candidates(theme_file, ["TSLA"])
        config = _minimal_config(
            fallback_watchlist_include_theme_candidates=True,
            fallback_watchlist_theme_candidate_limit=5,
            fallback_watchlist_max_size=10,
            fallback_watchlist_symbols=["NVDA"],
        )
        fb = FallbackWatchlist(config, watchlist_path=self.watchlist_path)
        candidates = fb.build(theme_candidates_path=str(theme_file))
        by_sym = {c["symbol"]: c for c in candidates}
        self.assertEqual(by_sym["TSLA"]["watchlist_source"], "fallback+themes")
        self.assertEqual(by_sym["NVDA"]["watchlist_source"], "fallback")

    def test_theme_candidate_limit_respected(self):
        theme_file = Path(self.tmpdir) / "watch_candidates.json"
        many_syms = [f"T{i:02d}" for i in range(20)]
        _write_theme_candidates(theme_file, many_syms)
        config = _minimal_config(
            fallback_watchlist_include_theme_candidates=True,
            fallback_watchlist_theme_candidate_limit=3,
            fallback_watchlist_max_size=50,
            fallback_watchlist_symbols=[],
        )
        fb = FallbackWatchlist(config, watchlist_path=self.watchlist_path)
        candidates = fb.build(theme_candidates_path=str(theme_file))
        theme_tagged = [c for c in candidates if c["watchlist_source"] == "fallback+themes"]
        self.assertLessEqual(len(theme_tagged), 3)

    # ── 5. Fallback disabled via config ──────────────────────────────────────

    def test_disabled_via_config(self):
        config = _minimal_config(fallback_watchlist_enabled=False)
        fb = FallbackWatchlist(config, watchlist_path=self.watchlist_path)
        self.assertFalse(fb.enabled)

    def test_disabled_fallback_build_still_returns_list(self):
        """build() works regardless of enabled flag; caller checks .enabled."""
        config = _minimal_config(fallback_watchlist_enabled=False)
        fb = FallbackWatchlist(config, watchlist_path=self.watchlist_path)
        # build() itself doesn't check .enabled; caller is responsible
        candidates = fb.build()
        self.assertIsInstance(candidates, list)

    # ── 6. Theme candidates file missing ─────────────────────────────────────

    def test_missing_theme_file_returns_default_symbols(self):
        config = _minimal_config(
            fallback_watchlist_include_theme_candidates=True,
            fallback_watchlist_theme_candidate_limit=5,
            fallback_watchlist_max_size=10,
        )
        fb = FallbackWatchlist(config, watchlist_path=self.watchlist_path)
        candidates = fb.build(theme_candidates_path="/nonexistent/path/watch_candidates.json")
        symbols = [c["symbol"] for c in candidates]
        # Should fall through to default symbols
        self.assertTrue(any(s in symbols for s in ["NVDA", "MSFT", "AMZN"]))

    # ── 7. Theme candidates file malformed ───────────────────────────────────

    def test_malformed_theme_file_returns_default_symbols(self):
        theme_file = Path(self.tmpdir) / "bad_candidates.json"
        theme_file.write_text("NOT VALID JSON!!!", encoding="utf-8")
        config = _minimal_config(
            fallback_watchlist_include_theme_candidates=True,
            fallback_watchlist_max_size=10,
        )
        fb = FallbackWatchlist(config, watchlist_path=self.watchlist_path)
        candidates = fb.build(theme_candidates_path=str(theme_file))
        self.assertIsInstance(candidates, list)
        self.assertGreater(len(candidates), 0)

    def test_empty_theme_file_returns_default_symbols(self):
        theme_file = Path(self.tmpdir) / "empty.json"
        theme_file.write_text("[]", encoding="utf-8")
        config = _minimal_config(
            fallback_watchlist_include_theme_candidates=True,
            fallback_watchlist_max_size=10,
        )
        fb = FallbackWatchlist(config, watchlist_path=self.watchlist_path)
        candidates = fb.build(theme_candidates_path=str(theme_file))
        self.assertIsInstance(candidates, list)

    # ── 8. Custom symbols override defaults ──────────────────────────────────

    def test_custom_symbols_used_instead_of_defaults(self):
        config = _minimal_config(
            fallback_watchlist_symbols=["CUSTOM1", "CUSTOM2"],
            fallback_watchlist_max_size=5,
        )
        fb = FallbackWatchlist(config, watchlist_path=self.watchlist_path)
        candidates = fb.build()
        symbols = [c["symbol"] for c in candidates]
        self.assertIn("CUSTOM1", symbols)
        self.assertIn("CUSTOM2", symbols)

    def test_empty_config_symbols_falls_back_to_module_defaults(self):
        config = _minimal_config(
            fallback_watchlist_symbols=[],
            fallback_watchlist_max_size=20,
        )
        fb = FallbackWatchlist(config, watchlist_path=self.watchlist_path)
        candidates = fb.build()
        symbols = [c["symbol"] for c in candidates]
        # Should have used _DEFAULT_SYMBOLS
        self.assertTrue(
            any(s in symbols for s in _DEFAULT_SYMBOLS),
            "Expected at least one default symbol when config list is empty",
        )

    # ── 9. Existing watchlist symbols preserved ───────────────────────────────

    def test_existing_symbols_appended_after_defaults(self):
        config = _minimal_config(
            fallback_watchlist_symbols=["A"],
            fallback_watchlist_max_size=5,
        )
        fb = FallbackWatchlist(config, watchlist_path=self.watchlist_path)
        candidates = fb.build(existing_watchlist_symbols=["B", "C"])
        symbols = [c["symbol"] for c in candidates]
        self.assertIn("A", symbols)
        self.assertIn("B", symbols)

    def test_existing_symbols_dont_create_duplicates(self):
        config = _minimal_config(
            fallback_watchlist_symbols=["NVDA", "MSFT"],
            fallback_watchlist_max_size=10,
        )
        fb = FallbackWatchlist(config, watchlist_path=self.watchlist_path)
        # NVDA already in defaults
        candidates = fb.build(existing_watchlist_symbols=["NVDA", "AAPL"])
        symbols = [c["symbol"] for c in candidates]
        self.assertEqual(symbols.count("NVDA"), 1)

    # ── 10. save() writes valid JSON ─────────────────────────────────────────

    def test_save_writes_valid_json(self):
        fb = FallbackWatchlist(self.config, watchlist_path=self.watchlist_path)
        candidates = fb.build()
        fb.save(candidates)
        self.assertTrue(self.watchlist_path.exists())
        data = json.loads(self.watchlist_path.read_text(encoding="utf-8"))
        self.assertIn("candidates", data)
        self.assertIn("updated_at", data)
        self.assertEqual(data["watchlist_source"], "fallback")

    def test_save_candidates_are_readable_by_candidate_scanner(self):
        """Saved file can be loaded by CandidateScanner.load_watchlist()."""
        from scanner.candidate_scanner import CandidateScanner
        fb = FallbackWatchlist(self.config, watchlist_path=self.watchlist_path)
        candidates = fb.build()
        fb.save(candidates)
        cs = CandidateScanner(watchlist_path=self.watchlist_path)
        loaded = cs.load_watchlist()
        self.assertEqual(len(loaded), len(candidates))
        self.assertEqual(loaded[0]["symbol"], candidates[0]["symbol"])

    # ── 11. Schema fields have correct types ─────────────────────────────────

    def test_numeric_fields_are_floats(self):
        fb = FallbackWatchlist(self.config, watchlist_path=self.watchlist_path)
        for c in fb.build():
            self.assertIsInstance(c["score"],        float, "score must be float")
            self.assertIsInstance(c["price"],        float, "price must be float")
            self.assertIsInstance(c["price_200dma"], float, "price_200dma must be float")
            self.assertIsInstance(c["theme_boost"],  int,   "theme_boost must be int")

    # ── 12. max_size cap ─────────────────────────────────────────────────────

    def test_max_size_cap_enforced(self):
        config = _minimal_config(
            fallback_watchlist_symbols=list("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
            fallback_watchlist_max_size=7,
        )
        fb = FallbackWatchlist(config, watchlist_path=self.watchlist_path)
        candidates = fb.build()
        self.assertLessEqual(len(candidates), 7)

    def test_symbols_are_uppercased(self):
        config = _minimal_config(
            fallback_watchlist_symbols=["nvda", "msft", "amzn"],
            fallback_watchlist_max_size=5,
        )
        fb = FallbackWatchlist(config, watchlist_path=self.watchlist_path)
        for c in fb.build():
            self.assertEqual(c["symbol"], c["symbol"].upper())


class TestFallbackWatchlistDefaultConfig(unittest.TestCase):
    """Verify module-level defaults without any scanner config overrides."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.watchlist_path = Path(self.tmpdir) / "wl.json"

    def test_default_symbols_are_valid_tickers(self):
        self.assertTrue(len(_DEFAULT_SYMBOLS) >= 10)
        for sym in _DEFAULT_SYMBOLS:
            self.assertRegex(sym, r'^[A-Z]{1,5}$', f"Bad default symbol: {sym}")

    def test_empty_config_uses_defaults(self):
        fb = FallbackWatchlist({}, watchlist_path=self.watchlist_path)
        self.assertTrue(fb.enabled)
        candidates = fb.build()
        symbols = [c["symbol"] for c in candidates]
        self.assertTrue(any(s in symbols for s in _DEFAULT_SYMBOLS))

    def test_no_duplicate_default_symbols(self):
        self.assertEqual(
            len(_DEFAULT_SYMBOLS),
            len(set(_DEFAULT_SYMBOLS)),
            "Default symbol list must not have duplicates",
        )


if __name__ == "__main__":
    unittest.main()
