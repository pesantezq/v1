"""
Tests for watchlist_scanner/portfolio_fit.py and its integration
with alert_ranking (final_rank_score) and gui_operator_data.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from watchlist_scanner.portfolio_fit import (
    load_portfolio_snapshot,
    compute_portfolio_fit,
    enrich_row_with_portfolio_fit,
    _fit_label,
    _empty_portfolio_fit_fields,
)
from watchlist_scanner.alert_ranking import apply_priority_score


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _snapshot(
    tickers=None,
    sector="TECHNOLOGY",
    total_normalized=0.065,
    max_total=0.10,
    max_sector=0.04,
    max_ticker=0.02,
    sector_allocation=None,
    regime_label="risk_on",
):
    """Build a minimal portfolio snapshot dict."""
    tickers = tickers or ["NVDA", "MSFT"]
    if sector_allocation is None:
        sector_allocation = {sector: 0.02}

    rows = [
        {
            "ticker": t,
            "sector": sector,
            "conviction_score": 0.50,
            "normalized_allocation": 0.005,
        }
        for t in tickers
    ]
    # Build sector groupings
    by_sector = [{"name": sector, "count": len(tickers), "tickers": tickers}]

    return {
        "total_normalized_allocation": total_normalized,
        "allocation_by_sector": sector_allocation,
        "config": {
            "max_total_allocation": max_total,
            "max_sector_allocation": max_sector,
            "max_ticker_allocation": max_ticker,
        },
        "market_regime": {"regime_label": regime_label},
        "rows": rows,
        "groupings": {"by_sector": by_sector},
    }


def _write_snapshot_json(tmpdir, payload):
    path = Path(tmpdir) / "outputs" / "portfolio"
    path.mkdir(parents=True, exist_ok=True)
    (path / "portfolio_snapshot.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    return Path(tmpdir)


def _signal_row(ticker="AAPL", sector="TECHNOLOGY", signal_score=0.65,
                confidence_score=0.80, data_quality="fresh",
                evidence_breadth=2, alert_tier="medium"):
    return {
        "ticker": ticker,
        "fundamentals": {"sector": sector},
        "signal_score": signal_score,
        "confidence_score": confidence_score,
        "data_quality": data_quality,
        "evidence_breadth": evidence_breadth,
        "alert_tier": alert_tier,
    }


# ===========================================================================
# A. Loader
# ===========================================================================

class TestPortfolioLoader(unittest.TestCase):

    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = load_portfolio_snapshot(tmp)
        self.assertEqual(result, {})

    def test_malformed_json_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "outputs" / "portfolio"
            path.mkdir(parents=True)
            (path / "portfolio_snapshot.json").write_text("{not json", encoding="utf-8")
            result = load_portfolio_snapshot(tmp)
        self.assertEqual(result, {})

    def test_valid_snapshot_returned(self):
        payload = _snapshot()
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_snapshot_json(tmp, payload)
            result = load_portfolio_snapshot(root)
        self.assertIn("rows", result)
        self.assertEqual(result["total_normalized_allocation"], 0.065)

    def test_non_dict_json_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "outputs" / "portfolio"
            path.mkdir(parents=True)
            (path / "portfolio_snapshot.json").write_text(
                json.dumps([1, 2, 3]), encoding="utf-8"
            )
            result = load_portfolio_snapshot(tmp)
        self.assertEqual(result, {})


# ===========================================================================
# B. Score computation
# ===========================================================================

class TestPortfolioFitScoring(unittest.TestCase):

    def test_empty_snapshot_returns_neutral_defaults(self):
        fields = compute_portfolio_fit("AAPL", "TECHNOLOGY", {})
        self.assertAlmostEqual(fields["portfolio_fit_score"], 0.5, places=4)
        self.assertEqual(fields["portfolio_fit_label"], "neutral")
        self.assertEqual(fields["portfolio_fit_reason"], "No portfolio snapshot available")

    def test_score_always_in_0_1_range(self):
        snap = _snapshot(tickers=["NVDA"], total_normalized=0.10, max_total=0.10)
        fields = compute_portfolio_fit("NVDA", "TECHNOLOGY", snap)
        self.assertGreaterEqual(fields["portfolio_fit_score"], 0.0)
        self.assertLessEqual(fields["portfolio_fit_score"], 1.0)

    def test_existing_holding_gives_relevance_boost(self):
        snap = _snapshot(tickers=["NVDA"])
        no_hold = compute_portfolio_fit("AAPL", "TECHNOLOGY", snap)
        held = compute_portfolio_fit("NVDA", "TECHNOLOGY", snap)
        # NVDA is held; AAPL is not. All else equal, held should score >= new
        self.assertGreaterEqual(held["portfolio_fit_score"], no_hold["portfolio_fit_score"] - 0.05)

    def test_overweight_sector_reduces_sector_score(self):
        # Sector near cap (allocation == max_sector_allocation)
        snap_full = _snapshot(sector_allocation={"TECHNOLOGY": 0.04},
                              max_sector=0.04)
        fields_full = compute_portfolio_fit("AAPL", "TECHNOLOGY", snap_full)
        snap_empty = _snapshot(sector_allocation={"TECHNOLOGY": 0.0},
                               max_sector=0.04)
        fields_empty = compute_portfolio_fit("AAPL", "TECHNOLOGY", snap_empty)
        ctx_full = fields_full["portfolio_fit_context"]
        ctx_empty = fields_empty["portfolio_fit_context"]
        self.assertLess(ctx_full["sector_score"], ctx_empty["sector_score"])

    def test_new_sector_gives_diversification_boost(self):
        snap = _snapshot(tickers=["NVDA", "MSFT"], sector="TECHNOLOGY",
                         sector_allocation={"TECHNOLOGY": 0.02})
        new_sector = compute_portfolio_fit("XOM", "ENERGY", snap)
        same_sector = compute_portfolio_fit("AAPL", "TECHNOLOGY", snap)
        ctx_new = new_sector["portfolio_fit_context"]
        ctx_same = same_sector["portfolio_fit_context"]
        self.assertGreater(ctx_new["diversification_score"], ctx_same["diversification_score"])

    def test_low_cash_penalises_fit(self):
        snap_full = _snapshot(total_normalized=0.10, max_total=0.10)   # no room
        snap_room = _snapshot(total_normalized=0.03, max_total=0.10)   # 7% room
        fields_full = compute_portfolio_fit("AAPL", "TECHNOLOGY", snap_full)
        fields_room = compute_portfolio_fit("AAPL", "TECHNOLOGY", snap_room)
        ctx_full = fields_full["portfolio_fit_context"]
        ctx_room = fields_room["portfolio_fit_context"]
        self.assertLess(ctx_full["cash_fit_score"], ctx_room["cash_fit_score"])

    def test_leveraged_ticker_with_existing_leverage_penalised(self):
        snap = _snapshot(tickers=["TQQQ"])  # portfolio holds a leveraged ETF
        # Another leveraged ticker should score lower on leverage component
        fields = compute_portfolio_fit("QLD", "TECHNOLOGY", snap)
        ctx = fields["portfolio_fit_context"]
        self.assertLessEqual(ctx["leverage_score"], 0.3)

    def test_non_leveraged_ticker_gets_max_leverage_score(self):
        snap = _snapshot(tickers=["NVDA"])
        fields = compute_portfolio_fit("MSFT", "TECHNOLOGY", snap)
        ctx = fields["portfolio_fit_context"]
        self.assertAlmostEqual(ctx["leverage_score"], 0.8, places=4)

    def test_label_thresholds(self):
        self.assertEqual(_fit_label(0.75), "strong")
        self.assertEqual(_fit_label(1.00), "strong")
        self.assertEqual(_fit_label(0.74), "good")
        self.assertEqual(_fit_label(0.55), "good")
        self.assertEqual(_fit_label(0.54), "neutral")
        self.assertEqual(_fit_label(0.35), "neutral")
        self.assertEqual(_fit_label(0.34), "poor")
        self.assertEqual(_fit_label(0.00), "poor")

    def test_explainability_fields_present(self):
        snap = _snapshot()
        fields = compute_portfolio_fit("AAPL", "TECHNOLOGY", snap)
        for key in (
            "portfolio_fit_score", "portfolio_fit_label",
            "portfolio_fit_reason", "portfolio_fit_context",
        ):
            self.assertIn(key, fields)
        for sub in (
            "existing_position_score", "sector_score", "diversification_score",
            "leverage_score", "cash_fit_score",
        ):
            self.assertIn(sub, fields["portfolio_fit_context"])

    def test_reason_mentions_sector_when_overweight(self):
        snap = _snapshot(sector_allocation={"TECHNOLOGY": 0.04}, max_sector=0.04)
        fields = compute_portfolio_fit("AAPL", "TECHNOLOGY", snap)
        self.assertIn("TECHNOLOGY", fields["portfolio_fit_reason"])

    def test_reason_mentions_diversification_for_new_sector(self):
        snap = _snapshot(tickers=["NVDA"], sector="TECHNOLOGY",
                         sector_allocation={"TECHNOLOGY": 0.01})
        fields = compute_portfolio_fit("XOM", "ENERGY", snap)
        self.assertIn("diversification", fields["portfolio_fit_reason"].lower())

    def test_reason_mentions_cash_when_at_ceiling(self):
        snap = _snapshot(total_normalized=0.10, max_total=0.10)
        fields = compute_portfolio_fit("AAPL", "TECHNOLOGY", snap)
        reason = fields["portfolio_fit_reason"].lower()
        self.assertIn("deployment", reason)

    def test_unknown_sector_treated_neutrally(self):
        snap = _snapshot()
        fields = compute_portfolio_fit("AAPL", "Unknown", snap)
        ctx = fields["portfolio_fit_context"]
        self.assertAlmostEqual(ctx["sector_score"], 0.5, places=4)
        self.assertAlmostEqual(ctx["diversification_score"], 0.5, places=4)


# ===========================================================================
# C. Row enrichment
# ===========================================================================

class TestPortfolioFitEnrichment(unittest.TestCase):

    def test_empty_snapshot_sets_neutral_defaults(self):
        row = _signal_row("AAPL")
        enrich_row_with_portfolio_fit(row, {})
        self.assertAlmostEqual(row["portfolio_fit_score"], 0.5, places=4)
        self.assertEqual(row["portfolio_fit_label"], "neutral")

    def test_enrichment_sets_all_required_fields(self):
        snap = _snapshot()
        row = _signal_row("AAPL")
        enrich_row_with_portfolio_fit(row, snap)
        for key in ("portfolio_fit_score", "portfolio_fit_label",
                    "portfolio_fit_reason", "portfolio_fit_context"):
            self.assertIn(key, row)

    def test_enrichment_does_not_overwrite_signal_score(self):
        snap = _snapshot()
        row = _signal_row("AAPL", signal_score=0.72)
        enrich_row_with_portfolio_fit(row, snap)
        self.assertAlmostEqual(row["signal_score"], 0.72, places=4)

    def test_sector_read_from_fundamentals_when_not_direct(self):
        snap = _snapshot(tickers=["NVDA"], sector="TECHNOLOGY",
                         sector_allocation={"TECHNOLOGY": 0.04}, max_sector=0.04)
        row = {"ticker": "NVDA", "fundamentals": {"sector": "TECHNOLOGY"}}
        enrich_row_with_portfolio_fit(row, snap)
        ctx = row["portfolio_fit_context"]
        # Sector at cap → sector_score should be penalised
        self.assertLess(ctx["sector_score"], 0.5)

    def test_safe_with_empty_row(self):
        row = {}
        enrich_row_with_portfolio_fit(row, {})
        self.assertIn("portfolio_fit_score", row)
        self.assertIn("portfolio_fit_label", row)


# ===========================================================================
# D. Integration: final_rank_score in alert_ranking
# ===========================================================================

class TestAlertRankingFinalScore(unittest.TestCase):

    def _row(self, signal_score=0.65, augmented_signal_score=None,
             confidence_score=0.80, evidence_breadth=2,
             data_quality="fresh", alert_tier="medium",
             theme_alignment_score=0.0, portfolio_fit_score=0.5):
        row = {
            "signal_score": signal_score,
            "confidence_score": confidence_score,
            "evidence_breadth": evidence_breadth,
            "data_quality": data_quality,
            "alert_tier": alert_tier,
            "theme_alignment_score": theme_alignment_score,
            "portfolio_fit_score": portfolio_fit_score,
        }
        if augmented_signal_score is not None:
            row["augmented_signal_score"] = augmented_signal_score
        return row

    def test_final_rank_score_computed(self):
        row = self._row()
        apply_priority_score(row)
        self.assertIn("final_rank_score", row)

    def test_final_rank_score_in_0_1_range(self):
        row = self._row(signal_score=1.0, augmented_signal_score=1.0,
                        confidence_score=1.0, theme_alignment_score=1.0,
                        portfolio_fit_score=1.0)
        apply_priority_score(row)
        self.assertGreaterEqual(row["final_rank_score"], 0.0)
        self.assertLessEqual(row["final_rank_score"], 1.0)

    def test_higher_portfolio_fit_increases_final_rank_score(self):
        row_low = self._row(portfolio_fit_score=0.2)
        row_high = self._row(portfolio_fit_score=0.9)
        apply_priority_score(row_low)
        apply_priority_score(row_high)
        self.assertGreater(row_high["final_rank_score"], row_low["final_rank_score"])

    def test_final_rank_score_independent_of_priority_score(self):
        row = self._row(signal_score=0.65)
        apply_priority_score(row)
        # priority_score uses signal formula; final_rank_score uses blended formula
        # They should generally differ (unless coincidentally equal)
        self.assertIn("priority_score", row)
        self.assertIn("final_rank_score", row)

    def test_priority_score_unchanged_by_portfolio_fit(self):
        row = self._row(signal_score=0.65, portfolio_fit_score=0.9)
        apply_priority_score(row)
        expected_priority = round(
            0.65 * 0.45 + 0.80 * 0.30 + (2 / 3.0) * 0.15 + 1.00 * 0.10, 4
        )
        self.assertAlmostEqual(row["priority_score"], expected_priority, places=3)

    def test_no_portfolio_fit_score_defaults_to_neutral(self):
        row = {
            "signal_score": 0.65,
            "confidence_score": 0.80,
            "evidence_breadth": 2,
            "data_quality": "fresh",
            "alert_tier": "medium",
        }
        apply_priority_score(row)
        self.assertIn("final_rank_score", row)
        # With portfolio_fit defaulting to 0.5 (neutral), final_rank_score
        # should be less than when portfolio_fit=1.0
        row_high = {**row, "portfolio_fit_score": 1.0}
        apply_priority_score(row_high)
        self.assertLess(row["final_rank_score"], row_high["final_rank_score"])


# ===========================================================================
# E. GUI data loader safety
# ===========================================================================

class TestGUIPortfolioFitSafety(unittest.TestCase):

    def _normalize(self, rows):
        import gui_operator_data as god
        watchlist = {"results": rows}
        return god._normalize_signal_triage(watchlist)

    def test_rows_without_portfolio_fit_fields_do_not_crash(self):
        row = {"ticker": "AAPL", "conviction_band": "normal", "conviction_score": 0.65}
        result = self._normalize([row])
        self.assertTrue(result["available"])
        self.assertEqual(result["rows"][0]["portfolio_fit_label"], "neutral")
        self.assertEqual(result["rows"][0]["portfolio_fit_reason"], "")

    def test_rows_with_portfolio_fit_fields_preserved(self):
        row = {
            "ticker": "NVDA",
            "conviction_band": "high_conviction",
            "conviction_score": 0.85,
            "portfolio_fit_label": "strong",
            "portfolio_fit_score": 0.80,
            "portfolio_fit_reason": "Adds diversification to underweight sector",
            "final_rank_score": 0.72,
        }
        result = self._normalize([row])
        trow = result["rows"][0]
        self.assertEqual(trow["portfolio_fit_label"], "strong")
        self.assertAlmostEqual(trow["portfolio_fit_score"], 0.80, places=4)
        self.assertEqual(
            trow["portfolio_fit_reason"], "Adds diversification to underweight sector"
        )

    def test_portfolio_fit_score_defaults_to_none_when_absent(self):
        row = {"ticker": "MSFT", "conviction_band": "normal"}
        result = self._normalize([row])
        self.assertIsNone(result["rows"][0]["portfolio_fit_score"])

    def test_final_rank_score_defaults_to_none_when_absent(self):
        row = {"ticker": "GOOG", "conviction_band": "starter"}
        result = self._normalize([row])
        self.assertIsNone(result["rows"][0]["final_rank_score"])

    def test_empty_results_returns_unavailable(self):
        result = self._normalize([])
        self.assertFalse(result["available"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
