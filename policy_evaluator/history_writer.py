"""
Append-only recommendation history writer.

Reads the current run's scored FinanceRecommendation objects plus context
metadata and appends one JSONL record per recommendation to
outputs/policy/recommendation_history.jsonl.

Backward compatibility is preserved by routing reads through the shared
history normalizer in `policy_evaluator.infrastructure`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

from policy_evaluator.infrastructure import (
    DEFAULT_HISTORY_PATH,
    append_jsonl_records,
    load_recommendation_history,
    strip_date_suffix,
)

logger = logging.getLogger("policy_evaluator.history_writer")

_HISTORY_PATH = DEFAULT_HISTORY_PATH


def _strip_date_suffix(rec_id: str) -> str:
    """Backward-compatible wrapper for callers/tests using the old helper name."""
    return strip_date_suffix(rec_id)


def _rec_to_record(
    rec: Any,
    *,
    run_id: str,
    run_mode: str,
    timestamp: str,
    regime: str,
    degraded_mode: bool,
    degraded_reason: Optional[str],
    degraded_confidence_penalty: float,
    data_mode: str,
    has_guardrail_violations: bool,
    guardrail_violation_types: List[str],
    growth_mode: str,
    drawdown_pct: float,
    drawdown_regime: str,
) -> dict:
    """Convert one FinanceRecommendation to a flat JSONL record dict."""
    rec_id: str = getattr(rec, "id", "unknown")
    return {
        "run_id": run_id,
        "timestamp": timestamp,
        "run_mode": run_mode,
        "regime": regime,
        "degraded_mode": degraded_mode,
        "degraded_reason": degraded_reason,
        "degraded_confidence_penalty": degraded_confidence_penalty,
        "data_mode": data_mode,
        "has_guardrail_violations": has_guardrail_violations,
        "guardrail_violation_types": guardrail_violation_types,
        "growth_mode": growth_mode,
        "drawdown_pct": round(drawdown_pct, 6),
        "drawdown_regime": drawdown_regime,
        "rec_id": rec_id,
        "rec_base_id": strip_date_suffix(rec_id),
        "impact_area": getattr(getattr(rec, "impact_area", None), "value", str(getattr(rec, "impact_area", ""))),
        "title": getattr(rec, "title", ""),
        "score": getattr(rec, "final_score", 0),
        "raw_score": getattr(getattr(rec, "components", None), "raw_score", getattr(rec, "final_score", 0)),
        "action_level": getattr(getattr(rec, "action_level", None), "value", str(getattr(rec, "action_level", ""))),
        "severity": getattr(getattr(rec, "components", None), "severity", 0),
        "persistence_score": getattr(getattr(rec, "components", None), "persistence", 0),
        "impact_score": getattr(getattr(rec, "components", None), "impact", 0),
        "priority": getattr(getattr(rec, "components", None), "priority", 0),
        "confidence": getattr(getattr(rec, "components", None), "confidence", 100),
        "trigger": getattr(rec, "trigger", ""),
    }


def append_run_recommendations(
    scored_recommendations: List[Any],
    *,
    run_id: str,
    run_mode: str,
    data_health: Optional[dict] = None,
    drawdown_state: Any = None,
    drawdown_regime: str = "normal",
    guardrails: Optional[dict] = None,
    growth_mode: str = "none",
    history_path: Optional[Path] = None,
    dry_run: bool = False,
) -> int:
    """
    Append one JSONL record per recommendation to the history file.

    This remains advisory-only and does not mutate any live investing outputs.
    """
    if not scored_recommendations:
        logger.debug("policy_evaluator: no scored recommendations - nothing to record")
        return 0

    _dh = data_health or {}
    degraded_mode: bool = bool(_dh.get("degraded_mode", False))
    degraded_reason: Optional[str] = _dh.get("degraded_reason")
    degraded_confidence_penalty: float = float(_dh.get("degraded_confidence_penalty", 0.0))
    data_mode: str = str(_dh.get("data_mode", "live"))

    _gr = guardrails or {}
    has_guardrail_violations: bool = not bool(_gr.get("pass", True))
    guardrail_violation_types: List[str] = [
        str(v.get("rule", "unknown")) for v in (_gr.get("violations") or [])
    ]

    drawdown_pct: float = 0.0
    if drawdown_state is not None:
        drawdown_pct = float(
            getattr(drawdown_state, "drawdown_from_12m_high", 0.0) or 0.0
        )

    timestamp = datetime.now().isoformat()
    records: list[dict[str, Any]] = []
    for rec in scored_recommendations:
        try:
            records.append(
                _rec_to_record(
                    rec,
                    run_id=run_id,
                    run_mode=run_mode,
                    timestamp=timestamp,
                    regime=drawdown_regime,
                    degraded_mode=degraded_mode,
                    degraded_reason=degraded_reason,
                    degraded_confidence_penalty=degraded_confidence_penalty,
                    data_mode=data_mode,
                    has_guardrail_violations=has_guardrail_violations,
                    guardrail_violation_types=guardrail_violation_types,
                    growth_mode=growth_mode,
                    drawdown_pct=drawdown_pct,
                    drawdown_regime=drawdown_regime,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("policy_evaluator: failed to serialize rec %r - %s", rec, exc)

    if not records:
        return 0

    if dry_run:
        logger.debug("policy_evaluator: dry_run=True - skipping write (%d records)", len(records))
        return len(records)

    out_path = history_path or _HISTORY_PATH
    try:
        appended = append_jsonl_records(records, out_path)
        logger.info("policy_evaluator: appended %d records to %s", appended, out_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("policy_evaluator: write failed (non-fatal) - %s", exc)
        return 0

    return len(records)


def load_history(history_path: Optional[Path] = None) -> List[dict]:
    """
    Load all records from the JSONL history file.

    Returns normalized rows and degrades gracefully on missing/invalid files.
    """
    return load_recommendation_history(history_path or _HISTORY_PATH)
