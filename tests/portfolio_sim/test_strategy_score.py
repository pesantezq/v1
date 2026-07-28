"""Tests for the master strategy score + expanded metrics."""
from __future__ import annotations

from portfolio_automation.portfolio_sim import metrics as m
from portfolio_automation.portfolio_sim.strategy_score import (
    rank, recompute_composite_from_decomposition, score,
)


def test_higher_excess_scores_higher():
    a = score({"excess_return_vs_spy": 0.10, "has_research": True, "overfit": 0.0})
    b = score({"excess_return_vs_spy": 0.02, "has_research": True, "overfit": 0.0})
    assert a["strategy_score"] > b["strategy_score"]


def test_overfit_penalizes():
    clean = score({"excess_return_vs_spy": 0.10, "overfit": 0.0, "has_research": True})
    overfit = score({"excess_return_vs_spy": 0.10, "overfit": 0.8, "has_research": True})
    assert overfit["strategy_score"] < clean["strategy_score"]


def test_overfit_unknown_flagged():
    s = score({"excess_return_vs_spy": 0.05})
    assert "overfit_unknown" in s["flags"]
    assert "no_academic_basis" in s["flags"]


def test_penalties_reduce_score():
    base = score({"excess_return_vs_spy": 0.10, "has_research": True, "overfit": 0.0})
    penalized = score({"excess_return_vs_spy": 0.10, "has_research": True, "overfit": 0.0,
                       "turnover": 1.0, "tax_drag": 1.0, "concentration": 1.0, "leverage": 1.0})
    assert penalized["strategy_score"] < base["strategy_score"]


def test_rank_orders_desc():
    out = rank([{"strategy_score": 1.0}, {"strategy_score": 3.0}, {"strategy_score": 2.0}])
    assert [s["strategy_score"] for s in out] == [3.0, 2.0, 1.0]


def test_metrics_time_underwater():
    assert m.time_underwater([100, 110, 105, 120]) > 0    # spent time below peak
    assert m.time_underwater([100, 110, 120]) == 0.0      # always at new highs


def test_worst_window_return():
    vals = [100, 90, 80, 120]
    assert m.worst_window_return(vals, 1) <= 0


def test_expected_shortfall_negative_tail():
    vals = [100, 90, 95, 80, 120, 100]
    assert m.expected_shortfall(vals, q=0.5) <= 0


def test_prob_beat():
    a = [100, 110, 120]   # +10%, +9%
    b = [100, 101, 102]   # +1%, +1%
    assert m.prob_beat(a, b) == 1.0


# --- Workstream 1a: score_decomposition tests (ws-01-strategy-score audit) ---
# Fixture set stands in for a diverse slice of real tactics: a top performer with
# overfit never measured, a mid-pack tactic, the one genuinely walk-forward-validated
# (overfit measured, large gap), a clean baseline with overfit measured as exactly
# 0.0, and a high-turnover crowd-style tactic.
_FIXTURE_TACTICS = [
    {"excess_return_vs_spy": 0.7078, "probability_beat_spy": 0.75, "drawdown": -0.30,
     "consistency": 0.75, "has_research": True, "turnover": 0.3, "tax_drag": 0.0,
     "concentration": 0.42, "leverage": 0.1, "overfit": None},
    {"excess_return_vs_spy": 0.12, "probability_beat_spy": 0.5, "drawdown": -0.18,
     "consistency": 0.5, "has_research": False, "turnover": 0.7, "tax_drag": 0.0,
     "concentration": 0.25, "leverage": 0.0, "overfit": None},
    {"excess_return_vs_spy": -0.05, "probability_beat_spy": 0.25, "drawdown": -0.50,
     "consistency": 0.25, "has_research": True, "turnover": 0.3, "tax_drag": 0.0,
     "concentration": 0.60, "leverage": 0.25, "overfit": 2.009588},
    {"excess_return_vs_spy": 0.02, "probability_beat_spy": 0.5, "drawdown": -0.10,
     "consistency": 0.5, "has_research": True, "turnover": 0.3, "tax_drag": 0.0,
     "concentration": 0.30, "leverage": 0.0, "overfit": 0.0},
    {"excess_return_vs_spy": 0.30, "probability_beat_spy": 1.0, "drawdown": -0.05,
     "consistency": 1.0, "has_research": False, "turnover": 0.7, "tax_drag": 0.0,
     "concentration": 0.35, "leverage": 0.0, "overfit": None},
]


