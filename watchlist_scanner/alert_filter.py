from __future__ import annotations

from datetime import datetime
from typing import Any, TypedDict


DEFAULT_CONFIDENCE_TIERS = {
    "high": 0.80,
    "medium": 0.65,
    "low": 0.50,
}

DEFAULT_TIER_COOLDOWN_HOURS = {
    "high": 6,
    "medium": 24,
    "low": 72,
}


class AlertFilterDecision(TypedDict, total=False):
    allowed: bool
    reason: str
    reason_code: str
    tier: str | None
    cooldown_applied_hours: int | None
    evidence_count: int
    confidence_score: float
    signal_score: float
    override_reason: str


def _signals_cfg(signals_config: dict[str, Any] | None) -> dict[str, Any]:
    return signals_config or {}


def classify_confidence_tier(
    confidence_score: float,
    signals_config: dict[str, Any] | None = None,
) -> str | None:
    cfg = _signals_cfg(signals_config)
    tiers = dict(DEFAULT_CONFIDENCE_TIERS)
    tiers.update(cfg.get("confidence_tiers") or {})

    if confidence_score >= float(tiers["high"]):
        return "high"
    if confidence_score >= float(tiers["medium"]):
        return "medium"
    if confidence_score >= float(tiers["low"]):
        return "low"
    return None


def cooldown_hours_for_tier(
    tier: str | None,
    signals_config: dict[str, Any] | None = None,
    *,
    fallback_hours: int = 72,
) -> int:
    if not tier:
        return fallback_hours
    cfg = _signals_cfg(signals_config)
    cooldowns = dict(DEFAULT_TIER_COOLDOWN_HOURS)
    cooldowns.update(cfg.get("cooldown") or {})
    return int(cooldowns.get(tier, fallback_hours))


def evidence_count(signal: dict[str, Any]) -> int:
    if signal.get("evidence_count") is not None:
        return int(signal.get("evidence_count") or 0)
    if signal.get("evidence_breadth") is not None:
        return int(signal.get("evidence_breadth") or 0)
    categories = signal.get("evidence_categories") or []
    return len(categories)


def should_emit_alert(
    signal: dict[str, Any],
    signals_config: dict[str, Any] | None = None,
) -> AlertFilterDecision:
    cfg = _signals_cfg(signals_config)
    signal_score = float(signal.get("signal_score") or 0.0)
    confidence_score = float(signal.get("confidence_score") or 0.0)
    min_signal_score = float(cfg.get("min_signal_score", 0.50))
    min_confidence_score = float(cfg.get("min_confidence_score", 0.50))
    min_evidence_count = int(cfg.get("min_evidence_count", 2))
    routed_priority = signal.get("routed_alert_priority", signal.get("alert_priority"))
    tier = classify_confidence_tier(confidence_score, cfg)
    count = evidence_count(signal)

    if routed_priority is None:
        reason_code = "below_min_signal_score" if signal_score < min_signal_score else "routed_suppressed"
        return {
            "allowed": False,
            "reason": "suppressed before emission routing",
            "reason_code": reason_code,
            "tier": tier,
            "cooldown_applied_hours": None,
            "evidence_count": count,
            "confidence_score": confidence_score,
            "signal_score": signal_score,
            "override_reason": "",
        }

    if signal_score < min_signal_score and "signal_score" in (signal.get("alert_basis") or []):
        return {
            "allowed": False,
            "reason": "signal score is below the configured emission threshold",
            "reason_code": "below_min_signal_score",
            "tier": tier,
            "cooldown_applied_hours": None,
            "evidence_count": count,
            "confidence_score": confidence_score,
            "signal_score": signal_score,
            "override_reason": "",
        }

    if confidence_score < min_confidence_score or tier is None:
        return {
            "allowed": False,
            "reason": "confidence score is below the configured minimum",
            "reason_code": "below_min_confidence",
            "tier": tier,
            "cooldown_applied_hours": None,
            "evidence_count": count,
            "confidence_score": confidence_score,
            "signal_score": signal_score,
            "override_reason": "",
        }

    cooldown_hours = cooldown_hours_for_tier(tier, cfg)

    if tier == "high":
        return {
            "allowed": True,
            "reason": "high-confidence alert allowed immediately",
            "reason_code": "allowed_high",
            "tier": tier,
            "cooldown_applied_hours": cooldown_hours,
            "evidence_count": count,
            "confidence_score": confidence_score,
            "signal_score": signal_score,
            "override_reason": "",
        }

    if tier == "medium":
        if count >= min_evidence_count:
            return {
                "allowed": True,
                "reason": "medium-confidence alert allowed because evidence threshold was met",
                "reason_code": "allowed_medium",
                "tier": tier,
                "cooldown_applied_hours": cooldown_hours,
                "evidence_count": count,
                "confidence_score": confidence_score,
                "signal_score": signal_score,
                "override_reason": "",
            }
        return {
            "allowed": False,
            "reason": "medium-confidence alert filtered because evidence threshold was not met",
            "reason_code": "insufficient_evidence",
            "tier": tier,
            "cooldown_applied_hours": cooldown_hours,
            "evidence_count": count,
            "confidence_score": confidence_score,
            "signal_score": signal_score,
            "override_reason": "",
        }

    return {
        "allowed": False,
        "reason": "low-confidence alert filtered before emission",
        "reason_code": "low_confidence_suppressed",
        "tier": tier,
        "cooldown_applied_hours": cooldown_hours,
        "evidence_count": count,
        "confidence_score": confidence_score,
        "signal_score": signal_score,
        "override_reason": "",
    }


