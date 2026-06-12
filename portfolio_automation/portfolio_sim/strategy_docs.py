"""
Strategy catalog producer — the mechanism behind the Strategy Documentation Rule.

For every tactic it emits a card: objective, universe, materialization (the tilt
map), caps, latest backtest metrics, decision rationale, and a plain-language
explanation. A tactic with no rationale flips `coverage_complete` to False so the
rule is testable. Pure; writers live in the orchestrator.
"""
from __future__ import annotations

from typing import Any

from portfolio_automation.portfolio_sim.tactics import Tactic

# Per-tactic rationale strings (the "decision & rationale" the rule requires).
# Keyed by tactic_id prefix / id. Sourced here so every shipped tactic is documented.
_RATIONALE = {
    "shadow_actual_baseline": "The operator's real holdings — the anchor every tactic is measured against.",
    "shadow_target_allocation_baseline": "Config target weights — where the portfolio is steering.",
    "shadow_engine_followed": "What the decision engine would hold (advisory reference).",
    "shadow_lower_risk": "Trims the largest position toward equal-weight to show a de-risked variant.",
    "shadow_discovery_enhanced": "Core + a capped sleeve of qualified discovery names.",
    "shadow_boom_bucket": "Core + a capped speculative sleeve (≤15%/≤5% per idea).",
    "profile_aggressive_growth": "Growth/leverage tilt within the leverage cap — max upside objective.",
    "profile_short_term_tactical": "APPROXIMATE static stand-in for a signal-driven tactic; faithful version deferred (look-ahead risk).",
    "profile_long_term_compounding": "Broad-ETF, low-turnover tilt for long-horizon after-tax compounding.",
    "profile_tax_aware": "Broad ETFs + new-cash rebalancing bias to minimize taxable churn.",
    "profile_defensive_capital_preservation": "Zeroes leverage, raises gold/bonds/low-vol — drawdown protection.",
    "profile_income_dividend": "Dividend + bond floors for yield with acceptable growth.",
    "profile_balanced_core_satellite": "Diversified core + a small tactical satellite within caps.",
    "profile_boom_bucket": "Asymmetric-upside tilt to leverage/growth within hard caps.",
    "benchmark_spy": "The S&P 500 — the operator's primary beat-the-market benchmark.",
    "benchmark_qqq": "Nasdaq-100 — secondary benchmark.",
}


def _explain(t: Tactic) -> str:
    mat = t.metadata.get("materialization")
    if isinstance(mat, dict) and mat.get("rules"):
        rules = ", ".join(mat["rules"])
        return f"{t.name}: weights derived from the actual portfolio with tilts [{rules}], normalized and clamped to config caps."
    return f"{t.name}: weight vector {sorted(t.target_weights.items(), key=lambda kv: -kv[1])[:5]}."


def build_strategy_catalog(
    tactics: list[Tactic],
    results_by_tactic: dict[str, list[dict[str, Any]]],
    *,
    extra_rationale: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the catalog dict. `results_by_tactic[tactic_id]` = list of metric dicts."""
    rationale_map = dict(_RATIONALE)
    rationale_map.update(extra_rationale or {})
    cards = []
    for t in tactics:
        rationale = rationale_map.get(t.tactic_id, "")
        cards.append({
            "tactic_id": t.tactic_id,
            "name": t.name,
            "source": t.source,
            "approximate": t.approximate,
            "objective": t.metadata.get("objective", ""),
            "universe": sorted(t.target_weights.keys()),
            "materialization": t.metadata.get("materialization"),
            "caps": t.metadata.get("caps", {}),
            "metrics_by_window": results_by_tactic.get(t.tactic_id, []),
            "rationale": rationale,
            "explanation": _explain(t),
        })
    coverage_complete = all(c["rationale"] for c in cards)
    return {
        "tactic_count": len(cards),
        "coverage_complete": coverage_complete,
        "undocumented": [c["tactic_id"] for c in cards if not c["rationale"]],
        "cards": cards,
    }


def render_strategy_catalog_md(catalog: dict[str, Any]) -> str:
    lines = ["# Strategy Catalog", "",
             "_Sandbox simulation strategies — observe-only. Not trade recommendations._", ""]
    lines.append(f"Tactics documented: {catalog['tactic_count']} · "
                 f"coverage complete: {catalog['coverage_complete']}")
    if catalog.get("undocumented"):
        lines.append(f"**Undocumented (rule violation): {', '.join(catalog['undocumented'])}**")
    lines.append("")
    for c in catalog["cards"]:
        lines.append(f"## {c['name']} (`{c['tactic_id']}`)")
        if c["approximate"]:
            lines.append("> ⚠️ Approximate static stand-in.")
        if c["objective"]:
            lines.append(f"- Objective: {c['objective']}")
        lines.append(f"- Universe: {', '.join(c['universe'])}")
        lines.append(f"- Rationale: {c['rationale']}")
        lines.append(f"- Explanation: {c['explanation']}")
        if c["metrics_by_window"]:
            best = c["metrics_by_window"][0]
            lines.append(f"- Latest: excess vs SPY {best.get('excess_vs_spy')}, "
                         f"CAGR {best.get('cagr')}, maxDD {best.get('max_drawdown')} "
                         f"({best.get('window_label')})")
        lines.append("")
    return "\n".join(lines)
