"""
Attribution + diagnostics for the weekly ETF bundle subsystem.

Explains changes in ranking quality by component, bundle, regime, and horizon,
and turns findings into Strat Lab challenger HYPOTHESES. It NEVER changes scoring
weights automatically — the diagnostic exists to seed controlled experiments,
not to close the loop.
"""
from __future__ import annotations

from typing import Any

from portfolio_automation.weekly_etf_bundles.evaluation import spearman
from portfolio_automation.weekly_etf_bundles.outcomes import STATUS_MATURED
from portfolio_automation.weekly_etf_bundles.scoring import DEFAULT_WEIGHTS

_PREDICTIVE = 0.05        # |rho| above this is a real signal
_HIT_RATE_MOVE = 0.03     # meaningful change in a hit rate


def component_attribution(matured_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Rank correlation of each score component vs realized excess return.
    Positive → predictive; negative → counterproductive; ~0 → neutral."""
    rows = [r for r in matured_rows if r.get("status") == STATUS_MATURED]
    out: dict[str, dict[str, Any]] = {}
    for comp in DEFAULT_WEIGHTS:
        pairs = [
            (r["score_components"][comp], r["excess_return"])
            for r in rows
            if isinstance(r.get("score_components"), dict)
            and r["score_components"].get(comp) is not None
            and r.get("excess_return") is not None
        ]
        if len(pairs) < 5:
            out[comp] = {"rho": None, "n": len(pairs), "contribution": "insufficient_sample"}
            continue
        rho = spearman([float(x) for x, _ in pairs], [float(y) for _, y in pairs])
        if rho is None:
            contribution = "insufficient_sample"
        elif rho > _PREDICTIVE:
            contribution = "predictive"
        elif rho < -_PREDICTIVE:
            contribution = "counterproductive"
        else:
            contribution = "neutral"
        out[comp] = {"rho": round(rho, 4) if rho is not None else None,
                     "n": len(pairs), "contribution": contribution}
    return out


def _bundle_deltas(current: dict[str, Any], prior: dict[str, Any] | None) -> list[dict[str, Any]]:
    cur_by = current.get("by_bundle", {})
    prev_by = (prior or {}).get("by_bundle", {})
    deltas: list[dict[str, Any]] = []
    for bundle_id, cur in cur_by.items():
        cur_rate = cur.get("relative_hit_rate")
        prev_rate = prev_by.get(bundle_id, {}).get("relative_hit_rate")
        delta = (round(cur_rate - prev_rate, 4)
                 if (cur_rate is not None and prev_rate is not None) else None)
        deltas.append({
            "bundle_id": bundle_id,
            "relative_hit_rate": cur_rate,
            "prior_relative_hit_rate": prev_rate,
            "delta": delta,
            "count": cur.get("count"),
        })
    deltas.sort(key=lambda d: (d["delta"] if d["delta"] is not None else 0.0))
    return deltas


def _hypotheses(comp_attr: dict[str, dict[str, Any]],
                calibration: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Map diagnostics → Strat Lab challenger experiment hypotheses. Advisory
    only; applying any of these requires the human-gated promotion flow."""
    hyps: list[dict[str, Any]] = []
    for comp, info in comp_attr.items():
        if info.get("contribution") == "counterproductive":
            hyps.append({
                "hypothesis": f"Down-weight '{comp}' — it correlated negatively with realized "
                              f"excess return (rho={info['rho']}, n={info['n']}).",
                "suggested_experiment": "weekly_etf_bundle_v_reweighted",
                "target_parameter": f"weights.{comp}",
                "auto_apply": False,
            })
        elif info.get("contribution") == "predictive" and info.get("rho", 0) and info["rho"] >= 0.15:
            hyps.append({
                "hypothesis": f"Consider up-weighting '{comp}' — strongly predictive "
                              f"(rho={info['rho']}, n={info['n']}).",
                "suggested_experiment": "weekly_etf_bundle_v_reweighted",
                "target_parameter": f"weights.{comp}",
                "auto_apply": False,
            })
    if calibration and calibration.get("calibration_status") in ("overconfident", "non_monotonic"):
        hyps.append({
            "hypothesis": f"Calibration is {calibration['calibration_status']} — test "
                          "regime-conditioned or breadth-adjusted scoring.",
            "suggested_experiment": "weekly_etf_bundle_v4_regime_conditioned",
            "target_parameter": "regime_conditioning",
            "auto_apply": False,
        })
    return hyps


def build_attribution(
    current_scorecard: dict[str, Any],
    matured_rows: list[dict[str, Any]],
    *,
    prior_scorecard: dict[str, Any] | None = None,
    calibration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    comp_attr = component_attribution(matured_rows)
    bundle_deltas = _bundle_deltas(current_scorecard, prior_scorecard)

    cur_hit = current_scorecard.get("benchmark_relative_hit_rate")
    prev_hit = (prior_scorecard or {}).get("benchmark_relative_hit_rate")
    headline_delta = (round(cur_hit - prev_hit, 4)
                      if (cur_hit is not None and prev_hit is not None) else None)

    primary_contributor = None
    if bundle_deltas and bundle_deltas[0].get("delta") is not None:
        worst = bundle_deltas[0]
        if worst["delta"] < -_HIT_RATE_MOVE:
            primary_contributor = worst["bundle_id"]

    return {
        "observe_only": True,
        "primary_horizon": current_scorecard.get("primary_horizon"),
        "benchmark_relative_hit_rate": cur_hit,
        "prior_benchmark_relative_hit_rate": prev_hit,
        "headline_hit_rate_delta": headline_delta,
        "primary_contributor_bundle": primary_contributor,
        "component_attribution": comp_attr,
        "bundle_deltas": bundle_deltas,
        "strat_lab_hypotheses": _hypotheses(comp_attr, calibration),
        "note": "Diagnostic only — weights are never changed automatically; "
                "hypotheses feed the human-gated Strat Lab promotion flow.",
    }
