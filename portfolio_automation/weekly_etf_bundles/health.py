"""
Health + governance validation for the weekly ETF bundle subsystem.

Follows the strategy_lab_health pattern: returns {status: GREEN|AMBER|RED,
reasons, signals} plus the posture flags. RED = a broken invariant or governance
breach; AMBER = degraded/stale/inert; GREEN = clean. Observe-only.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from portfolio_automation import weekly_etf_bundles as _pkg
from portfolio_automation.data_governance import OutputNamespace, get_output_path

logger = logging.getLogger("stockbot.weekly_etf_bundles.health")

GREEN, AMBER, RED = "GREEN", "AMBER", "RED"


def _bounded_scores_ok(analysis_payload: dict[str, Any]) -> bool:
    for r in analysis_payload.get("ranking_global", []):
        s = r.get("watch_score")
        if s is not None and not (0 <= s <= 100):
            return False
    for b in analysis_payload.get("bundles", []):
        for pct_key in ("pct_above_sma50", "pct_above_sma200", "pct_positive_momentum_4w"):
            v = b.get(pct_key)
            if v is not None and not (0.0 <= v <= 1.0):
                return False
    return True


def build_health(
    *,
    analysis_payload: dict[str, Any] | None,
    config_valid: bool,
    enabled_bundle_ids: list[str] | None = None,
    predictions: list[dict[str, Any]] | None = None,
    scorecard: dict[str, Any] | None = None,
    calibration: dict[str, Any] | None = None,
    strat_lab_comparison: dict[str, Any] | None = None,
    email_result: dict[str, Any] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Pure health assessment from in-memory artifacts."""
    reasons: list[str] = []
    signals: dict[str, Any] = {}
    payload = analysis_payload or {}

    # ── posture invariants (hardcoded) ──
    posture = dict(_pkg.POSTURE)
    if payload.get("feeds_decision_engine") not in (None, False):
        reasons.append("RED:feeds_decision_engine_true")  # authority breach
    if posture["feeds_decision_engine"] is not False or posture["observe_only"] is not True:
        reasons.append("RED:posture_invariant_broken")

    # ── config ──
    if not config_valid:
        reasons.append("RED:config_invalid")

    # ── bounded scores / percentages ──
    if not _bounded_scores_ok(payload):
        reasons.append("RED:score_or_pct_out_of_bounds")

    # ── enabled/disabled bundle coverage ──
    # Only a governance concern when analysis actually ran (status ok). A pure
    # no-data run legitimately has no bundles → that is AMBER (degraded), handled
    # below, not a RED breach.
    if enabled_bundle_ids is not None and payload.get("status") == "ok":
        present = {b.get("bundle_id") for b in payload.get("bundles", [])}
        missing = sorted(set(enabled_bundle_ids) - present)
        extra = sorted(present - set(enabled_bundle_ids))
        signals["bundles_present"] = sorted(present)
        if missing:
            reasons.append(f"RED:enabled_bundle_missing:{missing}")
        if extra:
            reasons.append(f"RED:disabled_bundle_present:{extra}")

    # ── prediction id uniqueness ──
    if predictions:
        ids = [p.get("prediction_id") for p in predictions]
        signals["prediction_count"] = len(ids)
        if len(set(ids)) != len(ids):
            reasons.append("RED:duplicate_prediction_ids")

    # ── no trade actions / no approvals leaked into artifacts ──
    _ACTION_TOKENS = {"buy", "sell", "scale", "starter", "trim", "rebalance", "allocation_usd"}
    blob = json.dumps(payload, default=str).lower()
    if any(f'"{tok}"' in blob for tok in ("decision_plan", "approval_record")):
        reasons.append("RED:forbidden_artifact_field")
    # action tokens as VALUES (expected_direction uses 'outperform'/'neutral' only)
    for r in payload.get("ranking_global", []):
        if str(r.get("expected_direction", "")).lower() in _ACTION_TOKENS:
            reasons.append("RED:action_direction_emitted")
            break

    # ── strat lab: no auto-promotion ──
    if strat_lab_comparison:
        for cand in strat_lab_comparison.get("pending_promotion_candidates", []):
            if cand.get("is_human_approved") is not False or cand.get("production_mutation") is not False \
               or cand.get("feeds_decision_engine") is not False or cand.get("target_lane") != "simulation":
                reasons.append("RED:promotion_authority_breach")
                break
        signals["pending_promotion_candidates"] = len(
            strat_lab_comparison.get("pending_promotion_candidates", []))

    # ── email content matches its recorded hash ──
    if email_result:
        signals["email_enabled"] = email_result.get("enabled")
        signals["email_sent"] = email_result.get("sent")
        signals["duplicate_suppressed"] = email_result.get("duplicate_suppressed", False)
        recv_hash = email_result.get("content_hash")
        sr = email_result.get("send_result") or {}
        if sr.get("content_hash") and recv_hash and sr["content_hash"] != recv_hash:
            reasons.append("RED:email_content_hash_mismatch")

    # ── AMBER conditions (degraded / inert) ──
    coverage = payload.get("coverage")
    signals["coverage"] = coverage
    signals["bundle_count"] = payload.get("bundle_count")
    signals["etf_count"] = payload.get("etf_count")
    signals["market_data_date"] = payload.get("market_data_date")
    if payload.get("status") and payload.get("status") != "ok":
        reasons.append(f"AMBER:analysis_status_{payload.get('status')}")
    if coverage is not None and coverage < 0.8:
        reasons.append(f"AMBER:low_coverage_{coverage}")
    if payload.get("stale_symbols"):
        reasons.append(f"AMBER:stale_symbols_{len(payload['stale_symbols'])}")
    if payload.get("failed_symbols"):
        reasons.append(f"AMBER:failed_symbols_{len(payload['failed_symbols'])}")
    if scorecard is not None:
        signals["sample_status"] = scorecard.get("sample_status")
        signals["matured_prediction_count"] = scorecard.get("matured_prediction_count")
        if scorecard.get("sample_status") != "sufficient":
            reasons.append(f"AMBER:sample_{scorecard.get('sample_status')}")
    if calibration is not None:
        signals["calibration_status"] = calibration.get("calibration_status")
        if calibration.get("higher_buckets_underperform_warning"):
            reasons.append("AMBER:calibration_higher_buckets_underperform")

    red = [r for r in reasons if r.startswith("RED")]
    amber = [r for r in reasons if r.startswith("AMBER")]
    status = RED if red else (AMBER if amber else GREEN)

    return {
        "status": status,
        "generated_at": generated_at,
        "observe_only": True,
        "simulation_active": posture["simulation_active"],
        "production_gated": posture["production_gated"],
        "human_approval_required_for_production": posture["human_approval_required_for_production"],
        "feeds_decision_engine": False,
        "config_valid": bool(config_valid),
        "schema_version": _pkg.SCHEMA_VERSION,
        "source": _pkg.SOURCE_LABEL,
        "market_data_date": payload.get("market_data_date"),
        "bundle_count": payload.get("bundle_count"),
        "etf_count": payload.get("etf_count"),
        "coverage": coverage,
        "stale_symbols": payload.get("stale_symbols", []),
        "failed_symbols": payload.get("failed_symbols", []),
        "reasons": reasons,
        "signals": signals,
    }


def load_health(root: str | Path) -> dict[str, Any]:
    """Read the persisted health.json (for analysis skills)."""
    path = get_output_path(OutputNamespace.WEEKLY_ETF_BUNDLES, "health.json",
                           base_dir=Path(root).resolve() / "outputs")
    if not path.exists():
        return {"status": AMBER, "reasons": ["AMBER:health_absent"], "observe_only": True}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": AMBER, "reasons": [f"AMBER:health_unreadable:{exc}"], "observe_only": True}
