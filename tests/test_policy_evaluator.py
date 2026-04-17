"""
Tests for policy_evaluator — recommendation observability layer.

Coverage areas
--------------
1. Artifact creation         — JSONL file is created and appended correctly
2. Scoring / metric logic    — hit rates, calibration, stability, gap
3. Sparse history handling   — 0 records, 1 run, missing file
4. Backward compatibility    — old records (missing new keys) parse safely
5. Report writer             — JSON + MD files are written and parseable
6. Base-id extraction        — date-suffix stripping is correct
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass, field
from datetime import datetime, date
from enum import Enum
from pathlib import Path
from typing import List, Optional
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Minimal stubs so the tests don't need the full scoring module
# ---------------------------------------------------------------------------

class _ImpactArea(Enum):
    CASH_SAFETY = "Cash Safety"
    PORTFOLIO_RISK = "Portfolio Risk"
    CASHFLOW = "Cashflow"


class _ActionLevel(Enum):
    FYI = "FYI"
    MONITOR = "Monitor"
    RECOMMENDED = "Recommended"
    ACTION_REQUIRED = "Action Required"


@dataclass
class _ScoringComponents:
    severity: int = 0
    persistence: int = 0
    impact: int = 0
    priority: int = 0
    confidence: int = 100

    @property
    def raw_score(self) -> int:
        return min(100, self.severity + self.persistence + self.impact + self.priority)

    @property
    def final_score(self) -> int:
        return int(self.raw_score * (self.confidence / 100))


@dataclass
class _FinanceRecommendation:
    id: str
    impact_area: _ImpactArea
    components: _ScoringComponents
    title: str = ""
    trigger: str = ""
    what_changed: str = ""
    why_it_matters: str = ""
    action: str = ""
    next_check: str = ""
    evidence: str = ""
    created_at: datetime = field(default_factory=datetime.now)
    last_sent: Optional[datetime] = None

    @property
    def final_score(self) -> int:
        return self.components.final_score

    @property
    def action_level(self) -> _ActionLevel:
        score = self.final_score
        if score >= 75:
            return _ActionLevel.ACTION_REQUIRED
        elif score >= 50:
            return _ActionLevel.RECOMMENDED
        elif score >= 25:
            return _ActionLevel.MONITOR
        return _ActionLevel.FYI


def _make_rec(
    rec_id: str,
    *,
    severity: int = 30,
    persistence: int = 10,
    impact: int = 15,
    priority: int = 6,
    confidence: int = 100,
    impact_area: _ImpactArea = _ImpactArea.CASH_SAFETY,
) -> _FinanceRecommendation:
    return _FinanceRecommendation(
        id=rec_id,
        impact_area=impact_area,
        components=_ScoringComponents(
            severity=severity,
            persistence=persistence,
            impact=impact,
            priority=priority,
            confidence=confidence,
        ),
        title=f"Test rec {rec_id}",
        trigger="test trigger",
    )


# ---------------------------------------------------------------------------
# 1. Artifact creation
# ---------------------------------------------------------------------------

class TestHistoryWriter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.history_path = Path(self.tmp.name) / "recommendation_history.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, recs, run_id="2026-01-01_daily", **kwargs):
        from policy_evaluator.history_writer import append_run_recommendations
        return append_run_recommendations(
            recs,
            run_id=run_id,
            run_mode=kwargs.get("run_mode", "daily"),
            data_health=kwargs.get("data_health"),
            drawdown_state=kwargs.get("drawdown_state"),
            drawdown_regime=kwargs.get("drawdown_regime", "normal"),
            guardrails=kwargs.get("guardrails"),
            growth_mode=kwargs.get("growth_mode", "accumulation_aggressive"),
            history_path=self.history_path,
        )

    def test_creates_file_on_first_write(self):
        recs = [_make_rec("emergency_fund_2026-01-01")]
        count = self._write(recs)
        self.assertEqual(count, 1)
        self.assertTrue(self.history_path.exists())

    def test_appends_on_subsequent_writes(self):
        recs1 = [_make_rec("emergency_fund_2026-01-01")]
        recs2 = [_make_rec("drift_QQQ_2026-01-02")]
        self._write(recs1, run_id="2026-01-01_daily")
        self._write(recs2, run_id="2026-01-02_daily")

        lines = self.history_path.read_text().strip().split("\n")
        self.assertEqual(len(lines), 2)

    def test_each_line_is_valid_json(self):
        recs = [
            _make_rec("emergency_fund_2026-01-01"),
            _make_rec("drift_QQQ_2026-01-01", impact_area=_ImpactArea.PORTFOLIO_RISK),
        ]
        self._write(recs)
        for line in self.history_path.read_text().strip().split("\n"):
            record = json.loads(line)
            self.assertIn("run_id", record)
            self.assertIn("rec_id", record)
            self.assertIn("score", record)

    def test_record_fields_present(self):
        recs = [_make_rec("emergency_fund_2026-01-01")]
        self._write(recs, run_id="2026-01-01_daily")
        record = json.loads(self.history_path.read_text().strip())

        required_fields = [
            "run_id", "timestamp", "run_mode", "regime", "degraded_mode",
            "degraded_reason", "degraded_confidence_penalty", "data_mode",
            "has_guardrail_violations", "growth_mode", "drawdown_pct",
            "drawdown_regime", "rec_id", "rec_base_id", "impact_area",
            "title", "score", "raw_score", "action_level", "severity",
            "persistence_score", "impact_score", "priority", "confidence", "trigger",
        ]
        for f in required_fields:
            self.assertIn(f, record, f"Missing field: {f}")

    def test_base_id_strips_date_suffix(self):
        recs = [_make_rec("drift_QQQ_2026-04-16")]
        self._write(recs)
        record = json.loads(self.history_path.read_text().strip())
        self.assertEqual(record["rec_base_id"], "drift_QQQ")

    def test_base_id_strips_hyphenated_date(self):
        recs = [_make_rec("structural_concentration_QQQ_2026-04-16")]
        self._write(recs)
        record = json.loads(self.history_path.read_text().strip())
        self.assertEqual(record["rec_base_id"], "structural_concentration_QQQ")

    def test_base_id_unchanged_without_date_suffix(self):
        from policy_evaluator.history_writer import _strip_date_suffix
        self.assertEqual(_strip_date_suffix("emergency_fund"), "emergency_fund")
        self.assertEqual(_strip_date_suffix("drift_QQQ"), "drift_QQQ")

    def test_dry_run_does_not_write(self):
        from policy_evaluator.history_writer import append_run_recommendations
        recs = [_make_rec("emergency_fund_2026-01-01")]
        count = append_run_recommendations(
            recs,
            run_id="2026-01-01_daily",
            run_mode="daily",
            history_path=self.history_path,
            dry_run=True,
        )
        self.assertEqual(count, 1)
        self.assertFalse(self.history_path.exists())

    def test_empty_recs_returns_zero(self):
        count = self._write([])
        self.assertEqual(count, 0)
        self.assertFalse(self.history_path.exists())

    def test_degraded_context_serialized(self):
        recs = [_make_rec("emergency_fund_2026-01-01")]
        self._write(
            recs,
            data_health={
                "degraded_mode": True,
                "degraded_reason": "circuit_breaker",
                "degraded_confidence_penalty": 0.3,
                "data_mode": "fallback",
            },
        )
        record = json.loads(self.history_path.read_text().strip())
        self.assertTrue(record["degraded_mode"])
        self.assertEqual(record["degraded_reason"], "circuit_breaker")
        self.assertAlmostEqual(record["degraded_confidence_penalty"], 0.3)

    def test_guardrail_violations_serialized(self):
        recs = [_make_rec("emergency_fund_2026-01-01")]
        self._write(
            recs,
            guardrails={
                "pass": False,
                "violations": [
                    {"rule": "concentration_cap", "symbol": "QQQ"},
                    {"rule": "leverage_cap"},
                ],
            },
        )
        record = json.loads(self.history_path.read_text().strip())
        self.assertTrue(record["has_guardrail_violations"])
        self.assertIn("concentration_cap", record["guardrail_violation_types"])


# ---------------------------------------------------------------------------
# 2. Scoring / metric logic
# ---------------------------------------------------------------------------

class TestEvaluator(unittest.TestCase):
    """Tests for policy_evaluator.evaluator.*"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.history_path = Path(self.tmp.name) / "recommendation_history.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _write_records(self, rows: list[dict]) -> None:
        with self.history_path.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def _base_record(self, **overrides) -> dict:
        base = {
            "run_id": "2026-01-01_daily",
            "timestamp": "2026-01-01T08:00:00",
            "run_mode": "daily",
            "regime": "normal",
            "degraded_mode": False,
            "degraded_reason": None,
            "degraded_confidence_penalty": 0.0,
            "data_mode": "live",
            "has_guardrail_violations": False,
            "guardrail_violation_types": [],
            "growth_mode": "accumulation_aggressive",
            "drawdown_pct": 0.0,
            "drawdown_regime": "normal",
            "rec_id": "emergency_fund_2026-01-01",
            "rec_base_id": "emergency_fund",
            "impact_area": "Cash Safety",
            "title": "Emergency fund below target",
            "score": 88,
            "raw_score": 88,
            "action_level": "Action Required",
            "severity": 40,
            "persistence_score": 18,
            "impact_score": 22,
            "priority": 8,
            "confidence": 100,
            "trigger": "EmergencyFund 0.2 months < 3 month target",
        }
        base.update(overrides)
        return base

    def test_empty_history_returns_zero(self):
        from policy_evaluator.evaluator import evaluate_history
        result = evaluate_history(history_path=self.history_path)
        self.assertEqual(result.total_records, 0)
        self.assertEqual(result.total_runs, 0)
        self.assertIsNone(result.date_range["first"])

    def test_single_run_no_hit_rate(self):
        from policy_evaluator.evaluator import evaluate_history
        self._write_records([self._base_record()])
        result = evaluate_history(history_path=self.history_path)
        self.assertEqual(result.total_records, 1)
        self.assertEqual(result.total_runs, 1)
        # Hit rate requires ≥ 2 runs
        self.assertEqual(result.hit_rate_by_regime, {})
        self.assertEqual(result.hit_rate_by_mode, {})

    def test_resolved_rec_increments_hit_count(self):
        from policy_evaluator.evaluator import evaluate_history
        # Run 1: emergency_fund present
        # Run 2: emergency_fund absent (resolved) → hit
        self._write_records([
            self._base_record(run_id="2026-01-01_daily", rec_base_id="emergency_fund"),
        ])
        self._write_records([
            self._base_record(
                run_id="2026-01-02_daily",
                timestamp="2026-01-02T08:00:00",
                rec_base_id="drift_QQQ",
                rec_id="drift_QQQ_2026-01-02",
            ),
        ])
        result = evaluate_history(history_path=self.history_path)
        self.assertEqual(result.total_runs, 2)
        regime_bucket = result.hit_rate_by_regime.get("normal")
        self.assertIsNotNone(regime_bucket)
        self.assertEqual(regime_bucket["total"], 1)
        self.assertEqual(regime_bucket["resolved"], 1)
        self.assertAlmostEqual(regime_bucket["hit_rate"], 1.0)

    def test_persistent_rec_not_counted_as_resolved(self):
        from policy_evaluator.evaluator import evaluate_history
        # Same base_id in both runs → not resolved
        self._write_records([
            self._base_record(run_id="2026-01-01_daily", rec_base_id="emergency_fund"),
        ])
        self._write_records([
            self._base_record(
                run_id="2026-01-02_daily",
                timestamp="2026-01-02T08:00:00",
                rec_base_id="emergency_fund",
            ),
        ])
        result = evaluate_history(history_path=self.history_path)
        regime_bucket = result.hit_rate_by_regime.get("normal")
        self.assertEqual(regime_bucket["resolved"], 0)
        self.assertAlmostEqual(regime_bucket["hit_rate"], 0.0)

    def test_hit_rate_by_mode_degraded_vs_normal(self):
        from policy_evaluator.evaluator import evaluate_history
        # Run 1: one normal rec, one degraded rec
        self._write_records([
            self._base_record(
                run_id="2026-01-01_daily",
                rec_base_id="rec_a",
                degraded_mode=False,
                regime="normal",
            ),
            self._base_record(
                run_id="2026-01-01_daily",
                rec_base_id="rec_b",
                rec_id="rec_b_2026-01-01",
                degraded_mode=True,
                degraded_reason="circuit_breaker",
                regime="normal",
            ),
        ])
        # Run 2: only rec_a present → rec_b (degraded) resolved
        self._write_records([
            self._base_record(
                run_id="2026-01-02_daily",
                timestamp="2026-01-02T08:00:00",
                rec_base_id="rec_a",
                degraded_mode=False,
            ),
        ])
        result = evaluate_history(history_path=self.history_path)
        normal_bucket = result.hit_rate_by_mode.get("normal", {})
        degraded_bucket = result.hit_rate_by_mode.get("degraded", {})
        # rec_a is normal and persisted → 0 resolved
        self.assertEqual(normal_bucket.get("resolved", 0), 0)
        # rec_b is degraded and resolved → 1 resolved
        self.assertEqual(degraded_bucket.get("resolved", 0), 1)

    def test_stability_first_run_churn_null(self):
        from policy_evaluator.evaluator import evaluate_history
        self._write_records([self._base_record()])
        result = evaluate_history(history_path=self.history_path)
        per_run = result.recommendation_stability["per_run"]
        self.assertEqual(len(per_run), 1)
        self.assertIsNone(per_run[0]["churn_rate"])

    def test_stability_churn_rate_all_new(self):
        from policy_evaluator.evaluator import evaluate_history
        self._write_records([
            self._base_record(run_id="2026-01-01_daily", rec_base_id="rec_a"),
        ])
        self._write_records([
            self._base_record(
                run_id="2026-01-02_daily",
                timestamp="2026-01-02T08:00:00",
                rec_base_id="rec_b",
                rec_id="rec_b_2026-01-02",
            ),
        ])
        result = evaluate_history(history_path=self.history_path)
        per_run = result.recommendation_stability["per_run"]
        second_run = per_run[1]
        self.assertAlmostEqual(second_run["churn_rate"], 1.0)
        self.assertEqual(second_run["carried_over"], 0)
        self.assertEqual(second_run["new_count"], 1)

    def test_stability_churn_rate_all_carried_over(self):
        from policy_evaluator.evaluator import evaluate_history
        self._write_records([
            self._base_record(run_id="2026-01-01_daily", rec_base_id="emergency_fund"),
        ])
        self._write_records([
            self._base_record(
                run_id="2026-01-02_daily",
                timestamp="2026-01-02T08:00:00",
                rec_base_id="emergency_fund",
            ),
        ])
        result = evaluate_history(history_path=self.history_path)
        per_run = result.recommendation_stability["per_run"]
        second_run = per_run[1]
        self.assertAlmostEqual(second_run["churn_rate"], 0.0)
        self.assertEqual(second_run["carried_over"], 1)

    def test_gap_action_required_positive(self):
        from policy_evaluator.evaluator import evaluate_history
        # Score 88 > threshold 75 → gap = 88-75 = 13
        self._write_records([self._base_record(score=88, raw_score=88)])
        result = evaluate_history(history_path=self.history_path)
        per_run = result.best_vs_recommended_gap["per_run"]
        self.assertEqual(per_run[0]["gap_best_vs_action_required_threshold"], 13)
        self.assertTrue(per_run[0]["has_action_required"])

    def test_gap_fyi_only_negative(self):
        from policy_evaluator.evaluator import evaluate_history
        # Score 20 < threshold 75 → gap = 20-75 = -55
        self._write_records([
            self._base_record(score=20, raw_score=20, action_level="FYI")
        ])
        result = evaluate_history(history_path=self.history_path)
        per_run = result.best_vs_recommended_gap["per_run"]
        self.assertEqual(per_run[0]["gap_best_vs_action_required_threshold"], -55)
        self.assertFalse(per_run[0]["has_action_required"])

    def test_confidence_discount_detected(self):
        from policy_evaluator.evaluator import evaluate_history
        # raw=88, final=62 (confidence penalty applied)
        self._write_records([
            self._base_record(score=62, raw_score=88, confidence=70, action_level="Recommended")
        ])
        result = evaluate_history(history_path=self.history_path)
        per_run = result.best_vs_recommended_gap["per_run"]
        self.assertEqual(per_run[0]["max_confidence_discount"], 26)

    def test_action_level_distribution_counts(self):
        from policy_evaluator.evaluator import evaluate_history
        self._write_records([
            self._base_record(action_level="Action Required"),
            self._base_record(action_level="Recommended", rec_id="rec_b", rec_base_id="rec_b"),
            self._base_record(action_level="FYI", rec_id="rec_c", rec_base_id="rec_c"),
        ])
        result = evaluate_history(history_path=self.history_path)
        dist = result.action_level_distribution
        self.assertEqual(dist.get("Action Required", 0), 1)
        self.assertEqual(dist.get("Recommended", 0), 1)
        self.assertEqual(dist.get("FYI", 0), 1)

    def test_impact_area_breakdown(self):
        from policy_evaluator.evaluator import evaluate_history
        self._write_records([
            self._base_record(impact_area="Cash Safety"),
            self._base_record(impact_area="Cash Safety", rec_id="r2", rec_base_id="r2"),
            self._base_record(impact_area="Portfolio Risk", rec_id="r3", rec_base_id="r3"),
        ])
        result = evaluate_history(history_path=self.history_path)
        breakdown = result.impact_area_breakdown
        self.assertEqual(breakdown.get("Cash Safety", 0), 2)
        self.assertEqual(breakdown.get("Portfolio Risk", 0), 1)

    def test_multiple_regimes_split_correctly(self):
        from policy_evaluator.evaluator import evaluate_history
        # Run 1: one rec in normal, one in risk_off
        self._write_records([
            self._base_record(run_id="r1", rec_base_id="rec_a", regime="normal"),
            self._base_record(run_id="r1", rec_base_id="rec_b", rec_id="rb_r1", regime="risk_off"),
        ])
        # Run 2: rec_a resolved, rec_b persists
        self._write_records([
            self._base_record(run_id="r2", rec_base_id="rec_b", rec_id="rb_r2",
                              timestamp="2026-01-02T08:00:00", regime="risk_off"),
        ])
        result = evaluate_history(history_path=self.history_path)
        normal_b = result.hit_rate_by_regime.get("normal", {})
        riskoff_b = result.hit_rate_by_regime.get("risk_off", {})
        self.assertEqual(normal_b.get("resolved", 0), 1)   # rec_a resolved
        self.assertEqual(riskoff_b.get("resolved", 0), 0)  # rec_b persisted


