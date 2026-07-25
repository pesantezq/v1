"""
Strat Lab integration for the weekly ETF bundle subsystem.

Registers a `weekly_etf_bundles` strategy family with parameterized challenger
variants, evaluates them walk-forward out-of-sample against the champion on the
SAME weekly data, applies deterministic promotion gates, and maintains a
champion/challenger registry.

Governance:
  * Only the CHAMPION appears in the operator email; challengers live here + in
    simulation prediction lanes.
  * Promotion NEVER happens automatically. A challenger that clears every gate
    yields a PENDING promotion candidate stamped with the four sim-governance
    authority invariants (target_lane=simulation, production_mutation=False,
    feeds_decision_engine=False, is_human_approved=False). Changing the champion
    requires the existing human-gated approval path (schemas.is_human_approver).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation import weekly_etf_bundles as _pkg
from portfolio_automation.data_governance import OutputNamespace, safe_write_json
from portfolio_automation.sim_governance.schemas import is_human_approver, make_candidate_id
from portfolio_automation.weekly_etf_bundles.analysis import (
    build_weekly_analysis,
    compute_market_context,
    last_on_or_before,
)
from portfolio_automation.weekly_etf_bundles.evaluation import build_scorecard
from portfolio_automation.weekly_etf_bundles.outcomes import PRIMARY_HORIZON, mature_prediction
from portfolio_automation.weekly_etf_bundles.predictions import build_predictions
from portfolio_automation.weekly_etf_bundles.scoring import DEFAULT_WEIGHTS, ScoringParams

logger = logging.getLogger("stockbot.weekly_etf_bundles.strat_lab")

STRATEGY_FAMILY = "weekly_etf_bundles"
CHAMPION_ID = "weekly_etf_bundle_v1_baseline"

# Deterministic promotion gates (all configurable). A challenger must clear EVERY
# gate to become a pending human-review candidate; require_human_approval keeps
# the final step human even then.
DEFAULT_GATES: dict[str, Any] = {
    "minimum_matured_4w_predictions": 100,
    "minimum_calendar_history_weeks": 26,
    "minimum_relative_hit_rate_improvement": 0.03,
    "minimum_information_coefficient": 0.05,
    "minimum_top_bottom_spread": 0.02,
    "maximum_drawdown_regression": 0.02,
    "require_positive_out_of_sample_result": True,
    "require_multi_regime_stability": True,
    "require_human_approval": True,
}


def _weights(**overrides: float) -> dict[str, float]:
    w = dict(DEFAULT_WEIGHTS)
    w.update(overrides)
    total = sum(w.values())
    return {k: round(v / total, 6) for k, v in w.items()}   # renormalize to 1.0


@dataclass(frozen=True)
class Variant:
    variant_id: str
    name: str
    description: str
    base_params: ScoringParams
    academic_basis: str
    regime_conditioned: bool = False
    regime_weight_overrides: dict[str, dict[str, float]] = field(default_factory=dict)

    def params_for(self, market_regime: str | None) -> ScoringParams:
        if not self.regime_conditioned:
            return self.base_params
        override = self.regime_weight_overrides.get(market_regime or "neutral")
        if not override:
            return self.base_params
        return ScoringParams(weights=_weights(**override))

    def metadata(self) -> dict[str, Any]:
        return {
            "variant_id": self.variant_id,
            "name": self.name,
            "description": self.description,
            "academic_basis": self.academic_basis,
            "regime_conditioned": self.regime_conditioned,
            "weights": dict(self.base_params.weights),
        }


VARIANTS: tuple[Variant, ...] = (
    Variant(
        variant_id="weekly_etf_bundle_v1_baseline",
        name="Baseline",
        description="v1 baseline weights: relative strength 30, momentum 20, trend 20, "
                    "52w-high 10, vol-adjusted 10, drawdown 10.",
        base_params=ScoringParams(weights=_weights()),
        academic_basis="Cross-sectional relative strength + trend following (Jegadeesh-Titman).",
    ),
    Variant(
        variant_id="weekly_etf_bundle_v2_momentum_heavy",
        name="Momentum Heavy",
        description="Up-weights 4-week momentum, down-weights 52w-high distance.",
        base_params=ScoringParams(weights=_weights(momentum_4w=0.35, distance_from_52w_high=0.05)),
        academic_basis="Short-horizon momentum persistence.",
    ),
    Variant(
        variant_id="weekly_etf_bundle_v3_breadth_adjusted",
        name="Breadth / Quality Adjusted",
        description="Tilts toward trend structure + drawdown resilience (participation/quality "
                    "proxy) and away from raw momentum.",
        base_params=ScoringParams(weights=_weights(trend_structure=0.28, drawdown_resilience=0.15,
                                                   momentum_4w=0.12)),
        academic_basis="Breadth/participation and downside-resilience factors.",
    ),
    Variant(
        variant_id="weekly_etf_bundle_v4_regime_conditioned",
        name="Regime Conditioned",
        description="Switches weights by market regime: risk_on tilts to momentum, risk_off "
                    "tilts to drawdown resilience + 52w-high proximity.",
        base_params=ScoringParams(weights=_weights()),
        academic_basis="Regime-dependent factor efficacy.",
        regime_conditioned=True,
        regime_weight_overrides={
            "risk_on": {"momentum_4w": 0.32, "relative_strength_12w": 0.33},
            "risk_off": {"drawdown_resilience": 0.22, "distance_from_52w_high": 0.18,
                         "momentum_4w": 0.10},
            "neutral": {},
        },
    ),
)


def variant_by_id(variant_id: str) -> Variant | None:
    return next((v for v in VARIANTS if v.variant_id == variant_id), None)


def evaluate_variant(
    config: Any,
    panel: Any,
    as_of_dates: list[str],
    variant: Variant,
    *,
    horizon: str = PRIMARY_HORIZON,
    now_date: str | None = None,
    min_sample: int = DEFAULT_GATES["minimum_matured_4w_predictions"],
    min_weeks: int = DEFAULT_GATES["minimum_calendar_history_weeks"],
) -> dict[str, Any]:
    """Walk-forward OOS evaluation of one variant over historical as-of dates.
    Each week's predictions are frozen point-in-time and matured only with
    post-date prices — leakage-safe. Variant weights are fixed presets (not fit
    on the data), so there is no train/test contamination."""
    now_date = now_date or (panel.dates[-1] if getattr(panel, "dates", None) else None)
    default_bm = str(config.defaults.get("benchmark", "SPY"))
    matured: list[dict[str, Any]] = []

    for as_of in as_of_dates:
        mdd = last_on_or_before(panel.dates, as_of)
        if mdd is None:
            continue
        ctx = compute_market_context(panel, mdd, benchmark=default_bm)
        params = variant.params_for(ctx.get("market_regime"))
        payload = build_weekly_analysis(
            config, panel, as_of=mdd, params=params,
            strategy_id=variant.variant_id, model_version=variant.variant_id,
        )
        preds = build_predictions(payload, lane="challenger", strategy_variant=variant.variant_id)
        for p in preds:
            matured.append(mature_prediction(p, panel, horizon, now_date=now_date))

    scorecard = build_scorecard(matured, primary_horizon=horizon,
                                min_sample=min_sample, min_weeks=min_weeks)
    return {"variant_id": variant.variant_id, "matured": matured, "scorecard": scorecard}


def evaluate_promotion_gates(
    champion_sc: dict[str, Any],
    challenger_sc: dict[str, Any],
    gates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deterministic gate evaluation. Returns pass/fail per gate + an overall
    status. Never approves — passing only makes a challenger eligible for the
    human-gated review."""
    g = {**DEFAULT_GATES, **(gates or {})}
    checks: list[dict[str, Any]] = []

    def _chk(name: str, ok: bool | None, detail: str):
        checks.append({"gate": name, "ok": bool(ok), "detail": detail})

    n = challenger_sc.get("matured_prediction_count", 0)
    weeks = challenger_sc.get("calendar_weeks_span", 0)
    sample_ok = n >= g["minimum_matured_4w_predictions"]
    weeks_ok = weeks >= g["minimum_calendar_history_weeks"]
    _chk("minimum_matured_4w_predictions", sample_ok, f"{n} >= {g['minimum_matured_4w_predictions']}")
    _chk("minimum_calendar_history_weeks", weeks_ok, f"{weeks} >= {g['minimum_calendar_history_weeks']}")

    ch_hit = challenger_sc.get("benchmark_relative_hit_rate")
    cp_hit = champion_sc.get("benchmark_relative_hit_rate")
    improvement = (ch_hit - cp_hit) if (ch_hit is not None and cp_hit is not None) else None
    _chk("minimum_relative_hit_rate_improvement",
         improvement is not None and improvement >= g["minimum_relative_hit_rate_improvement"],
         f"delta={improvement}")

    ic = challenger_sc.get("information_coefficient")
    _chk("minimum_information_coefficient",
         ic is not None and ic >= g["minimum_information_coefficient"], f"ic={ic}")

    spread = challenger_sc.get("top_bottom_score_spread")
    _chk("minimum_top_bottom_spread",
         spread is not None and spread >= g["minimum_top_bottom_spread"], f"spread={spread}")

    ch_dd = challenger_sc.get("avg_max_drawdown")
    cp_dd = champion_sc.get("avg_max_drawdown")
    dd_regression = (cp_dd - ch_dd) if (ch_dd is not None and cp_dd is not None) else None
    # drawdowns are negative; challenger must not be worse than champion by > cap
    dd_ok = dd_regression is None or dd_regression <= g["maximum_drawdown_regression"]
    _chk("maximum_drawdown_regression", dd_ok, f"regression={dd_regression}")

    if g["require_positive_out_of_sample_result"]:
        avg_excess = challenger_sc.get("avg_excess_return")
        _chk("require_positive_out_of_sample_result",
             avg_excess is not None and avg_excess > 0, f"avg_excess={avg_excess}")

    if g["require_multi_regime_stability"]:
        by_regime = challenger_sc.get("by_market_regime", {})
        stable = sum(1 for v in by_regime.values()
                     if v.get("relative_hit_rate") is not None and v.get("count", 0) >= 5) >= 2
        _chk("require_multi_regime_stability", stable, f"regimes_with_signal={len(by_regime)}")

    all_pass = all(c["ok"] for c in checks)
    metrics_pass = all(c["ok"] for c in checks
                       if c["gate"] not in ("minimum_matured_4w_predictions",
                                            "minimum_calendar_history_weeks"))

    if all_pass:
        status = "ready_for_human_review"
    elif metrics_pass and not (sample_ok and weeks_ok):
        status = "promising_but_insufficient_sample"
    else:
        status = "not_eligible"

    return {
        "status": status,
        "passes_all_gates": all_pass,
        "checks": checks,
        "requires_human_approval": bool(g["require_human_approval"]),
        "auto_promotion": False,
    }


