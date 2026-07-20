"""Phase 10 tests — additive evidence alignment.

Asserts: optional institutional_* fields, three-way alignment states, and the
critical additive invariants — missing institutional data yields
institutional_alignment='unknown' (never degrades retail/market), and this layer
never references the crowd score or cross_source metrics.
"""

from __future__ import annotations

from portfolio_automation.institutional_intelligence import consensus as cons
from portfolio_automation.institutional_intelligence import evidence_alignment as ea


def _consensus(state, conf=0.7):
    return {"consensus_state": state, "consensus_score": 0.5, "consensus_confidence": conf,
            "crowding_score": 0.3, "effective_independent_managers": 2.4,
            "filing_age_max": 30, "warnings": []}


def test_institutional_fields_absent_are_unknown_not_zeroed():
    f = ea.institutional_fields(None)
    assert f["institutional_positioning_score"] is None       # not 0.0
    assert f["institutional_consensus_state"] is None
    assert f["institutional_manager_count"] == 0
    assert f["institutional_warnings"] == []


def test_institutional_fields_present():
    f = ea.institutional_fields(_consensus(cons.STATE_MODERATE_ACCUM), manager_count=3)
    assert f["institutional_consensus_state"] == cons.STATE_MODERATE_ACCUM
    assert f["institutional_effective_independent_count"] == 2.4
    assert f["institutional_manager_count"] == 3


def test_missing_institutional_does_not_degrade_retail_market():
    # retail + market both support; institutional unknown -> alignment reflects
    # retail/market fully, institutional is 'unknown', NOT negative.
    a = ea.compute_evidence_alignment(retail_supports=True, market_context_supports=True,
                                      institutional_consensus=None)
    assert a["retail_market_alignment"] == "aligned"
    assert a["institutional_alignment"] == "unknown"
    assert a["three_way_alignment"] == ea.ALIGN_RETAIL_MARKET_INST_UNKNOWN
    assert a["disagreement_flags"] == []


def test_three_way_support():
    a = ea.compute_evidence_alignment(retail_supports=True, market_context_supports=True,
                                      institutional_consensus=_consensus(cons.STATE_MODERATE_ACCUM))
    assert a["three_way_alignment"] == ea.ALIGN_THREE_WAY_SUPPORT
    assert a["institutional_alignment"] == "accumulation"


def test_crowded_three_way():
    a = ea.compute_evidence_alignment(retail_supports=True, market_context_supports=True,
                                      institutional_consensus=_consensus(cons.STATE_CROWDED_ACCUM))
    assert a["three_way_alignment"] == ea.ALIGN_CROWDED_THREE_WAY


def test_institutional_support_market_quiet():
    a = ea.compute_evidence_alignment(retail_supports=False, market_context_supports=False,
                                      institutional_consensus=_consensus(cons.STATE_STRONG_ACCUM))
    assert a["three_way_alignment"] == ea.ALIGN_INST_SUPPORT_MARKET_QUIET


def test_institutional_distribution_against_attention():
    a = ea.compute_evidence_alignment(retail_supports=True, market_context_supports=False,
                                      institutional_consensus=_consensus(cons.STATE_STRONG_DIST))
    assert a["three_way_alignment"] == ea.ALIGN_INST_DIST_AGAINST_ATTENTION
    assert "institutional_distribution_vs_attention" in a["disagreement_flags"]


def test_all_quiet_insufficient():
    a = ea.compute_evidence_alignment(retail_supports=False, market_context_supports=False,
                                      institutional_consensus=None)
    assert a["three_way_alignment"] == ea.ALIGN_INSUFFICIENT


def test_insufficient_institutional_treated_as_unknown():
    a = ea.compute_evidence_alignment(retail_supports=True, market_context_supports=False,
                                      institutional_consensus=_consensus(cons.STATE_INSUFFICIENT))
    assert a["institutional_alignment"] == "unknown"


def test_module_never_references_crowd_score_or_weights():
    # Additive-invariant guard: the module's CODE (not its docstring) must not
    # use WEIGHTS / crowd_score / cross_source identifiers or import normalization.
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(ea))
    forbidden = {"WEIGHTS", "crowd_confidence", "cross_source_confirmation_score",
                 "cross_source_divergence_score", "retail_vs_fmp_attention_delta"}
    used_identifiers: set[str] = set()
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            used_identifiers.add(node.attr)
        elif isinstance(node, ast.Name):
            used_identifiers.add(node.id)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module or "")
    assert not (forbidden & used_identifiers), forbidden & used_identifiers
    assert not any("normalization" in m for m in imports)
