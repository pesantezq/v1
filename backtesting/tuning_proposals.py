"""
Pattern efficacy → tuning *proposal* for the POC backtest  (additive | advisory-only | observe-only)

Pattern-Improvement Loop — Step 4 (the loop's edge). Converts OOS, regime-aware,
per-pattern efficacy (from Steps 2–3) into SMALL *proposed* deltas to each signal's
``default_weight`` in config/signal_registry.yaml — written as a review artifact to
the POLICY namespace, **never applied**. Applying approved proposals is the
separate, protected Step 5 (owner-approval gate), which this module deliberately
does not implement.

Guardrails baked in:
  - sample gate: signals below ``min_n`` OOS samples → 'insufficient_evidence', no delta.
  - significance gate: a Wilson/efficacy CI that straddles the 50% coin-flip line
    → 'no_significant_edge', no delta.
  - magnitude bound: every delta is clamped to ±``max_abs_delta`` and the resulting
    weight is clamped to [0, 1].
  - unknown signals are flagged, never silently dropped.

Observe-only: reads the registry read-only and writes only the proposal artifact.
No protected scoring/decision/allocation logic is touched and the registry file is
guaranteed byte-identical before/after.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

_COIN_FLIP = 50.0  # a hit rate of 50% is no better than chance


def _load_registry_weights(registry_path: str) -> dict[str, float]:
    """Read {signal_id: default_weight} from the registry. Read-only; returns {}
    when the file is missing or malformed (degraded, never raises)."""
    p = Path(registry_path)
    if not p.exists():
        return {}
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    weights: dict[str, float] = {}
    for entry in (doc.get("signals") or []):
        if not isinstance(entry, dict):
            continue
        sid = entry.get("signal_id")
        w = entry.get("default_weight")
        if sid is not None and isinstance(w, (int, float)):
            weights[str(sid)] = float(w)
    return weights


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _entry_signal_id(entry: dict) -> str:
    return str(entry.get("signal_id") or entry.get("pattern") or "")


def _entry_n(entry: dict) -> int:
    raw = entry.get("n", entry.get("count"))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _ci_excludes_coinflip(ci: Any) -> bool | None:
    """True if a [low, high] hit-rate CI (in %) is entirely above or below 50%;
    False if it straddles 50%; None when no usable CI is provided."""
    if not isinstance(ci, (list, tuple)) or len(ci) != 2:
        return None
    try:
        low, high = float(ci[0]), float(ci[1])
    except (TypeError, ValueError):
        return None
    return low > _COIN_FLIP or high < _COIN_FLIP


def _build_proposal(entry: dict, current_weight: float | None, *,
                    min_n: int, max_abs_delta: float) -> dict[str, Any]:
    sid = _entry_signal_id(entry)
    n = _entry_n(entry)
    hit_rate = entry.get("hit_rate")
    ci = entry.get("hit_rate_ci95")
    base = {
        "signal_id": sid,
        "current_weight": current_weight,
        "oos_n": n,
        "oos_hit_rate": hit_rate,
        "oos_hit_rate_ci95": list(ci) if isinstance(ci, (list, tuple)) else None,
        "avg_return": entry.get("avg_return"),
        "proposed_delta": 0.0,
        "proposed_weight": current_weight,
    }

    if current_weight is None:
        return {**base, "status": "unknown_signal",
                "rationale": f"{sid!r} is not in the signal registry; no weight to propose."}
    if n < min_n:
        return {**base, "status": "insufficient_evidence",
                "rationale": f"OOS sample {n} < min_n {min_n}; not enough evidence to propose a change."}

    significant = _ci_excludes_coinflip(ci)
    if significant is False:
        return {**base, "status": "no_significant_edge",
                "rationale": f"hit-rate CI {list(ci)} straddles the {_COIN_FLIP:.0f}% coin-flip line; edge not significant."}

    if not isinstance(hit_rate, (int, float)):
        return {**base, "status": "no_significant_edge",
                "rationale": "no usable OOS hit rate; cannot size a delta."}

    raw_delta = (float(hit_rate) - _COIN_FLIP) / 100.0
    if abs(raw_delta) < 1e-9:
        return {**base, "status": "no_significant_edge",
                "rationale": f"OOS hit rate {hit_rate}% is at the coin-flip line; no edge to act on."}

    sign = 1.0 if raw_delta > 0 else -1.0
    delta = round(sign * min(abs(raw_delta), max_abs_delta), 4)
    proposed_weight = round(_clamp(current_weight + delta), 4)
    ci_note = f", CI {list(ci)}" if isinstance(ci, (list, tuple)) else " (no CI provided)"
    direction = "raise" if delta > 0 else "lower"
    return {
        **base,
        "proposed_delta": delta,
        "proposed_weight": proposed_weight,
        "status": "proposed",
        "rationale": (
            f"OOS hit rate {hit_rate}% over n={n}{ci_note} implies a {direction} of "
            f"default_weight; delta bounded to ±{max_abs_delta} → {current_weight} → {proposed_weight}."
        ),
    }


def propose_weight_changes(
    per_pattern_oos: list[dict],
    registry_path: str = "config/signal_registry.yaml",
    *,
    min_n: int = 50,
    max_abs_delta: float = 0.05,
) -> dict[str, Any]:
    """For each signal with sufficient, significant OOS evidence, compute a SMALL
    bounded proposed delta to ``default_weight`` (clamped to ±``max_abs_delta``;
    resulting weight clamped to [0, 1]), with rationale, sample size, OOS hit rate
    + CI, and the noise-control gate. Returns a proposal payload; does NOT edit the
    registry. Empty input → empty proposals, never raises.
    """
    weights = _load_registry_weights(registry_path)
    proposals = [
        _build_proposal(entry, weights.get(_entry_signal_id(entry)),
                        min_n=min_n, max_abs_delta=max_abs_delta)
        for entry in (per_pattern_oos or [])
        if isinstance(entry, dict)
    ]

    proposed = [p for p in proposals if p["status"] == "proposed"]
    current_weights = {
        p["signal_id"]: p["current_weight"]
        for p in proposals if p["current_weight"] is not None
    }
    return {
        "observe_only": True,
        "proposed_only": True,
        "advisory_only": True,
        "generated_by": "backtesting.tuning_proposals",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": "oos_per_pattern_to_bounded_weight_delta",
        "disclaimer": ("Proposes only. These deltas are a review artifact for owner "
                       "approval; the registry is never modified by this layer (Step 5 "
                       "is the protected apply path). No trades implied."),
        "params": {"registry_path": registry_path, "min_n": min_n, "max_abs_delta": max_abs_delta},
        "current_weights": current_weights,
        "proposals": proposals,
        "summary": {
            "evaluated": len(proposals),
            "proposed_count": len(proposed),
            "insufficient_evidence": sum(1 for p in proposals if p["status"] == "insufficient_evidence"),
            "no_significant_edge": sum(1 for p in proposals if p["status"] == "no_significant_edge"),
            "unknown_signal": sum(1 for p in proposals if p["status"] == "unknown_signal"),
        },
    }


def write_proposals(payload: dict[str, Any], base_dir: str = "outputs") -> Path:
    """Write the proposal payload to the POLICY namespace
    (outputs/policy/signal_weight_proposals.json). Review artifact only."""
    from portfolio_automation.data_governance import OutputNamespace, safe_write_json
    return safe_write_json(OutputNamespace.POLICY, "signal_weight_proposals.json",
                           payload, base_dir=base_dir)
