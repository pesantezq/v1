import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from watchlist_scanner.__main__ import _load_prior_regime
from market_regime import detect_market_regime


class TestLoadPriorRegime(unittest.TestCase):
    """Unit tests for the _load_prior_regime helper."""

    def _write_snapshot(self, portfolio_out: Path, data: dict) -> None:
        (portfolio_out / "portfolio_snapshot.json").write_text(
            json.dumps(data), encoding="utf-8"
        )

    def test_no_snapshot_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _load_prior_regime(Path(tmp))
        self.assertIsNone(result)

    def test_valid_snapshot_returns_regime_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            portfolio_out = Path(tmp)
            self._write_snapshot(portfolio_out, {
                "market_regime": {"regime_label": "risk_on", "regime_confidence": 0.82}
            })
            result = _load_prior_regime(portfolio_out)
        self.assertIsNotNone(result)
        self.assertEqual(result["regime_label"], "risk_on")

    def test_malformed_json_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            portfolio_out = Path(tmp)
            (portfolio_out / "portfolio_snapshot.json").write_text(
                "{bad json}", encoding="utf-8"
            )
            result = _load_prior_regime(portfolio_out)
        self.assertIsNone(result)

    def test_snapshot_missing_market_regime_key_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            portfolio_out = Path(tmp)
            self._write_snapshot(portfolio_out, {"rows": [], "warnings": []})
            result = _load_prior_regime(portfolio_out)
        self.assertIsNone(result)

    def test_regime_label_absent_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            portfolio_out = Path(tmp)
            self._write_snapshot(portfolio_out, {
                "market_regime": {"regime_confidence": 0.5}
            })
            result = _load_prior_regime(portfolio_out)
        self.assertIsNone(result)

    def test_blank_regime_label_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            portfolio_out = Path(tmp)
            self._write_snapshot(portfolio_out, {
                "market_regime": {"regime_label": "", "regime_confidence": 0.5}
            })
            result = _load_prior_regime(portfolio_out)
        self.assertIsNone(result)

    def test_non_dict_snapshot_root_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            portfolio_out = Path(tmp)
            (portfolio_out / "portfolio_snapshot.json").write_text(
                json.dumps([1, 2, 3]), encoding="utf-8"
            )
            result = _load_prior_regime(portfolio_out)
        self.assertIsNone(result)


class TestPriorRegimePipelineIntegration(unittest.TestCase):
    """
    Verify that prior_regime loaded by _load_prior_regime activates hysteresis
    in detect_market_regime — end-to-end confirmation that the wiring is correct.
    """

    def _write_risk_on_snapshot(self, portfolio_out: Path) -> None:
        (portfolio_out / "portfolio_snapshot.json").write_text(
            json.dumps({
                "market_regime": {
                    "regime_label": "risk_on",
                    "regime_confidence": 0.85,
                    "regime_held": False,
                    "regime_raw_label": "risk_on",
                }
            }),
            encoding="utf-8",
        )

    def test_first_run_no_prior_no_hysteresis(self):
        with tempfile.TemporaryDirectory() as tmp:
            prior = _load_prior_regime(Path(tmp))
        self.assertIsNone(prior)
        regime = detect_market_regime(
            regime_inputs={"index_trend_state": "mixed", "breadth_sma50": 0.50},
            prior_regime=prior,
        )
        self.assertFalse(regime["regime_held"])
        self.assertEqual(regime["regime_label"], "neutral")

    def test_subsequent_run_with_prior_activates_hysteresis(self):
        with tempfile.TemporaryDirectory() as tmp:
            portfolio_out = Path(tmp)
            self._write_risk_on_snapshot(portfolio_out)
            prior = _load_prior_regime(portfolio_out)
        self.assertIsNotNone(prior)
        # Weak inputs — should hold at risk_on rather than switching to neutral
        regime = detect_market_regime(
            regime_inputs={"index_trend_state": "mixed", "breadth_sma50": 0.50},
            prior_regime=prior,
        )
        self.assertTrue(regime["regime_held"])
        self.assertEqual(regime["regime_label"], "risk_on")
        self.assertEqual(regime["regime_raw_label"], "neutral")

    def test_held_regime_reason_recorded_in_reasoning(self):
        with tempfile.TemporaryDirectory() as tmp:
            portfolio_out = Path(tmp)
            self._write_risk_on_snapshot(portfolio_out)
            prior = _load_prior_regime(portfolio_out)
        regime = detect_market_regime(
            regime_inputs={"index_trend_state": "mixed", "breadth_sma50": 0.50},
            prior_regime=prior,
        )
        self.assertIn("held", regime["regime_reasoning"].lower())

    def test_strong_signals_override_prior_risk_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            portfolio_out = Path(tmp)
            self._write_risk_on_snapshot(portfolio_out)
            prior = _load_prior_regime(portfolio_out)
        # Multi-signal downside evidence — switch confirmed despite prior being risk_on
        regime = detect_market_regime(
            regime_inputs={
                "index_trend_state": "down",
                "breadth_sma50": 0.25,
                "breadth_sma20": 0.30,
                "avg_price_change_pct": -1.5,
                "volatility_proxy": 1.0,
            },
            prior_regime=prior,
        )
        self.assertEqual(regime["regime_label"], "risk_off")
        self.assertFalse(regime["regime_held"])

    def test_malformed_prior_artifact_falls_back_to_stateless(self):
        with tempfile.TemporaryDirectory() as tmp:
            portfolio_out = Path(tmp)
            (portfolio_out / "portfolio_snapshot.json").write_text(
                "not valid json{{", encoding="utf-8"
            )
            prior = _load_prior_regime(portfolio_out)
        self.assertIsNone(prior)
        # Without prior, behavior is pure stateless — no hold possible
        regime = detect_market_regime(
            regime_inputs={"index_trend_state": "mixed", "breadth_sma50": 0.50},
            prior_regime=prior,
        )
        self.assertFalse(regime["regime_held"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