# ---------------------------------------------------------------------------
# 3. Sparse history handling
# ---------------------------------------------------------------------------

class TestSparseHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.history_path = Path(self.tmp.name) / "recommendation_history.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_returns_empty_result(self):
        from policy_evaluator.evaluator import evaluate_history
        result = evaluate_history(history_path=self.history_path)
        self.assertEqual(result.total_records, 0)
        self.assertEqual(result.total_runs, 0)

    def test_empty_file_returns_empty_result(self):
        from policy_evaluator.evaluator import evaluate_history
        self.history_path.write_text("", encoding="utf-8")
        result = evaluate_history(history_path=self.history_path)
        self.assertEqual(result.total_records, 0)

    def test_blank_lines_skipped(self):
        from policy_evaluator.evaluator import evaluate_history
        record = {
            "run_id": "r1", "timestamp": "2026-01-01T00:00:00",
            "run_mode": "daily", "regime": "normal",
            "degraded_mode": False, "rec_id": "r1", "rec_base_id": "r",
            "score": 50, "raw_score": 50, "action_level": "Recommended",
        }
        self.history_path.write_text(
            "\n" + json.dumps(record) + "\n\n", encoding="utf-8"
        )
        result = evaluate_history(history_path=self.history_path)
        self.assertEqual(result.total_records, 1)

    def test_single_run_stability_has_one_row(self):
        from policy_evaluator.evaluator import evaluate_history
        record = {
            "run_id": "r1", "timestamp": "2026-01-01T00:00:00",
            "run_mode": "daily", "regime": "normal", "degraded_mode": False,
            "rec_id": "ef_2026-01-01", "rec_base_id": "ef",
            "score": 88, "raw_score": 88, "action_level": "Action Required",
        }
        self.history_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        result = evaluate_history(history_path=self.history_path)
        per_run = result.recommendation_stability["per_run"]
        self.assertEqual(len(per_run), 1)
        self.assertIsNone(per_run[0]["churn_rate"])
        self.assertIsNone(result.recommendation_stability["avg_churn_rate"])

    def test_calibration_with_single_run_no_resolution_rate(self):
        from policy_evaluator.evaluator import evaluate_history
        record = {
            "run_id": "r1", "timestamp": "2026-01-01T00:00:00",
            "run_mode": "daily", "regime": "normal", "degraded_mode": False,
            "rec_id": "ef_2026-01-01", "rec_base_id": "ef",
            "score": 88, "raw_score": 88, "action_level": "Action Required",
            "confidence": 100,
        }
        self.history_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        result = evaluate_history(history_path=self.history_path)
        cal = result.confidence_calibration
        self.assertIsNone(cal.get("calibration_score"))

    def test_gap_with_zero_records_is_safe(self):
        from policy_evaluator.evaluator import evaluate_history
        result = evaluate_history(history_path=self.history_path)
        gap = result.best_vs_recommended_gap
        self.assertIsNone(gap.get("avg_gap_vs_action_required_threshold"))
        self.assertIsNone(gap.get("max_gap_vs_action_required_threshold"))


