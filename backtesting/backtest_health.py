"""
Analysis-health pairing for the Pattern-Improvement Loop  (additive | advisory-only | observe-only)

Pattern-Improvement Loop — Step 6. Satisfies the repo rule (CLAUDE.md, "Analysis +
Health Coverage Requirement") that every shipped feature is paired with a health
check. The backtest loop runs at yearly/lifetime cadence, so this check is wired
into `.claude/commands/yearly-tool-analysis.md` (Quant + Developer lens).

Reads the loop's two output artifacts — `outputs/backtest/poc_simulation_results.json`
(Steps 0–3) and `outputs/policy/signal_weight_proposals.json` (Step 4) — and flags:
  - results_missing        (RED)   — no backtest artifact at all
  - looks_fresh_but_empty  (RED)   — artifact present/recent but evaluated == 0
                                     (the content_liveness failure mode)
  - degenerate_regimes     (RED)   — every per-regime bucket is 'unknown'
  - stale                  (AMBER) — generated_at older than max_age_days
  - low_sample             (AMBER) — evaluated below min_evaluated
  - calibration_slope_flipped (AMBER) — calibration slope went negative
  - no_proposals / proposals_missing (AMBER) — Step 4 produced nothing to review

Observe-only: reads artifacts and returns a status dict; writes nothing and touches
no protected scoring/decision logic. Any read failure degrades to a flag, never raises.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_OBSERVE_ONLY = True


def _load_json(path: Path) -> Any | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None


def _parse_dt(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def assess_backtest_health(
    *,
    backtest_dir: str = "outputs/backtest",
    proposals_path: str = "outputs/policy/signal_weight_proposals.json",
    calibration_proposal_path: str = "outputs/policy/calibration_correction_proposal.json",
    tagging_proposal_path: str = "outputs/policy/signal_tagging_proposal.json",
    auto_apply_audit_path: str = "outputs/policy/auto_apply_audit.json",
    reconstruction_audit_path: str = "outputs/backtest/reconstruction_audit.json",
    now: datetime | None = None,
    max_age_days: int = 400,
    min_evaluated: int = 30,
    max_untagged_pct: float = 0.50,
    run_score_gate: bool = False,
    registry_path: str = "config/signal_registry.yaml",
) -> dict[str, Any]:
    """Assess the Pattern-Loop backtest artifacts and return
    ``{observe_only, status, flags, details}`` where status is GREEN | AMBER | RED.
    RED = a critical correctness/liveness failure; AMBER = a quality warning;
    GREEN = healthy. Never raises (read failures become flags).

    ``run_score_gate`` (opt-in, default off so the cheap artifact-only path is
    unchanged) additionally runs the Step 5 protected-score invariance gate
    (``score_invariance_gate``) on a temp registry copy: a RED gate verdict means
    a registry weight delta now moves a protected score (a coupling regression)
    and adds the RED flag ``score_coupling_regression`` — a hard block on any
    live Step 5 apply. Wire this on in the yearly Quant-lens review and before
    approving any apply."""
    now = now or datetime.now(timezone.utc)
    red: list[str] = []
    amber: list[str] = []
    details: dict[str, Any] = {}

    results = _load_json(Path(backtest_dir) / "poc_simulation_results.json")
    if not isinstance(results, dict):
        red.append("results_missing")
    else:
        perf = results.get("performance") or {}
        evaluated = perf.get("evaluated") or 0
        details["evaluated"] = evaluated
        details["oos_window"] = results.get("oos_window")

        if evaluated == 0:
            # Present (and possibly recent) but nothing resolved → silent-zero.
            red.append("looks_fresh_but_empty")
        else:
            per_regime = (results.get("added_metrics") or {}).get("per_regime") or []
            regimes = [str(r.get("regime")) for r in per_regime if isinstance(r, dict)]
            details["regimes"] = regimes
            if regimes and all(r == "unknown" for r in regimes):
                red.append("degenerate_regimes")
            if evaluated < min_evaluated:
                amber.append("low_sample")

        generated = _parse_dt(results.get("generated_at"))
        if generated is not None:
            age_days = (now - generated).days
            details["age_days"] = age_days
            if age_days > max_age_days:
                amber.append("stale")

        slope = (results.get("calibration") or {}).get("calibration_slope")
        details["calibration_slope"] = slope
        if isinstance(slope, (int, float)) and slope < 0:
            amber.append("calibration_slope_flipped")

    proposals = _load_json(Path(proposals_path))
    if not isinstance(proposals, dict):
        amber.append("proposals_missing")
    else:
        proposed_count = (proposals.get("summary") or {}).get("proposed_count", 0)
        details["proposed_count"] = proposed_count
        if not proposed_count:
            amber.append("no_proposals")

    # Sub-project D — feedback proposers (absence tolerated → no flag).
    cal_prop = _load_json(Path(calibration_proposal_path))
    if isinstance(cal_prop, dict) and cal_prop.get("inverted") is True:
        details["calibration_inverted"] = True
        amber.append("calibration_correction_available")

    tag_prop = _load_json(Path(tagging_proposal_path))
    if isinstance(tag_prop, dict):
        untagged_pct = tag_prop.get("untagged_pct")
        if isinstance(untagged_pct, (int, float)):
            details["untagged_pct"] = untagged_pct
            if untagged_pct >= max_untagged_pct:
                amber.append("high_untagged_rate")

    # Sub-project E — auto-apply audit (absence tolerated → no flag).
    audit = _load_json(Path(auto_apply_audit_path))
    if isinstance(audit, list) and audit:
        last = audit[-1] if isinstance(audit[-1], dict) else {}
        last_status = last.get("status")
        details["auto_apply"] = {"last_status": last_status, "entries": len(audit)}
        if last_status == "rolled_back":
            red.append("auto_apply_rolled_back")
        elif last_status == "applied":
            amber.append("auto_apply_active")

    # Sub-project F — reconstruction look-ahead audit (absence tolerated → no flag).
    recon = _load_json(Path(reconstruction_audit_path))
    if isinstance(recon, dict):
        details["reconstruction"] = recon
        if recon.get("look_ahead_clean") is False:
            red.append("reconstruction_lookahead_dirty")

    if run_score_gate:
        try:
            from backtesting.score_invariance_gate import assert_scores_invariant_across_apply
            gate = assert_scores_invariant_across_apply(registry_path=registry_path)
            details["score_invariance"] = gate.get("status")
            if gate.get("status") == "RED":
                red.append("score_coupling_regression")
        except Exception as exc:  # never let the gate break the health read
            details["score_invariance"] = f"error:{exc}"

    status = "RED" if red else ("AMBER" if amber else "GREEN")
    return {
        "observe_only": _OBSERVE_ONLY,
        "status": status,
        "flags": red + amber,
        "details": details,
    }