def _tier_rank(tier: str | None) -> int:
    return {"high": 3, "medium": 2, "low": 1}.get(str(tier or ""), 0)


def cooldown_decision(
    signal: dict[str, Any],
    state: dict[str, Any] | None,
    signals_config: dict[str, Any] | None = None,
) -> AlertFilterDecision:
    """
    Decide whether a cooldown window is still active for this alert.

    Bypass rules (evaluated in order inside the active-cooldown window):
    1. Tier or priority upgrade (existing behaviour).
    2. ``cooldown_allow_high_confidence_bypass`` — pass when confidence >=
       high-tier threshold AND signal >= ``strong_signal_threshold``.
    3. ``cooldown_min_effective_score_delta_for_reset`` — pass when
       effective_score improved by at least this delta vs the stored state.
    4. Material state-hash change (content/quality shift) — gated by
       ``cooldown_allow_direction_change_bypass`` (True by default).

    Cooldown duration follows tier order, but ``cooldown_hours_strong_signal``
    and ``cooldown_hours_weak_signal`` override the tier-based value when the
    current effective_score crosses ``strong_signal_threshold`` /
    ``weak_signal_threshold``.
    """
    cfg = _signals_cfg(signals_config)
    tier = str(signal.get("alert_tier") or "") or None

    # ── Resolve effective_score for signal-strength-based cooldown hours ──────
    _sig  = float(signal.get("signal_score") or 0.0)
    _conf = float(signal.get("confidence_score") or 0.0)
    effective_score = float(signal.get("effective_score") or (_sig * _conf))

    strong_threshold = float(cfg.get("strong_signal_threshold", 0.75))
    weak_threshold   = float(cfg.get("weak_signal_threshold", 0.40))
    hours_strong     = cfg.get("cooldown_hours_strong_signal")
    hours_weak       = cfg.get("cooldown_hours_weak_signal")

    cooldown_hours = cooldown_hours_for_tier(tier, cfg)
    if hours_strong is not None and effective_score >= strong_threshold:
        cooldown_hours = int(hours_strong)
    elif hours_weak is not None and effective_score < weak_threshold:
        cooldown_hours = int(hours_weak)

    if not state:
        return {
            "allowed": True,
            "reason": "no prior alert state",
            "reason_code": f"allowed_{tier}" if tier else "allowed_initial",
            "tier": tier,
            "cooldown_applied_hours": cooldown_hours,
            "evidence_count": evidence_count(signal),
            "confidence_score": float(signal.get("confidence_score") or 0.0),
            "signal_score": float(signal.get("signal_score") or 0.0),
            "override_reason": "",
        }

    last_emailed = state.get("last_emailed")
    if not last_emailed:
        return {
            "allowed": True,
            "reason": "prior state exists but has not been notified yet",
            "reason_code": f"allowed_{tier}" if tier else "allowed_initial",
            "tier": tier,
            "cooldown_applied_hours": cooldown_hours,
            "evidence_count": evidence_count(signal),
            "confidence_score": float(signal.get("confidence_score") or 0.0),
            "signal_score": float(signal.get("signal_score") or 0.0),
            "override_reason": "",
        }

    state_hash = str(signal.get("alert_state_hash") or "")
    previous_state_hash = str(state.get("state_hash") or "")
    allow_direction_bypass = bool(cfg.get("cooldown_allow_direction_change_bypass", True))
    if allow_direction_bypass and state_hash and previous_state_hash and state_hash != previous_state_hash:
        return {
            "allowed": True,
            "reason": "alert state changed materially — direction or signal quality shifted",
            "reason_code": f"allowed_{tier}" if tier else "allowed_changed",
            "tier": tier,
            "cooldown_applied_hours": cooldown_hours,
            "evidence_count": evidence_count(signal),
            "confidence_score": float(signal.get("confidence_score") or 0.0),
            "signal_score": float(signal.get("signal_score") or 0.0),
            "override_reason": "direction_change_bypass",
        }

    try:
        last_dt = datetime.fromisoformat(str(last_emailed))
    except (TypeError, ValueError):
        return {
            "allowed": True,
            "reason": "prior alert timestamp was unreadable",
            "reason_code": f"allowed_{tier}" if tier else "allowed_initial",
            "tier": tier,
            "cooldown_applied_hours": cooldown_hours,
            "evidence_count": evidence_count(signal),
            "confidence_score": float(signal.get("confidence_score") or 0.0),
            "signal_score": float(signal.get("signal_score") or 0.0),
            "override_reason": "",
        }

    age_hours = (datetime.now() - last_dt).total_seconds() / 3600.0
    previous_tier = str(state.get("alert_tier") or "") or None
    previous_priority = str(state.get("severity") or "")
    current_priority = str(signal.get("alert_priority") or "")
    tier_upgrade = _tier_rank(tier) > _tier_rank(previous_tier)
    priority_upgrade = bool(previous_priority) and (
        {"high": 3, "normal": 2, "watch": 1}.get(current_priority, 0)
        > {"high": 3, "normal": 2, "watch": 1}.get(previous_priority, 0)
    )

    if age_hours < cooldown_hours:
        if tier_upgrade or priority_upgrade:
            return {
                "allowed": True,
                "reason": "allowed despite cooldown because the alert upgraded in importance",
                "reason_code": "allowed_tier_upgrade",
                "tier": tier,
                "cooldown_applied_hours": cooldown_hours,
                "evidence_count": evidence_count(signal),
                "confidence_score": float(signal.get("confidence_score") or 0.0),
                "signal_score": float(signal.get("signal_score") or 0.0),
                "override_reason": (
                    f"tier {previous_tier or 'none'} -> {tier or 'none'}"
                    if tier_upgrade
                    else f"priority {previous_priority or 'none'} -> {current_priority or 'none'}"
                ),
            }

        # ── High-confidence + strong-signal bypass ────────────────────────────
        if bool(cfg.get("cooldown_allow_high_confidence_bypass", False)):
            _tiers_cfg = dict(DEFAULT_CONFIDENCE_TIERS)
            _tiers_cfg.update(cfg.get("confidence_tiers") or {})
            if (
                float(signal.get("confidence_score") or 0.0) >= float(_tiers_cfg["high"])
                and float(signal.get("signal_score") or 0.0) >= strong_threshold
            ):
                return {
                    "allowed": True,
                    "reason": (
                        "high-confidence strong signal bypasses cooldown "
                        f"(conf>={_tiers_cfg['high']}, signal>={strong_threshold})"
                    ),
                    "reason_code": "allowed_high_conf_bypass",
                    "tier": tier,
                    "cooldown_applied_hours": cooldown_hours,
                    "evidence_count": evidence_count(signal),
                    "confidence_score": float(signal.get("confidence_score") or 0.0),
                    "signal_score": float(signal.get("signal_score") or 0.0),
                    "override_reason": "high_confidence_bypass",
                }

        # ── Effective-score delta bypass ──────────────────────────────────────
        min_eff_delta = float(cfg.get("cooldown_min_effective_score_delta_for_reset") or 0.0)
        if min_eff_delta > 0:
            prior_eff = (
                float(state.get("last_signal_score") or 0.0)
                * float(state.get("last_confidence_score") or 0.0)
            )
            delta = effective_score - prior_eff
            if delta >= min_eff_delta:
                return {
                    "allowed": True,
                    "reason": (
                        f"effective_score improved by {delta:.3f} "
                        f"(>= reset threshold {min_eff_delta})"
                    ),
                    "reason_code": "allowed_effective_score_jump",
                    "tier": tier,
                    "cooldown_applied_hours": cooldown_hours,
                    "evidence_count": evidence_count(signal),
                    "confidence_score": float(signal.get("confidence_score") or 0.0),
                    "signal_score": float(signal.get("signal_score") or 0.0),
                    "override_reason": f"effective_score_delta={delta:.3f}",
                }

        return {
            "allowed": False,
            "reason": f"unchanged {tier or 'alert'} tier is still inside its cooldown window",
            "reason_code": f"cooldown_active_{tier or 'unknown'}",
            "tier": tier,
            "cooldown_applied_hours": cooldown_hours,
            "evidence_count": evidence_count(signal),
            "confidence_score": float(signal.get("confidence_score") or 0.0),
            "signal_score": float(signal.get("signal_score") or 0.0),
            "override_reason": "",
        }

    return {
        "allowed": True,
        "reason": "cooldown window has expired",
        "reason_code": f"allowed_{tier}" if tier else "allowed_rearm",
        "tier": tier,
        "cooldown_applied_hours": cooldown_hours,
        "evidence_count": evidence_count(signal),
        "confidence_score": float(signal.get("confidence_score") or 0.0),
        "signal_score": float(signal.get("signal_score") or 0.0),
        "override_reason": "",
    }
