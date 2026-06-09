"""Opportunity scoring (Phase 6, spec §8).

A deterministic, explainable opportunity model that is **distinct** from the
protected portfolio recommendation scores (it never reuses
``signal_score``/``confidence_score``/``effective_score``/``conviction_score``/
``final_rank_score``/``recommendation_score`` names). It scores candidates
surfaced by the universe scanner into a status the operator can review.

Hard rules (each a test):
* A high ``boom_score`` **alone** can never reach ``QUALIFIED`` /
  ``APPROVED_WATCHLIST_REVIEW`` — qualification additionally requires evidence
  and investability.
* Low investability caps the status at ``ACCESS_LIMITED`` (or
  ``PRIVATE_WATCH_ONLY`` for private candidates).
* Single-headline ideas (low evidence) cannot qualify — they need corroboration.
* Hype / crowding dominance routes to ``HYPE_NOISE``.
* Private companies are **never** treated as tradeable tickers.
* Penalties are surfaced in the output (explainable in the GUI).

Output carries ``observe_only: true``. This module computes scores only — it
trades nothing and writes no official recommendation.
"""
from __future__ import annotations

from typing import Any

from portfolio_automation.next_stage.contracts import (
    OpportunityScore, OpportunityStatus as S, CandidateType, AccessRoute,
)


def _clamp(x: float) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.0


def _mean(*vals: float) -> float:
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else 0.0


# Dimension weights for the headline opportunity_score (sum ≈ 1.0 before penalties).
_W = {
    "catalyst_strength": 0.16,
    "price_volume_confirmation": 0.12,
    "fundamental_support": 0.12,
    "market_regime_fit": 0.10,
    "portfolio_diversification_value": 0.10,
    "access_investability": 0.12,
    "risk_adjusted_timing": 0.10,
    "boom_potential": 0.06,      # deliberately small: boom alone must not drive status
    "evidence_quality": 0.07,
    "liquidity_quality": 0.03,
    "data_quality": 0.02,
}
_PENALTY_W = {
    "hype_penalty": 0.30,
    "crowded_trade_penalty": 0.25,
    "single_headline_penalty": 0.25,
    "portfolio_overlap_penalty": 0.20,
}

# Thresholds (explainable, tunable).
_QUALIFY_OPP = 0.60
_APPROVE_OPP = 0.70
_QUALIFY_EVIDENCE = 0.50
_QUALIFY_INVESTABILITY = 0.50
_LOW_INVESTABILITY = 0.30
_SINGLE_HEADLINE_BLOCK = 0.50
_HYPE_DOMINANT = 0.70
_PRIVATE_INVESTABILITY_OK = 0.60


