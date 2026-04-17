"""
Shared infrastructure helpers for advisory recommendation history and evaluation.

These helpers are intentionally read-only with respect to live investing logic.
They normalize recommendation history rows, parse timestamps consistently,
expose safe artifact readers, and build reusable forward-window metadata for
downstream evaluation layers.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("policy_evaluator.infrastructure")

DEFAULT_HISTORY_PATH = Path("outputs/policy/recommendation_history.jsonl")
DEFAULT_POLICY_RECOMMENDATION_PATH = Path("outputs/policy/policy_recommendation.json")
DEFAULT_REGIME_PERFORMANCE_PATH = Path("outputs/regime/regime_performance.json")
DEFAULT_POLICY_SIMULATION_PATH = Path("outputs/simulations/policy_simulation.json")

DEFAULT_FORWARD_WINDOWS = (1, 3, 5, 10)
_DATE_SUFFIX_RE = re.compile(r"_\d{4}-\d{2}-\d{2}$")


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    return default


def _safe_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if value is None:
        return []
    return [value]


def _safe_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def strip_date_suffix(rec_id: str) -> str:
    """Return the stable base ID by removing a trailing _YYYY-MM-DD suffix."""
    return _DATE_SUFFIX_RE.sub("", str(rec_id or ""))


def parse_timestamp(value: Any) -> datetime | None:
    """
    Parse supported timestamp forms into a naive UTC-normalized datetime.

    Supported forms:
    - ISO datetime with or without timezone
    - ISO date
    - Existing datetime objects
    """
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = None
        for candidate in (text, text.replace(" ", "T")):
            try:
                parsed = datetime.fromisoformat(candidate)
                break
            except ValueError:
                continue
        if parsed is None:
            try:
                parsed = datetime.combine(date.fromisoformat(text[:10]), time.min)
            except ValueError:
                return None

    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def timestamp_to_iso(value: Any) -> str | None:
    parsed = parse_timestamp(value)
    return parsed.isoformat() if parsed is not None else None


def safe_read_json(path: Path | None) -> dict[str, Any] | None:
    """Safely read a JSON object from disk, returning None on missing/invalid data."""
    if path is None:
        return None
    try:
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            logger.warning("policy_evaluator: expected JSON object in %s", path)
            return None
        return payload
    except Exception as exc:  # noqa: BLE001
        logger.warning("policy_evaluator: failed reading %s - %s", path, exc)
        return None


def read_policy_recommendation(path: Path | None = None) -> dict[str, Any] | None:
    return safe_read_json(path or DEFAULT_POLICY_RECOMMENDATION_PATH)


def read_regime_performance(path: Path | None = None) -> dict[str, Any] | None:
    return safe_read_json(path or DEFAULT_REGIME_PERFORMANCE_PATH)


def read_policy_simulation(path: Path | None = None) -> dict[str, Any] | None:
    return safe_read_json(path or DEFAULT_POLICY_SIMULATION_PATH)


def normalize_history_row(row: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize old/new recommendation history schemas into one safe row shape.

    Old rows can omit most fields. Newer rows may embed recommendation context.
    Downstream code should still use `.get()`, but this function fills the most
    common defaults so evaluators can stay simple and backward-compatible.
    """
    normalized = dict(row or {})
    recommendation = _safe_dict(normalized.get("recommendation"))
    current_context = _safe_dict(normalized.get("current_context"))

    timestamp_value = (
        normalized.get("timestamp")
        or normalized.get("recorded_at")
        or normalized.get("generated_at")
        or current_context.get("timestamp")
    )
    timestamp_iso = timestamp_to_iso(timestamp_value)

    rec_id = str(
        normalized.get("rec_id")
        or normalized.get("recommendation_id")
        or normalized.get("id")
        or "unknown"
    )
    rec_base_id = str(normalized.get("rec_base_id") or strip_date_suffix(rec_id) or rec_id)

    regime_label = str(
        normalized.get("regime_label")
        or normalized.get("regime")
        or current_context.get("regime_label")
        or recommendation.get("regime_label")
        or normalized.get("drawdown_regime")
        or "unknown"
    )

    normalized["timestamp"] = timestamp_iso or str(timestamp_value or "")
    normalized["run_id"] = str(normalized.get("run_id") or "unknown")
    normalized["run_mode"] = str(normalized.get("run_mode") or "unknown")
    normalized["rec_id"] = rec_id
    normalized["rec_base_id"] = rec_base_id
    normalized["regime"] = regime_label
    normalized.setdefault("regime_label", regime_label)
    normalized["degraded_mode"] = _safe_bool(
        normalized.get("degraded_mode", current_context.get("degraded_mode", False))
    )
    normalized.setdefault(
        "degraded_reason",
        normalized.get("degraded_reason") or current_context.get("degraded_reason"),
    )
    normalized["degraded_confidence_penalty"] = float(
        _safe_float(normalized.get("degraded_confidence_penalty"), 0.0) or 0.0
    )
    normalized["data_mode"] = str(
        normalized.get("data_mode")
        or current_context.get("data_mode")
        or "live"
    )
    normalized["has_guardrail_violations"] = _safe_bool(
        normalized.get("has_guardrail_violations"), False
    )
    normalized["guardrail_violation_types"] = [
        str(item) for item in _safe_list(normalized.get("guardrail_violation_types"))
    ]
    normalized["growth_mode"] = str(normalized.get("growth_mode") or "none")
    normalized["drawdown_pct"] = float(_safe_float(normalized.get("drawdown_pct"), 0.0) or 0.0)
    normalized["drawdown_regime"] = str(
        normalized.get("drawdown_regime")
        or regime_label
        or "unknown"
    )
    normalized["impact_area"] = str(normalized.get("impact_area") or "unknown")
    normalized["title"] = str(normalized.get("title") or "")
    normalized["score"] = _safe_int(
        normalized.get("score", recommendation.get("recommendation_score")),
        0,
    )
    normalized["raw_score"] = _safe_int(
        normalized.get("raw_score", normalized.get("score")),
        normalized["score"],
    )
    normalized["action_level"] = str(normalized.get("action_level") or "unknown")
    normalized["severity"] = _safe_int(normalized.get("severity"), 0)
    normalized["persistence_score"] = _safe_int(normalized.get("persistence_score"), 0)
    normalized["impact_score"] = _safe_int(normalized.get("impact_score"), 0)
    normalized["priority"] = _safe_int(normalized.get("priority"), 0)
    normalized["confidence"] = _safe_int(normalized.get("confidence"), 100)
    normalized["trigger"] = str(normalized.get("trigger") or "")

    normalized["recommended_policy"] = (
        normalized.get("recommended_policy")
        or recommendation.get("recommended_policy")
    )
    normalized["recommended_profile"] = (
        normalized.get("recommended_profile")
        or recommendation.get("recommended_profile")
    )
    normalized["recommendation_confidence"] = _safe_float(
        normalized.get("recommendation_confidence", recommendation.get("recommendation_confidence"))
    )
    normalized["recommendation_score"] = _safe_float(
        normalized.get("recommendation_score", recommendation.get("recommendation_score"))
    )
    normalized["recommendation_reasoning"] = _safe_list(
        normalized.get("recommendation_reasoning", recommendation.get("recommendation_reasoning"))
    )
    normalized["recommendation_inputs"] = _safe_dict(
        normalized.get("recommendation_inputs", recommendation.get("recommendation_inputs"))
    )
    normalized["recommendation_data_quality"] = (
        normalized.get("recommendation_data_quality")
        or recommendation.get("recommendation_data_quality")
    )
    normalized["recommendation_source"] = (
        normalized.get("recommendation_source")
        or recommendation.get("recommendation_source")
    )

    return normalized


