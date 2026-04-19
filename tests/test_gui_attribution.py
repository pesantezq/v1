"""
GUI Attribution Panel — Read-Only Tests
=========================================
Covers five areas required by the attribution GUI workstream:

A. Missing artifact handling
B. Execution attribution rendering (value extraction and row correctness)
C. Confidence-band rendering (fixed order, small-sample markers)
D. Rotation quality rendering (margin bands, strategy breakdown, resolved outcomes)
E. No backend mutation (GUI path is read-only)

All tests exercise load_profit_attribution() and load_rotation_events() from
gui_operator_data — the same functions the render tabs consume.  No Streamlit
import is needed because correctness is verified at the data-dict level.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from gui_operator_data import load_profit_attribution, load_rotation_events


# ---------------------------------------------------------------------------
# Shared fixture builders
# ---------------------------------------------------------------------------

def _minimal_profit_attribution(**overrides) -> dict:
    base: dict = {
        "generated_at": "2026-04-17T08:00:00",
        "metrics": {
            "total_entries": 10,
            "attributable_entries": 8,
            "entries_with_5d": 7,
            "coverage_rate": 0.80,
            "win_rate": 0.625,
            "avg_gain": 0.032,
            "avg_loss": -0.018,
            "risk_reward": 1.78,
            "expectancy": 0.013,
            "capital_efficiency": 0.72,
            "strong_win_rate": 0.25,
            "adverse_rate": 0.125,
            "avg_mfe": 0.041,
            "avg_mae": -0.022,
            "avg_exit_quality": 0.64,
            "avg_hold_days": 5.2,
        },
        "by_strategy": [],
        "by_score_band": [],
        "by_regime": [],
        "trade_ledger": [],
        "exit_summary": {
            "protected": 3, "partial": 2, "gave_back": 1,
            "reversed": 1, "no_gain": 0, "unresolved": 1,
        },
        "exit_classified": [],
        "missed_opportunities": [],
        "total_opportunity_cost": None,
        "best_trades": [
            {
                "symbol": "NVDA", "strategy_type": "momentum",
                "return_5d": 0.058, "mfe": 0.07,
                "entry_score": 82.0, "entry_regime": "risk_on",
                "entry_date": "2026-04-10",
            },
        ],
        "worst_trades": [
            {
                "symbol": "TSLA", "strategy_type": "momentum",
                "return_5d": -0.035, "mae": -0.04,
                "entry_score": 64.0, "entry_date": "2026-04-10",
            },
        ],
        "data_quality_notes": [],
        "execution": None,
    }
    base.update(overrides)
    return base


def _minimal_execution_block(**overrides) -> dict:
    base: dict = {
        "generated_at": "2026-04-17T08:00:00",
        "total_events": 6,
        "matched_events": 5,
        "match_rate": 0.833,
        "by_action": [
            {
                "action": "BUY",
                "total_events": 4,
                "matched_events": 4,
                "win_rate": 0.75,
                "avg_gain": 0.028,
                "avg_loss": -0.015,
                "risk_reward": 1.87,
                "expectancy": 0.017,
                "avg_exit_quality": None,
            },
            {
                "action": "SELL",
                "total_events": 2,
                "matched_events": 1,
                "win_rate": None,
                "avg_gain": None,
                "avg_loss": None,
                "risk_reward": None,
                "expectancy": None,
                "avg_exit_quality": 0.71,
            },
        ],
        "by_strategy": [],
        "by_score_band": [],
        "by_regime": [],
        "by_confidence_band": [
            {
                "name": "low", "total_entries": 1, "attributable": 1,
                "win_rate": 0.0, "avg_gain": None, "avg_loss": -0.02,
                "risk_reward": None, "avg_hold_days": 5.0, "small_sample": True,
            },
            {
                "name": "medium", "total_entries": 2, "attributable": 2,
                "win_rate": 0.5, "avg_gain": 0.02, "avg_loss": -0.01,
                "risk_reward": 2.0, "avg_hold_days": 5.0, "small_sample": True,
            },
            {
                "name": "high", "total_entries": 2, "attributable": 2,
                "win_rate": 1.0, "avg_gain": 0.04, "avg_loss": None,
                "risk_reward": None, "avg_hold_days": 5.0, "small_sample": True,
            },
        ],
        "confidence_calibration": {
            "observe_only": True,
            "status": "insufficient_data",
            "sample_summary": {
                "low_matched": 1, "medium_matched": 2,
                "high_matched": 2, "total_matched": 5,
            },
            "low_win_rate": 0.0,
            "medium_win_rate": 0.5,
            "high_win_rate": 1.0,
            "low_expectancy": -0.02,
            "medium_expectancy": 0.005,
            "high_expectancy": 0.04,
            "band_order_valid": True,
            "strongest_band": "high",
            "weakest_band": "low",
            "recommendation": "Observe — insufficient data to draw conclusions.",
            "recommendation_reason": "Total matched events < 10.",
        },
        "execution_ledger": [],
        "data_quality_notes": ["Only 5 matched execution events — treat statistics as preliminary."],
    }
    base.update(overrides)
    return base


def _rotation_event(
    symbol: str = "TSLA",
    strategy: str = "momentum",
    triggered: bool = True,
    actual_margin: float = 8.0,
    required_margin: float = 5.0,
    challenger_is_breakout: bool = False,
    degraded_mode: bool = False,
    forward_return_5d: float | None = None,
    outcome_resolved: bool = False,
) -> dict:
    return {
        "event_id": f"{symbol}_run_001",
        "timestamp": "2026-04-17T08:00:00",
        "run_id": "run_001",
        "symbol": symbol,
        "strategy_type": strategy,
        "incumbent_score": 60.0,
        "challenger_score": 60.0 + actual_margin,
        "actual_margin": actual_margin,
        "required_margin": required_margin,
        "rotation_triggered": triggered,
        "score_basis": "composite_0_to_100",
        "challenger_symbol": "AMZN",
        "challenger_is_breakout": challenger_is_breakout,
        "degraded_mode": degraded_mode,
        "drawdown_regime": "normal",
        "forward_return_5d": forward_return_5d,
        "outcome_resolved": outcome_resolved,
    }


# ---------------------------------------------------------------------------
# Shared temp-directory helper
# ---------------------------------------------------------------------------

class _TmpDir:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def cleanup(self):
        self._tmp.cleanup()

    def write_json(self, rel: str, payload: dict) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return p

    def write_jsonl(self, rel: str, records: list) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
        return p


# ===========================================================================
# A. Missing artifact handling
# ===========================================================================

class TestMissingArtifactHandling(unittest.TestCase):

    def setUp(self):
        self.tmp = _TmpDir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_profit_attribution_returns_empty_dict(self):
        result = load_profit_attribution(self.tmp.root)
        self.assertEqual(result, {})

    def test_missing_rotation_events_returns_empty_list(self):
        result = load_rotation_events(self.tmp.root)
        self.assertEqual(result, [])

    def test_empty_profit_attribution_file_returns_empty_dict(self):
        p = self.tmp.root / "outputs" / "policy" / "profit_attribution.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")
        result = load_profit_attribution(self.tmp.root)
        self.assertEqual(result, {})

    def test_malformed_profit_attribution_returns_empty_dict(self):
        p = self.tmp.root / "outputs" / "policy" / "profit_attribution.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not valid json", encoding="utf-8")
        result = load_profit_attribution(self.tmp.root)
        self.assertEqual(result, {})

    def test_empty_rotation_events_file_returns_empty_list(self):
        p = self.tmp.root / "outputs" / "policy" / "rotation_events.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")
        result = load_rotation_events(self.tmp.root)
        self.assertEqual(result, [])

    def test_malformed_rotation_events_lines_are_skipped(self):
        p = self.tmp.root / "outputs" / "policy" / "rotation_events.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            '{"symbol": "NVDA", "rotation_triggered": true}\n'
            '{bad json\n'
            '{"symbol": "TSLA", "rotation_triggered": false}\n',
            encoding="utf-8",
        )
        result = load_rotation_events(self.tmp.root)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["symbol"], "NVDA")
        self.assertEqual(result[1]["symbol"], "TSLA")

    def test_no_execution_block_in_attribution_is_safe(self):
        pa = _minimal_profit_attribution(execution=None)
        self.tmp.write_json("outputs/policy/profit_attribution.json", pa)
        result = load_profit_attribution(self.tmp.root)
        self.assertIsNone(result.get("execution"))
        self.assertIn("metrics", result)


# ===========================================================================
# B. Execution attribution rendering
# ===========================================================================

class TestExecutionAttributionRendering(unittest.TestCase):

    def setUp(self):
        self.tmp = _TmpDir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_summary_values_present(self):
        ex = _minimal_execution_block()
        pa = _minimal_profit_attribution(execution=ex)
        self.tmp.write_json("outputs/policy/profit_attribution.json", pa)
        result = load_profit_attribution(self.tmp.root)
        execution = result["execution"]
        self.assertEqual(execution["total_events"], 6)
        self.assertEqual(execution["matched_events"], 5)
        self.assertAlmostEqual(execution["match_rate"], 0.833, places=2)

    def test_buy_sell_trim_rows_present(self):
        buy  = {"action": "BUY",  "total_events": 3, "matched_events": 3,
                "win_rate": 0.67, "avg_gain": 0.025, "avg_loss": -0.012,
                "risk_reward": 2.08, "expectancy": 0.013, "avg_exit_quality": None}
        sell = {"action": "SELL", "total_events": 1, "matched_events": 1,
                "win_rate": None, "avg_gain": None, "avg_loss": None,
                "risk_reward": None, "expectancy": None, "avg_exit_quality": 0.81}
        trim = {"action": "TRIM", "total_events": 1, "matched_events": 0,
                "win_rate": None, "avg_gain": None, "avg_loss": None,
                "risk_reward": None, "expectancy": None, "avg_exit_quality": None}
        ex = _minimal_execution_block(by_action=[buy, sell, trim])
        pa = _minimal_profit_attribution(execution=ex)
        self.tmp.write_json("outputs/policy/profit_attribution.json", pa)
        result = load_profit_attribution(self.tmp.root)
        actions = {a["action"] for a in result["execution"]["by_action"]}
        self.assertIn("BUY", actions)
        self.assertIn("SELL", actions)
        self.assertIn("TRIM", actions)

    def test_unmatched_counts_accessible(self):
        ex = _minimal_execution_block()
        ex["by_action"][0]["total_events"] = 4
        ex["by_action"][0]["matched_events"] = 2
        pa = _minimal_profit_attribution(execution=ex)
        self.tmp.write_json("outputs/policy/profit_attribution.json", pa)
        result = load_profit_attribution(self.tmp.root)
        buy_row = next(a for a in result["execution"]["by_action"] if a["action"] == "BUY")
        unmatched = buy_row["total_events"] - buy_row["matched_events"]
        self.assertEqual(unmatched, 2)

    def test_data_quality_notes_accessible(self):
        ex = _minimal_execution_block(data_quality_notes=["test note about data gap"])
        pa = _minimal_profit_attribution(execution=ex)
        self.tmp.write_json("outputs/policy/profit_attribution.json", pa)
        result = load_profit_attribution(self.tmp.root)
        notes = result["execution"]["data_quality_notes"]
        self.assertIn("test note about data gap", notes)


# ===========================================================================
# C. Confidence-band rendering
# ===========================================================================

class TestConfidenceBandRendering(unittest.TestCase):

    def setUp(self):
        self.tmp = _TmpDir()

    def tearDown(self):
        self.tmp.cleanup()

    def _load(self, ex_overrides: dict | None = None) -> dict:
        ex = _minimal_execution_block(**(ex_overrides or {}))
        pa = _minimal_profit_attribution(execution=ex)
        self.tmp.write_json("outputs/policy/profit_attribution.json", pa)
        return load_profit_attribution(self.tmp.root)

    def test_bands_present_in_fixed_order(self):
        result = self._load()
        bands = [b["name"] for b in result["execution"]["by_confidence_band"]]
        for band in ("low", "medium", "high"):
            self.assertIn(band, bands)
        self.assertLess(bands.index("low"), bands.index("medium"))
        self.assertLess(bands.index("medium"), bands.index("high"))

    def test_small_sample_marker_true_when_expected(self):
        result = self._load()
        low_band = next(b for b in result["execution"]["by_confidence_band"] if b["name"] == "low")
        self.assertTrue(low_band["small_sample"])

    def test_small_sample_marker_false_when_sufficient(self):
        ex_data = _minimal_execution_block()
        ex_data["by_confidence_band"][2]["small_sample"] = False
        ex_data["by_confidence_band"][2]["total_entries"] = 12
        pa = _minimal_profit_attribution(execution=ex_data)
        self.tmp.write_json("outputs/policy/profit_attribution.json", pa)
        result = load_profit_attribution(self.tmp.root)
        high_band = next(b for b in result["execution"]["by_confidence_band"] if b["name"] == "high")
        self.assertFalse(high_band["small_sample"])

    def test_calibration_status_present(self):
        result = self._load()
        cal = result["execution"]["confidence_calibration"]
        self.assertIn("status", cal)
        self.assertIn(cal["status"], {"healthy", "weak_separation", "insufficient_data", "no_data"})

    def test_observe_only_flag_is_true(self):
        result = self._load()
        cal = result["execution"]["confidence_calibration"]
        self.assertTrue(cal["observe_only"])

    def test_band_order_valid_accessible(self):
        result = self._load()
        cal = result["execution"]["confidence_calibration"]
        self.assertIn("band_order_valid", cal)

    def test_empty_confidence_band_list_safe(self):
        result = self._load({"by_confidence_band": []})
        self.assertEqual(result["execution"]["by_confidence_band"], [])


# ===========================================================================
# D. Rotation quality rendering
# ===========================================================================

class TestRotationQualityRendering(unittest.TestCase):

    def setUp(self):
        self.tmp = _TmpDir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_events_load_correctly(self):
        events = [
            _rotation_event("TSLA", "momentum",   triggered=True,  actual_margin=8.0, required_margin=5.0),
            _rotation_event("AAPL", "compounder", triggered=False, actual_margin=2.0, required_margin=5.0),
        ]
        self.tmp.write_jsonl("outputs/policy/rotation_events.jsonl", events)
        result = load_rotation_events(self.tmp.root)
        self.assertEqual(len(result), 2)

    def test_strategy_breakdown_accessible(self):
        events = [
            _rotation_event("TSLA", "momentum",   triggered=True),
            _rotation_event("AAPL", "compounder", triggered=False),
            _rotation_event("AMZN", "momentum",   triggered=True),
        ]
        self.tmp.write_jsonl("outputs/policy/rotation_events.jsonl", events)
        result = load_rotation_events(self.tmp.root)
        strategies = {e["strategy_type"] for e in result}
        self.assertIn("momentum", strategies)
        self.assertIn("compounder", strategies)

    def test_margin_band_data_accessible(self):
        events = [
            _rotation_event("TSLA", actual_margin=6.0, required_margin=5.0, triggered=True),
            _rotation_event("AAPL", actual_margin=5.5, required_margin=5.0, triggered=True),
        ]
        self.tmp.write_jsonl("outputs/policy/rotation_events.jsonl", events)
        result = load_rotation_events(self.tmp.root)
        for e in result:
            self.assertIn("actual_margin", e)
            self.assertIn("required_margin", e)

    def test_breakout_type_accessible(self):
        events = [
            _rotation_event("TSLA", challenger_is_breakout=True,  triggered=True),
            _rotation_event("AAPL", challenger_is_breakout=False, triggered=False),
        ]
        self.tmp.write_jsonl("outputs/policy/rotation_events.jsonl", events)
        result = load_rotation_events(self.tmp.root)
        breakout_count = sum(1 for e in result if e.get("challenger_is_breakout"))
        self.assertEqual(breakout_count, 1)

    def test_recommendation_text_from_resolved_events(self):
        events = [
            _rotation_event("TSLA", triggered=True,  forward_return_5d=0.04, outcome_resolved=True),
            _rotation_event("AAPL", triggered=False, forward_return_5d=0.01, outcome_resolved=True),
        ]
        self.tmp.write_jsonl("outputs/policy/rotation_events.jsonl", events)
        result = load_rotation_events(self.tmp.root)
        resolved = [e for e in result if e["outcome_resolved"]]
        self.assertEqual(len(resolved), 2)
        triggered_returns = [e["forward_return_5d"] for e in resolved if e["rotation_triggered"]]
        self.assertAlmostEqual(triggered_returns[0], 0.04)

    def test_empty_state_when_absent(self):
        result = load_rotation_events(self.tmp.root)
        self.assertEqual(result, [])

    def test_degraded_mode_flag_present(self):
        events = [
            _rotation_event("TSLA", degraded_mode=True),
            _rotation_event("AAPL", degraded_mode=False),
        ]
        self.tmp.write_jsonl("outputs/policy/rotation_events.jsonl", events)
        result = load_rotation_events(self.tmp.root)
        degraded = sum(1 for e in result if e.get("degraded_mode"))
        self.assertEqual(degraded, 1)


# ===========================================================================
# E. No backend mutation — GUI path is read-only
# ===========================================================================

class TestNoBackendMutation(unittest.TestCase):

    def setUp(self):
        self.tmp = _TmpDir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_profit_attribution_does_not_write_files(self):
        pa = _minimal_profit_attribution()
        pa_path = self.tmp.write_json("outputs/policy/profit_attribution.json", pa)
        mtime_before = pa_path.stat().st_mtime
        _ = load_profit_attribution(self.tmp.root)
        mtime_after = pa_path.stat().st_mtime
        self.assertEqual(mtime_before, mtime_after,
                         "profit_attribution.json was modified by the loader")

    def test_load_rotation_events_does_not_write_files(self):
        rot_path = self.tmp.write_jsonl("outputs/policy/rotation_events.jsonl", [_rotation_event()])
        mtime_before = rot_path.stat().st_mtime
        _ = load_rotation_events(self.tmp.root)
        mtime_after = rot_path.stat().st_mtime
        self.assertEqual(mtime_before, mtime_after,
                         "rotation_events.jsonl was modified by the loader")

    def test_load_profit_attribution_does_not_create_new_files(self):
        pa = _minimal_profit_attribution()
        self.tmp.write_json("outputs/policy/profit_attribution.json", pa)
        files_before = set(self.tmp.root.rglob("*"))
        _ = load_profit_attribution(self.tmp.root)
        files_after = set(self.tmp.root.rglob("*"))
        self.assertEqual(files_before, files_after, "Loader created unexpected files")

    def test_load_rotation_events_does_not_create_new_files(self):
        self.tmp.write_jsonl("outputs/policy/rotation_events.jsonl", [_rotation_event()])
        files_before = set(self.tmp.root.rglob("*"))
        _ = load_rotation_events(self.tmp.root)
        files_after = set(self.tmp.root.rglob("*"))
        self.assertEqual(files_before, files_after, "Loader created unexpected files")

    def test_modifying_returned_dict_does_not_affect_file(self):
        pa = _minimal_profit_attribution()
        pa_path = self.tmp.write_json("outputs/policy/profit_attribution.json", pa)
        result = load_profit_attribution(self.tmp.root)
        result["metrics"]["win_rate"] = 9999.0
        on_disk = json.loads(pa_path.read_text(encoding="utf-8"))
        self.assertNotEqual(on_disk.get("metrics", {}).get("win_rate"), 9999.0)

    def test_modifying_returned_list_does_not_affect_file(self):
        rot_path = self.tmp.write_jsonl("outputs/policy/rotation_events.jsonl", [_rotation_event()])
        result = load_rotation_events(self.tmp.root)
        result.append({"injected": True})
        lines = rot_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1, "Loader wrote extra records to jsonl")


if __name__ == "__main__":
    unittest.main()
