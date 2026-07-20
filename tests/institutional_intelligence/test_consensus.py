"""Phase 8 tests — consensus, crowding, artifact envelope.

Consensus: independent support, correlated-manager discount (parent + archetype
cluster), market-maker discount, opposing managers, crowding (crowded != more
bullish), stale-filing warning, duplicate-organization discount, insufficient
data. Crowding: dual-natured. Artifacts: full invariant envelope + status states.
"""

from __future__ import annotations

from portfolio_automation.institutional_intelligence import artifact_writer as aw
from portfolio_automation.institutional_intelligence import consensus as cons
from portfolio_automation.institutional_intelligence import crowding as cr


def _m(mid, score, *, archetype="value", clone=0.8, mm=False, opt=False,
       stale=False, amend=False, parent=None, age=20, dq=1.0):
    return cons.ManagerConsensusInput(
        internal_id=mid, archetype=archetype, cloneability=clone, final_score=score,
        filing_age_days=age, data_quality=dq, market_maker=mm, options_dominated=opt,
        is_stale=stale, is_amended=amend, parent_org=parent)


def test_independent_support_accumulation():
    # 3 genuinely independent, distinct-archetype supporters -> accumulation.
    managers = [_m("a", 0.6, archetype="value"),
                _m("b", 0.55, archetype="quality_compounder"),
                _m("c", 0.5, archetype="sector_specialist")]
    c = cons.build_symbol_consensus("BE", managers)
    assert c.consensus_state in (cons.STATE_STRONG_ACCUM, cons.STATE_MODERATE_ACCUM,
                                 cons.STATE_CROWDED_ACCUM)
    assert c.consensus_score > 0 and c.effective_independent_managers >= 1.5
    assert c.supporting_count == 3


def test_correlated_cluster_discounted():
    # 4 supporters, all SAME archetype => low effective-independent count.
    same = [_m(f"m{i}", 0.6, archetype="value") for i in range(4)]
    diverse = [_m("a", 0.6, archetype="value"), _m("b", 0.6, archetype="activist"),
               _m("c", 0.6, archetype="macro_multistrategy", clone=0.2),
               _m("d", 0.6, archetype="sector_specialist")]
    c_same = cons.build_symbol_consensus("X", same)
    c_div = cons.build_symbol_consensus("Y", diverse)
    assert c_same.effective_independent_managers < c_div.effective_independent_managers


def test_same_parent_org_discounted():
    twins = [_m("a", 0.6, parent="BigCo"), _m("b", 0.6, parent="BigCo"),
             _m("c", 0.6, parent="BigCo")]
    indep = [_m("a", 0.6, parent="One"), _m("b", 0.6, parent="Two"),
             _m("c", 0.6, parent="Three")]
    assert (cons.build_symbol_consensus("X", twins).effective_independent_managers
            < cons.build_symbol_consensus("Y", indep).effective_independent_managers)


def test_market_maker_discounted():
    mm = [_m("mm", 0.6, mm=True, clone=0.2, archetype="macro_multistrategy")]
    reg = [_m("r", 0.6, mm=False, clone=0.8)]
    assert (cons.build_symbol_consensus("X", mm).effective_independent_managers
            < cons.build_symbol_consensus("Y", reg).effective_independent_managers)


def test_opposing_managers_reduce_score():
    mixed = [_m("a", 0.7, archetype="value"), _m("b", 0.6, archetype="activist"),
             _m("c", -0.7, archetype="sector_specialist"),
             _m("d", -0.6, archetype="quality_compounder")]
    c = cons.build_symbol_consensus("X", mixed)
    assert c.opposing_count == 2
    assert c.consensus_state in (cons.STATE_MIXED, cons.STATE_NEUTRAL)
    assert c.disagreement_score > 0


def test_crowding_is_dual_natured_not_more_bullish():
    # 8 supporters -> crowded. State is crowded_accumulation with a caution
    # warning, NOT a higher score than a clean accumulation.
    many = [_m(f"m{i}", 0.6, archetype=["value", "activist", "sector_specialist",
                                        "quality_compounder"][i % 4],
               parent=f"p{i}") for i in range(8)]
    c = cons.build_symbol_consensus("CROWD", many)
    assert c.crowding_score >= cr.CROWDED_THRESHOLD
    assert c.consensus_state == cons.STATE_CROWDED_ACCUM
    assert any("crowded" in w for w in c.warnings)