def test_decomposition_reproducible_for_fixture_tactics():
    """Core workstream assertion: recomputing the composite from the persisted
    score_decomposition must equal the stored strategy_score, tightly."""
    for components in _FIXTURE_TACTICS:
        sc = score(components)
        decomp = sc["score_decomposition"]
        recomputed = recompute_composite_from_decomposition(decomp)
        assert abs(recomputed - sc["strategy_score"]) < 1e-6
        assert decomp["reproducible"] is True
        assert abs(decomp["residual"]) < 1e-6


def test_decomposition_ordering_parity():
    """Leaderboard order must equal the order produced by sorting on composites
    reconstructed purely from the persisted decompositions."""
    scored = [score(c) for c in _FIXTURE_TACTICS]
    rows = [{"strategy_score": sc["strategy_score"], "score_decomposition": sc["score_decomposition"]}
            for sc in scored]
    stored_order = [r["strategy_score"] for r in rank(rows)]
    reconstructed_order = sorted(
        (recompute_composite_from_decomposition(r["score_decomposition"]) for r in rows), reverse=True,
    )
    assert stored_order == reconstructed_order


def test_decomposition_missing_data_honesty():
    """A missing component (overfit never walk-forward-validated) must be recorded
    as missing-with-reason, NOT silently substituted with 0.0."""
    sc = score({"excess_return_vs_spy": 0.05, "has_research": True})
    overfit_entry = sc["score_decomposition"]["components"]["overfit"]
    assert overfit_entry["missing"] is True
    assert overfit_entry["raw"] is None          # not 0.0 — measured-zero vs not-measured
    assert overfit_entry["normalized"] is None   # not 0.0
    assert overfit_entry["missing_reason"]
    assert overfit_entry["raw"] != 0.0
    # The unchanged fallback behavior (audit-flagged, gated separately) still applies
    # to the actual contribution/score math — only raw/normalized are marked missing.
    assert overfit_entry["contribution"] == 0.0

    # A tactic where overfit genuinely was measured as 0.0 must NOT look "missing".
    sc_measured = score({"excess_return_vs_spy": 0.05, "has_research": True, "overfit": 0.0})
    measured_entry = sc_measured["score_decomposition"]["components"]["overfit"]
    assert measured_entry["missing"] is False
    assert measured_entry["raw"] == 0.0
    assert measured_entry["missing_reason"] is None


def test_decomposition_no_change_guard():
    """Scores and ranking must be byte-identical to the pre-decomposition formula
    for the same inputs — this change is artifact-only, no score/rank value moves."""
    import portfolio_automation.portfolio_sim.strategy_score as ss

    def _reference(components):
        """Independent re-implementation of the pre-existing `total` formula,
        used only to guard that `score()`'s math did not change."""
        w = ss.DEFAULT_WEIGHTS
        excess = float(components.get("excess_return_vs_spy", 0.0))
        pbeat = float(components.get("probability_beat_spy", 0.0))
        drawdown = float(components.get("drawdown", 0.0))
        consistency = float(components.get("consistency", 0.0))
        has_research = 1.0 if components.get("has_research") else 0.0
        turnover = float(components.get("turnover", 0.0))
        tax_drag = float(components.get("tax_drag", 0.0))
        concentration = float(components.get("concentration", 0.0))
        leverage = float(components.get("leverage", 0.0))
        overfit = components.get("overfit")
        overfit_val = 0.0 if overfit is None else max(0.0, float(overfit))
        total = (
            w["excess_return_vs_spy"] * excess
            + w["probability_beat_spy_bonus"] * (pbeat - 0.5) * 2
            + w["drawdown_control_bonus"] * (1.0 + drawdown)
            + w["consistency_bonus"] * consistency
            + w["research_support_bonus"] * has_research
            - w["turnover_penalty"] * turnover
            - w["tax_drag_penalty"] * tax_drag
            - w["concentration_penalty"] * concentration
            - w["leverage_penalty"] * leverage
            - w["overfit_penalty"] * overfit_val
        )
        return round(total, 4)

    scores = [score(c)["strategy_score"] for c in _FIXTURE_TACTICS]
    reference_scores = [_reference(c) for c in _FIXTURE_TACTICS]
    assert scores == reference_scores

    rows = [{"strategy_score": s} for s in scores]
    ref_rows = [{"strategy_score": s} for s in reference_scores]
    assert [r["strategy_score"] for r in rank(rows)] == [r["strategy_score"] for r in rank(ref_rows)]