def build_promotion_candidate(
    challenger_id: str, gate_result: dict[str, Any], *, salt: str,
) -> dict[str, Any]:
    """Build a PENDING promotion candidate with the four authority invariants.
    Never approved; feeds the human-gated review only."""
    candidate_id = make_candidate_id("weekly_etf_champion_change", challenger_id, salt)
    return {
        "candidate_id": candidate_id,
        "proposal_type": "weekly_etf_champion_change",
        "strategy_family": STRATEGY_FAMILY,
        "challenger_id": challenger_id,
        # ── four authority invariants (mirror institutional sim_candidates) ──
        "target_lane": "simulation",
        "production_mutation": False,
        "feeds_decision_engine": False,
        "is_human_approved": False,
        # ── governance state ──
        "approval_status": "pending",
        "ready_for_production_review": gate_result.get("status") == "ready_for_human_review",
        "gate_result": gate_result,
        "what_changed": f"Propose promoting challenger {challenger_id} to champion.",
        "requires_human_approver": True,
        "note": "AI/gates can recommend readiness but can NEVER approve; champion "
                "change requires schemas.is_human_approver.",
    }


def run_strat_lab_comparison(
    config: Any,
    panel: Any,
    as_of_dates: list[str],
    *,
    horizon: str = PRIMARY_HORIZON,
    gates: dict[str, Any] | None = None,
    champion_id: str = CHAMPION_ID,
    generated_at: str | None = None,
    now_date: str | None = None,
) -> dict[str, Any]:
    """Evaluate every variant, gate the challengers vs the champion, and produce
    the comparison + challenger registry + pending promotion candidates."""
    ts = generated_at or datetime.now(timezone.utc).isoformat()
    results = {v.variant_id: evaluate_variant(config, panel, as_of_dates, v,
                                              horizon=horizon, now_date=now_date)
               for v in VARIANTS}
    champion = results.get(champion_id)
    champion_sc = champion["scorecard"] if champion else {}

    variant_summaries: dict[str, Any] = {}
    candidates: list[dict[str, Any]] = []
    for v in VARIANTS:
        sc = results[v.variant_id]["scorecard"]
        summary = {
            "metadata": v.metadata(),
            "matured_prediction_count": sc.get("matured_prediction_count"),
            "sample_status": sc.get("sample_status"),
            "benchmark_relative_hit_rate": sc.get("benchmark_relative_hit_rate"),
            "information_coefficient": sc.get("information_coefficient"),
            "top_bottom_score_spread": sc.get("top_bottom_score_spread"),
            "avg_excess_return": sc.get("avg_excess_return"),
            "avg_max_drawdown": sc.get("avg_max_drawdown"),
            "is_champion": v.variant_id == champion_id,
        }
        if v.variant_id != champion_id:
            gate_result = evaluate_promotion_gates(champion_sc, sc, gates)
            summary["gate_result"] = gate_result
            if gate_result["status"] in ("ready_for_human_review", "promising_but_insufficient_sample"):
                candidates.append(build_promotion_candidate(v.variant_id, gate_result, salt=ts[:10]))
        variant_summaries[v.variant_id] = summary

    # Leaderboard: rank by IC then relative-hit-rate (None sorts last), deterministic.
    def _key(vid: str) -> tuple[float, float, str]:
        s = variant_summaries[vid]
        ic = s["information_coefficient"] if s["information_coefficient"] is not None else -9.9
        hr = s["benchmark_relative_hit_rate"] if s["benchmark_relative_hit_rate"] is not None else -9.9
        return (-ic, -hr, vid)
    leaderboard = sorted(variant_summaries.keys(), key=_key)

    return {
        "generated_at": ts,
        "observe_only": True,
        "feeds_decision_engine": False,
        "schema_version": _pkg.SCHEMA_VERSION,
        "source": _pkg.SOURCE_LABEL,
        "strategy_family": STRATEGY_FAMILY,
        "champion_id": champion_id,
        "horizon": horizon,
        "gates": {**DEFAULT_GATES, **(gates or {})},
        "walk_forward_dates": list(as_of_dates),
        "leaderboard": leaderboard,
        "variants": variant_summaries,
        "pending_promotion_candidates": candidates,
        "note": "Challenger evaluation is walk-forward OOS on frozen point-in-time "
                "predictions; no automatic promotion. Champion change is human-gated.",
    }


