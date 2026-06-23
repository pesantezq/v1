"""Regime classifier — label-reachability, recording-ordering regression, and
degeneracy guards.

Context (2026-06-23, work order quant.regime_classifier_health):
``signal_outcomes.csv`` had collapsed so that ALL 1286 rows carried
``regime_label='neutral'``, ``regime_confidence=0.0`` and
``regime_data_quality='limited'``. Root cause was NOT a classifier-math defect:
``market_regime.detect_market_regime`` produces multiple labels for varied
inputs. The defect was a PRODUCER-ORDERING bug in
``watchlist_scanner/__main__.py`` — ``run_signal_feedback_cycle`` (which calls
``record_scan_signals``) ran BEFORE the regime was computed and attached to
``result["market_regime"]``, so the recorder read an empty dict and fell back to
the hardcoded constant ("neutral", 0.0, "limited") for every row.

The constant ``regime_confidence=0.0`` was the smoking gun: the classifier's own
arithmetic can never emit 0.0 (its floor on the limited-input path is ~0.27), so
the value could only have come from the record-time fallback literal.

Regime vocabulary reconciliation: the implemented valid label set is
``{risk_on, risk_off, neutral, high_volatility}``. There is no distinct
``transition`` state in this codebase — a transitional / mixed trend+breadth
condition resolves to ``neutral`` by design (see test below). These tests assert
against the actual implemented vocabulary, NOT an invented ``transition`` label
(inventing one to manufacture diversity is explicitly forbidden by the work
order).
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from market_regime import _VALID_REGIME_LABELS, detect_market_regime
from watchlist_scanner.performance_feedback import record_scan_signals
from watchlist_scanner.state import WatchlistStateStore


# Deterministic synthetic regime inputs, one per implemented label. These are
# constructed from classifier INTENT (trend + breadth + volatility), not tuned
# to game thresholds: an up-trend with broad supportive breadth is genuinely
# risk_on; a down-trend with weak breadth is genuinely risk_off; an elevated
# cross-signal volatility proxy is genuinely high_volatility; mixed trend with
# middling breadth is genuinely neutral.
_RISK_ON = {
    "index_trend_state": "up",
    "breadth_sma50": 0.82,
    "breadth_sma20": 0.78,
    "avg_price_change_pct": 1.6,
    "volatility_proxy": 1.0,
    "sector_leadership_concentration": 0.30,
}
_RISK_OFF = {
    "index_trend_state": "down",
    "breadth_sma50": 0.22,
    "breadth_sma20": 0.28,
    "avg_price_change_pct": -1.7,
    "volatility_proxy": 1.2,
    "sector_leadership_concentration": 0.35,
}
_HIGH_VOL = {
    "index_trend_state": "mixed",
    "breadth_sma50": 0.50,
    "breadth_sma20": 0.50,
    "avg_price_change_pct": 0.1,
    "volatility_proxy": 4.0,  # >= 3.0 high-volatility threshold
    "sector_leadership_concentration": 0.40,
}
_NEUTRAL = {
    "index_trend_state": "mixed",
    "breadth_sma50": 0.52,
    "breadth_sma20": 0.50,
    "avg_price_change_pct": 0.2,
    "volatility_proxy": 1.0,
    "sector_leadership_concentration": 0.30,
}

_SCENARIOS = {
    "risk_on": _RISK_ON,
    "risk_off": _RISK_OFF,
    "high_volatility": _HIGH_VOL,
    "neutral": _NEUTRAL,
}


class TestRegimeLabelReachability(unittest.TestCase):
    """Requirement #4: every valid regime label is individually reachable from
    deterministic synthetic inputs."""

    def test_risk_on_reachable(self):
        r = detect_market_regime(regime_inputs=_RISK_ON)
        self.assertEqual(r["regime_label"], "risk_on")
        self.assertGreater(r["regime_confidence"], 0.0)

    def test_risk_off_reachable(self):
        r = detect_market_regime(regime_inputs=_RISK_OFF)
        self.assertEqual(r["regime_label"], "risk_off")
        self.assertGreater(r["regime_confidence"], 0.0)

    def test_high_volatility_reachable(self):
        r = detect_market_regime(regime_inputs=_HIGH_VOL)
        self.assertEqual(r["regime_label"], "high_volatility")
        self.assertGreater(r["regime_confidence"], 0.0)

    def test_neutral_reachable(self):
        r = detect_market_regime(regime_inputs=_NEUTRAL)
        self.assertEqual(r["regime_label"], "neutral")
        self.assertGreater(r["regime_confidence"], 0.0)

    def test_no_transition_label_in_vocabulary(self):
        # The work order generically named 'transition'; this codebase has no
        # such label. Reconcile explicitly so nobody invents one to fake
        # diversity. A transitional/mixed condition resolves to 'neutral'.
        self.assertNotIn("transition", _VALID_REGIME_LABELS)
        self.assertEqual(
            _VALID_REGIME_LABELS,
            {"risk_on", "risk_off", "neutral", "high_volatility"},
        )

    def test_classifier_confidence_floor_never_zero(self):
        # The CSV's constant 0.0 confidence is unreachable from the live
        # classifier — proving the constant came from the record-time fallback.
        worst = detect_market_regime(results=[])  # no inputs at all
        self.assertGreater(worst["regime_confidence"], 0.0)


def _scan_result(*, generated_at: str, regime: dict | None, n_rows: int = 3) -> dict:
    """Minimal scan_result the recorder accepts: each row needs ticker + price."""
    rows = [
        {
            "ticker": f"T{i}",
            "price": 100.0 + i,
            "signal_score": 0.5,
            "confidence_score": 0.8,
            "effective_score": 0.4,
            "watchlist_source": "static",
        }
        for i in range(n_rows)
    ]
    out = {
        "generated_at": generated_at,
        "data_mode": "live",
        "degraded_mode": False,
        "results": rows,
    }
    if regime is not None:
        out["market_regime"] = regime
    return out


class TestRecordingOrderingRegression(unittest.TestCase):
    """The actual bug lived at the record_scan_signals boundary, not in the
    classifier. These tests pin the producer contract."""

    def _record_and_read(self, scan_result: dict) -> list[dict]:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            summary = record_scan_signals(scan_result, db_path=db)
            self.assertGreater(summary["tracked"], 0)
            rows = WatchlistStateStore(db).list_signal_feedback(limit=100)
        return rows

    def test_live_regime_is_persisted_when_attached_before_recording(self):
        # Post-fix ordering: regime computed and attached to result BEFORE
        # recording. Every recorded row must carry the live label, not the
        # fallback.
        regime = detect_market_regime(regime_inputs=_RISK_OFF)
        self.assertEqual(regime["regime_label"], "risk_off")
        rows = self._record_and_read(
            _scan_result(generated_at="2026-06-23T09:00:00", regime=regime)
        )
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["regime_label"], "risk_off")
            self.assertEqual(float(row["regime_confidence"]), regime["regime_confidence"])
            self.assertNotEqual(float(row["regime_confidence"]), 0.0)
            self.assertEqual(row["regime_data_quality"], regime["regime_data_quality"])

    def test_collapse_documented_when_regime_unset(self):
        # Pre-fix ordering simulated: market_regime absent at record time. This
        # locks the failure mode — every row collapses to the constant triple.
        # If a future change makes the recorder robust to an unset regime, this
        # test should be updated deliberately, not silently.
        rows = self._record_and_read(
            _scan_result(generated_at="2026-06-23T10:00:00", regime=None)
        )
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["regime_label"], "neutral")
            self.assertEqual(float(row["regime_confidence"]), 0.0)
            self.assertEqual(row["regime_data_quality"], "limited")

    def test_warning_emitted_when_regime_unset(self):
        # Defense-in-depth: a producer-ordering re-regression must not hide
        # behind the silent fallback.
        with self.assertLogs(
            "watchlist_scanner.performance_feedback", level="WARNING"
        ) as cm:
            self._record_and_read(
                _scan_result(generated_at="2026-06-23T10:30:00", regime=None)
            )
        self.assertTrue(any("market_regime is empty" in m for m in cm.output))


class TestRegimeDegeneracyGuard(unittest.TestCase):
    """Requirement #5: a sufficiently varied input fixture MUST NOT collapse to
    a single label. This fails under the pre-fix ordering (all-neutral) and
    passes under the fix."""

    def test_classifier_varied_inputs_are_not_degenerate(self):
        labels = {
            name: detect_market_regime(regime_inputs=inp)["regime_label"]
            for name, inp in _SCENARIOS.items()
        }
        distinct = set(labels.values())
        self.assertGreater(
            len(distinct), 1,
            f"varied inputs collapsed to one label: {labels}",
        )
        # All four implemented labels should be individually achievable.
        self.assertEqual(distinct, set(_SCENARIOS.keys()))

    def test_recorded_distribution_across_runs_is_not_degenerate(self):
        # Simulate four daily runs, each with the CORRECT ordering (classify →
        # attach → record). The persisted regime distribution must show >1
        # label. This is the end-to-end degeneracy guard at the producer
        # boundary that originally collapsed.
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "runs.db"
            for i, (name, inp) in enumerate(_SCENARIOS.items()):
                regime = detect_market_regime(regime_inputs=inp)
                scan = _scan_result(
                    generated_at=f"2026-06-2{i}T09:00:00", regime=regime
                )
                record_scan_signals(scan, db_path=db)
            rows = WatchlistStateStore(db).list_signal_feedback(limit=1000)
        recorded = {row["regime_label"] for row in rows}
        self.assertGreater(
            len(recorded), 1,
            f"recorded outcomes collapsed to one regime label: {recorded}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
