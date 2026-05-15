"""
Loader and validator for the approved_allocation_policy artifact.

Reads the artifact produced by allocation_policy_activation.py and verifies
that it is safe to use for advisory sizing enrichment. A valid result means
rank-aware sizing metadata may be attached to AllocationSuggestion outputs —
it does NOT mean live allocation changes, portfolio mutations, or alert-gating
changes of any kind.

Returns:
  None                               — file absent; caller silently uses default sizing
  {"_valid": False, "reason": ...}   — file exists but fails validation; caller logs and
                                       uses default sizing
  {"_valid": True, ...}              — policy is usable for advisory sizing enrichment

Validation rules
----------------
- activation_status must be "approved_not_live"
- applied_to_live must be False (True is rejected for safety)
- sample_size must be present and numeric
- rank_aware dict must be present and contain capital_efficiency
- delta.efficiency_delta must be > 0
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("watchlist_scanner.approved_allocation_policy_loader")

_ARTIFACT_REL = ("outputs", "performance", "approved_allocation_policy.json")


def load_approved_allocation_policy(policy_path: Path | str) -> dict[str, Any] | None:
    """
    Load and validate approved_allocation_policy.json.

    Returns None when the file is absent (silent fallback).
    Returns {"_valid": False, "reason": ..., ...} when validation fails.
    Returns {"_valid": True, ...} when the policy is usable.
    """
    policy_path = Path(policy_path)
    if not policy_path.exists():
        return None

    try:
        data = json.loads(policy_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "approved_allocation_policy_loader: could not read %s — %s", policy_path, exc
        )
        return {
            "_valid": False,
            "reason": f"read error: {exc}",
            "activation_status": None,
            "approved_at": None,
        }

    if not isinstance(data, dict):
        return {
            "_valid": False,
            "reason": "policy is not a JSON object",
            "activation_status": None,
            "approved_at": None,
        }

    activation_status = data.get("activation_status")
    if activation_status != "approved_not_live":
        return {
            "_valid": False,
            "reason": (
                f"activation_status is {activation_status!r}, expected 'approved_not_live'"
            ),
            "activation_status": activation_status,
            "approved_at": data.get("approved_at"),
        }

    if data.get("applied_to_live") is True:
        return {
            "_valid": False,
            "reason": "applied_to_live is True — policy rejected for safety",
            "activation_status": activation_status,
            "approved_at": data.get("approved_at"),
        }

    sample_size_raw = data.get("sample_size")
    if sample_size_raw is None:
        return {
            "_valid": False,
            "reason": "sample_size is missing",
            "activation_status": activation_status,
            "approved_at": data.get("approved_at"),
        }
    try:
        sample_size = int(sample_size_raw)
    except (TypeError, ValueError) as exc:
        return {
            "_valid": False,
            "reason": f"sample_size is not numeric: {exc}",
            "activation_status": activation_status,
            "approved_at": data.get("approved_at"),
        }

    rank_aware = data.get("rank_aware")
    if not isinstance(rank_aware, dict) or "capital_efficiency" not in rank_aware:
        return {
            "_valid": False,
            "reason": "rank_aware metrics are missing or incomplete (need capital_efficiency)",
            "activation_status": activation_status,
            "approved_at": data.get("approved_at"),
        }

    delta = data.get("delta") or {}
    efficiency_delta_raw = delta.get("efficiency_delta")
    if efficiency_delta_raw is None:
        return {
            "_valid": False,
            "reason": "delta.efficiency_delta is missing",
            "activation_status": activation_status,
            "approved_at": data.get("approved_at"),
        }
    try:
        efficiency_delta = float(efficiency_delta_raw)
    except (TypeError, ValueError) as exc:
        return {
            "_valid": False,
            "reason": f"delta.efficiency_delta is not numeric: {exc}",
            "activation_status": activation_status,
            "approved_at": data.get("approved_at"),
        }
    if efficiency_delta <= 0.0:
        return {
            "_valid": False,
            "reason": f"delta.efficiency_delta {efficiency_delta:+.4f} is not positive",
            "activation_status": activation_status,
            "approved_at": data.get("approved_at"),
        }

    return {
        "_valid": True,
        "activation_status": activation_status,
        "approved_at": data.get("approved_at"),
        "sample_size": sample_size,
        "primary_window_days": data.get("primary_window_days"),
        "baseline": dict(data.get("baseline") or {}),
        "rank_aware": dict(rank_aware),
        "delta": dict(delta),
        "approval_note": data.get("approval_note"),
    }


def default_policy_path(root: Path | str | None = None) -> Path:
    """Return the default path to approved_allocation_policy.json."""
    root_path = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    return root_path.joinpath(*_ARTIFACT_REL)