def build_challenger_registry(comparison: dict[str, Any]) -> dict[str, Any]:
    champion_id = comparison.get("champion_id")
    challengers = []
    for vid, s in comparison.get("variants", {}).items():
        if vid == champion_id:
            continue
        challengers.append({
            "variant_id": vid,
            "status": s.get("gate_result", {}).get("status", "unknown"),
            "information_coefficient": s.get("information_coefficient"),
            "benchmark_relative_hit_rate": s.get("benchmark_relative_hit_rate"),
        })
    return {
        "generated_at": comparison.get("generated_at"),
        "observe_only": True,
        "strategy_family": STRATEGY_FAMILY,
        "champion": champion_id,
        "champion_locked": True,   # only a human approval can change it
        "challengers": challengers,
        "pending_promotion_candidates": comparison.get("pending_promotion_candidates", []),
    }


def write_strat_lab_artifacts(comparison: dict[str, Any], *, root: str | Path) -> dict[str, str]:
    root_path = Path(root).resolve()
    registry = build_challenger_registry(comparison)
    paths = {
        "strat_lab_comparison": safe_write_json(
            OutputNamespace.WEEKLY_ETF_BUNDLES, "strat_lab_comparison.json",
            comparison, base_dir=root_path / "outputs"),
        "challenger_registry": safe_write_json(
            OutputNamespace.WEEKLY_ETF_BUNDLES, "challenger_registry.json",
            registry, base_dir=root_path / "outputs"),
    }
    return {k: str(v) for k, v in paths.items()}


def assert_no_auto_approval(candidate: dict[str, Any]) -> None:
    """Defensive invariant used by tests + health: a promotion candidate can
    never be self-approved by the AI/gates."""
    assert candidate["is_human_approved"] is False
    assert candidate["production_mutation"] is False
    assert candidate["feeds_decision_engine"] is False
    assert candidate["target_lane"] == "simulation"
    assert not is_human_approver("auto")
    assert not is_human_approver("system")
    assert not is_human_approver("gpt")
