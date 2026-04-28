"""
Tests for theme-weighted signal boosting.

Covers:
    - Boost applied when both alignment_score and strength_score >= 0.6
    - No boost when alignment_score < 0.6 (strength above threshold)
    - No boost when strength_score < 0.6 (alignment above threshold)
    - signal_score and confidence_score capped at 1.0
    - augmented_signal_score uses boosted signal_score
    - theme_boost_applied / theme_boost_factor metadata correct
    - Ranking order changes when boost is applied
    - load_theme_signals returns correct data from file
    - _compute_theme_strength returns max confidence for symbol
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from watchlist_scanner.theme_alignment import (
    enrich_row_with_theme,
    load_theme_signals,
    _compute_theme_strength,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_row(
    ticker: str = "NVDA",
    signal_score: float = 0.70,
    confidence_score: float = 0.80,
    theme_alignment_score: float = 0.0,
) -> dict:
    return {
        "ticker": ticker,
        "signal_score": signal_score,
        "confidence_score": confidence_score,
        "theme_alignment_score": theme_alignment_score,
    }


def _lm_theme(name: str, confidence: float, tickers: list[str]) -> dict:
    return {"name": name, "confidence": confidence, "tickers": tickers}


def _discovery_theme(name: str, score: float, confidence: float, tickers: list[str]) -> dict:
    """Minimal theme_opportunities entry that compute_theme_alignment accepts."""
    return {
        "name": name,
        "score": score,
        "confidence": confidence,
        "tickers": tickers,
        "theme_type": "sector",
        "persistence_score": 0.0,
        "acceleration_score": 0.0,
        "source_count": 1,
    }


# ---------------------------------------------------------------------------
# TestThemeBoostApplied
# ---------------------------------------------------------------------------

class TestThemeBoostApplied(unittest.TestCase):

    def _enrich(self, row: dict, discovery_themes: list, lm_themes: list) -> dict:
        enrich_row_with_theme(row, discovery_themes, lm_themes=lm_themes)
        return row

    # -- boost applied -------------------------------------------------------

    def test_boost_applied_when_both_thresholds_met(self):
        """When alignment >= 0.6 and strength >= 0.6, boost is applied."""
        # Build a discovery theme that yields alignment_score >= 0.6
        # strongest_component = score * confidence = 1.0 * 1.0 = 1.0
        # raw_alignment = 0.5 * 1.0 = 0.5 + persistence/acceleration components
        # Use persistence and acceleration to push it above 0.6
        disc = [_discovery_theme("AI Infrastructure", 1.0, 1.0, ["NVDA"])]
        # Override persistence and acceleration to push alignment >= 0.6
        disc[0]["persistence_score"] = 0.6
        disc[0]["acceleration_score"] = 0.5
        # raw = 0.5*1.0 + 0.2*0.6 + 0.2*0.5 + 0.1*min(1/3,1) = 0.5+0.12+0.10+0.033 = 0.753
        lm = [_lm_theme("AI Infrastructure", 0.85, ["NVDA"])]
        row = _make_row("NVDA", signal_score=0.70, confidence_score=0.80)
        self._enrich(row, disc, lm)
        self.assertTrue(row["theme_boost_applied"])

    def test_boost_increases_signal_score(self):
        disc = [_discovery_theme("AI Infrastructure", 1.0, 1.0, ["NVDA"])]
        disc[0]["persistence_score"] = 0.6
        disc[0]["acceleration_score"] = 0.5
        lm = [_lm_theme("AI Infrastructure", 0.85, ["NVDA"])]
        row = _make_row("NVDA", signal_score=0.70, confidence_score=0.80)
        orig_signal = row["signal_score"]
        self._enrich(row, disc, lm)
        self.assertGreater(row["signal_score"], orig_signal)

    def test_boost_increases_confidence_score(self):
        disc = [_discovery_theme("AI Infrastructure", 1.0, 1.0, ["NVDA"])]
        disc[0]["persistence_score"] = 0.6
        disc[0]["acceleration_score"] = 0.5
        lm = [_lm_theme("AI Infrastructure", 0.85, ["NVDA"])]
        row = _make_row("NVDA", signal_score=0.70, confidence_score=0.80)
        orig_conf = row["confidence_score"]
        self._enrich(row, disc, lm)
        self.assertGreater(row["confidence_score"], orig_conf)

    def test_boost_formula_signal_score(self):
        """signal_score *= (1 + 0.15 * theme_strength_score)."""
        disc = [_discovery_theme("AI Infrastructure", 1.0, 1.0, ["NVDA"])]
        disc[0]["persistence_score"] = 0.6
        disc[0]["acceleration_score"] = 0.5
        strength = 0.85
        lm = [_lm_theme("AI Infrastructure", strength, ["NVDA"])]
        row = _make_row("NVDA", signal_score=0.70, confidence_score=0.80)
        self._enrich(row, disc, lm)
        expected = round(min(0.70 * (1 + 0.15 * strength), 1.0), 4)
        self.assertAlmostEqual(row["signal_score"], expected, places=4)

    def test_boost_formula_confidence_score(self):
        """confidence_score *= (1 + 0.10 * theme_strength_score)."""
        disc = [_discovery_theme("AI Infrastructure", 1.0, 1.0, ["NVDA"])]
        disc[0]["persistence_score"] = 0.6
        disc[0]["acceleration_score"] = 0.5
        strength = 0.85
        lm = [_lm_theme("AI Infrastructure", strength, ["NVDA"])]
        row = _make_row("NVDA", signal_score=0.70, confidence_score=0.80)
        self._enrich(row, disc, lm)
        expected = round(min(0.80 * (1 + 0.10 * strength), 1.0), 4)
        self.assertAlmostEqual(row["confidence_score"], expected, places=4)

    def test_boost_factor_metadata(self):
        """theme_boost_factor == 1 + 0.15 * strength when boost applied."""
        disc = [_discovery_theme("AI Infrastructure", 1.0, 1.0, ["NVDA"])]
        disc[0]["persistence_score"] = 0.6
        disc[0]["acceleration_score"] = 0.5
        strength = 0.85
        lm = [_lm_theme("AI Infrastructure", strength, ["NVDA"])]
        row = _make_row("NVDA", signal_score=0.70, confidence_score=0.80)
        self._enrich(row, disc, lm)
        expected_factor = round(1.0 + 0.15 * strength, 4)
        self.assertAlmostEqual(row["theme_boost_factor"], expected_factor, places=4)

    def test_theme_strength_score_recorded(self):
        """theme_strength_score field is set to the LLM confidence of the matched theme."""
        disc = [_discovery_theme("AI Infrastructure", 1.0, 1.0, ["NVDA"])]
        disc[0]["persistence_score"] = 0.6
        disc[0]["acceleration_score"] = 0.5
        lm = [_lm_theme("AI Infrastructure", 0.85, ["NVDA"])]
        row = _make_row("NVDA")
        self._enrich(row, disc, lm)
        self.assertAlmostEqual(row["theme_strength_score"], 0.85, places=4)

    # -- no boost when one threshold fails -----------------------------------

    def test_no_boost_when_alignment_below_threshold(self):
        """No boost when alignment_score < 0.6, even if strength >= 0.6."""
        # Low score/confidence → low alignment
        disc = [_discovery_theme("AI Infrastructure", 0.3, 0.3, ["NVDA"])]
        lm = [_lm_theme("AI Infrastructure", 0.90, ["NVDA"])]
        row = _make_row("NVDA", signal_score=0.70, confidence_score=0.80)
        orig_signal = row["signal_score"]
        self._enrich(row, disc, lm)
        self.assertFalse(row["theme_boost_applied"])
        self.assertAlmostEqual(row["signal_score"], orig_signal, places=4)

    def test_no_boost_when_strength_below_threshold(self):
        """No boost when strength_score < 0.6, even if alignment >= 0.6."""
        disc = [_discovery_theme("AI Infrastructure", 1.0, 1.0, ["NVDA"])]
        disc[0]["persistence_score"] = 0.6
        disc[0]["acceleration_score"] = 0.5
        lm = [_lm_theme("AI Infrastructure", 0.50, ["NVDA"])]  # below 0.6
        row = _make_row("NVDA", signal_score=0.70, confidence_score=0.80)
        orig_signal = row["signal_score"]
        self._enrich(row, disc, lm)
        self.assertFalse(row["theme_boost_applied"])
        self.assertAlmostEqual(row["signal_score"], orig_signal, places=4)

    def test_no_boost_when_no_lm_themes(self):
        """No boost when LLM themes list is empty (strength=0 < threshold)."""
        disc = [_discovery_theme("AI Infrastructure", 1.0, 1.0, ["NVDA"])]
        disc[0]["persistence_score"] = 0.6
        disc[0]["acceleration_score"] = 0.5
        row = _make_row("NVDA", signal_score=0.70)
        self._enrich(row, disc, lm_themes=[])
        self.assertFalse(row["theme_boost_applied"])
        self.assertEqual(row["theme_strength_score"], 0.0)

    def test_no_boost_when_ticker_not_in_lm_themes(self):
        """No boost when symbol doesn't appear in any LLM theme tickers."""
        disc = [_discovery_theme("AI Infrastructure", 1.0, 1.0, ["NVDA"])]
        disc[0]["persistence_score"] = 0.6
        disc[0]["acceleration_score"] = 0.5
        lm = [_lm_theme("AI Infrastructure", 0.90, ["AMD", "INTC"])]  # no NVDA
        row = _make_row("NVDA", signal_score=0.70)
        self._enrich(row, disc, lm_themes=lm)
        self.assertFalse(row["theme_boost_applied"])
        self.assertEqual(row["theme_strength_score"], 0.0)

    def test_boost_factor_is_1_when_not_applied(self):
        """theme_boost_factor == 1.0 when no boost applied."""
        row = _make_row("NVDA")
        self._enrich(row, [], lm_themes=[])
        self.assertEqual(row["theme_boost_factor"], 1.0)

    # -- cap enforcement -----------------------------------------------------

    def test_signal_score_capped_at_1(self):
        """Boosted signal_score never exceeds 1.0."""
        disc = [_discovery_theme("AI Infrastructure", 1.0, 1.0, ["NVDA"])]
        disc[0]["persistence_score"] = 0.6
        disc[0]["acceleration_score"] = 0.5
        lm = [_lm_theme("AI Infrastructure", 1.0, ["NVDA"])]
        row = _make_row("NVDA", signal_score=0.99, confidence_score=0.50)
        self._enrich(row, disc, lm)
        self.assertLessEqual(row["signal_score"], 1.0)

    def test_confidence_score_capped_at_1(self):
        """Boosted confidence_score never exceeds 1.0."""
        disc = [_discovery_theme("AI Infrastructure", 1.0, 1.0, ["NVDA"])]
        disc[0]["persistence_score"] = 0.6
        disc[0]["acceleration_score"] = 0.5
        lm = [_lm_theme("AI Infrastructure", 1.0, ["NVDA"])]
        row = _make_row("NVDA", signal_score=0.50, confidence_score=0.99)
        self._enrich(row, disc, lm)
        self.assertLessEqual(row["confidence_score"], 1.0)

    def test_augmented_signal_score_capped_at_1(self):
        """augmented_signal_score never exceeds 1.0 after boost."""
        disc = [_discovery_theme("AI Infrastructure", 1.0, 1.0, ["NVDA"])]
        disc[0]["persistence_score"] = 1.0
        disc[0]["acceleration_score"] = 1.0
        lm = [_lm_theme("AI Infrastructure", 1.0, ["NVDA"])]
        row = _make_row("NVDA", signal_score=1.0, confidence_score=1.0)
        self._enrich(row, disc, lm)
        self.assertLessEqual(row["augmented_signal_score"], 1.0)

    # -- augmented_signal_score uses boosted signal --------------------------

    def test_augmented_signal_score_uses_boosted_signal(self):
        """augmented_signal_score = boosted_signal + theme_component (capped at 1.0)."""
        disc = [_discovery_theme("AI Infrastructure", 1.0, 1.0, ["NVDA"])]
        disc[0]["persistence_score"] = 0.6
        disc[0]["acceleration_score"] = 0.5
        strength = 0.85
        lm = [_lm_theme("AI Infrastructure", strength, ["NVDA"])]
        row = _make_row("NVDA", signal_score=0.70, confidence_score=0.80)
        self._enrich(row, disc, lm)
        boosted_signal = row["signal_score"]
        theme_component = row["theme_component"]
        expected_augmented = round(min(boosted_signal + theme_component, 1.0), 4)
        self.assertAlmostEqual(row["augmented_signal_score"], expected_augmented, places=4)