def score_opportunity(candidate: dict[str, Any]) -> OpportunityScore:
    """Score one candidate dict → :class:`OpportunityScore`. Deterministic, pure."""
    g = lambda k: _clamp(candidate.get(k, 0.0))  # noqa: E731
    ctype = str(candidate.get("candidate_type", CandidateType.PUBLIC_TICKER.value))
    aroute = str(candidate.get("access_route", AccessRoute.WATCH_ONLY.value))

    dims = {k: g(k) for k in _W}
    pens = {k: g(k) for k in _PENALTY_W}

    evidence_score = _mean(dims["evidence_quality"], dims["data_quality"],
                           dims["price_volume_confirmation"])
    investability_score = _mean(dims["access_investability"], dims["liquidity_quality"])
    boom_score = round(_clamp(0.7 * dims["boom_potential"] + 0.3 * dims["catalyst_strength"]), 4)
    portfolio_fit_score = round(_clamp(
        _mean(dims["portfolio_diversification_value"], dims["market_regime_fit"])
        - pens["portfolio_overlap_penalty"] * 0.5), 4)
    risk_score = round(_mean(1.0 - dims["risk_adjusted_timing"], pens["hype_penalty"],
                             pens["crowded_trade_penalty"], pens["single_headline_penalty"]), 4)

    raw = sum(_W[k] * dims[k] for k in _W) - sum(_PENALTY_W[k] * pens[k] for k in _PENALTY_W)
    opportunity_score = round(_clamp(raw), 4)

    status = _classify(ctype, aroute, opportunity_score, evidence_score,
                       investability_score, boom_score, pens)

    return OpportunityScore(
        candidate=str(candidate.get("candidate", candidate.get("symbol", ""))),
        candidate_type=ctype, access_route=aroute,
        catalyst_strength=dims["catalyst_strength"],
        price_volume_confirmation=dims["price_volume_confirmation"],
        fundamental_support=dims["fundamental_support"],
        market_regime_fit=dims["market_regime_fit"],
        portfolio_diversification_value=dims["portfolio_diversification_value"],
        access_investability=dims["access_investability"],
        risk_adjusted_timing=dims["risk_adjusted_timing"],
        boom_potential=dims["boom_potential"],
        evidence_quality=dims["evidence_quality"],
        liquidity_quality=dims["liquidity_quality"],
        data_quality=dims["data_quality"],
        hype_penalty=pens["hype_penalty"],
        crowded_trade_penalty=pens["crowded_trade_penalty"],
        single_headline_penalty=pens["single_headline_penalty"],
        portfolio_overlap_penalty=pens["portfolio_overlap_penalty"],
        opportunity_score=opportunity_score, boom_score=boom_score,
        risk_score=risk_score, investability_score=investability_score,
        evidence_score=evidence_score, portfolio_fit_score=portfolio_fit_score,
        final_status=status.value,
    )


def _classify(ctype, aroute, opp, evidence, investability, boom, pens) -> S:
    # 1. Private companies are never tradeable tickers.
    if ctype == CandidateType.PRIVATE_IPO.value:
        has_public_route = aroute in (AccessRoute.ETF.value, AccessRoute.PUBLIC_SUPPLIER.value,
                                      AccessRoute.PROXY.value, AccessRoute.FUND.value)
        if has_public_route and investability >= _PRIVATE_INVESTABILITY_OK and evidence >= _QUALIFY_EVIDENCE:
            return S.ACCESS_LIMITED  # investable only via proxy/ETF — never a direct buy
        return S.PRIVATE_WATCH_ONLY

    # 2. Hype/crowd dominance with weak evidence → noise.
    max_hype = max(pens["hype_penalty"], pens["crowded_trade_penalty"])
    if max_hype >= _HYPE_DOMINANT and evidence < 0.4:
        return S.HYPE_NOISE

    # 3. Can't access it → access-limited regardless of score.
    if investability < _LOW_INVESTABILITY:
        return S.ACCESS_LIMITED

    # 4. Single-headline / thin evidence → cannot qualify; needs corroboration.
    if pens["single_headline_penalty"] >= _SINGLE_HEADLINE_BLOCK and evidence < _QUALIFY_EVIDENCE:
        return S.WATCHING

    # 5. Qualification requires evidence AND investability — boom alone is insufficient
    #    (boom_potential carries only 0.06 weight, so a high boom with weak evidence/
    #    investability cannot push opp past the gate, and these explicit gates enforce it).
    qualifies = (opp >= _QUALIFY_OPP and evidence >= _QUALIFY_EVIDENCE
                 and investability >= _QUALIFY_INVESTABILITY)
    if qualifies and opp >= _APPROVE_OPP:
        return S.APPROVED_WATCHLIST_REVIEW
    if qualifies:
        return S.QUALIFIED
    if opp >= 0.45:
        return S.WATCHING
    if opp >= 0.25:
        return S.SANDBOX_TRACKING
    if opp < 0.12:
        return S.REJECTED
    return S.DISCOVERED


def score_candidates(candidates: list[dict[str, Any]]) -> list[OpportunityScore]:
    """Score a list of candidate dicts (skips malformed ones)."""
    out: list[OpportunityScore] = []
    for c in candidates or []:
        try:
            out.append(score_opportunity(c))
        except Exception:
            continue
    return out
