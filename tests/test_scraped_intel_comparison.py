"""
Tests for scraped_intel shadow-mode comparison report.

Covers:
  - _compute_soft_composite: weighting, normalisation, clamping
  - compute_comparison: enriched scores, rank changes, output schema
  - write_comparison_json / write_comparison_md: file creation, key presence
  - run_comparison: end-to-end pipeline (no network calls)
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from scraped_intel.models import IntelBundle, ScrapedRecord, SoftSignals
from scraped_intel.comparison import (
    ComparisonRow,
    _DEFAULT_BLEND_WEIGHTS,
    _DEFAULT_MAX_SIGNAL_BOOST,
    _DEFAULT_MAX_CONF_BOOST,
    _compute_soft_composite,
    compute_comparison,
    run_comparison,
    write_comparison_json,
    write_comparison_md,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_signals(
    scraped_confidence: float = 0.0,
    recency_score: float = 0.0,
    theme_alignment_score: float = 0.0,
    mention_acceleration: float = 0.0,
    source_count: int = 0,
    headline_count_30d: int = 0,
) -> SoftSignals:
    return SoftSignals(
        symbol="TEST",
        as_of_date="2025-04-10",
        scraped_confidence=scraped_confidence,
        recency_score=recency_score,
        theme_alignment_score=theme_alignment_score,
        mention_acceleration=mention_acceleration,
        source_count=source_count,
        headline_count_30d=headline_count_30d,
    )


def _make_bundle(symbol: str, signals: SoftSignals | None = None) -> IntelBundle:
    bundle = IntelBundle(symbol=symbol, as_of_date="2025-04-10")
    bundle.signals = signals
    return bundle


def _make_scan_result(ticker: str, signal_score: float, confidence_score: float) -> dict:
    return {
        "ticker": ticker,
        "signal_score": signal_score,
        "confidence_score": confidence_score,
    }


# ---------------------------------------------------------------------------
# _compute_soft_composite
# ---------------------------------------------------------------------------

class TestComputeSoftComposite(unittest.TestCase):

    def test_zero_signals_give_zero_composite(self):
        # mention_acceleration=-1.0 → accel_norm=0.0; all other fields default 0
        # → composite must be 0.0
        signals = _make_signals(mention_acceleration=-1.0)
        composite, features = _compute_soft_composite(signals)
        self.assertEqual(composite, 0.0)

    def test_all_max_signals_give_nonzero_composite(self):
        signals = _make_signals(
            scraped_confidence=1.0,
            recency_score=1.0,
            theme_alignment_score=1.0,
            mention_acceleration=1.0,   # max acceleration
        )
        composite, _ = _compute_soft_composite(signals)
        self.assertGreater(composite, 0.0)
        self.assertLessEqual(composite, 1.0)

    def test_mention_acceleration_normalised(self):
        """mention_acceleration = -1 (min) → accel_norm = 0.0 → zero contribution."""
        signals_neg = _make_signals(mention_acceleration=-1.0)
        signals_pos = _make_signals(mention_acceleration=1.0)
        comp_neg, _ = _compute_soft_composite(signals_neg)
        comp_pos, _ = _compute_soft_composite(signals_pos)
        # neutral (0.0) acceleration gives accel_norm = 0.5
        # negative accel gives accel_norm = 0.0 → lower composite
        self.assertLess(comp_neg, comp_pos)

    def test_composite_bounded_above(self):
        """composite must be in [0, 1] regardless of inputs."""
        signals = _make_signals(
            scraped_confidence=1.0,
            recency_score=1.0,
            theme_alignment_score=1.0,
            mention_acceleration=1.0,
        )
        composite, _ = _compute_soft_composite(signals)
        self.assertLessEqual(composite, 1.0)
        self.assertGreaterEqual(composite, 0.0)

    def test_top_features_returned(self):
        signals = _make_signals(
            scraped_confidence=0.8,
            recency_score=0.6,
            theme_alignment_score=0.4,
            mention_acceleration=0.5,
        )
        _, features = _compute_soft_composite(signals)
        self.assertGreater(len(features), 0)
        self.assertLessEqual(len(features), 3)

    def test_feature_schema(self):
        signals = _make_signals(scraped_confidence=0.8, recency_score=0.5)
        _, features = _compute_soft_composite(signals)
        for feat in features:
            self.assertIn("feature", feat)
            self.assertIn("value", feat)
            self.assertIn("weight", feat)
            self.assertIn("contribution", feat)

    def test_features_sorted_by_contribution_desc(self):
        signals = _make_signals(
            scraped_confidence=0.9,
            recency_score=0.1,
            theme_alignment_score=0.5,
        )
        _, features = _compute_soft_composite(signals)
        contributions = [f["contribution"] for f in features]
        self.assertEqual(contributions, sorted(contributions, reverse=True))

    def test_custom_weights_respected(self):
        """Override weights: put all weight on recency_score."""
        signals = _make_signals(scraped_confidence=0.0, recency_score=1.0)
        custom_weights = {
            "scraped_confidence": 0.0,
            "recency_score": 1.0,
            "theme_alignment_score": 0.0,
            "mention_accel_norm": 0.0,
        }
        composite, _ = _compute_soft_composite(signals, weights=custom_weights)
        self.assertAlmostEqual(composite, 1.0)


# ---------------------------------------------------------------------------
# compute_comparison
# ---------------------------------------------------------------------------

class TestComputeComparison(unittest.TestCase):

    def _two_symbol_setup(self):
        """AAPL has strong signals; MSFT has no bundle."""
        scan_results = [
            _make_scan_result("AAPL", signal_score=0.60, confidence_score=0.70),
            _make_scan_result("MSFT", signal_score=0.55, confidence_score=0.80),
        ]
        signals_aapl = _make_signals(
            scraped_confidence=0.85,
            recency_score=0.90,
            theme_alignment_score=0.80,
            mention_acceleration=0.60,
            source_count=3,
            headline_count_30d=12,
        )
        bundles = {"AAPL": _make_bundle("AAPL", signals=signals_aapl)}
        return scan_results, bundles

    def test_empty_scan_returns_empty(self):
        rows = compute_comparison([], {})
        self.assertEqual(rows, [])

    def test_no_bundles_scores_unchanged(self):
        scan_results = [_make_scan_result("NVDA", 0.70, 0.65)]
        rows = compute_comparison(scan_results, {})
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r.signal_delta, 0.0)
        self.assertEqual(r.confidence_delta, 0.0)
        self.assertFalse(r.soft_signals_available)

    def test_enriched_score_exceeds_baseline_with_strong_signals(self):
        scan_results, bundles = self._two_symbol_setup()
        rows = compute_comparison(scan_results, bundles)
        aapl = next(r for r in rows if r.symbol == "AAPL")
        self.assertGreater(aapl.enriched_signal_score, aapl.baseline_signal_score)
        self.assertGreater(aapl.signal_delta, 0.0)

    def test_symbol_without_bundle_unchanged(self):
        scan_results, bundles = self._two_symbol_setup()
        rows = compute_comparison(scan_results, bundles)
        msft = next(r for r in rows if r.symbol == "MSFT")
        self.assertEqual(msft.signal_delta, 0.0)
        self.assertEqual(msft.confidence_delta, 0.0)
        self.assertFalse(msft.soft_signals_available)

    def test_enriched_signal_capped_at_one(self):
        scan_results = [_make_scan_result("AAPL", signal_score=0.99, confidence_score=0.99)]
        signals = _make_signals(
            scraped_confidence=1.0, recency_score=1.0,
            theme_alignment_score=1.0, mention_acceleration=1.0,
        )
        bundles = {"AAPL": _make_bundle("AAPL", signals=signals)}
        rows = compute_comparison(scan_results, bundles, max_signal_boost=1.0)
        self.assertLessEqual(rows[0].enriched_signal_score, 1.0)

    def test_enriched_confidence_capped_at_one(self):
        scan_results = [_make_scan_result("X", signal_score=0.5, confidence_score=0.99)]
        signals = _make_signals(scraped_confidence=1.0)
        bundles = {"X": _make_bundle("X", signals=signals)}
        rows = compute_comparison(scan_results, bundles, max_conf_boost=1.0)
        self.assertLessEqual(rows[0].enriched_confidence_score, 1.0)

    def test_signal_delta_bounded_by_max_boost(self):
        scan_results = [_make_scan_result("NVDA", 0.50, 0.60)]
        signals = _make_signals(
            scraped_confidence=1.0, recency_score=1.0,
            theme_alignment_score=1.0, mention_acceleration=1.0,
        )
        bundles = {"NVDA": _make_bundle("NVDA", signals=signals)}
        max_boost = 0.10
        rows = compute_comparison(scan_results, bundles, max_signal_boost=max_boost)
        self.assertLessEqual(rows[0].signal_delta, max_boost + 1e-6)

    def test_ranks_assigned_to_all_rows(self):
        scan_results, bundles = self._two_symbol_setup()
        rows = compute_comparison(scan_results, bundles)
        for r in rows:
            self.assertGreater(r.baseline_rank, 0)
            self.assertGreater(r.enriched_rank, 0)

    def test_rank_change_positive_when_boosted(self):
        """AAPL (lower baseline rank but boosted) should move up."""
        scan_results = [
            _make_scan_result("MSFT", 0.80, 0.80),  # rank 1 baseline
            _make_scan_result("AAPL", 0.40, 0.60),  # rank 2 baseline
        ]
        signals = _make_signals(
            scraped_confidence=1.0, recency_score=1.0,
            theme_alignment_score=1.0, mention_acceleration=1.0,
        )
        bundles = {"AAPL": _make_bundle("AAPL", signals=signals)}
        rows = compute_comparison(scan_results, bundles, max_signal_boost=0.50)
        aapl = next(r for r in rows if r.symbol == "AAPL")
        msft = next(r for r in rows if r.symbol == "MSFT")
        # AAPL moved up → rank_change > 0
        self.assertGreater(aapl.rank_change, 0)
        # MSFT did not move up (it was displaced)
        self.assertLessEqual(msft.rank_change, 0)

    def test_rank_change_zero_when_no_signals(self):
        scan_results = [
            _make_scan_result("A", 0.8, 0.8),
            _make_scan_result("B", 0.5, 0.5),
        ]
        rows = compute_comparison(scan_results, {})
        for r in rows:
            self.assertEqual(r.rank_change, 0)

    def test_output_sorted_by_abs_signal_delta_desc(self):
        scan_results = [
            _make_scan_result("AAPL", 0.40, 0.50),
            _make_scan_result("NVDA", 0.60, 0.70),
            _make_scan_result("META", 0.55, 0.65),
        ]
        signals_aapl = _make_signals(
            scraped_confidence=0.90, recency_score=0.85,
            theme_alignment_score=0.75, mention_acceleration=0.5,
        )
        signals_nvda = _make_signals(scraped_confidence=0.20)
        bundles = {
            "AAPL": _make_bundle("AAPL", signals=signals_aapl),
            "NVDA": _make_bundle("NVDA", signals=signals_nvda),
        }
        rows = compute_comparison(scan_results, bundles)
        deltas = [abs(r.signal_delta) for r in rows]
        self.assertEqual(deltas, sorted(deltas, reverse=True))

    def test_comparison_row_schema(self):
        scan_results = [_make_scan_result("TSLA", 0.60, 0.70)]
        signals = _make_signals(scraped_confidence=0.8, recency_score=0.7)
        bundles = {"TSLA": _make_bundle("TSLA", signals=signals)}
        rows = compute_comparison(scan_results, bundles)
        d = rows[0].to_dict()
        required_keys = {
            "symbol", "baseline_signal_score", "enriched_signal_score", "signal_delta",
            "baseline_confidence_score", "enriched_confidence_score", "confidence_delta",
            "baseline_rank", "enriched_rank", "rank_change",
            "soft_composite", "top_features",
            "source_count", "evidence_count", "scraped_confidence",
            "soft_signals_available",
        }
        self.assertTrue(required_keys.issubset(set(d.keys())))

    def test_no_hard_data_fields_in_output(self):
        """ComparisonRow.to_dict() must not contain any hard-data field names."""
        scan_results = [_make_scan_result("AMZN", 0.55, 0.65)]
        signals = _make_signals(scraped_confidence=0.75)
        bundles = {"AMZN": _make_bundle("AMZN", signals=signals)}
        rows = compute_comparison(scan_results, bundles)
        d = rows[0].to_dict()
        forbidden = {"price", "fundamentals", "technicals", "news"}
        self.assertTrue(forbidden.isdisjoint(set(d.keys())))

    def test_bundle_with_zero_confidence_treated_as_no_signals(self):
        """A bundle with scraped_confidence=0 should produce no boost."""
        scan_results = [_make_scan_result("AMD", 0.55, 0.60)]
        signals = _make_signals(scraped_confidence=0.0)
        bundles = {"AMD": _make_bundle("AMD", signals=signals)}
        rows = compute_comparison(scan_results, bundles)
        self.assertEqual(rows[0].signal_delta, 0.0)
        self.assertFalse(rows[0].soft_signals_available)

    def test_symbol_case_normalisation(self):
        """scan_results ticker and bundles key are both uppercased."""
        scan_results = [_make_scan_result("aapl", 0.60, 0.70)]
        signals = _make_signals(scraped_confidence=0.80, recency_score=0.70)
        bundles = {"AAPL": _make_bundle("AAPL", signals=signals)}
        rows = compute_comparison(scan_results, bundles)
        self.assertEqual(rows[0].symbol, "AAPL")
        self.assertTrue(rows[0].soft_signals_available)


# ---------------------------------------------------------------------------
# write_comparison_json
# ---------------------------------------------------------------------------

class TestWriteComparisonJson(unittest.TestCase):

    def _make_rows(self) -> list[ComparisonRow]:
        scan_results = [
            _make_scan_result("NVDA", 0.70, 0.75),
            _make_scan_result("AMD",  0.50, 0.60),
        ]
        signals = _make_signals(
            scraped_confidence=0.85, recency_score=0.80,
            theme_alignment_score=0.70, mention_acceleration=0.3,
            source_count=2, headline_count_30d=8,
        )
        bundles = {"NVDA": _make_bundle("NVDA", signals=signals)}
        return compute_comparison(scan_results, bundles)

    def test_json_file_created(self):
        with tempfile.TemporaryDirectory() as d:
            rows = self._make_rows()
            path = write_comparison_json(rows, Path(d))
            self.assertTrue(path.exists())

    def test_json_top_level_keys(self):
        with tempfile.TemporaryDirectory() as d:
            rows = self._make_rows()
            path = write_comparison_json(rows, Path(d))
            data = json.loads(path.read_text())
            required = {
                "generated_at", "mode", "blend_weights",
                "max_signal_boost", "max_conf_boost",
                "symbols_total", "symbols_with_soft_signals",
                "symbols_rank_changed", "max_signal_delta",
                "comparison",
            }
            self.assertTrue(required.issubset(set(data.keys())))

    def test_json_comparison_list_length(self):
        with tempfile.TemporaryDirectory() as d:
            rows = self._make_rows()
            path = write_comparison_json(rows, Path(d))
            data = json.loads(path.read_text())
            self.assertEqual(len(data["comparison"]), len(rows))

    def test_json_mode_is_shadow_comparison(self):
        with tempfile.TemporaryDirectory() as d:
            rows = self._make_rows()
            path = write_comparison_json(rows, Path(d))
            data = json.loads(path.read_text())
            self.assertEqual(data["mode"], "shadow_comparison")

    def test_json_symbols_with_signals_count(self):
        with tempfile.TemporaryDirectory() as d:
            rows = self._make_rows()
            path = write_comparison_json(rows, Path(d))
            data = json.loads(path.read_text())
            # Only NVDA has signals; AMD does not
            self.assertEqual(data["symbols_with_soft_signals"], 1)

    def test_json_each_row_has_required_keys(self):
        with tempfile.TemporaryDirectory() as d:
            rows = self._make_rows()
            path = write_comparison_json(rows, Path(d))
            data = json.loads(path.read_text())
            required = {"symbol", "signal_delta", "rank_change", "soft_signals_available"}
            for row in data["comparison"]:
                self.assertTrue(required.issubset(set(row.keys())))


# ---------------------------------------------------------------------------
# write_comparison_md
# ---------------------------------------------------------------------------

class TestWriteComparisonMd(unittest.TestCase):

    def _make_rows(self) -> list[ComparisonRow]:
        scan_results = [
            _make_scan_result("GOOGL", 0.65, 0.80),
            _make_scan_result("META",  0.48, 0.55),
        ]
        signals = _make_signals(
            scraped_confidence=0.90, recency_score=0.85,
            source_count=4, headline_count_30d=15,
        )
        bundles = {"GOOGL": _make_bundle("GOOGL", signals=signals)}
        return compute_comparison(scan_results, bundles)

    def test_md_file_created(self):
        with tempfile.TemporaryDirectory() as d:
            rows = self._make_rows()
            path = write_comparison_md(rows, Path(d))
            self.assertTrue(path.exists())

    def test_md_contains_table_header(self):
        with tempfile.TemporaryDirectory() as d:
            rows = self._make_rows()
            path = write_comparison_md(rows, Path(d))
            text = path.read_text(encoding="utf-8")
            self.assertIn("| Symbol |", text)
            self.assertIn("Enr Sig", text)   # ASCII-safe column name present
            self.assertIn("Rank", text)

    def test_md_contains_top_movers_section(self):
        with tempfile.TemporaryDirectory() as d:
            rows = self._make_rows()
            path = write_comparison_md(rows, Path(d))
            text = path.read_text()
            self.assertIn("Top Signal Movers", text)

    def test_md_contains_symbol_names(self):
        with tempfile.TemporaryDirectory() as d:
            rows = self._make_rows()
            path = write_comparison_md(rows, Path(d))
            text = path.read_text()
            self.assertIn("GOOGL", text)
            self.assertIn("META", text)

    def test_md_marks_symbols_with_signals(self):
        with tempfile.TemporaryDirectory() as d:
            rows = self._make_rows()
            path = write_comparison_md(rows, Path(d))
            text = path.read_text(encoding="utf-8")
            # GOOGL has signals → appears in table with the soft-data marker
            self.assertIn("GOOGL", text)
            # The marker character is present somewhere in the document
            self.assertIn("\u2713", text)   # ✓ CHECK MARK

    def test_md_shadow_disclaimer_present(self):
        with tempfile.TemporaryDirectory() as d:
            rows = self._make_rows()
            path = write_comparison_md(rows, Path(d))
            text = path.read_text()
            self.assertIn("shadow mode", text)


# ---------------------------------------------------------------------------
# run_comparison (integration)
# ---------------------------------------------------------------------------

class TestRunComparison(unittest.TestCase):

    def test_run_creates_both_files(self):
        scan_results = [
            _make_scan_result("TSLA", 0.55, 0.65),
            _make_scan_result("AAPL", 0.70, 0.80),
        ]
        signals = _make_signals(
            scraped_confidence=0.80, recency_score=0.70,
            theme_alignment_score=0.60, mention_acceleration=0.4,
            source_count=3, headline_count_30d=10,
        )
        bundles = {"TSLA": _make_bundle("TSLA", signals=signals)}
        with tempfile.TemporaryDirectory() as d:
            rows = run_comparison(scan_results, bundles, output_dir=d)
            self.assertTrue((Path(d) / "scraped_intel_comparison.json").exists())
            self.assertTrue((Path(d) / "scraped_intel_comparison.md").exists())

    def test_run_returns_correct_row_count(self):
        scan_results = [
            _make_scan_result("A", 0.60, 0.70),
            _make_scan_result("B", 0.50, 0.60),
            _make_scan_result("C", 0.40, 0.50),
        ]
        signals_a = _make_signals(scraped_confidence=0.75)
        bundles = {"A": _make_bundle("A", signals=signals_a)}
        with tempfile.TemporaryDirectory() as d:
            rows = run_comparison(scan_results, bundles, output_dir=d)
        self.assertEqual(len(rows), 3)

    def test_run_respects_config_boost_params(self):
        scan_results = [_make_scan_result("X", 0.50, 0.60)]
        signals = _make_signals(
            scraped_confidence=1.0, recency_score=1.0,
            theme_alignment_score=1.0, mention_acceleration=1.0,
        )
        bundles = {"X": _make_bundle("X", signals=signals)}
        cfg = {
            "comparison_max_signal_boost": 0.05,
            "comparison_max_conf_boost": 0.03,
        }
        with tempfile.TemporaryDirectory() as d:
            rows = run_comparison(scan_results, bundles, output_dir=d, config=cfg)
        self.assertLessEqual(rows[0].signal_delta, 0.05 + 1e-6)
        self.assertLessEqual(rows[0].confidence_delta, 0.03 + 1e-6)

    def test_run_empty_scan_results(self):
        with tempfile.TemporaryDirectory() as d:
            rows = run_comparison([], {}, output_dir=d)
        self.assertEqual(rows, [])

    def test_run_json_valid(self):
        scan_results = [_make_scan_result("AMZN", 0.60, 0.70)]
        signals = _make_signals(scraped_confidence=0.70)
        bundles = {"AMZN": _make_bundle("AMZN", signals=signals)}
        with tempfile.TemporaryDirectory() as d:
            run_comparison(scan_results, bundles, output_dir=d)
            text = (Path(d) / "scraped_intel_comparison.json").read_text()
        data = json.loads(text)   # must not raise
        self.assertEqual(data["symbols_total"], 1)


if __name__ == "__main__":
    unittest.main()