def load_recommendation_history(history_path: Path | None = None) -> list[dict[str, Any]]:
    """
    Safely load and normalize recommendation history JSONL rows.

    Missing files, empty files, malformed lines, and non-dict rows all degrade
    gracefully by returning only the valid normalized rows.
    """
    path = history_path or DEFAULT_HISTORY_PATH
    if not path.exists():
        return []

    records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, 1):
                text = line.strip()
                if not text:
                    continue
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError as exc:
                    logger.warning("policy_evaluator: bad JSONL at line %d - %s", lineno, exc)
                    continue
                if not isinstance(parsed, dict):
                    logger.warning("policy_evaluator: non-object JSONL row at line %d", lineno)
                    continue
                records.append(normalize_history_row(parsed))
    except Exception as exc:  # noqa: BLE001
        logger.warning("policy_evaluator: failed reading history %s - %s", path, exc)
        return []

    return records


def build_forward_window_boundaries(
    anchor_timestamp: Any,
    *,
    windows: tuple[int, ...] = DEFAULT_FORWARD_WINDOWS,
) -> dict[str, dict[str, Any]]:
    """
    Build reusable 1d/3d/5d/10d-style forward window boundaries.

    The start is inclusive at the recommendation timestamp. The end is an
    exclusive boundary at `anchor + window_days`.
    """
    anchor_dt = parse_timestamp(anchor_timestamp)
    if anchor_dt is None:
        return {}

    out: dict[str, dict[str, Any]] = {}
    for window_days in windows:
        label = f"{int(window_days)}d"
        window_end = anchor_dt + timedelta(days=int(window_days))
        out[label] = {
            "window_label": label,
            "window_days": int(window_days),
            "window_start": anchor_dt.isoformat(),
            "window_end": window_end.isoformat(),
            "window_start_date": anchor_dt.date().isoformat(),
            "window_end_date": window_end.date().isoformat(),
        }
    return out


