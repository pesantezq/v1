from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("watchlist_scanner.approved_config_loader")

_REQUIRED_WEIGHT_KEYS = frozenset({
    "augmented_signal_score",
    "confidence_score",
    "theme_alignment_score",
    "portfolio_fit_score",
})
_WEIGHT_SUM_TOLERANCE = 0.02


def load_approved_weights(config_path: Path | str) -> dict[str, Any] | None:
    """
    Load and validate approved ranking weights from approved_ranking_config.json.

    Returns None when the file is absent (silent fallback — caller uses defaults).
    Returns {"_valid": False, "reason": ..., ...} when the file exists but fails
    validation (caller logs and falls back to defaults).
    Returns {"_valid": True, "weights": {...}, ...} when the config is usable.

    Validation rules
    ----------------
    - applied_to_live must be False or absent (True is rejected for safety)
    - proposed_weights must be a non-empty dict
    - All four required keys must be present
    - Each weight value must be numeric (float-coercible)
    - Required-key weights must sum to 1.0 ± _WEIGHT_SUM_TOLERANCE (0.02)
    """
    config_path = Path(config_path)
    if not config_path.exists():
        return None

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("approved_config_loader: could not read %s — %s", config_path, exc)
        return {
            "_valid": False,
            "reason": f"read error: {exc}",
            "recommended_candidate": None,
            "approved_at": None,
        }

    if not isinstance(data, dict):
        return {
            "_valid": False,
            "reason": "config is not a JSON object",
            "recommended_candidate": None,
            "approved_at": None,
        }

    if data.get("applied_to_live") is True:
        return {
            "_valid": False,
            "reason": "applied_to_live is True — weights rejected for safety",
            "recommended_candidate": data.get("recommended_candidate"),
            "approved_at": data.get("approved_at"),
        }

    weights_raw = data.get("proposed_weights")
    if not weights_raw or not isinstance(weights_raw, dict):
        return {
            "_valid": False,
            "reason": "missing or empty proposed_weights",
            "recommended_candidate": data.get("recommended_candidate"),
            "approved_at": data.get("approved_at"),
        }

    missing_keys = sorted(_REQUIRED_WEIGHT_KEYS - weights_raw.keys())
    if missing_keys:
        return {
            "_valid": False,
            "reason": f"missing required weight keys: {missing_keys}",
            "recommended_candidate": data.get("recommended_candidate"),
            "approved_at": data.get("approved_at"),
        }

    try:
        weights = {k: float(v) for k, v in weights_raw.items()}
    except (TypeError, ValueError) as exc:
        return {
            "_valid": False,
            "reason": f"non-numeric weight value: {exc}",
            "recommended_candidate": data.get("recommended_candidate"),
            "approved_at": data.get("approved_at"),
        }

    weight_sum = sum(weights[k] for k in _REQUIRED_WEIGHT_KEYS)
    if abs(weight_sum - 1.0) > _WEIGHT_SUM_TOLERANCE:
        return {
            "_valid": False,
            "reason": f"weights sum to {weight_sum:.4f}, expected ~1.0 (±{_WEIGHT_SUM_TOLERANCE})",
            "recommended_candidate": data.get("recommended_candidate"),
            "approved_at": data.get("approved_at"),
        }

    return {
        "_valid": True,
        "weights": weights,
        "recommended_candidate": data.get("recommended_candidate"),
        "approved_at": data.get("approved_at"),
    }
