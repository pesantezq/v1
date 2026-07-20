"""Phase 13 tests — simulation-governance candidates + stable dedup.

Covers: authority invariants on every candidate, display-only context never
proposed, behavior-affecting rerank/overlay gated to material transitions,
stable candidate IDs (unchanged filing -> same id -> no duplicate proposal),
material band change -> new id + proposal, strategy_profile gated on flag +
transition, separate rank hint (not crowd velocity).
"""

from __future__ import annotations

from portfolio_automation.institutional_intelligence import sim_candidates as sc


def _consensus(score, conf=0.7, eff=2.4, crowding=0.2, age=30, fit=0.8):
    return {"consensus_score": score, "consensus_confidence": conf,
            "effective_independent_managers": eff, "crowding_score": crowding,
            "filing_age_max": age, "strategy_fit": fit, "data_quality": 1.0}


def test_material_score_band():
    assert sc.material_score_band(0.6) == "strong_pos"
    assert sc.material_score_band(0.3) == "pos"
    assert sc.material_score_band(0.0) == "neutral"
    assert sc.material_score_band(-0.3) == "neg"
    assert sc.material_score_band(-0.6) == "strong_neg"


def test_all_candidates_carry_authority_invariants():
    for c in sc.build_candidates("BE", _consensus(0.6), accession="acc1",
                                 prior_band="neutral", strategy_enabled=True):
        assert c.target_lane == "simulation"
        assert c.production_mutation is False
        assert c.feeds_decision_engine is False
        assert c.is_human_approved is False


def test_display_only_never_proposed():
    cands = sc.build_candidates("BE", _consensus(0.6), accession="acc1",
                                prior_band="neutral")
    ctx = [c for c in cands if c.proposal_type in sc.DISPLAY_ONLY_TYPES]
    assert ctx and all(c.display_only for c in ctx)
    assert all(c.ready_for_production_review is False for c in ctx)  # never a backlog


def test_no_transition_only_display_candidates():
    # unchanged band -> only the two display-only context candidates, no gated ones
    cands = sc.build_candidates("BE", _consensus(0.6), accession="acc1",
                                prior_band="strong_pos", strategy_enabled=True)
    types = {c.proposal_type for c in cands}
    assert types == {sc.PROP_ADVISORY_CONTEXT, sc.PROP_WATCHLIST_CONTEXT}
    assert all(not c.material_transition for c in cands)


def test_material_transition_emits_gated_candidates():
    cands = sc.build_candidates("BE", _consensus(0.6), accession="acc2",
                                prior_band="neutral", strategy_enabled=False)
    types = {c.proposal_type for c in cands}
    assert sc.PROP_WATCHLIST_RANK in types and sc.PROP_RISK_OVERLAY in types
    rank = next(c for c in cands if c.proposal_type == sc.PROP_WATCHLIST_RANK)
    assert rank.material_transition and rank.ready_for_production_review
    # strategy profile absent unless the flag is on
    assert sc.PROP_STRATEGY_PROFILE not in types


def test_strategy_profile_gated_on_flag_and_transition():
    on = sc.build_candidates("BE", _consensus(0.6), accession="acc3",
                             prior_band="neutral", strategy_enabled=True)
    assert any(c.proposal_type == sc.PROP_STRATEGY_PROFILE for c in on)
    # flag on but NO transition -> no strategy profile
    none = sc.build_candidates("BE", _consensus(0.6), accession="acc3",
                               prior_band="strong_pos", strategy_enabled=True)
    assert not any(c.proposal_type == sc.PROP_STRATEGY_PROFILE for c in none)


def test_stable_id_unchanged_filing_no_duplicate():
    # Same filing + same band across two daily runs -> identical candidate ids.
    run1 = sc.build_candidates("BE", _consensus(0.6), accession="acc1", prior_band="strong_pos")
    run2 = sc.build_candidates("BE", _consensus(0.6), accession="acc1", prior_band="strong_pos")
    assert [c.candidate_id for c in run1] == [c.candidate_id for c in run2]


def test_band_change_new_id():
    a = sc.make_candidate_id(sc.PROP_WATCHLIST_RANK, "BE", "acc1", "pos")
    b = sc.make_candidate_id(sc.PROP_WATCHLIST_RANK, "BE", "acc1", "strong_pos")
    assert a != b     # material band change -> new candidate id
    # different accession (new/amended filing) -> new id too
    c = sc.make_candidate_id(sc.PROP_WATCHLIST_RANK, "BE", "acc2", "pos")
    assert a != c


def test_rank_hint_bounded_and_not_velocity():
    r = sc.institutional_rank_hint(_consensus(0.8, conf=0.9, eff=3.0))
    assert 0.0 <= r <= 1.0
    # higher consensus/confidence/effective -> higher rank hint
    lo = sc.institutional_rank_hint(_consensus(0.2, conf=0.55, eff=1.5))
    assert r > lo
    # The hint is derived only from consensus fields — it takes no velocity/
    # crowd-attention input at all (its only argument is the consensus dict).
    import inspect
    assert list(inspect.signature(sc.institutional_rank_hint).parameters) == ["consensus"]