# ---------------------------------------------------------------------------
# TestThemeBoostRanking
# ---------------------------------------------------------------------------

class TestThemeBoostRanking(unittest.TestCase):

    def _enrich_and_rank(
        self,
        rows: list[dict],
        disc_per_row: list[list],
        lm_per_row: list[list],
    ) -> list[dict]:
        for row, disc, lm in zip(rows, disc_per_row, lm_per_row):
            enrich_row_with_theme(row, disc, lm_themes=lm)
        rows.sort(key=lambda r: r.get("augmented_signal_score", 0.0), reverse=True)
        return rows

    def test_boosted_signal_ranks_higher_than_unboosted(self):
        """A signal that receives boost should rank above an unboosted peer."""
        disc_strong = [_discovery_theme("AI Infrastructure", 1.0, 1.0, ["NVDA"])]
        disc_strong[0]["persistence_score"] = 0.6
        disc_strong[0]["acceleration_score"] = 0.5
        lm_strong = [_lm_theme("AI Infrastructure", 0.85, ["NVDA"])]

        disc_weak = [_discovery_theme("Cloud", 0.3, 0.3, ["MSFT"])]
        lm_weak = [_lm_theme("Cloud", 0.50, ["MSFT"])]

        nvda = _make_row("NVDA", signal_score=0.65, confidence_score=0.80)
        msft = _make_row("MSFT", signal_score=0.70, confidence_score=0.80)

        ranked = self._enrich_and_rank(
            [nvda, msft],
            [disc_strong, disc_weak],
            [lm_strong, lm_weak],
        )

        self.assertTrue(ranked[0]["theme_boost_applied"], "Expected NVDA (boosted) to rank first")
        self.assertEqual(ranked[0]["ticker"], "NVDA")

    def test_two_boosted_signals_ranked_by_boosted_score(self):
        """When both signals are boosted, higher raw signal still ranks first."""
        def _strong_disc(ticker: str) -> list:
            d = [_discovery_theme("AI Infrastructure", 1.0, 1.0, [ticker])]
            d[0]["persistence_score"] = 0.6
            d[0]["acceleration_score"] = 0.5
            return d

        lm_high = [_lm_theme("AI Infrastructure", 0.90, ["NVDA"])]
        lm_low = [_lm_theme("AI Infrastructure", 0.80, ["AMD"])]

        nvda = _make_row("NVDA", signal_score=0.75, confidence_score=0.80)
        amd = _make_row("AMD", signal_score=0.65, confidence_score=0.80)

        ranked = self._enrich_and_rank(
            [amd, nvda],
            [_strong_disc("AMD"), _strong_disc("NVDA")],
            [lm_low, lm_high],
        )
        self.assertEqual(ranked[0]["ticker"], "NVDA")

    def test_no_boost_preserves_original_ranking(self):
        """When no boost conditions are met, original relative ranking is unchanged."""
        nvda = _make_row("NVDA", signal_score=0.80)
        amd = _make_row("AMD", signal_score=0.60)

        for row in [nvda, amd]:
            enrich_row_with_theme(row, [], lm_themes=[])

        self.assertGreater(nvda["augmented_signal_score"], amd["augmented_signal_score"])