def test_stale_filings_warning():
    stale = [_m("a", 0.6, age=140), _m("b", 0.6, age=150, archetype="activist"),
             _m("c", 0.6, age=145, archetype="sector_specialist")]
    c = cons.build_symbol_consensus("X", stale)
    assert any("stale" in w for w in c.warnings)


def test_insufficient_data():
    # single low-cloneability manager -> below min effective / confidence
    c = cons.build_symbol_consensus("X", [_m("solo", 0.6, clone=0.1,
                                             archetype="macro_multistrategy")])
    assert c.consensus_state == cons.STATE_INSUFFICIENT
    assert c.reasons


def test_empty_managers_insufficient():
    c = cons.build_symbol_consensus("X", [])
    assert c.consensus_state == cons.STATE_INSUFFICIENT


# --- crowding ------------------------------------------------------------

def test_crowding_score_bounds_and_correlation():
    assert cr.crowding_score(supporting_count=0, effective_independent=0.0) == 0.0
    lonely = cr.crowding_score(supporting_count=1, effective_independent=1.0)
    correlated = cr.crowding_score(supporting_count=8, effective_independent=1.5)
    independent = cr.crowding_score(supporting_count=8, effective_independent=7.5)
    assert 0.0 <= lonely <= correlated <= 1.0
    # A correlated crowd scores at least as crowded/risky as an independent one.
    assert correlated >= independent


# --- artifacts -----------------------------------------------------------

def test_envelope_invariants():
    env = aw.envelope(generated_at="2026-05-15T00:00:00Z", data_as_of="2026-05-15",
                      source="institutional_intelligence")
    assert env["feeds_decision_engine"] is False
    assert env["observe_only"] is True and env["no_trade"] is True
    assert env["simulation_active"] is True and env["production_gated"] is True
    assert env["sandbox_only"] is True
    assert env["source_limitations"] and any("delayed" in s for s in env["source_limitations"])


def test_status_states():
    rec_ok = [{"filing_age_days": 20, "consensus_confidence": 0.7,
               "consensus_state": "moderate_accumulation", "warnings": []}]
    rec_stale = [{"filing_age_days": 200, "consensus_confidence": 0.7,
                  "consensus_state": "moderate_accumulation", "warnings": []}]
    common = dict(stale_after_days=140, min_confidence=0.55)
    assert aw.determine_status(enabled=False, failed=False, records=[], **common) == aw.STATUS_DISABLED
    assert aw.determine_status(enabled=True, failed=True, records=rec_ok, **common) == aw.STATUS_FAILED
    assert aw.determine_status(enabled=True, failed=False, records=[], **common) == aw.STATUS_INSUFFICIENT
    assert aw.determine_status(enabled=True, failed=False, records=rec_stale, **common) == aw.STATUS_STALE
    assert aw.determine_status(enabled=True, failed=False, records=rec_ok, **common) == aw.STATUS_OK


def test_no_new_filings_run_not_failed():
    # A valid run with zero symbols is insufficient_data, never failed.
    s = aw.determine_status(enabled=True, failed=False, records=[],
                            stale_after_days=140, min_confidence=0.55)
    assert s == aw.STATUS_INSUFFICIENT and s != aw.STATUS_FAILED


def test_symbol_record_shape():
    c = cons.build_symbol_consensus("BE", [
        cons.ManagerConsensusInput("a", "value", 0.8, 0.6, filing_age_days=24),
        cons.ManagerConsensusInput("b", "activist", 0.7, 0.55, filing_age_days=30),
        cons.ManagerConsensusInput("c", "sector_specialist", 0.8, 0.5, filing_age_days=18),
    ])
    from dataclasses import asdict
    rec = aw.build_symbol_record(symbol="BE", as_of="2026-05-15",
                                 consensus=asdict(c), latest_report_period="2026-03-31",
                                 filing_age_days=24)
    assert rec["symbol"] == "BE" and rec["filing_age_days"] == 24
    assert rec["consensus_state"] == c.consensus_state
    assert "manager_signals" in rec and "evidence_refs" in rec
