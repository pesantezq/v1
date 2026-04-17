"""
Focused tests for the confidence-aware weighting and cooldown meta-layer.

Covers all 10 Phase-5 scenarios:

  1.  effective_score = signal_score × confidence_score
  2.  Degraded-mode penalty reduces effective_score
  3.  Cooldown activates on repeated unchanged signals
  4.  Cooldown expires after the configured window
  5.  Materially stronger signal (effective_score delta) bypasses cooldown
  6.  High-confidence + strong signal passes filter
  7.  Low-confidence signal is suppressed by alert filter
  8.  Degraded-mode + low confidence suppresses alert
  9.  Output schema contains all new fields
  10. Backward compatibility — existing fields/flows unchanged

Also covers the new config keys added in this session:
  - cooldown_hours_strong_signal / cooldown_hours_weak_signal
  - cooldown_allow_direction_change_bypass
  - cooldown_allow_high_confidence_bypass
  - cooldown_min_effective_score_delta_for_reset
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from watchlist_scanner.alert_filter import (
    cooldown_decision,
    cooldown_hours_for_tier,
    should_emit_alert,
)
from watchlist_scanner.postprocess import (
    _annotate_signal_meta,
    _apply_alert_cooldown,
    _apply_signal_meta_layer,
    _confidence_action_decision,
)
from watchlist_scanner.output_writers import _write_signals_json


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _data_health(
    degraded: bool = False,
    penalty: float = 0.15,
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "degraded_mode": degraded,
        "degraded_reason": reason or ("degraded" if degraded else None),
        "degraded_confidence_penalty": penalty,
        "data_sources_used": ["cache"] if degraded else ["alphavantage"],
        "data_mode": "fallback" if degraded else "live",
    }


def _row(
    ticker: str = "NVDA",
    signal_score: float = 0.70,
    confidence_score: float = 0.85,
    notification_status: str = "alerted",
    alert_priority: str | None = "high",
    alert_tier: str = "high",
    price_change_pct: float = 3.5,
    **extra,
) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "signal_score": signal_score,
        "confidence_score": confidence_score,
        "confidence_band": "high" if confidence_score >= 0.80 else ("medium" if confidence_score >= 0.65 else "low"),
        "notification_status": notification_status,
        "alert_priority": alert_priority,
        "alert_tier": alert_tier,
        "price_change_pct": price_change_pct,
        "routed_alert_priority": alert_priority,
        "evidence_breadth": 2,
        "evidence_categories": ["technical", "news_theme"],
        "data_quality": "fresh",
        "watchlist_source": "static",
        "score_breakdown": {"theme_news_score": 0.5, "technical_score": 0.7, "fundamental_context_score": 0.4},
        "technicals": {},
        "fundamentals": {"sector": "TECHNOLOGY"},
        "news": {},
        **extra,
    }


def _scan_result(rows: list[dict], alerts: list[dict] | None = None) -> dict[str, Any]:
    return {
        "run_date": "2026-04-14",
        "generated_at": "2026-04-14T10:00:00",
        "calls_used": 1,
        "scan_summary": {"scan_status": "ok", "symbols_fresh": len(rows)},
        "results": rows,
        "alerts": alerts if alerts is not None else list(rows),
    }


def _signals_cfg(**overrides) -> dict[str, Any]:
    base = {
        "min_signal_score": 0.50,
        "min_confidence_score": 0.50,
        "min_evidence_count": 2,
        "confidence_tiers": {"high": 0.80, "medium": 0.65, "low": 0.50},
        "cooldown": {"high": 6, "medium": 24, "low": 72},
    }
    base.update(overrides)
    return base


def _state_with_emailed(hours_ago: float, **extra) -> dict[str, Any]:
    ts = (datetime.now() - timedelta(hours=hours_ago)).isoformat()
    return {"last_emailed": ts, "state_hash": "", "alert_tier": "high", **extra}


# ---------------------------------------------------------------------------
# 1. effective_score = signal_score × confidence_score
# ---------------------------------------------------------------------------

class TestEffectiveScoreCalculation(unittest.TestCase):

    def _annotate(self, row, data_health=None):
        r = dict(row)
        _annotate_signal_meta(r, data_health=data_health or _data_health())
        return r

    def test_effective_score_is_product(self):
        r = self._annotate(_row(signal_score=0.80, confidence_score=0.90))
        self.assertAlmostEqual(r["effective_score"], round(0.80 * 0.90, 3))

    def test_confidence_weight_equals_confidence_score(self):
        r = self._annotate(_row(signal_score=0.60, confidence_score=0.75))
        self.assertAlmostEqual(r["confidence_weight"], 0.75)

    def test_effective_score_clamped_to_zero(self):
        r = self._annotate(_row(signal_score=0.0, confidence_score=0.0))
        self.assertGreaterEqual(r["effective_score"], 0.0)

    def test_signal_score_unchanged(self):
        row = _row(signal_score=0.65, confidence_score=0.85)
        r = self._annotate(row)
        self.assertEqual(r["signal_score"], 0.65)

    def test_confidence_score_unchanged(self):
        row = _row(signal_score=0.65, confidence_score=0.85)
        r = self._annotate(row)
        self.assertEqual(r["confidence_score"], 0.85)

    def test_max_effective_score_not_above_one(self):
        r = self._annotate(_row(signal_score=1.0, confidence_score=1.0))
        self.assertLessEqual(r["effective_score"], 1.0)


# ---------------------------------------------------------------------------
# 2. Degraded-mode penalty reduces effective_score
# ---------------------------------------------------------------------------

class TestDegradedModePenalty(unittest.TestCase):

    def _annotate(self, row, degraded=True, penalty=0.15):
        r = dict(row)
        _annotate_signal_meta(r, data_health=_data_health(degraded=degraded, penalty=penalty))
        return r

    def test_degraded_reduces_effective_score(self):
        row = _row(signal_score=0.80, confidence_score=0.90)
        normal  = dict(row); _annotate_signal_meta(normal,  data_health=_data_health(False))
        degraded = dict(row); _annotate_signal_meta(degraded, data_health=_data_health(True, penalty=0.20))
        self.assertLess(degraded["effective_score"], normal["effective_score"])

    def test_degraded_penalty_formula(self):
        r = self._annotate(_row(signal_score=0.80, confidence_score=0.90), penalty=0.20)
        expected = round(0.80 * 0.90 * (1.0 - 0.20), 3)
        self.assertAlmostEqual(r["effective_score"], expected)

    def test_zero_penalty_no_change(self):
        row = _row(signal_score=0.80, confidence_score=0.90)
        r = self._annotate(row, degraded=True, penalty=0.0)
        self.assertAlmostEqual(r["effective_score"], round(0.80 * 0.90, 3))

    def test_full_penalty_zeroes_effective_score(self):
        r = self._annotate(_row(signal_score=0.80, confidence_score=0.90), penalty=1.0)
        self.assertAlmostEqual(r["effective_score"], 0.0)

    def test_signal_score_unchanged_in_degraded_mode(self):
        row = _row(signal_score=0.65, confidence_score=0.80)
        r = self._annotate(row, degraded=True, penalty=0.25)
        self.assertEqual(r["signal_score"], 0.65)

    def test_confidence_score_unchanged_in_degraded_mode(self):
        row = _row(signal_score=0.65, confidence_score=0.80)
        r = self._annotate(row, degraded=True, penalty=0.25)
        self.assertEqual(r["confidence_score"], 0.80)


# ---------------------------------------------------------------------------
# 3. Cooldown activates on repeated unchanged signals
# ---------------------------------------------------------------------------

class TestCooldownActivation(unittest.TestCase):

    def _suppressed_decision(self, hours_ago: float = 1.0, tier: str = "high") -> dict:
        signal = _row(alert_tier=tier, alert_state_hash="aabbccdd")
        state  = _state_with_emailed(hours_ago, state_hash="aabbccdd", alert_tier=tier)
        return cooldown_decision(signal, state, _signals_cfg())

    def test_unchanged_high_tier_within_6h_is_suppressed(self):
        d = self._suppressed_decision(hours_ago=3.0, tier="high")
        self.assertFalse(d["allowed"])
        self.assertIn("cooldown_active", d["reason_code"])

    def test_unchanged_medium_tier_within_24h_is_suppressed(self):
        signal = _row(alert_tier="medium", alert_state_hash="hash1")
        state  = _state_with_emailed(12.0, state_hash="hash1", alert_tier="medium")
        d = cooldown_decision(signal, state, _signals_cfg())
        self.assertFalse(d["allowed"])

    def test_unchanged_low_tier_within_72h_is_suppressed(self):
        signal = _row(alert_tier="low", alert_state_hash="hash2")
        state  = _state_with_emailed(48.0, state_hash="hash2", alert_tier="low")
        d = cooldown_decision(signal, state, _signals_cfg())
        self.assertFalse(d["allowed"])

    def test_no_prior_state_is_not_suppressed(self):
        d = cooldown_decision(_row(), None, _signals_cfg())
        self.assertTrue(d["allowed"])

    def test_prior_state_not_yet_notified_is_not_suppressed(self):
        d = cooldown_decision(_row(), {"last_emailed": None}, _signals_cfg())
        self.assertTrue(d["allowed"])

    def test_reason_code_names_tier(self):
        d = self._suppressed_decision(tier="medium")
        self.assertIn("medium", d["reason_code"])


# ---------------------------------------------------------------------------
# 4. Cooldown expires after the configured window
# ---------------------------------------------------------------------------

class TestCooldownExpiry(unittest.TestCase):

    def test_high_tier_expires_after_6h(self):
        signal = _row(alert_tier="high", alert_state_hash="hash3")
        state  = _state_with_emailed(7.0, state_hash="hash3", alert_tier="high")
        d = cooldown_decision(signal, state, _signals_cfg())
        self.assertTrue(d["allowed"])
        self.assertEqual(d["reason_code"], "allowed_high")

    def test_medium_tier_expires_after_24h(self):
        signal = _row(alert_tier="medium", alert_state_hash="hash4")
        state  = _state_with_emailed(25.0, state_hash="hash4", alert_tier="medium")
        d = cooldown_decision(signal, state, _signals_cfg())
        self.assertTrue(d["allowed"])

    def test_low_tier_expires_after_72h(self):
        signal = _row(alert_tier="low", alert_state_hash="hash5")
        state  = _state_with_emailed(73.0, state_hash="hash5", alert_tier="low")
        d = cooldown_decision(signal, state, _signals_cfg())
        self.assertTrue(d["allowed"])

    def test_strong_signal_uses_shorter_cooldown(self):
        """cooldown_hours_strong_signal = 4 → expires after 5h even for medium tier."""
        cfg    = _signals_cfg(
            cooldown_hours_strong_signal=4,
            strong_signal_threshold=0.60,
        )
        signal = _row(
            alert_tier="medium",
            signal_score=0.80,
            confidence_score=0.90,
            effective_score=0.72,    # > 0.60 threshold
            alert_state_hash="hash6",
        )
        state  = _state_with_emailed(5.0, state_hash="hash6", alert_tier="medium")
        d = cooldown_decision(signal, state, cfg)
        # With normal 24h cooldown this would be suppressed; 4h window expired
        self.assertTrue(d["allowed"])
        self.assertEqual(d["cooldown_applied_hours"], 4)

    def test_weak_signal_uses_longer_cooldown(self):
        """cooldown_hours_weak_signal = 96 → still active after 48h for weak signal."""
        cfg    = _signals_cfg(
            cooldown_hours_weak_signal=96,
            weak_signal_threshold=0.30,
        )
        signal = _row(
            alert_tier="high",
            signal_score=0.30,
            confidence_score=0.80,
            effective_score=0.24,    # < 0.30 threshold
            alert_state_hash="hash7",
        )
        state  = _state_with_emailed(48.0, state_hash="hash7", alert_tier="high")
        d = cooldown_decision(signal, state, cfg)
        # 96h window not yet expired
        self.assertFalse(d["allowed"])
        self.assertEqual(d["cooldown_applied_hours"], 96)


# ---------------------------------------------------------------------------
# 5. Materially stronger signal bypasses cooldown
# ---------------------------------------------------------------------------

class TestEffectiveScoreDeltaBypass(unittest.TestCase):

    def _state_with_scores(self, hours_ago: float, last_sig: float, last_conf: float) -> dict:
        ts = (datetime.now() - timedelta(hours=hours_ago)).isoformat()
        return {
            "last_emailed": ts,
            "state_hash": "same_hash",
            "alert_tier": "high",
            "last_signal_score": last_sig,
            "last_confidence_score": last_conf,
        }

    def test_large_delta_bypasses_cooldown(self):
        cfg    = _signals_cfg(cooldown_min_effective_score_delta_for_reset=0.15)
        signal = _row(
            alert_tier="high",
            signal_score=0.90,
            confidence_score=0.92,
            effective_score=0.828,   # vs prior 0.60 * 0.70 = 0.42 → delta 0.408
            alert_state_hash="same_hash",
        )
        state  = self._state_with_scores(1.0, 0.60, 0.70)
        d      = cooldown_decision(signal, state, cfg)
        self.assertTrue(d["allowed"])
        self.assertEqual(d["reason_code"], "allowed_effective_score_jump")

    def test_small_delta_does_not_bypass(self):
        cfg    = _signals_cfg(cooldown_min_effective_score_delta_for_reset=0.20)
        signal = _row(
            alert_tier="high",
            signal_score=0.72,
            confidence_score=0.85,
            effective_score=0.612,   # vs prior 0.70 * 0.85 = 0.595 → delta ~0.017
            alert_state_hash="same_hash",
        )
        state  = self._state_with_scores(1.0, 0.70, 0.85)
        d      = cooldown_decision(signal, state, cfg)
        self.assertFalse(d["allowed"])

    def test_zero_min_delta_disables_feature(self):
        """min_delta = 0 means the bypass is not applied."""
        cfg    = _signals_cfg(cooldown_min_effective_score_delta_for_reset=0.0)
        signal = _row(
            alert_tier="high",
            signal_score=1.0,
            confidence_score=1.0,
            effective_score=1.0,
            alert_state_hash="same_hash",
        )
        state  = self._state_with_scores(1.0, 0.0, 0.0)
        d      = cooldown_decision(signal, state, cfg)
        # Only tier-upgrade bypass applies; signal didn't change tier
        self.assertFalse(d["allowed"])

    def test_override_reason_contains_delta(self):
        cfg    = _signals_cfg(cooldown_min_effective_score_delta_for_reset=0.10)
        signal = _row(
            alert_tier="high",
            signal_score=0.90,
            confidence_score=0.90,
            effective_score=0.81,
            alert_state_hash="same_hash",
        )
        state  = self._state_with_scores(2.0, 0.50, 0.50)
        d      = cooldown_decision(signal, state, cfg)
        self.assertTrue(d["allowed"])
        self.assertIn("effective_score_delta", d["override_reason"])


# ---------------------------------------------------------------------------
# 6. High-confidence signal passes filter
# ---------------------------------------------------------------------------

class TestHighConfidencePassesFilter(unittest.TestCase):

    def test_high_conf_high_priority_allowed(self):
        d = should_emit_alert(
            {"signal_score": 0.72, "confidence_score": 0.88,
             "routed_alert_priority": "high", "evidence_breadth": 1},
            _signals_cfg(),
        )
        self.assertTrue(d["allowed"])
        self.assertEqual(d["tier"], "high")

    def test_high_conf_bypass_within_cooldown(self):
        cfg    = _signals_cfg(
            cooldown_allow_high_confidence_bypass=True,
            strong_signal_threshold=0.70,
        )
        signal = _row(
            alert_tier="high",
            signal_score=0.82,
            confidence_score=0.88,
            alert_state_hash="same_hash",
        )
        state  = _state_with_emailed(1.0, state_hash="same_hash", alert_tier="high")
        d      = cooldown_decision(signal, state, cfg)
        self.assertTrue(d["allowed"])
        self.assertEqual(d["reason_code"], "allowed_high_conf_bypass")
        self.assertEqual(d["override_reason"], "high_confidence_bypass")

    def test_high_conf_bypass_off_by_default(self):
        """cooldown_allow_high_confidence_bypass defaults to False."""
        cfg    = _signals_cfg()   # no bypass flag
        signal = _row(
            alert_tier="high",
            signal_score=0.90,
            confidence_score=0.95,
            alert_state_hash="same_hash",
        )
        state  = _state_with_emailed(1.0, state_hash="same_hash", alert_tier="high")
        d      = cooldown_decision(signal, state, cfg)
        self.assertFalse(d["allowed"])

    def test_high_conf_bypass_requires_strong_signal(self):
        """confidence is high but signal is weak → bypass should NOT fire."""
        cfg    = _signals_cfg(
            cooldown_allow_high_confidence_bypass=True,
            strong_signal_threshold=0.80,
        )
        signal = _row(
            alert_tier="high",
            signal_score=0.50,    # below strong_threshold 0.80
            confidence_score=0.90,
            alert_state_hash="same_hash",
        )
        state  = _state_with_emailed(1.0, state_hash="same_hash", alert_tier="high")
        d      = cooldown_decision(signal, state, cfg)
        self.assertFalse(d["allowed"])


# ---------------------------------------------------------------------------
# 7. Low-confidence signal is suppressed by alert filter
# ---------------------------------------------------------------------------

class TestLowConfidenceSuppressed(unittest.TestCase):

    def test_below_min_confidence_suppressed(self):
        d = should_emit_alert(
            {"signal_score": 0.90, "confidence_score": 0.45,
             "routed_alert_priority": "watch", "evidence_breadth": 3},
            _signals_cfg(),
        )
        self.assertFalse(d["allowed"])
        self.assertEqual(d["reason_code"], "below_min_confidence")

    def test_no_confidence_tier_suppressed(self):
        d = should_emit_alert(
            {"signal_score": 0.90, "confidence_score": 0.30,
             "routed_alert_priority": "high", "evidence_breadth": 5},
            _signals_cfg(),
        )
        self.assertFalse(d["allowed"])

    def test_action_filter_suppresses_low_confidence(self):
        row = _row(signal_score=0.70, confidence_score=0.40)
        d   = _confidence_action_decision(
            row,
            data_health=_data_health(False),
            signals_config=_signals_cfg(action_filter={"min_confidence_score": 0.55}),
        )
        self.assertFalse(d["allowed"])
        self.assertIn("confidence", d["reason"])

    def test_medium_conf_without_evidence_suppressed(self):
        d = should_emit_alert(
            {"signal_score": 0.68, "confidence_score": 0.70,
             "routed_alert_priority": "watch", "evidence_breadth": 1},
            _signals_cfg(),
        )
        self.assertFalse(d["allowed"])
        self.assertEqual(d["reason_code"], "insufficient_evidence")


# ---------------------------------------------------------------------------
# 8. Degraded-mode + low confidence suppresses alert
# ---------------------------------------------------------------------------

class TestDegradedModeLowConfidenceSuppression(unittest.TestCase):

    def test_degraded_confidence_action_suppresses(self):
        row = _row(signal_score=0.70, confidence_score=0.60)
        d   = _confidence_action_decision(
            row,
            data_health=_data_health(degraded=True, penalty=0.15),
            signals_config=_signals_cfg(
                action_filter={"min_degraded_confidence_score": 0.65}
            ),
        )
        # degraded_confidence = 0.60 - 0.15 = 0.45 < 0.65
        self.assertFalse(d["allowed"])
        self.assertIn("degraded", d["reason"])

    def test_high_confidence_passes_even_in_degraded_mode(self):
        row = _row(signal_score=0.90, confidence_score=0.90)
        d   = _confidence_action_decision(
            row,
            data_health=_data_health(degraded=True, penalty=0.15),
            signals_config=_signals_cfg(
                action_filter={
                    "high_confidence_score": 0.85,
                    "strong_signal_score": 0.80,
                    "min_degraded_confidence_score": 0.70,
                }
            ),
        )
        self.assertTrue(d["allowed"])

    def test_signal_meta_layer_marks_action_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "test.db")
            row = _row(
                signal_score=0.70,
                confidence_score=0.60,
                notification_status="alerted",
                alert_priority="watch",
            )
            result = _scan_result([row], alerts=[row])
            result = _apply_signal_meta_layer(
                result,
                data_health=_data_health(degraded=True, penalty=0.15),
                db_path=db,
                signals_config=_signals_cfg(
                    action_filter={"min_degraded_confidence_score": 0.65}
                ),
            )
        suppressed = [r for r in result["results"] if r.get("action_suppressed")]
        self.assertEqual(len(suppressed), 1)
        self.assertIn("degraded", suppressed[0]["action_suppression_reason"])

    def test_meta_layer_increments_action_suppressed_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "test.db")
            row1 = _row("LOW",  signal_score=0.60, confidence_score=0.55)
            row2 = _row("HIGH", signal_score=0.90, confidence_score=0.92)
            for r in (row1, row2):
                r["notification_status"] = "alerted"
                r["alert_priority"] = "watch"
            result = _scan_result([row1, row2], alerts=[row1, row2])
            result = _apply_signal_meta_layer(
                result,
                data_health=_data_health(degraded=True, penalty=0.15),
                db_path=db,
                signals_config=_signals_cfg(
                    action_filter={
                        "min_degraded_confidence_score": 0.65,
                        "high_confidence_score": 0.88,
                        "strong_signal_score": 0.85,
                    }
                ),
            )
        self.assertGreaterEqual(result["scan_summary"]["alerts_action_suppressed"], 1)


# ---------------------------------------------------------------------------
# 9. Output schema contains all new fields
# ---------------------------------------------------------------------------

class TestOutputSchemaNewFields(unittest.TestCase):

    NEW_ROW_FIELDS = [
        "confidence_weight",
        "effective_score",
        "cooldown_active",
        "cooldown_reason",
        "actionable_signal",
        "action_suppressed",
        "action_suppression_reason",
        "last_alert_timestamp",
        "last_action_taken",
        "recent_signal_strength",
    ]

    NEW_SUMMARY_FIELDS = [
        "alerts_action_suppressed",
        "signals_suppressed",
        "cooldown_hits",
    ]

    def _run_meta_layer(self, rows, alerts=None, degraded=False):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "test.db")
            result = _scan_result(rows, alerts)
            return _apply_signal_meta_layer(
                result,
                data_health=_data_health(degraded=degraded),
                db_path=db,
                signals_config=_signals_cfg(),
            )

    def test_result_rows_have_new_fields(self):
        result = self._run_meta_layer([_row()])
        row = result["results"][0]
        for field in self.NEW_ROW_FIELDS:
            self.assertIn(field, row, f"Missing field: {field}")

    def test_scan_summary_has_new_fields(self):
        result = self._run_meta_layer([_row()])
        summary = result["scan_summary"]
        for field in self.NEW_SUMMARY_FIELDS:
            self.assertIn(field, summary, f"Missing summary field: {field}")

    def test_signals_json_contains_new_fields(self):
        result = self._run_meta_layer([_row()])
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            _write_signals_json(out_dir, result)
            data = json.loads((out_dir / "watchlist_signals.json").read_text(encoding="utf-8"))
        row = data["results"][0]
        for field in self.NEW_ROW_FIELDS:
            self.assertIn(field, row, f"Missing in JSON: {field}")

    def test_cooldown_active_is_bool(self):
        result = self._run_meta_layer([_row()])
        row = result["results"][0]
        self.assertIsInstance(row["cooldown_active"], bool)

    def test_effective_score_is_float(self):
        result = self._run_meta_layer([_row(signal_score=0.70, confidence_score=0.85)])
        row = result["results"][0]
        self.assertIsInstance(row["effective_score"], float)

    def test_confidence_weight_is_float(self):
        result = self._run_meta_layer([_row(confidence_score=0.85)])
        row = result["results"][0]
        self.assertIsInstance(row["confidence_weight"], float)


# ---------------------------------------------------------------------------
# 10. Backward compatibility — existing fields/flows unchanged
# ---------------------------------------------------------------------------

class TestBackwardCompatibility(unittest.TestCase):

    def test_signal_score_not_modified_by_meta_layer(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "test.db")
            row = _row(signal_score=0.65)
            result = _apply_signal_meta_layer(
                _scan_result([row]),
                data_health=_data_health(),
                db_path=db,
            )
        self.assertEqual(result["results"][0]["signal_score"], 0.65)

    def test_confidence_score_not_modified_by_meta_layer(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "test.db")
            row = _row(confidence_score=0.82)
            result = _apply_signal_meta_layer(
                _scan_result([row]),
                data_health=_data_health(),
                db_path=db,
            )
        self.assertEqual(result["results"][0]["confidence_score"], 0.82)

    def test_alert_priority_not_modified_by_meta_layer(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "test.db")
            row = _row(alert_priority="high")
            result = _apply_signal_meta_layer(
                _scan_result([row]),
                data_health=_data_health(),
                db_path=db,
            )
        self.assertEqual(result["results"][0]["alert_priority"], "high")

    def test_score_breakdown_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "test.db")
            breakdown = {"theme_news_score": 0.6, "technical_score": 0.7, "fundamental_context_score": 0.5}
            row = _row(score_breakdown=breakdown)
            result = _apply_signal_meta_layer(
                _scan_result([row]),
                data_health=_data_health(),
                db_path=db,
            )
        self.assertEqual(result["results"][0]["score_breakdown"], breakdown)

    def test_cooldown_decision_no_state_still_allows(self):
        """Existing behaviour: no state → allow."""
        d = cooldown_decision(_row(), None)
        self.assertTrue(d["allowed"])
        self.assertIn("allowed", d["reason_code"])

    def test_cooldown_decision_expired_state_still_allows(self):
        """Existing behaviour: expired window → allow."""
        signal = _row(alert_tier="high", alert_state_hash="h")
        state  = _state_with_emailed(7.0, state_hash="h", alert_tier="high")
        d = cooldown_decision(signal, state, _signals_cfg())
        self.assertTrue(d["allowed"])

    def test_should_emit_high_conf_still_passes_without_new_config(self):
        """Existing behaviour unchanged when no new config keys are set."""
        d = should_emit_alert(
            {"signal_score": 0.72, "confidence_score": 0.88,
             "routed_alert_priority": "high", "evidence_breadth": 2},
            _signals_cfg(),
        )
        self.assertTrue(d["allowed"])
        self.assertEqual(d["reason_code"], "allowed_high")

    def test_scan_result_structure_unchanged(self):
        """run_date, generated_at, calls_used survive the meta layer."""
        with tempfile.TemporaryDirectory() as tmp:
            db  = str(Path(tmp) / "test.db")
            res = _apply_signal_meta_layer(
                _scan_result([_row()]),
                data_health=_data_health(),
                db_path=db,
            )
        self.assertEqual(res["run_date"], "2026-04-14")
        self.assertEqual(res["generated_at"], "2026-04-14T10:00:00")
        self.assertEqual(res["calls_used"], 1)


# ---------------------------------------------------------------------------
# New config keys
# ---------------------------------------------------------------------------

class TestNewConfigKeys(unittest.TestCase):

    def test_direction_bypass_default_true_allows_state_change(self):
        """Without explicit flag, direction bypass defaults to True (existing behaviour)."""
        signal = _row(alert_tier="high", alert_state_hash="new_hash")
        state  = _state_with_emailed(1.0, state_hash="old_hash", alert_tier="high")
        d = cooldown_decision(signal, state, _signals_cfg())
        self.assertTrue(d["allowed"])

    def test_direction_bypass_false_blocks_state_change_bypass(self):
        cfg    = _signals_cfg(cooldown_allow_direction_change_bypass=False)
        signal = _row(alert_tier="high", alert_state_hash="new_hash")
        state  = _state_with_emailed(1.0, state_hash="old_hash", alert_tier="high")
        d = cooldown_decision(signal, state, cfg)
        # Without direction bypass, only tier/priority upgrade can bypass
        self.assertFalse(d["allowed"])

    def test_high_conf_bypass_enabled_short_circuits_cooldown(self):
        cfg    = _signals_cfg(
            cooldown_allow_high_confidence_bypass=True,
            strong_signal_threshold=0.70,
        )
        signal = _row(
            alert_tier="medium",
            signal_score=0.80,
            confidence_score=0.88,
            alert_state_hash="same",
        )
        state  = _state_with_emailed(2.0, state_hash="same", alert_tier="medium")
        d = cooldown_decision(signal, state, cfg)
        self.assertTrue(d["allowed"])
        self.assertEqual(d["reason_code"], "allowed_high_conf_bypass")

    def test_strong_signal_cooldown_hours_applied(self):
        cfg    = _signals_cfg(
            cooldown_hours_strong_signal=4,
            strong_signal_threshold=0.60,
        )
        signal = _row(
            alert_tier="medium",
            signal_score=0.80,
            confidence_score=0.90,
            effective_score=0.72,
            alert_state_hash="same2",
        )
        state  = _state_with_emailed(1.0, state_hash="same2", alert_tier="medium")
        d = cooldown_decision(signal, state, cfg)
        self.assertEqual(d["cooldown_applied_hours"], 4)

    def test_weak_signal_cooldown_hours_applied(self):
        cfg    = _signals_cfg(
            cooldown_hours_weak_signal=96,
            weak_signal_threshold=0.30,
        )
        signal = _row(
            alert_tier="high",
            signal_score=0.20,
            confidence_score=0.80,
            effective_score=0.16,    # < 0.30 threshold
            alert_state_hash="same3",
        )
        state  = _state_with_emailed(1.0, state_hash="same3", alert_tier="high")
        d = cooldown_decision(signal, state, cfg)
        self.assertEqual(d["cooldown_applied_hours"], 96)

    def test_strong_and_weak_absent_falls_back_to_tier(self):
        """Neither key set → tier-based hours used."""
        cfg    = _signals_cfg()   # no override keys
        d_high = cooldown_hours_for_tier("high", cfg)
        d_med  = cooldown_hours_for_tier("medium", cfg)
        self.assertEqual(d_high, 6)
        self.assertEqual(d_med, 24)


if __name__ == "__main__":
    unittest.main()
