"""
Master strategy score — rank tactics by after-cost, risk-adjusted excess vs SPY,
rewarding consistency + research support, penalizing overfit / turnover / tax /
concentration / leverage. Pure; weights configurable.

A higher score = a more trustworthy strategy, NOT just the highest ending balance.
"""
from __future__ import annotations

from typing import Any

DEFAULT_WEIGHTS = {
    "excess_return_vs_spy": 1.0,
    "probability_beat_spy_bonus": 0.5,
    "drawdown_control_bonus": 0.5,
    "consistency_bonus": 0.5,
    "research_support_bonus": 0.25,
    "turnover_penalty": 0.3,
    "tax_drag_penalty": 0.3,
    "concentration_penalty": 0.3,
    "leverage_penalty": 0.3,
    "overfit_penalty": 0.8,
}

# Identity of the weight set actually applied. Bump this string if DEFAULT_WEIGHTS'
# *meaning* ever changes (not on every tweak) so persisted decompositions stay
# attributable to the formula version that produced them.
WEIGHT_SET_VERSION = "strategy_score_default_v1"

# Workstream 1a (ws-01-strategy-score audit): schema version for the persisted
# `score_decomposition` block. Bump on any structural change to the block shape.
DECOMPOSITION_SCHEMA_VERSION = "1.0"

# Direction-of-goodness per component, made structured/machine-checkable (audit §3):
# "higher_better" = a larger normalized value increases the score;
# "lower_better"  = a larger raw value is being penalized (contribution is negative).
_COMPONENT_DIRECTIONS = {
    "excess_return_vs_spy": "higher_better",
    "probability_beat_spy": "higher_better",
    "drawdown": "higher_better",  # normalized as (1 + drawdown); a less-negative raw drawdown is better
    "consistency": "higher_better",
    "has_research": "higher_better",
    "turnover": "lower_better",
    "tax_drag": "lower_better",
    "concentration": "lower_better",
    "leverage": "lower_better",
    "overfit": "lower_better",
}

# Component-specific hint for *why* a value is missing, where known. Anything not
# listed falls back to the generic reason below. NOTE: this only documents the
# ALREADY-missing case (components.get(key) is None) — it does not change which
# components can be None (audit §4: only `overfit` is None in production today).
_MISSING_REASON_HINTS = {
    "overfit": "walk_forward_not_run_or_status_not_ok",
}
_DEFAULT_MISSING_REASON = "component_value_unavailable"


def _component_missing(components: dict[str, Any], key: str) -> bool:
    """A component is 'missing' if its key is absent or explicitly None — never
    inferred from a real 0.0 value. Keeps 'measured zero' distinct from 'not measured'."""
    return key not in components or components.get(key) is None


def recompute_composite_from_decomposition(decomposition: dict[str, Any]) -> float:
    """
    Pure re-derivation of the composite `strategy_score` from a persisted
    `score_decomposition` block: sums each component's stored `contribution`.

    This is the ONE shared implementation of "how do the parts add up to the
    total" — tests and any future consumer should call this rather than
    re-deriving the formula, so the two can never drift apart.
    """
    comps = decomposition.get("components", {}) or {}
    total = sum(float(c.get("contribution", 0.0)) for c in comps.values())
    return round(total, 4)


