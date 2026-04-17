from __future__ import annotations

from typing import Any


DEFAULT_CONVICTION_CONFIG = {
    "enabled": True,
    "observe_only": True,
    "degraded_mode_cap": "normal",
    "cooldown_band_cap": "defer",
    "historical_performance_weight": 0.10,
    "degraded_penalty_weight": 0.20,
    "confidence_weight": 0.40,
    "effective_score_weight": 0.50,
}

DEFAULT_SIZING_CONFIG = {
    "multipliers": {
        "defer": 0.00,
        "observe": 0.00,
        "starter": 0.25,
        "normal": 0.50,
        "high_conviction": 1.00,
    },
    "target_allocation_bands": {
        "defer": "0%",
        "observe": "0%",
        "starter": "0.25-0.50%",
        "normal": "0.50-1.00%",
        "high_conviction": "1.00-2.00%",
    },
    "minimum_conviction_for_starter": 0.35,
    "minimum_conviction_for_normal": 0.60,
    "minimum_conviction_for_high_conviction": 0.80,
}

_BAND_RANK = {
    "defer": 0,
    "observe": 1,
    "starter": 2,
    "normal": 3,
    "high_conviction": 4,
}


def _cfg(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(base)
    if not isinstance(override, dict):
        return merged
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            nested = dict(merged[key])
            nested.update(value)
            merged[key] = nested
        else:
            merged[key] = value
    return merged


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _reliability_adjustment(reliability: str, historical_performance_score: float | None, weight: float) -> tuple[float, str]:
    if historical_performance_score is None:
        return 0.0, "no history"
    score = float(historical_performance_score)
    if reliability == "strong":
        return round(weight * min(1.0, score), 3), "strong history uplift"
    if reliability == "weak":
        return round(-weight * max(0.5, 1.0 - score), 3), "weak history penalty"
    if reliability == "mixed":
        return round((score - 0.5) * weight, 3), "mixed history adjustment"
    return 0.0, "unproven history"


def _band_for_score(score: float, sizing_cfg: dict[str, Any]) -> str:
    if score >= float(sizing_cfg["minimum_conviction_for_high_conviction"]):
        return "high_conviction"
    if score >= float(sizing_cfg["minimum_conviction_for_normal"]):
        return "normal"
    if score >= float(sizing_cfg["minimum_conviction_for_starter"]):
        return "starter"
    if score >= 0.15:
        return "observe"
    return "defer"


def _apply_band_cap(current_band: str, cap_band: str) -> str:
    if _BAND_RANK[current_band] <= _BAND_RANK[cap_band]:
        return current_band
    return cap_band


def _summary_line(counts: dict[str, int]) -> str:
    return (
        f"Conviction summary: {counts['high_conviction']} high_conviction, "
        f"{counts['normal']} normal, {counts['starter']} starter, "
        f"{counts['observe']} observe, {counts['defer']} defer"
    )


def apply_conviction_layer(
    scan_result: dict[str, Any],
    *,
    conviction_config: dict[str, Any] | None = None,
    sizing_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    conviction_cfg = _cfg(DEFAULT_CONVICTION_CONFIG, conviction_config)
    sizing_cfg = _cfg(DEFAULT_SIZING_CONFIG, sizing_config)
    if not conviction_cfg.get("enabled", True):
        return scan_result

    counts = {band: 0 for band in _BAND_RANK}

    def _annotate_row(row: dict[str, Any], *, count_band: bool) -> None:
        effective_score = float(row.get("effective_score") or 0.0)
        confidence_score = float(row.get("confidence_score") or 0.0)
        degraded_penalty = float(row.get("degraded_confidence_penalty") or 0.0)
        degraded_mode = bool(row.get("data_mode") == "fallback" or scan_result.get("degraded_mode"))
        cooldown_active = bool(row.get("cooldown_active", False))
        historical_score = row.get("historical_performance_score")
        reliability = str(row.get("signal_reliability") or "unproven")
        data_mode = str(row.get("data_mode") or scan_result.get("data_mode") or "live")

        history_adjustment, history_reason = _reliability_adjustment(
            reliability,
            float(historical_score) if historical_score is not None else None,
            float(conviction_cfg["historical_performance_weight"]),
        )
        degraded_adjustment = -float(conviction_cfg["degraded_penalty_weight"]) * degraded_penalty if degraded_mode else 0.0
        cooldown_adjustment = -0.25 if cooldown_active else 0.0
        raw_score = (
            (float(conviction_cfg["effective_score_weight"]) * effective_score)
            + (float(conviction_cfg["confidence_weight"]) * confidence_score)
            + history_adjustment
            + degraded_adjustment
            + cooldown_adjustment
        )
        conviction_score = round(_clamp(raw_score), 3)
        band = _band_for_score(conviction_score, sizing_cfg)
        caps_applied: list[str] = []

        if degraded_mode:
            capped = _apply_band_cap(band, str(conviction_cfg["degraded_mode_cap"]))
            if capped != band:
                caps_applied.append("degraded_mode_cap")
                band = capped

        if cooldown_active:
            capped = _apply_band_cap(band, str(conviction_cfg["cooldown_band_cap"]))
            if capped != band:
                caps_applied.append("cooldown_band_cap")
                band = capped

        if confidence_score < 0.80 and band == "high_conviction":
            band = "normal"
            caps_applied.append("low_confidence_high_conviction_cap")

        if reliability == "weak":
            capped = _apply_band_cap(band, "observe")
            if capped != band:
                caps_applied.append("weak_reliability_cap")
                band = capped

        if count_band:
            counts[band] += 1
        multiplier = float(sizing_cfg["multipliers"][band])
        target_band = str(sizing_cfg["target_allocation_bands"][band])
        sizing_reason_parts = [
            f"effective={effective_score:.2f}",
            f"confidence={confidence_score:.2f}",
            f"history={history_reason}",
            f"data_mode={data_mode}",
        ]
        if degraded_mode:
            sizing_reason_parts.append(f"degraded_penalty={degraded_penalty:.2f}")
        if cooldown_active:
            sizing_reason_parts.append("cooldown active")
        if caps_applied:
            sizing_reason_parts.append("caps=" + ",".join(caps_applied))

        row["conviction_score"] = conviction_score
        row["conviction_band"] = band
        row["sizing_recommendation"] = band
        row["sizing_reason"] = "; ".join(sizing_reason_parts)
        row["target_allocation_band"] = target_band
        row["sizing_multiplier"] = multiplier
        row["capital_sizing_note"] = (
            f"Informational sizing only ({multiplier:.2f}x baseline). "
            f"Observe-only={'yes' if conviction_cfg.get('observe_only', True) else 'no'}."
        )
        row["conviction_inputs"] = {
            "signal_score": float(row.get("signal_score") or 0.0),
            "confidence_score": confidence_score,
            "effective_score": effective_score,
            "historical_performance_score": historical_score,
            "signal_reliability": reliability,
            "degraded_mode": degraded_mode,
            "degraded_confidence_penalty": degraded_penalty,
            "cooldown_active": cooldown_active,
            "data_mode": data_mode,
        }
        row["conviction_caps_applied"] = caps_applied

    for row in scan_result.get("results", []) or []:
        _annotate_row(row, count_band=True)
    for row in scan_result.get("alerts", []) or []:
        _annotate_row(row, count_band=False)

    scan_result.setdefault("scan_summary", {})
    scan_result["scan_summary"]["conviction_band_counts"] = counts
    scan_result["scan_summary"]["conviction_summary_line"] = _summary_line(counts)
    scan_result["conviction"] = {
        "enabled": True,
        "observe_only": bool(conviction_cfg.get("observe_only", True)),
        "band_counts": counts,
        "summary_line": scan_result["scan_summary"]["conviction_summary_line"],
        "config": {
            "degraded_mode_cap": conviction_cfg["degraded_mode_cap"],
            "cooldown_band_cap": conviction_cfg["cooldown_band_cap"],
            "historical_performance_weight": conviction_cfg["historical_performance_weight"],
            "degraded_penalty_weight": conviction_cfg["degraded_penalty_weight"],
            "confidence_weight": conviction_cfg["confidence_weight"],
            "effective_score_weight": conviction_cfg["effective_score_weight"],
        },
    }
    return scan_result
