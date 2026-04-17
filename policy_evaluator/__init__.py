"""
Policy Evaluator — recommendation observability layer.

Measures recommendation quality, calibration, stability, regime-aware
hit rates, and outcome-linked forward portfolio performance over time.
Purely advisory; never alters live scoring logic.

Public API
----------
append_run_recommendations(...)   — write one JSONL record per recommendation
evaluate_history(history_path)    — compute churn / calibration / gap metrics
write_evaluation_reports(result)  — persist JSON + MD reports (churn metrics)
run_outcome_attribution(...)      — link recs to realized portfolio returns
write_outcome_reports(result)     — persist JSON + MD (outcome metrics)
"""

from policy_evaluator.history_writer import append_run_recommendations
from policy_evaluator.evaluator import evaluate_history, EvaluationResult
from policy_evaluator.report_writer import write_evaluation_reports
from policy_evaluator.outcome_attributor import run_outcome_attribution, OutcomeResult
from policy_evaluator.outcome_writer import write_outcome_reports

__all__ = [
    "append_run_recommendations",
    "evaluate_history",
    "EvaluationResult",
    "write_evaluation_reports",
    "run_outcome_attribution",
    "OutcomeResult",
    "write_outcome_reports",
]
