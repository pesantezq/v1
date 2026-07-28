"""E5 probes -- meaningful population & nonzero variance.

Scenario 1  -- an artifact exists but contains zero meaningful records.
Scenario 4  -- a leaderboard ranks while all scores are equal.
Scenario 13 -- an experiment runs with no admissible input for weeks.

All three probes call REAL repo functions (not re-implementations) and, for
the fixed defects, reproduce the pre-fix behaviour inline to prove the probe
would have failed then and passes now.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from portfolio_automation.sim_governance import daily_governance_run as DGR
from portfolio_automation.universe_sanitation import _diagnose_ranking

from tests.probes.assertions import (
    assert_meaningful_population,
    assert_nonzero_variance,
)

# ---------------------------------------------------------------------------
# Scenario 13 -- F13.1: daily_governance_run._enrich_baseline key mismatch
# (FIXED on main, b013b24c). Ships gated OFF by default.
# ---------------------------------------------------------------------------


def _promotion_candidates_artifact(n_monitor: int, n_other: int) -> dict:
    """A realistic automatic_promotion_candidates.json shape: real container
    key is "decisions" (automatic_promotion_governance._report_to_dict), not
    "candidates"."""
    rows = [
        {"ticker": f"SYM{i}", "proposed_status": "MONITOR",
         "corroboration_score": 0.6, "catalyst_flags": [], "risk_flags": []}
        for i in range(n_monitor)
    ] + [
        {"ticker": f"REJ{i}", "proposed_status": "REJECTED",
         "corroboration_score": 0.1, "catalyst_flags": [], "risk_flags": []}
        for i in range(n_other)
    ]
    return {"decisions": rows}


def _write_promo(root: Path, payload: dict) -> None:
    d = root / "outputs" / "sandbox" / "discovery"
    d.mkdir(parents=True, exist_ok=True)
    (d / "automatic_promotion_candidates.json").write_text(json.dumps(payload), encoding="utf-8")


def test_enrich_baseline_populates_real_candidates_when_gate_enabled(tmp_path):
    """Fixed behaviour: with the WS13 gate enabled, real MONITOR decisions
    become discovery_candidates -- a meaningful, non-empty population."""
    _write_promo(tmp_path, _promotion_candidates_artifact(n_monitor=6, n_other=3))
    cfg = {"experiments": {"watchlist_discovery_adds_enabled": True}}
    baseline = DGR._enrich_baseline(tmp_path, {}, cfg=cfg)

    assert_meaningful_population(
        baseline["discovery_candidates"], min_count=1,
        context="daily_governance_run.discovery_candidates (gate enabled, real decisions)")
    assert len(baseline["discovery_candidates"]) == 6


def test_enrich_baseline_key_mismatch_would_have_produced_zero_forever(tmp_path):
    """Verify-by-construction: reproduce the EXACT pre-fix defect (reading
    the wrong container key, "candidates" instead of "decisions") against
    the SAME real artifact used above. The old code returned 0 candidates no
    matter how many real decisions the producer emitted -- confirmed here to
    fail the population probe -- which is precisely how the bug went
    unnoticed for its entire existence (F13.1: "ran successfully with no
    admissible input for six weeks" is a real prior incident, not a
    hypothetical)."""
    payload = _promotion_candidates_artifact(n_monitor=6, n_other=3)
    _write_promo(tmp_path, payload)

    # Pre-fix container-key read (function's ORIGINAL implementation, verbatim
    # shape): `.get("candidates", [])` against a real "decisions"-keyed file.
    promo = json.loads((tmp_path / "outputs" / "sandbox" / "discovery"
                        / "automatic_promotion_candidates.json").read_text())
    pre_fix_rows = promo.get("candidates", [])  # the bug

    with pytest.raises(AssertionError, match="functionally empty"):
        assert_meaningful_population(
            pre_fix_rows, min_count=1,
            context="daily_governance_run.discovery_candidates (PRE-FIX key mismatch)")

    # And confirm the fix (real function, same artifact) does NOT reproduce
    # the zero-population defect.
    cfg = {"experiments": {"watchlist_discovery_adds_enabled": True}}
    baseline = DGR._enrich_baseline(tmp_path, {}, cfg=cfg)
    assert len(baseline["discovery_candidates"]) == 6


def test_enrich_baseline_default_gate_off_is_honestly_empty_not_silently_broken(tmp_path):
    """Default-OFF gate state: population is legitimately empty (operator has
    not opted in) and the artifact says so explicitly via `_experiment_gates`
    -- distinct from the pre-fix case, where the population was silently
    empty regardless of any gate or of how much real data existed."""
    _write_promo(tmp_path, _promotion_candidates_artifact(n_monitor=6, n_other=3))
    baseline = DGR._enrich_baseline(tmp_path, {})  # cfg=None -> gate defaults off
    assert baseline["discovery_candidates"] == []
    assert baseline["_experiment_gates"]["watchlist_discovery_adds"]["enabled"] is False
    assert baseline["_experiment_gates"]["watchlist_discovery_adds"]["reason"] == \
        "config_disabled_default"


# ---------------------------------------------------------------------------
# Scenario 4 -- F9.1: universe_sanitation degenerate-ranking diagnostic
# (FIXED on main, f30433b1).
# ---------------------------------------------------------------------------


def _candidate(symbol: str, score: float, *, sources=("static",), theme_conf=0.0,
              hit_rate=None) -> dict:
    return {"symbol": symbol, "score": score, "sources": list(sources),
            "theme_confidence_max": theme_conf, "recent_hit_rate_1d": hit_rate}


_SCORE_WEIGHTS = {"sources_presence": 0.3, "theme_confidence": 0.3,
                  "recent_hit_rate": 0.2, "fmp_top100_presence": 0.2}


def test_diagnose_ranking_flags_zero_variance_all_tied():
    """31-row universe, every score identical (the real 2026-07-28 shape:
    17/31 rows tied at 0.16) -- the diagnostic must flag it, and the score
    column itself must fail the general nonzero-variance probe."""
    candidates = [_candidate(f"S{i:02d}", 0.16) for i in range(31)]
    diag = _diagnose_ranking(candidates, _SCORE_WEIGHTS)

    assert diag["degenerate_ranking"] is True
    assert diag["zero_variance"] is True
    assert diag["alphabetical_tiebreak_detected"] is True

    with pytest.raises(AssertionError, match="does not discriminate"):
        assert_nonzero_variance(
            [c["score"] for c in candidates], min_distinct=2, min_sample=1,
            context="top100_daily.score (all tied)")


def test_diagnose_ranking_partial_tie_still_flagged_degenerate():
    """The real production shape: NOT fully zero-variance (8 distinct score
    buckets across 31 rows) but 17/31 (55%) tie exactly at the top bucket and
    fall back to alphabetical order within that group -- must still be
    flagged degenerate even though `assert_nonzero_variance` alone (which
    only checks the WHOLE column) would not catch a partial dominant-tie."""
    tied = [_candidate(f"S{i:02d}", 0.16) for i in range(17)]
    varied = [_candidate(f"V{i:02d}", 0.16 + i * 0.05) for i in range(1, 15)]
    candidates = tied + varied
    diag = _diagnose_ranking(candidates, _SCORE_WEIGHTS)

    assert diag["degenerate_ranking"] is True
    assert diag["largest_tie_fraction"] >= 0.5
    assert diag["alphabetical_tiebreak_detected"] is True
    # The full column DOES discriminate (8 distinct buckets) -- confirms the
    # tie-group diagnostic catches something the plain variance check alone
    # would miss.
    assert diag["distinct_score_count"] > 1


def test_diagnose_ranking_pre_fix_had_no_diagnostic_at_all():
    """Verify-by-construction: before WS9, nothing computed
    `ranking_diagnostics` at all -- a naive pre-fix consumer that only
    checked "the payload has rows" would read the exact degenerate,
    alphabetically-collapsed 31-row universe above as healthy. Reproduce
    that naive check here and show it is blind to the defect the real
    diagnostic (still called on the same input above) catches."""
    candidates = [_candidate(f"S{i:02d}", 0.16) for i in range(31)]

    def _pre_fix_naive_health(cands: list[dict]) -> str:
        return "GREEN" if len(cands) > 0 else "AMBER"

    assert _pre_fix_naive_health(candidates) == "GREEN"  # the bug: blind to degeneracy
    diag = _diagnose_ranking(candidates, _SCORE_WEIGHTS)
    assert diag["degenerate_ranking"] is True  # the fix: same input, correctly caught


def test_diagnose_ranking_healthy_universe_is_not_flagged():
    """Negative control: a genuinely discriminative universe must NOT be
    flagged degenerate (the diagnostic must not cry wolf)."""
    candidates = [_candidate(f"S{i:02d}", round(0.1 + i * 0.03, 4),
                             sources=("static", "fmp_top100") if i % 2 else ("static",),
                             theme_conf=0.1 * i, hit_rate=0.1 * (i % 5))
                 for i in range(20)]
    diag = _diagnose_ranking(candidates, _SCORE_WEIGHTS)
    assert diag["degenerate_ranking"] is False
    assert_nonzero_variance([c["score"] for c in candidates], min_distinct=2,
                            context="top100_daily.score (healthy)")


# ---------------------------------------------------------------------------
# Scenario 1 -- generic: an artifact "runs" but is functionally empty.
# ---------------------------------------------------------------------------


def test_generic_population_probe_catches_zero_record_artifact():
    """The generic shared helper, applied with no repo-specific knowledge --
    demonstrates it is reusable beyond the two concrete cases above."""
    artifact = {"generated_at": "2026-07-28T09:00:00+00:00", "status": "ok", "rows": []}
    with pytest.raises(AssertionError, match="functionally empty"):
        assert_meaningful_population(artifact["rows"], min_count=1, context="generic_artifact.rows")


def test_generic_population_probe_ignores_placeholder_entries_via_predicate():
    """A population of N placeholder/None entries must not count as
    'meaningful' just because len() > 0 -- exercises the predicate hook."""
    rows = [None, None, {"symbol": "AAPL"}, None]
    assert_meaningful_population(rows, min_count=1, predicate=lambda r: r is not None,
                                 context="generic_artifact.rows (with placeholders)")
    with pytest.raises(AssertionError):
        assert_meaningful_population(rows, min_count=2, predicate=lambda r: r is not None,
                                     context="generic_artifact.rows (insufficient real rows)")