# ---------------------------------------------------------------------------
# TestLoadThemeSignals
# ---------------------------------------------------------------------------

class TestLoadThemeSignals(unittest.TestCase):

    def test_load_theme_signals_returns_themes(self):
        """load_theme_signals returns the themes list from a valid JSON file."""
        themes = [
            {"name": "AI Infrastructure", "confidence": 0.85, "tickers": ["NVDA", "AMD"]},
            {"name": "Cybersecurity", "confidence": 0.72, "tickers": ["CRWD"]},
        ]
        payload = {"run_date": "2026-04-28", "themes": themes}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "outputs" / "latest"
            out.mkdir(parents=True)
            (out / "theme_signals.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            result = load_theme_signals(root)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["name"], "AI Infrastructure")

    def test_load_theme_signals_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = load_theme_signals(Path(tmp))
        self.assertEqual(result, [])

    def test_load_theme_signals_malformed_json_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "outputs" / "latest"
            out.mkdir(parents=True)
            (out / "theme_signals.json").write_text("NOT JSON", encoding="utf-8")
            result = load_theme_signals(root)
        self.assertEqual(result, [])

    def test_load_theme_signals_empty_themes_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "outputs" / "latest"
            out.mkdir(parents=True)
            (out / "theme_signals.json").write_text(
                json.dumps({"themes": []}), encoding="utf-8"
            )
            result = load_theme_signals(root)
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# TestComputeThemeStrength
# ---------------------------------------------------------------------------

class TestComputeThemeStrength(unittest.TestCase):

    def test_returns_max_confidence_for_symbol(self):
        """Returns max confidence when symbol appears in multiple themes."""
        lm = [
            _lm_theme("AI Infrastructure", 0.85, ["NVDA", "AMD"]),
            _lm_theme("Semiconductor", 0.72, ["NVDA"]),
        ]
        self.assertAlmostEqual(_compute_theme_strength("NVDA", lm), 0.85)

    def test_returns_zero_when_symbol_not_found(self):
        lm = [_lm_theme("AI Infrastructure", 0.85, ["AMD"])]
        self.assertEqual(_compute_theme_strength("NVDA", lm), 0.0)

    def test_returns_zero_for_empty_list(self):
        self.assertEqual(_compute_theme_strength("NVDA", []), 0.0)

    def test_returns_single_match_confidence(self):
        lm = [_lm_theme("Cybersecurity", 0.70, ["CRWD", "PANW"])]
        self.assertAlmostEqual(_compute_theme_strength("CRWD", lm), 0.70)


if __name__ == "__main__":
    unittest.main()
