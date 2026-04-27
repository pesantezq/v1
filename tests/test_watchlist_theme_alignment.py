"""
Tests for watchlist_scanner/theme_alignment.py and its integration
with alert_ranking and gui_operator_data.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from watchlist_scanner.theme_alignment import (
    load_theme_opportunities,
    match_symbol_themes,
    compute_theme_alignment,
    enrich_row_with_theme,
    _alignment_label,
    _empty_theme_fields,
)
from watchlist_scanner.alert_ranking import apply_priority_score


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _theme(name="ai infrastructure", theme_type="emerging", score=0.80,
           confidence=0.70, persistence=0.60, acceleration=0.75,
           tickers=None, source_count=3):
    return {
        "name": name,
        "theme_type": theme_type,
        "score": score,
        "confidence": confidence,
        "persistence_score": persistence,
        "acceleration_score": acceleration,
        "tickers": tickers if tickers is not None else ["NVDA", "ANET"],
        "source_count": source_count,
        "mention_count": 6,
    }


def _write_theme_json(tmpdir, themes):
    path = Path(tmpdir) / "outputs" / "latest"
    path.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": "2026-04-27T10:00:00", "theme_count": len(themes), "themes": themes}
    (path / "theme_opportunities.json").write_text(json.dumps(payload), encoding="utf-8")
    return Path(tmpdir)


def _signal_row(ticker="NVDA", signal_score=0.65, confidence_score=0.80,
                data_quality="fresh", evidence_breadth=2, alert_tier="medium"):
    return {
        "ticker": ticker,
        "signal_score": signal_score,
        "confidence_score": confidence_score,
        "data_quality": data_quality,
        "evidence_breadth": evidence_breadth,
        "alert_tier": alert_tier,
    }


# ===========================================================================
# A. Loader
# ===========================================================================

class TestLoader(unittest.TestCase):

    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = load_theme_opportunities(tmp)
        self.assertEqual(result, [])

    def test_malformed_json_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "outputs" / "latest"
            path.mkdir(parents=True)
            (path / "theme_opportunities.json").write_text("{not valid json", encoding="utf-8")
            result = load_theme_opportunities(tmp)
        self.assertEqual(result, [])

    def test_valid_empty_themes_list_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_theme_json(tmp, [])
            result = load_theme_opportunities(root)
        self.assertEqual(result, [])

    def test_valid_themes_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_theme_json(tmp, [_theme("energy"), _theme("semiconductors")])
            result = load_theme_opportunities(root)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["name"], "energy")

    def test_wrong_schema_no_themes_key_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "outputs" / "latest"
            path.mkdir(parents=True)
            (path / "theme_opportunities.json").write_text(
                json.dumps({"generated_at": "2026-04-27", "count": 0}), encoding="utf-8"
            )
            result = load_theme_opportunities(tmp)
        self.assertEqual(result, [])


# ===========================================================================
# B. Matching
# ===========================================================================

class TestMatching(unittest.TestCase):

    def setUp(self):
        self.themes = [
            _theme("ai infrastructure", tickers=["NVDA", "ANET"], score=0.80),
            _theme("energy", tickers=["XOM", "CVX"], score=0.70),
            _theme("semiconductors", tickers=["NVDA", "AMD", "INTC"], score=0.75),
        ]

    def test_no_match_returns_empty(self):
        self.assertEqual(match_symbol_themes("AAPL", self.themes), [])

    def test_single_match(self):
        matched = match_symbol_themes("XOM", self.themes)
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["name"], "energy")

    def test_multiple_matches(self):
        matched = match_symbol_themes("NVDA", self.themes)
        self.assertEqual(len(matched), 2)
        names = [t["name"] for t in matched]
        self.assertIn("ai infrastructure", names)
        self.assertIn("semiconductors", names)

    def test_sort_order_score_desc(self):
        matched = match_symbol_themes("NVDA", self.themes)
        scores = [t["score"] for t in matched]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_sort_order_name_asc_on_tie(self):
        tied = [
            _theme("zzz theme", tickers=["SYM"], score=0.50),
            _theme("aaa theme", tickers=["SYM"], score=0.50),
        ]
        matched = match_symbol_themes("SYM", tied)
        self.assertEqual(matched[0]["name"], "aaa theme")

    def test_deduplication_stable(self):
        # same call twice must return same order
        m1 = match_symbol_themes("NVDA", self.themes)
        m2 = match_symbol_themes("NVDA", self.themes)
        self.assertEqual([t["name"] for t in m1], [t["name"] for t in m2])

    def test_empty_themes_list(self):
        self.assertEqual(match_symbol_themes("NVDA", []), [])


# ===========================================================================
# C. Scoring
# ===========================================================================

class TestScoring(unittest.TestCase):

    def test_no_matches_gives_zero_score_and_none_label(self):
        fields = compute_theme_alignment([])
        self.assertEqual(fields["theme_alignment_score"], 0.0)
        self.assertEqual(fields["theme_alignment_label"], "none")
        self.assertFalse(fields["theme_support_present"])

    def test_strong_theme_gives_high_score(self):
        t = _theme(score=1.0, confidence=1.0, persistence=1.0, acceleration=1.0)
        fields = compute_theme_alignment([t])
        self.assertGreaterEqual(fields["theme_alignment_score"], 0.65)
        self.assertEqual(fields["theme_alignment_label"], "strong")

    def test_weak_theme_gives_low_score(self):
        t = _theme(score=0.10, confidence=0.20, persistence=0.05, acceleration=0.10)
        fields = compute_theme_alignment([t])
        self.assertLess(fields["theme_alignment_score"], 0.35)

    def test_multiple_themes_gives_bounded_lift(self):
        single = compute_theme_alignment([_theme(score=0.70, confidence=0.70)])
        multi = compute_theme_alignment([
            _theme(score=0.70, confidence=0.70),
            _theme("b", score=0.65, confidence=0.60, tickers=["X"]),
            _theme("c", score=0.50, confidence=0.50, tickers=["Y"]),
        ])
        # Multi-theme should score >= single due to breadth bonus
        self.assertGreaterEqual(multi["theme_alignment_score"], single["theme_alignment_score"])
        # But must stay capped at 1.0
        self.assertLessEqual(multi["theme_alignment_score"], 1.0)

    def test_alignment_score_in_0_1_range(self):
        t = _theme(score=0.99, confidence=0.99, persistence=0.99, acceleration=0.99)
        fields = compute_theme_alignment([t])
        self.assertGreaterEqual(fields["theme_alignment_score"], 0.0)
        self.assertLessEqual(fields["theme_alignment_score"], 1.0)

    def test_label_thresholds(self):
        self.assertEqual(_alignment_label(0.0), "none")
        self.assertEqual(_alignment_label(0.10), "weak")
        self.assertEqual(_alignment_label(0.34), "weak")
        self.assertEqual(_alignment_label(0.35), "moderate")
        self.assertEqual(_alignment_label(0.64), "moderate")
        self.assertEqual(_alignment_label(0.65), "strong")
        self.assertEqual(_alignment_label(1.0), "strong")

    def test_explainability_fields_present(self):
        t = _theme()
        fields = compute_theme_alignment([t])
        for key in (
            "theme_top_name", "theme_top_type", "theme_top_score",
            "theme_top_confidence", "theme_top_persistence_score",
            "theme_top_acceleration_score", "theme_reason", "theme_context",
        ):
            self.assertIn(key, fields)

    def test_theme_reason_mentions_name(self):
        t = _theme(name="green energy")
        fields = compute_theme_alignment([t])
        self.assertIn("green energy", fields["theme_reason"])

    def test_persistence_flag_in_reason(self):
        t = _theme(persistence=0.80)
        fields = compute_theme_alignment([t])
        self.assertIn("persistent", fields["theme_reason"])

    def test_no_persistence_flag_when_low(self):
        t = _theme(persistence=0.30)
        fields = compute_theme_alignment([t])
        self.assertNotIn("persistent", fields["theme_reason"])


# ===========================================================================
# D. Row enrichment
# ===========================================================================

class TestRowEnrichment(unittest.TestCase):

    def test_enrich_no_themes_sets_zero_alignment(self):
        row = _signal_row("AAPL")
        enrich_row_with_theme(row, [])
        self.assertEqual(row["theme_alignment_score"], 0.0)
        self.assertEqual(row["theme_alignment_label"], "none")

    def test_enrich_no_themes_augmented_equals_signal(self):
        row = _signal_row("AAPL", signal_score=0.65)
        enrich_row_with_theme(row, [])
        self.assertAlmostEqual(row["augmented_signal_score"], 0.65, places=4)

    def test_enrich_matching_theme_augmented_greater_than_signal(self):
        row = _signal_row("NVDA", signal_score=0.65)
        themes = [_theme(score=0.80, confidence=0.80, persistence=0.70, acceleration=0.70,
                         tickers=["NVDA"])]
        enrich_row_with_theme(row, themes)
        self.assertGreater(row["augmented_signal_score"], 0.65)

    def test_augmented_capped_at_1(self):
        row = _signal_row("NVDA", signal_score=0.98)
        themes = [_theme(score=1.0, confidence=1.0, persistence=1.0, acceleration=1.0,
                         tickers=["NVDA"])]
        enrich_row_with_theme(row, themes)
        self.assertLessEqual(row["augmented_signal_score"], 1.0)

    def test_enrich_sets_theme_component(self):
        row = _signal_row("NVDA", signal_score=0.60)
        themes = [_theme(tickers=["NVDA"])]
        enrich_row_with_theme(row, themes)
        self.assertIn("theme_component", row)
        self.assertGreater(row["theme_component"], 0.0)

    def test_enrich_no_match_symbol_not_in_tickers(self):
        row = _signal_row("MSFT", signal_score=0.60)
        themes = [_theme(tickers=["NVDA", "AMD"])]
        enrich_row_with_theme(row, themes)
        self.assertFalse(row["theme_support_present"])
        self.assertAlmostEqual(row["augmented_signal_score"], 0.60, places=4)

    def test_enrich_safe_with_empty_row(self):
        row = {}
        enrich_row_with_theme(row, [])
        self.assertIn("theme_alignment_score", row)
        self.assertIn("augmented_signal_score", row)

    def test_enrich_does_not_overwrite_signal_score(self):
        row = _signal_row("NVDA", signal_score=0.72)
        themes = [_theme(tickers=["NVDA"])]
        enrich_row_with_theme(row, themes)
        self.assertAlmostEqual(row["signal_score"], 0.72, places=4)


# ===========================================================================
# D. Integration: alert_ranking augmented_priority_score
# ===========================================================================

class TestAlertRankingAugmented(unittest.TestCase):

    def _row(self, signal_score=0.65, augmented_signal_score=None,
             confidence_score=0.80, evidence_breadth=2, data_quality="fresh",
             alert_tier="medium"):
        row = {
            "signal_score": signal_score,
            "confidence_score": confidence_score,
            "evidence_breadth": evidence_breadth,
            "data_quality": data_quality,
            "alert_tier": alert_tier,
        }
        if augmented_signal_score is not None:
            row["augmented_signal_score"] = augmented_signal_score
        return row

    def test_no_augmented_score_priority_score_unchanged(self):
        row = self._row(signal_score=0.65)
        apply_priority_score(row)
        self.assertIn("priority_score", row)
        self.assertIn("augmented_priority_score", row)
        # Without augmented, both should be equal
        self.assertAlmostEqual(row["priority_score"], row["augmented_priority_score"], places=4)

    def test_augmented_priority_score_higher_with_theme(self):
        row = self._row(signal_score=0.65, augmented_signal_score=0.72)
        apply_priority_score(row)
        self.assertGreater(row["augmented_priority_score"], row["priority_score"])

    def test_priority_score_unchanged_when_augmented_present(self):
        row = self._row(signal_score=0.65, augmented_signal_score=0.80)
        apply_priority_score(row)
        expected_priority = round(
            0.65 * 0.45 + 0.80 * 0.30 + (2 / 3.0) * 0.15 + 1.00 * 0.10, 4
        )
        self.assertAlmostEqual(row["priority_score"], expected_priority, places=3)

    def test_augmented_priority_score_capped_implicitly(self):
        # augmented_signal_score at 1.0 should give a valid float <= 1.0
        row = self._row(signal_score=0.90, augmented_signal_score=1.0,
                        confidence_score=1.0, data_quality="fresh", evidence_breadth=3)
        apply_priority_score(row)
        self.assertLessEqual(row["augmented_priority_score"], 1.0)


# ===========================================================================
# E. GUI/data loader safety (gui_operator_data triage normalization)
# ===========================================================================

class TestGUIDataLoaderSafety(unittest.TestCase):

    def _normalize(self, rows):
        """Invoke the real _normalize_signal_triage via gui_operator_data."""
        import gui_operator_data as god
        watchlist = {"results": rows}
        return god._normalize_signal_triage(watchlist)

    def test_rows_without_theme_fields_do_not_crash(self):
        row = {
            "ticker": "AAPL",
            "conviction_band": "normal",
            "conviction_score": 0.65,
        }
        result = self._normalize([row])
        self.assertTrue(result["available"])
        self.assertEqual(result["rows"][0]["theme_alignment_label"], "none")
        self.assertIsNone(result["rows"][0]["theme_top_name"])

    def test_rows_with_theme_fields_preserved(self):
        row = {
            "ticker": "NVDA",
            "conviction_band": "high_conviction",
            "conviction_score": 0.85,
            "theme_alignment_label": "strong",
            "theme_top_name": "ai infrastructure",
            "theme_match_count": 2,
            "augmented_signal_score": 0.77,
            "theme_reason": "Matched 2 themes",
        }
        result = self._normalize([row])
        trow = result["rows"][0]
        self.assertEqual(trow["theme_alignment_label"], "strong")
        self.assertEqual(trow["theme_top_name"], "ai infrastructure")
        self.assertEqual(trow["theme_match_count"], 2)
        self.assertAlmostEqual(trow["augmented_signal_score"], 0.77, places=4)

    def test_empty_results_returns_unavailable(self):
        result = self._normalize([])
        self.assertFalse(result["available"])

    def test_theme_match_count_defaults_to_zero(self):
        row = {"ticker": "MSFT", "conviction_band": "normal"}
        result = self._normalize([row])
        self.assertEqual(result["rows"][0]["theme_match_count"], 0)

    def test_theme_reason_defaults_to_empty_string(self):
        row = {"ticker": "GOOG", "conviction_band": "starter"}
        result = self._normalize([row])
        self.assertEqual(result["rows"][0]["theme_reason"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
