from __future__ import annotations

import math
from typing import Any


def read_value(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def as_finite_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(numeric):
        return default
    return numeric


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def normalize_symbol(value: Any, default: str = "UNKNOWN") -> str:
    symbol = str(value or "").strip().upper()
    return symbol or default


def normalize_score(value: Any, default: float = 0.0) -> float:
    score = as_finite_float(value, default=None)
    if score is None:
        return default
    if -1.0 <= score <= 1.0:
        return score * 100.0
    return score


def normalize_confidence(value: Any, default: float = 0.5) -> float:
    confidence = as_finite_float(value, default=None)
    if confidence is None:
        return default
    if confidence > 1.0 or confidence < 0.0:
        confidence = confidence / 100.0
    return clamp(confidence, 0.0, 1.0)


def normalize_strategy_type(value: Any, default: str = "compounder") -> str:
    strategy_type = str(value or "").strip().lower()
    if strategy_type in {"compounder", "momentum"}:
        return strategy_type
    return default


def factor_breakdown_dict(obj: Any) -> dict[str, Any]:
    raw = read_value(obj, "factor_breakdown", {}) or {}
    if isinstance(raw, dict):
        return raw
    to_dict = getattr(raw, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        return payload if isinstance(payload, dict) else {}
    return {
        "momentum": read_value(raw, "momentum"),
        "relative_strength": read_value(raw, "relative_strength"),
        "volume_confirmation": read_value(raw, "volume_confirmation"),
        "volatility_sanity": read_value(raw, "volatility_sanity"),
    }
