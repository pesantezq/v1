from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from watchlist_scanner.state import WatchlistStateStore
from watchlist_scanner.weight_tuning import (
    CANDIDATE_WEIGHTS,
    CURRENT_WEIGHTS,
    _evaluate_candidate,
)

logger = logging.getLogger("watchlist_scanner.policy_simulation")

_OBSERVE_ONLY_NOTE = (
    "This proposal is observe-only. No live config has been changed. "
    "Apply manually after validation."
)


def _delta(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return round(a - b, 4)


def _add_deltas(policy: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of policy with delta_vs_current added (policy metric minus current metric)."""
    result = dict(policy)
    result["delta_vs_current"] = {
        "hit_rate": _delta(
            policy.get("top_quartile_hit_rate"),
            current.get("top_quartile_hit_rate"),
        ),
        "avg_return": _delta(
            policy.get("top_quartile_avg_return"),
            current.get("top_quartile_avg_return"),
        ),
        "direction_correct_rate": _delta(
            policy.get("top_quartile_direction_correct_rate"),
            current.get("top_quartile_direction_correct_rate"),
        ),
    }
    return result


def _rank_policies(policies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Return a copy of policies with a 'rank' field (1 = best).

    Sufficient-sample policies (no warning) sort before thin ones.
    Within each group: highest hit_rate first, then highest avg_return.
    """
    def _key(p: dict[str, Any]) -> tuple:
        warning = bool(p.get("low_sample_warning", True))
        hit = float(p.get("top_quartile_hit_rate") or 0.0)
        ret = float(p.get("top_quartile_avg_return") or 0.0)
        return (warning, -hit, -ret)

    ranked = []
    for i, p in enumerate(sorted(policies, key=_key)):
        ranked.append({**p, "rank": i + 1})
    return ranked


def _pick_recommended_from_rows(evaluated: dict[str, dict[str, Any]]) -> str:
    """Derive recommended candidate from evaluated results when no external suggestion exists."""
    candidates = list(evaluated.values())
    sufficient = [c for c in candidates if not c.get("low_sample_warning") and c.get("top_quartile_hit_rate") is not None]
    pool = sufficient or [c for c in candidates if c.get("top_quartile_hit_rate") is not None]
    if not pool:
        return "current"
    best = max(pool, key=lambda c: (
        float(c.get("top_quartile_hit_rate") or 0.0),
        float(c.get("top_quartile_avg_return") or 0.0),
    ))
    return best["name"]


def build_policy_simulation(
    rows: list[dict[str, Any]],
    *,
    weight_tuning_suggestions: dict[str, Any] | None = None,
    primary_window_days: int = 3,
    candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Build an observe-only policy simulation comparing all weight candidates.

    weight_tuning_suggestions: result from build_weight_tuning_suggestions() or the
    loaded weight_tuning_suggestions.json dict. Used only to carry forward the
    recommended_candidate name. Falls back to deriving it from simulation results.
    """
    candidate_list = candidates if candidates is not None else CANDIDATE_WEIGHTS
    return_col = f"outcome_return_{primary_window_days}d"

    evaluated: dict[str, dict[str, Any]] = {
        c["name"]: _evaluate_candidate(rows, c, primary_window_days=primary_window_days)
        for c in candidate_list
    }

    current_result = evaluated.get("current") or {}

    if weight_tuning_suggestions and weight_tuning_suggestions.get("recommended_candidate"):
        recommended_name = str(weight_tuning_suggestions["recommended_candidate"])
        if recommended_name not in evaluated:
            recommended_name = _pick_recommended_from_rows(evaluated)
    else:
        recommended_name = _pick_recommended_from_rows(evaluated)

    all_with_deltas = [_add_deltas(v, current_result) for v in evaluated.values()]
    all_ranked = _rank_policies(all_with_deltas)

    current_ranked = next((p for p in all_ranked if p["name"] == "current"), all_ranked[0] if all_ranked else {})
    recommended_ranked = next((p for p in all_ranked if p["name"] == recommended_name), current_ranked)

    return {
        "generated_at": datetime.now().isoformat(),
        "observe_only": True,
        "primary_window_days": primary_window_days,
        "total_rows": len(rows),
        "resolved_rows": sum(1 for r in rows if r.get(return_col) is not None),
        "recommended_candidate": recommended_name,
        "current_policy": current_ranked,
        "recommended_policy": recommended_ranked,
        "all_policies": all_ranked,
    }


def build_config_proposal(
    simulation: dict[str, Any],
    *,
    weight_tuning_suggestions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build an observe-only config proposal from a policy simulation result.

    applied is always False. This dict must never be written to a live config file.
    """
    recommended_name = simulation.get("recommended_candidate", "current")
    recommended_policy = simulation.get("recommended_policy") or {}
    proposed_weights = dict(recommended_policy.get("weights") or CURRENT_WEIGHTS)
    current_weights = dict(CURRENT_WEIGHTS)

    weight_deltas = {
        k: round(proposed_weights.get(k, 0.0) - current_weights.get(k, 0.0), 4)
        for k in current_weights
    }

    delta_vs = recommended_policy.get("delta_vs_current") or {}
    reason = str((weight_tuning_suggestions or {}).get("recommendation_reason") or "")

    return {
        "generated_at": datetime.now().isoformat(),
        "observe_only": True,
        "applied": False,
        "proposal_status": "not_applied",
        "source": "policy_simulation",
        "recommended_candidate": recommended_name,
        "recommendation_reason": reason,
        "proposed_weights": proposed_weights,
        "current_weights": current_weights,
        "weight_deltas": weight_deltas,
        "performance_delta": {
            "hit_rate_delta": delta_vs.get("hit_rate"),
            "avg_return_delta": delta_vs.get("avg_return"),
            "direction_correct_rate_delta": delta_vs.get("direction_correct_rate"),
        },
        "advisory_note": _OBSERVE_ONLY_NOTE,
    }


def generate_policy_simulation_report(
    *,
    db_path: str | Path = "data/portfolio.db",
    output_dir: str | Path = "outputs/performance",
    primary_window_days: int = 3,
) -> dict[str, Any]:
    """
    Load signal feedback, run policy simulation, write policy_simulation.json
    and config_proposal.json. Reads weight_tuning_suggestions.json for the
    recommended candidate name; falls back gracefully when absent.
    """
    out_dir = Path(output_dir)
    wt_path = out_dir / "weight_tuning_suggestions.json"
    weight_tuning_suggestions: dict[str, Any] | None = None
    if wt_path.exists():
        try:
            weight_tuning_suggestions = json.loads(wt_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("policy_simulation: could not load %s — %s", wt_path, exc)

    store = WatchlistStateStore(db_path)
    rows = store.list_signal_feedback(limit=10000)

    simulation = build_policy_simulation(
        rows,
        weight_tuning_suggestions=weight_tuning_suggestions,
        primary_window_days=primary_window_days,
    )
    proposal = build_config_proposal(simulation, weight_tuning_suggestions=weight_tuning_suggestions)

    out_dir.mkdir(parents=True, exist_ok=True)
    sim_path = out_dir / "policy_simulation.json"
    prop_path = out_dir / "config_proposal.json"
    sim_path.write_text(json.dumps(simulation, indent=2), encoding="utf-8")
    prop_path.write_text(json.dumps(proposal, indent=2), encoding="utf-8")
    logger.info("Policy simulation written: %s, %s", sim_path, prop_path)

    return {
        "simulation": simulation,
        "proposal": proposal,
        "paths": {"simulation": str(sim_path), "proposal": str(prop_path)},
    }
