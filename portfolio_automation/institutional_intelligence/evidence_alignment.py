"""
Evidence alignment — an ADDITIVE layer joining retail attention, FMP market
context, and institutional (13F) positioning into a higher-level agreement view.

Strict additivity rules (enforced by tests):
  * Institutional positioning is a SEPARATE evidence dimension. It is NEVER
    injected into normalization.WEIGHTS, the crowd score, or the existing
    cross_source_confirmation / cross_source_divergence / retail_vs_fmp metrics
    (those keep their retail-vs-market-context meaning).
  * Missing institutional data must NOT lower the existing crowd score — it
    yields ``institutional_alignment == "unknown"`` and nothing else.

This module produces the optional ``institutional_*`` context fields and an
``evidence_alignment`` block that a downstream unified consumer can attach
additively to a per-symbol row.
"""

from __future__ import annotations

from typing import Any

from . import consensus as cons

# Three-way alignment states.
ALIGN_THREE_WAY_SUPPORT = "three_way_support"
ALIGN_RETAIL_MARKET_INST_UNKNOWN = "retail_market_support_institutional_unknown"
ALIGN_INST_SUPPORT_MARKET_QUIET = "institutional_support_market_quiet"
ALIGN_INST_DIST_AGAINST_ATTENTION = "institutional_distribution_against_attention"
ALIGN_CROWDED_THREE_WAY = "crowded_three_way_support"
ALIGN_MIXED = "mixed_evidence"
ALIGN_INSUFFICIENT = "insufficient_data"

_ACCUM_STATES = {cons.STATE_STRONG_ACCUM, cons.STATE_MODERATE_ACCUM,
                 cons.STATE_CROWDED_ACCUM}
_DIST_STATES = {cons.STATE_STRONG_DIST, cons.STATE_MODERATE_DIST,
                cons.STATE_CROWDED_DIST}


def institutional_fields(consensus: dict[str, Any] | None,
                         *, manager_count: int = 0,
                         options_ambiguity: float | None = None,
                         evidence_refs: list[str] | None = None) -> dict[str, Any]:
    """Optional institutional_* context fields (all default to None/unknown when
    no institutional data is present — additive, never subtractive)."""
    if not consensus:
        return {
            "institutional_positioning_score": None,
            "institutional_positioning_confidence": None,
            "institutional_consensus_state": None,
            "institutional_filing_age_days": None,
            "institutional_crowding_score": None,
            "institutional_strategy_fit": None,
            "institutional_manager_count": 0,
            "institutional_effective_independent_count": None,
            "institutional_options_ambiguity": None,
            "institutional_warnings": [],
            "institutional_evidence_refs": [],
        }
    return {
        "institutional_positioning_score": consensus.get("consensus_score"),
        "institutional_positioning_confidence": consensus.get("consensus_confidence"),
        "institutional_consensus_state": consensus.get("consensus_state"),
        "institutional_filing_age_days": consensus.get("filing_age_max"),
        "institutional_crowding_score": consensus.get("crowding_score"),
        "institutional_strategy_fit": consensus.get("strategy_fit"),
        "institutional_manager_count": manager_count,
        "institutional_effective_independent_count":
            consensus.get("effective_independent_managers"),
        "institutional_options_ambiguity": options_ambiguity,
        "institutional_warnings": list(consensus.get("warnings") or []),
        "institutional_evidence_refs": list(evidence_refs or []),
    }


def compute_evidence_alignment(
    *,
    retail_supports: bool,
    market_context_supports: bool,
    institutional_consensus: dict[str, Any] | None,
) -> dict[str, Any]:
    """Higher-level agreement across the three evidence dimensions.

    ``retail_supports`` / ``market_context_supports`` come from the EXISTING
    crowd lanes (unchanged). Institutional is optional; when absent its alignment
    is 'unknown' and never degrades the retail/market read.
    """
    disagreement_flags: list[str] = []

    retail_market_alignment = (
        "aligned" if retail_supports and market_context_supports
        else "retail_only" if retail_supports
        else "market_only" if market_context_supports
        else "quiet")

    inst_state = (institutional_consensus or {}).get("consensus_state")
    inst_conf = (institutional_consensus or {}).get("consensus_confidence")
    inst_available = bool(institutional_consensus) and inst_state not in (
        None, cons.STATE_INSUFFICIENT)

    if not inst_available:
        institutional_alignment = "unknown"
    elif inst_state in _ACCUM_STATES:
        institutional_alignment = "accumulation"
    elif inst_state in _DIST_STATES:
        institutional_alignment = "distribution"
    else:
        institutional_alignment = "neutral"

    crowded = inst_state in (cons.STATE_CROWDED_ACCUM, cons.STATE_CROWDED_DIST)

    # Three-way.
    if not inst_available:
        if retail_supports or market_context_supports:
            three_way = ALIGN_RETAIL_MARKET_INST_UNKNOWN
        else:
            three_way = ALIGN_INSUFFICIENT
    elif institutional_alignment == "accumulation":
        if retail_supports and market_context_supports:
            three_way = ALIGN_CROWDED_THREE_WAY if crowded else ALIGN_THREE_WAY_SUPPORT
        elif not retail_supports and not market_context_supports:
            three_way = ALIGN_INST_SUPPORT_MARKET_QUIET
        else:
            three_way = ALIGN_MIXED
    elif institutional_alignment == "distribution":
        if retail_supports or market_context_supports:
            three_way = ALIGN_INST_DIST_AGAINST_ATTENTION
            disagreement_flags.append("institutional_distribution_vs_attention")
        else:
            three_way = ALIGN_MIXED
    else:
        three_way = ALIGN_MIXED

    return {
        "retail_market_alignment": retail_market_alignment,
        "institutional_alignment": institutional_alignment,
        "three_way_alignment": three_way,
        "institutional_confidence": inst_conf,
        "disagreement_flags": disagreement_flags,
    }
