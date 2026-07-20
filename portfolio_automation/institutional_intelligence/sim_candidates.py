"""
Simulation-governance candidates for institutional signals.

Produces candidates for the ACTIVE simulation lane WITHOUT bypassing governance.
Every candidate carries the authority invariants the sim-governance auto-approval
gates require: ``target_lane="simulation"``, ``production_mutation=False``,
``feeds_decision_engine=False``, ``is_human_approved=False``.

Classification:
  * ``institutional_advisory_context`` / ``institutional_watchlist_context`` are
    DISPLAY-ONLY (observe-only annotations). They never become pending proposals
    and never accumulate an approval backlog.
  * ``institutional_watchlist_rank`` / ``institutional_risk_overlay`` are
    BEHAVIOR-AFFECTING (persistent rerank / risk rule) and stay GATED — a
    proposal is emitted only on a MATERIAL state transition.
  * ``institutional_strategy_profile`` is eligible for the existing simulation-
    only strategy auto-approval path (within its bounds). Production promotion
    still requires the human-gated approval path — no new production mutation.

Stable candidate IDs are derived from (proposal_type, symbol, filing/accession
identity, material score band). A quarterly disclosure therefore changes a
candidate ID only when a new filing/amendment creates a material band transition
— unchanged filings across daily runs produce the SAME id and do not spawn
duplicate proposals.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

# Proposal types.
PROP_ADVISORY_CONTEXT = "institutional_advisory_context"
PROP_WATCHLIST_CONTEXT = "institutional_watchlist_context"
PROP_WATCHLIST_RANK = "institutional_watchlist_rank"
PROP_RISK_OVERLAY = "institutional_risk_overlay"
PROP_STRATEGY_PROFILE = "institutional_strategy_profile"

# Display-only (never a pending proposal / backlog).
DISPLAY_ONLY_TYPES = frozenset({PROP_ADVISORY_CONTEXT, PROP_WATCHLIST_CONTEXT})
# Behavior-affecting -> gated; proposal only on material transition.
GATED_TYPES = frozenset({PROP_WATCHLIST_RANK, PROP_RISK_OVERLAY})
# Simulation-only strategy auto-approval eligible (still human-gated for prod).
STRATEGY_TYPES = frozenset({PROP_STRATEGY_PROFILE})

# Material score bands (transition between these = a material change).
_STRONG_POS, _POS, _NEG, _STRONG_NEG = 0.50, 0.20, -0.20, -0.50


def material_score_band(score: float) -> str:
    if score >= _STRONG_POS:
        return "strong_pos"
    if score >= _POS:
        return "pos"
    if score <= _STRONG_NEG:
        return "strong_neg"
    if score <= _NEG:
        return "neg"
    return "neutral"


def make_candidate_id(proposal_type: str, symbol: str, accession: str,
                      band: str) -> str:
    """Deterministic id — same inputs => same id (dedup across daily runs)."""
    raw = f"{proposal_type}|{symbol}|{accession or '-'}|{band}"
    return "inst_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def institutional_rank_hint(consensus: dict) -> float:
    """A SEPARATE institutional rank score (NOT daily crowd velocity).

    Blends consensus score/confidence, effective-independent count, strategy fit,
    freshness (from filing age), inverse crowding, inverse price staleness, and
    data quality. Bounded [0,1]. A quarterly signal should move rank only on a
    new filing/amendment (the caller gates on material transition).
    """
    def c01(x):
        return max(0.0, min(1.0, float(x)))

    score = abs(float(consensus.get("consensus_score") or 0.0))
    conf = c01(consensus.get("consensus_confidence") or 0.0)
    eff = c01((consensus.get("effective_independent_managers") or 0.0) / 3.0)
    fit = c01(consensus.get("strategy_fit") if consensus.get("strategy_fit") is not None else 0.5)
    age = consensus.get("filing_age_max")
    freshness = 1.0 if age is None else c01(1.0 - (age / 160.0))
    inv_crowd = c01(1.0 - (consensus.get("crowding_score") or 0.0))
    inv_stale = c01(1.0 - (consensus.get("price_staleness_penalty") or 0.0))
    dq = c01(consensus.get("data_quality") if consensus.get("data_quality") is not None else 1.0)
    return round(c01(0.25 * score + 0.2 * conf + 0.2 * eff + 0.1 * fit
                     + 0.1 * freshness + 0.075 * inv_crowd + 0.05 * inv_stale
                     + 0.025 * dq), 4)


@dataclass(frozen=True)
class InstitutionalCandidate:
    candidate_id: str
    proposal_type: str
    symbol: str
    target_lane: str
    production_mutation: bool
    feeds_decision_engine: bool
    is_human_approved: bool
    display_only: bool
    ready_for_production_review: bool
    material_transition: bool
    material_band: str
    rank_hint: float
    what_changed: str
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id, "proposal_type": self.proposal_type,
            "symbol": self.symbol, "target_lane": self.target_lane,
            "production_mutation": self.production_mutation,
            "feeds_decision_engine": self.feeds_decision_engine,
            "is_human_approved": self.is_human_approved,
            "display_only": self.display_only,
            "ready_for_production_review": self.ready_for_production_review,
            "material_transition": self.material_transition,
            "material_band": self.material_band, "rank_hint": self.rank_hint,
            "what_changed": self.what_changed, "warnings": list(self.warnings),
        }


def _candidate(proposal_type: str, symbol: str, accession: str, band: str,
               *, material_transition: bool, rank_hint: float,
               what_changed: str) -> InstitutionalCandidate:
    display_only = proposal_type in DISPLAY_ONLY_TYPES
    # Behavior-affecting types are ready for (human) production review only on a
    # material transition; display-only types are never proposed for production.
    ready = (not display_only) and material_transition
    return InstitutionalCandidate(
        candidate_id=make_candidate_id(proposal_type, symbol, accession, band),
        proposal_type=proposal_type, symbol=symbol,
        target_lane="simulation", production_mutation=False,
        feeds_decision_engine=False, is_human_approved=False,
        display_only=display_only, ready_for_production_review=ready,
        material_transition=material_transition, material_band=band,
        rank_hint=rank_hint, what_changed=what_changed)


def build_candidates(
    symbol: str,
    consensus: dict,
    *,
    accession: str,
    prior_band: str | None = None,
    strategy_enabled: bool = False,
) -> list[InstitutionalCandidate]:
    """Build the institutional sim candidates for one symbol's consensus."""
    band = material_score_band(float(consensus.get("consensus_score") or 0.0))
    transition = prior_band is not None and prior_band != band
    rank = institutional_rank_hint(consensus)
    what = (f"consensus band {prior_band}->{band}" if transition
            else f"consensus band {band} (unchanged)")

    cands = [
        _candidate(PROP_ADVISORY_CONTEXT, symbol, accession, band,
                   material_transition=transition, rank_hint=rank, what_changed=what),
        _candidate(PROP_WATCHLIST_CONTEXT, symbol, accession, band,
                   material_transition=transition, rank_hint=rank, what_changed=what),
    ]
    # Behavior-affecting candidates only when a material transition occurred.
    if transition:
        cands.append(_candidate(PROP_WATCHLIST_RANK, symbol, accession, band,
                                material_transition=True, rank_hint=rank, what_changed=what))
        cands.append(_candidate(PROP_RISK_OVERLAY, symbol, accession, band,
                                material_transition=True, rank_hint=rank, what_changed=what))
    if strategy_enabled and transition:
        cands.append(_candidate(PROP_STRATEGY_PROFILE, symbol, accession, band,
                                material_transition=True, rank_hint=rank, what_changed=what))
    return cands