def score(components: dict[str, float], weights: dict[str, float] | None = None) -> dict[str, Any]:
    """
    Combine normalized score components into a single number + flags.

    `components` keys (all expected in ~[-1,1] or [0,1] normalized form):
      excess_return_vs_spy, probability_beat_spy, drawdown (≤0), consistency (0..1),
      has_research (bool/0-1), turnover (0..1), tax_drag (0..1), concentration (0..1),
      leverage (0..1), overfit (0..1 IS-OOS gap; None → unknown).

    Returns `score_decomposition` alongside `strategy_score`/`flags`/`components` —
    an additive, persistable audit trail (raw value, normalized/contributing value,
    weight, direction-of-goodness, contribution, and an explicit missing marker per
    component; plus the weight-set identity and a self-consistency residual/
    reproducible check). It does not affect `strategy_score` in any way — see
    `recompute_composite_from_decomposition` for the shared re-derivation formula.
    """
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    weights_source = "default" if not weights else "config_override"
    flags: list[str] = []

    excess = float(components.get("excess_return_vs_spy", 0.0))
    pbeat = float(components.get("probability_beat_spy", 0.0))
    drawdown = float(components.get("drawdown", 0.0))            # ≤ 0
    consistency = float(components.get("consistency", 0.0))
    has_research = 1.0 if components.get("has_research") else 0.0
    turnover = float(components.get("turnover", 0.0))
    tax_drag = float(components.get("tax_drag", 0.0))
    concentration = float(components.get("concentration", 0.0))
    leverage = float(components.get("leverage", 0.0))
    overfit = components.get("overfit")
    if overfit is None:
        overfit_val = 0.0
        flags.append("overfit_unknown")
    else:
        overfit_val = max(0.0, float(overfit))

    # --- Unchanged composite math (do not alter — this is the protected computation) ---
    total = (
        w["excess_return_vs_spy"] * excess
        + w["probability_beat_spy_bonus"] * (pbeat - 0.5) * 2      # center at 0.5
        + w["drawdown_control_bonus"] * (1.0 + drawdown)            # less drawdown → higher
        + w["consistency_bonus"] * consistency
        + w["research_support_bonus"] * has_research
        - w["turnover_penalty"] * turnover
        - w["tax_drag_penalty"] * tax_drag
        - w["concentration_penalty"] * concentration
        - w["leverage_penalty"] * leverage
        - w["overfit_penalty"] * overfit_val
    )
    if not has_research:
        flags.append("no_academic_basis")
    strategy_score = round(total, 4)

    # --- Additive decomposition (ws-01a): records what already got computed above,
    # persists it so the composite is reproducible from stored data. Does NOT feed
    # back into `total`/`strategy_score` — purely a recording of the same terms. ---
    def _entry(key: str, raw: Any, normalized: float, weight: float, contribution: float) -> dict[str, Any]:
        missing = _component_missing(components, key)
        return {
            "raw": (None if missing else raw),
            "normalized": (None if missing else normalized),
            "weight": weight,
            "direction": _COMPONENT_DIRECTIONS[key],
            "contribution": contribution,
            "missing": missing,
            "missing_reason": (_MISSING_REASON_HINTS.get(key, _DEFAULT_MISSING_REASON) if missing else None),
        }

    comp_entries = {
        "excess_return_vs_spy": _entry(
            "excess_return_vs_spy", excess, excess,
            w["excess_return_vs_spy"], w["excess_return_vs_spy"] * excess),
        "probability_beat_spy": _entry(
            "probability_beat_spy", pbeat, (pbeat - 0.5) * 2,
            w["probability_beat_spy_bonus"], w["probability_beat_spy_bonus"] * (pbeat - 0.5) * 2),
        "drawdown": _entry(
            "drawdown", drawdown, (1.0 + drawdown),
            w["drawdown_control_bonus"], w["drawdown_control_bonus"] * (1.0 + drawdown)),
        "consistency": _entry(
            "consistency", consistency, consistency,
            w["consistency_bonus"], w["consistency_bonus"] * consistency),
        "has_research": _entry(
            "has_research", bool(components.get("has_research")), has_research,
            w["research_support_bonus"], w["research_support_bonus"] * has_research),
        "turnover": _entry(
            "turnover", turnover, turnover,
            w["turnover_penalty"], -w["turnover_penalty"] * turnover),
        "tax_drag": _entry(
            "tax_drag", tax_drag, tax_drag,
            w["tax_drag_penalty"], -w["tax_drag_penalty"] * tax_drag),
        "concentration": _entry(
            "concentration", concentration, concentration,
            w["concentration_penalty"], -w["concentration_penalty"] * concentration),
        "leverage": _entry(
            "leverage", leverage, leverage,
            w["leverage_penalty"], -w["leverage_penalty"] * leverage),
        # overfit=None stays None→0.0-penalty behavior above (unchanged, gated separately);
        # here we only ADD the honest record that the raw value was never measured.
        "overfit": _entry(
            "overfit", overfit, overfit_val,
            w["overfit_penalty"], -w["overfit_penalty"] * overfit_val),
    }
    recomputed = round(sum(e["contribution"] for e in comp_entries.values()), 4)
    residual = round(recomputed - strategy_score, 6)
    decomposition = {
        "schema_version": DECOMPOSITION_SCHEMA_VERSION,
        "weight_set": {"version": WEIGHT_SET_VERSION, "source": weights_source, "weights": dict(w)},
        # No cross-sectional normalization occurs anywhere in this formula (audit §3) —
        # recorded explicitly rather than silently omitted.
        "normalization": {"method": "none", "population": None, "population_date": None},
        "components": comp_entries,
        "stored_composite": strategy_score,
        "recomputed_composite": recomputed,
        "residual": residual,
        "reproducible": abs(residual) < 1e-6,
    }

    return {
        "strategy_score": strategy_score,
        "flags": flags,
        "components": components,
        "score_decomposition": decomposition,
    }


def rank(scored: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort scored tactic dicts by strategy_score descending."""
    return sorted(scored, key=lambda s: s.get("strategy_score", 0.0), reverse=True)