# ---------------------------------------------------------------------------
# 4. Backward compatibility
# ---------------------------------------------------------------------------

class TestBackwardCompatibility(unittest.TestCase):
    """Old records lacking newer keys must not crash the evaluator."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.history_path = Path(self.tmp.name) / "recommendation_history.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _minimal_record(self, run_id="r1") -> dict:
        """Minimum record as it would look in an early version of the schema."""
        return {
            "run_id": run_id,
            "timestamp": "2026-01-01T00:00:00",
            "rec_id": "ef_2026-01-01",
            "score": 88,
            "action_level": "Action Required",
        }

    def test_minimal_record_does_not_crash_evaluate(self):
        from policy_evaluator.evaluator import evaluate_history
        self.history_path.write_text(
            json.dumps(self._minimal_record()) + "\n", encoding="utf-8"
        )
        result = evaluate_history(history_path=self.history_path)
        self.assertEqual(result.total_records, 1)

    def test_missing_rec_base_id_falls_back_to_rec_id(self):
        from policy_evaluator.evaluator import evaluate_history
        # Two runs — old record has no rec_base_id
        r1 = {"run_id": "r1", "timestamp": "2026-01-01T00:00:00", "rec_id": "ef", "score": 88, "action_level": "Action Required"}
        r2 = {"run_id": "r2", "timestamp": "2026-01-02T00:00:00", "rec_id": "ef", "score": 88, "action_level": "Action Required"}
        with self.history_path.open("w") as fh:
            fh.write(json.dumps(r1) + "\n")
            fh.write(json.dumps(r2) + "\n")
        result = evaluate_history(history_path=self.history_path)
        self.assertEqual(result.total_runs, 2)

    def test_missing_confidence_defaults_to_100(self):
        from policy_evaluator.evaluator import evaluate_history
        record = {
            "run_id": "r1", "timestamp": "2026-01-01T00:00:00",
            "rec_id": "ef_2026-01-01", "rec_base_id": "ef",
            "score": 88, "action_level": "Action Required",
            # no "confidence" key
        }
        self.history_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        result = evaluate_history(history_path=self.history_path)
        # Should succeed without KeyError
        self.assertEqual(result.total_records, 1)

    def test_bad_json_line_skipped(self):
        from policy_evaluator.evaluator import evaluate_history
        good = json.dumps({"run_id": "r1", "timestamp": "2026-01-01T00:00:00",
                           "rec_id": "ef", "score": 88, "action_level": "Action Required"})
        self.history_path.write_text(good + "\n{invalid json\n", encoding="utf-8")
        result = evaluate_history(history_path=self.history_path)
        self.assertEqual(result.total_records, 1)

    def test_extra_unknown_fields_ignored(self):
        from policy_evaluator.evaluator import evaluate_history
        record = {
            "run_id": "r1", "timestamp": "2026-01-01T00:00:00",
            "rec_id": "ef_2026-01-01", "rec_base_id": "ef",
            "score": 88, "action_level": "Action Required",
            "future_field_v99": "some_value",
            "another_new_field": 42,
        }
        self.history_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        result = evaluate_history(history_path=self.history_path)
        self.assertEqual(result.total_records, 1)


# ---------------------------------------------------------------------------
# 5. Report writer
# ---------------------------------------------------------------------------

class TestReportWriter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.policy_dir = Path(self.tmp.name) / "policy"
        self.history_path = Path(self.tmp.name) / "recommendation_history.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _two_run_history(self):
        base = {
            "run_mode": "daily", "regime": "normal",
            "degraded_mode": False, "degraded_reason": None,
            "degraded_confidence_penalty": 0.0, "data_mode": "live",
            "has_guardrail_violations": False, "guardrail_violation_types": [],
            "growth_mode": "none", "drawdown_pct": 0.0, "drawdown_regime": "normal",
            "impact_area": "Cash Safety", "title": "t", "trigger": "x",
            "severity": 40, "persistence_score": 18, "impact_score": 22,
            "priority": 8, "confidence": 100,
        }
        rows = [
            {**base, "run_id": "r1", "timestamp": "2026-01-01T08:00:00",
             "rec_id": "ef_r1", "rec_base_id": "ef",
             "score": 88, "raw_score": 88, "action_level": "Action Required"},
            {**base, "run_id": "r2", "timestamp": "2026-01-02T08:00:00",
             "rec_id": "drift_r2", "rec_base_id": "drift",
             "score": 61, "raw_score": 61, "action_level": "Recommended"},
        ]
        with self.history_path.open("w") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def test_json_report_written_and_parseable(self):
        from policy_evaluator.evaluator import evaluate_history
        from policy_evaluator.report_writer import write_evaluation_reports
        self._two_run_history()
        result = evaluate_history(history_path=self.history_path)
        write_evaluation_reports(result, policy_dir=self.policy_dir)
        json_path = self.policy_dir / "recommendation_evaluation.json"
        self.assertTrue(json_path.exists())
        data = json.loads(json_path.read_text())
        self.assertIn("total_records", data)
        self.assertIn("total_runs", data)
        self.assertEqual(data["total_records"], 2)

    def test_md_report_written(self):
        from policy_evaluator.evaluator import evaluate_history
        from policy_evaluator.report_writer import write_evaluation_reports
        self._two_run_history()
        result = evaluate_history(history_path=self.history_path)
        write_evaluation_reports(result, policy_dir=self.policy_dir)
        md_path = self.policy_dir / "recommendation_evaluation.md"
        self.assertTrue(md_path.exists())
        content = md_path.read_text()
        self.assertIn("# Recommendation Evaluation Report", content)
        self.assertIn("Recommendation Stability", content)

    def test_dry_run_no_files_written(self):
        from policy_evaluator.evaluator import evaluate_history
        from policy_evaluator.report_writer import write_evaluation_reports
        self._two_run_history()
        result = evaluate_history(history_path=self.history_path)
        write_evaluation_reports(result, policy_dir=self.policy_dir, dry_run=True)
        self.assertFalse((self.policy_dir / "recommendation_evaluation.json").exists())
        self.assertFalse((self.policy_dir / "recommendation_evaluation.md").exists())

    def test_empty_result_report_writes_safely(self):
        from policy_evaluator.evaluator import evaluate_history
        from policy_evaluator.report_writer import write_evaluation_reports
        result = evaluate_history(history_path=self.history_path)
        ok = write_evaluation_reports(result, policy_dir=self.policy_dir)
        self.assertTrue(ok)
        json_path = self.policy_dir / "recommendation_evaluation.json"
        data = json.loads(json_path.read_text())
        self.assertEqual(data["total_records"], 0)

    def test_memo_summary_non_empty(self):
        from policy_evaluator.evaluator import evaluate_history
        from policy_evaluator.report_writer import build_memo_summary
        self._two_run_history()
        result = evaluate_history(history_path=self.history_path)
        summary = build_memo_summary(result)
        self.assertIn("Policy Evaluation", summary)

    def test_memo_summary_empty_history(self):
        from policy_evaluator.evaluator import evaluate_history
        from policy_evaluator.report_writer import build_memo_summary
        result = evaluate_history(history_path=self.history_path)
        summary = build_memo_summary(result)
        self.assertIn("no history", summary.lower())


# ---------------------------------------------------------------------------
# 6. Full-stack round-trip using real FinanceRecommendation objects
# ---------------------------------------------------------------------------

class TestRoundTripWithRealRecs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.history_path = Path(self.tmp.name) / "recommendation_history.jsonl"
        self.policy_dir = Path(self.tmp.name) / "policy"

    def tearDown(self):
        self.tmp.cleanup()

    def test_real_scoring_objects_serialized_and_evaluated(self):
        from policy_evaluator.history_writer import append_run_recommendations
        from policy_evaluator.evaluator import evaluate_history
        from policy_evaluator.report_writer import write_evaluation_reports

        recs = [
            _make_rec("emergency_fund_2026-04-16", severity=40, persistence=18,
                      impact=22, priority=8, confidence=100),
            _make_rec("drift_QQQ_2026-04-16", severity=25, persistence=18,
                      impact=12, priority=6, confidence=100,
                      impact_area=_ImpactArea.PORTFOLIO_RISK),
        ]

        # Write run 1
        append_run_recommendations(
            recs,
            run_id="2026-04-16_daily",
            run_mode="daily",
            data_health={"degraded_mode": False, "data_mode": "live"},
            drawdown_regime="normal",
            history_path=self.history_path,
        )

        # Write run 2 — emergency_fund resolved, drift persists
        recs2 = [
            _make_rec("drift_QQQ_2026-04-17", severity=25, persistence=18,
                      impact=12, priority=6, confidence=100,
                      impact_area=_ImpactArea.PORTFOLIO_RISK),
        ]
        append_run_recommendations(
            recs2,
            run_id="2026-04-17_daily",
            run_mode="daily",
            history_path=self.history_path,
        )

        result = evaluate_history(history_path=self.history_path)
        write_evaluation_reports(result, policy_dir=self.policy_dir)

        self.assertEqual(result.total_records, 3)
        self.assertEqual(result.total_runs, 2)

        # emergency_fund should be resolved (not in run 2)
        regime_bucket = result.hit_rate_by_regime.get("normal", {})
        self.assertEqual(regime_bucket.get("total"), 2)
        self.assertEqual(regime_bucket.get("resolved"), 1)

        # Stability: run 2 has 1 rec; drift_QQQ carried over → churn = 0
        per_run = result.recommendation_stability["per_run"]
        self.assertEqual(per_run[1]["carried_over"], 1)
        self.assertAlmostEqual(per_run[1]["churn_rate"], 0.0)

        # Reports exist
        self.assertTrue((self.policy_dir / "recommendation_evaluation.json").exists())
        self.assertTrue((self.policy_dir / "recommendation_evaluation.md").exists())


if __name__ == "__main__":
    unittest.main()
