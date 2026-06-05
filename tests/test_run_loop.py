"""
Tests for backtesting/run_loop.py — the Pattern-Improvement Loop end-to-end
driver (chains Steps 1→4 in one command; observe-only, proposes-only).

Fully offline and deterministic (no network, no API keys): signals are evaluated
through the real FMPBacktester driven by the harness's SyntheticPriceProvider.

Covers the pure registry-id mapping (Step 1b direction resolution), the
per-signal OOS bridge (Step 2 walk-forward grouped by registry signal_id →
Step 4 proposal input shape), a HEALTHY end-to-end run that writes BOTH loop
artifacts (poc_simulation_results.json + signal_weight_proposals.json) with
observe_only/proposed_only asserted, the no-signals degraded path, and the
protected invariant that config/signal_registry.yaml is byte-identical
before/after (this driver proposes, never applies — Step 5 stays inert).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from backtesting.fmp_backtester import FMPBacktester
from backtesting.poc_simulation_harness import SyntheticPriceProvider
from backtesting.run_loop import main, per_signal_oos, registry_signal_id, run_loop

_END = date(2026, 5, 1)  # pinned provider end → deterministic synthetic paths
_REGISTRY = "config/signal_registry.yaml"


def _bt() -> FMPBacktester:
    return FMPBacktester(SyntheticPriceProvider(seed=7, end=_END), years_default=3)


def _spread(pattern: str, *, n: int, symbols: int = 6, latest_offset: int = 35) -> list[dict]:
    """n signals on distinct consecutive days ending `latest_offset` days before
    the pinned provider end, so each forward window has price data."""
    latest = _END - timedelta(days=latest_offset)
    out = []
    for i in range(n):
        d = latest - timedelta(days=i)
        out.append({
            "ticker": f"S{i % symbols:02d}",
            "scan_time": d.isoformat(),
            "pattern": pattern,
            "patterns": [pattern],
            "signal_score": round((i % 10) / 10.0, 4),
            "confidence_score": round((i % 7) / 7.0, 4),
        })
    return out


def _results_artifact(path: Path, *, basis: list[str], n: int, symbols: int = 6) -> None:
    """Write a minimal watchlist_signals.json (the shape load_signals_from_artifact
    expects) with time-spread rows dated in the past relative to today."""
    latest = date.today() - timedelta(days=40)
    rows = []
    for i in range(n):
        d = latest - timedelta(days=i)
        rows.append({
            "ticker": f"S{i % symbols:02d}",
            "scan_time": d.isoformat(),
            "alert_basis": list(basis),
            "signal_score": round((i % 10) / 10.0, 4),
            "confidence_score": round((i % 7) / 7.0, 4),
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"results": rows}), encoding="utf-8")


# ---------------------------------------------------------------------------
# Step 1b — registry signal-id mapping (pure)
# ---------------------------------------------------------------------------

class TestRegistrySignalId(unittest.TestCase):
    def test_strong_move_resolves_to_up_by_default(self):
        self.assertEqual(
            registry_signal_id({"pattern": "STRONG_MOVE", "patterns": ["STRONG_MOVE"]}),
            "STRONG_MOVE_UP",
        )

    def test_strong_move_down_when_direction_down(self):
        self.assertEqual(
            registry_signal_id({"pattern": "STRONG_MOVE", "direction": "down"}),
            "STRONG_MOVE_DOWN",
        )

    def test_known_family_passthrough(self):
        self.assertEqual(registry_signal_id({"pattern": "VOLUME_SPIKE"}), "VOLUME_SPIKE")
        self.assertEqual(registry_signal_id({"pattern": "BREAKOUT_PROXY"}), "BREAKOUT_PROXY")

    def test_non_registry_family_passes_through_for_flagging(self):
        # SIGNAL_SCORE / UNKNOWN are not registry signal_ids; they must pass
        # through so propose_weight_changes flags them 'unknown_signal', not drop.
        self.assertEqual(registry_signal_id({"pattern": "SIGNAL_SCORE"}), "SIGNAL_SCORE")
        self.assertEqual(registry_signal_id({}), "UNKNOWN")


# ---------------------------------------------------------------------------
# Step 2 — per-signal OOS bridge
# ---------------------------------------------------------------------------

class TestPerSignalOOS(unittest.TestCase):
    def test_groups_by_signal_id_and_returns_proposal_input_shape(self):
        signals = _spread("BREAKOUT_PROXY", n=200) + _spread("VOLUME_SPIKE", n=200)
        oos = per_signal_oos(
            signals, _bt(), forward_days=10,
            train_days=120, test_days=40, step_days=40, min_signals_per_fold=20,
        )
        ids = {e["signal_id"] for e in oos}
        self.assertEqual(ids, {"BREAKOUT_PROXY", "VOLUME_SPIKE"})
        for e in oos:
            for key in ("signal_id", "n", "hit_rate", "hit_rate_ci95", "avg_return", "oos_status"):
                self.assertIn(key, e)
        # With this much time-spread data, at least one group reaches OOS 'ok'.
        self.assertTrue(any(e["oos_status"] == "ok" for e in oos))

    def test_insufficient_history_still_returns_entry_not_dropped(self):
        # Too few signals → walk-forward aggregate 'insufficient'; the group must
        # still surface (so Step 4 flags 'insufficient_evidence'), never vanish.
        oos = per_signal_oos(_spread("BREAKOUT_PROXY", n=5), _bt(), forward_days=10)
        self.assertEqual([e["signal_id"] for e in oos], ["BREAKOUT_PROXY"])
        self.assertEqual(oos[0]["oos_status"], "insufficient")


# ---------------------------------------------------------------------------
# Steps 1→4 — end-to-end driver
# ---------------------------------------------------------------------------

class TestRunLoopEndToEnd(unittest.TestCase):
    def test_writes_both_artifacts_with_observe_only_flags(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "in" / "watchlist_signals.json"
            _results_artifact(src, basis=["breakout"], n=200)
            out = run_loop(
                signals_source=str(src), live=False, seed=7,
                forward_days=10, train_days=120, test_days=40, step_days=40,
                min_signals_per_fold=20, registry_path=_REGISTRY,
                write=True, base_dir=str(root / "outputs"),
            )
            self.assertEqual(out["status"], "ok")
            self.assertTrue(out["observe_only"])

            poc = root / "outputs" / "backtest" / "poc_simulation_results.json"
            prop = root / "outputs" / "policy" / "signal_weight_proposals.json"
            self.assertTrue(poc.exists(), "poc artifact not written")
            self.assertTrue(prop.exists(), "proposals artifact not written")

            payload = json.loads(prop.read_text())
            self.assertTrue(payload["observe_only"])
            self.assertTrue(payload["proposed_only"])
            self.assertIn("summary", payload)

    def test_clears_proposals_missing_health_flag(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "in" / "watchlist_signals.json"
            _results_artifact(src, basis=["breakout"], n=120)
            run_loop(signals_source=str(src), seed=7, train_days=120, test_days=40,
                     step_days=40, min_signals_per_fold=20, registry_path=_REGISTRY,
                     write=True, base_dir=str(root / "outputs"))
            from backtesting.backtest_health import assess_backtest_health
            health = assess_backtest_health(
                backtest_dir=str(root / "outputs" / "backtest"),
                proposals_path=str(root / "outputs" / "policy" / "signal_weight_proposals.json"),
            )
            self.assertNotIn("proposals_missing", health["flags"])

    def test_no_signals_degrades_without_writing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out = run_loop(signals_source=str(root / "missing.json"),
                           write=True, base_dir=str(root / "outputs"))
            self.assertEqual(out["status"], "no_signals")
            self.assertFalse((root / "outputs" / "policy" / "signal_weight_proposals.json").exists())

    def test_registry_is_byte_identical_after_run(self):
        before = Path(_REGISTRY).read_bytes()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "in" / "watchlist_signals.json"
            _results_artifact(src, basis=["breakout"], n=120)
            run_loop(signals_source=str(src), seed=7, train_days=120, test_days=40,
                     step_days=40, min_signals_per_fold=20, registry_path=_REGISTRY,
                     write=True, base_dir=str(root / "outputs"))
        self.assertEqual(Path(_REGISTRY).read_bytes(), before,
                         "run_loop must never modify the registry (Step 5 is the apply path)")

    def test_main_smoke_returns_zero(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "in" / "watchlist_signals.json"
            _results_artifact(src, basis=["volume_spike"], n=60)
            rc = main(["--signals-source", str(src), "--no-write", "--seed", "7"])
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