def align_row_to_forward_window(
    row: dict[str, Any],
    *,
    window_days: int,
) -> dict[str, Any] | None:
    """Attach one normalized history row to a specific forward evaluation window."""
    anchor_dt = parse_timestamp(row.get("timestamp"))
    if anchor_dt is None:
        return None
    window = build_forward_window_boundaries(anchor_dt, windows=(window_days,)).get(f"{int(window_days)}d")
    if window is None:
        return None
    return {
        "run_id": row.get("run_id", "unknown"),
        "timestamp": anchor_dt.isoformat(),
        "rec_id": row.get("rec_id", "unknown"),
        "rec_base_id": row.get("rec_base_id", row.get("rec_id", "unknown")),
        "recommended_policy": row.get("recommended_policy"),
        "recommended_profile": row.get("recommended_profile"),
        "window": window,
    }


def build_forward_return_inputs(row: dict[str, Any], *, window_days: int) -> dict[str, Any] | None:
    """Return shared inputs for a forward-return computation layer."""
    aligned = align_row_to_forward_window(row, window_days=window_days)
    if aligned is None:
        return None
    return {
        "anchor_timestamp": aligned["timestamp"],
        "window_label": aligned["window"]["window_label"],
        "window_days": aligned["window"]["window_days"],
        "window_start": aligned["window"]["window_start"],
        "window_end": aligned["window"]["window_end"],
        "recommended_policy": row.get("recommended_policy"),
        "recommended_profile": row.get("recommended_profile"),
        "regime_label": row.get("regime_label", row.get("regime")),
    }


def build_mfe_mae_inputs(row: dict[str, Any], *, window_days: int) -> dict[str, Any] | None:
    """Return shared inputs for a future MFE/MAE attribution layer."""
    aligned = align_row_to_forward_window(row, window_days=window_days)
    if aligned is None:
        return None
    return {
        "anchor_timestamp": aligned["timestamp"],
        "window_label": aligned["window"]["window_label"],
        "window_days": aligned["window"]["window_days"],
        "window_start": aligned["window"]["window_start"],
        "window_end": aligned["window"]["window_end"],
        "rec_id": row.get("rec_id"),
        "rec_base_id": row.get("rec_base_id"),
        "recommended_policy": row.get("recommended_policy"),
        "recommended_profile": row.get("recommended_profile"),
    }


def append_jsonl_records(records: list[dict[str, Any]], path: Path) -> int:
    """
    Append a block of JSONL rows with a single append write + fsync.

    This preserves prior rows, handles missing parent directories, and reduces
    the chance of torn partial writes compared with line-by-line appends.
    """
    if not records:
        return 0

    payload = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd: int | None = None
    try:
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY)
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
        return len(records)
    finally:
        if fd is not None:
            os.close(fd)
