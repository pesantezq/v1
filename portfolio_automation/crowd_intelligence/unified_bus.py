"""
Unified Crowd Intelligence — pure join + cross-source metric computation.

No I/O, no network. Takes already-loaded Lane A (ApeWisdom / social_intelligence)
records and Lane B (FMP / crowd_intelligence) per-symbol signals and produces a
normalized list of :class:`UnifiedCrowdRow`.

Join is an OUTER join by ticker: tickers present in only one lane are KEPT (with a
``*_only`` warning and lowered confidence), never dropped.

Cross-source metrics (deterministic, documented):

- ``retail`` (r)  = normalized ApeWisdom mention velocity in [0,1].
- ``fmp`` (f)     = FMP context activation in [0,1] = data_freshness * (records/scale).
- confirmation    = min(r, f) * breadth_factor      -> high iff BOTH fire AND breadth>=2
- divergence      = |r - f|                          -> high iff the two sides disagree
- delta           = r - f  (in [-1,1])               -> retail-minus-fmp alignment

Confirmation and divergence are mutually exclusive by construction (a high ``min``
implies the values are close, which forces ``|r-f|`` low), which keeps the
``crowd_state`` machine unambiguous.
"""
from __future__ import annotations

from typing import Any

from portfolio_automation.crowd_intelligence.unified_schema import (
    BREADTH_MULTI,
    FMP_CONTEXT_CATEGORIES,
    FMP_RECORDS_FULL_SCALE,
    RETAIL_ATTENTION_FLAT,
    RETAIL_ATTENTION_FULL_SCALE,
    SINGLE_LANE_CONFIDENCE_FACTOR,
    SS_AVAILABLE,
    SS_DISABLED,
    SS_PLAN_LOCKED,
    SS_UNKNOWN,
    STATE_BROAD_SUPPORT,
    STATE_CAUTION_LOW_BREADTH,
    STATE_CONFIRMED_ATTENTION,
    STATE_DIVERGENT_ATTENTION,
    STATE_INSTITUTIONAL_ONLY,
    STATE_MARKET_CONTEXT_ONLY,
    STATE_INSUFFICIENT_DATA,
    STATE_RETAIL_ONLY,
    TAU_HI,
    TAU_LO,
    TAU_MID,
    UnifiedCrowdRow,
    clamp01,
    clamp_signed,
)

_STALE_SOCIAL_PENALTY = 0.5
_STALE_FMP_PENALTY = 0.5


def normalize_retail_attention(mention_velocity: float | None) -> float:
    """Map ApeWisdom mention_velocity ratio (1.0 == flat) onto [0,1].

    Excess over the flat baseline is scaled so a 5x+ surge reads ~1.0; a flat or
    declining ticker reads ~0.0.
    """
    if mention_velocity is None:
        return 0.0
    try:
        v = float(mention_velocity)
    except (TypeError, ValueError):
        return 0.0
    excess = v - RETAIL_ATTENTION_FLAT
    span = RETAIL_ATTENTION_FULL_SCALE - RETAIL_ATTENTION_FLAT
    if span <= 0:
        return clamp01(v)
    return clamp01(excess / span)


def _fmp_activation(signal: dict[str, Any]) -> float:
    """FMP 'how much market context is live' in [0,1]: freshness * (records/scale)."""
    freshness = clamp01(signal.get("data_freshness"))
    records = signal.get("source_records_count") or 0
    try:
        records = float(records)
    except (TypeError, ValueError):
        records = 0.0
    return clamp01(freshness * clamp01(records / FMP_RECORDS_FULL_SCALE))


def _fmp_active_categories(signal: dict[str, Any]) -> list[str]:
    """Context categories with a non-zero directional score for this symbol."""
    scores = signal.get("category_scores") or {}
    out: list[str] = []
    for cat in FMP_CONTEXT_CATEGORIES:
        try:
            if abs(float(scores.get(cat, 0.0) or 0.0)) > 0.0:
                out.append(cat)
        except (TypeError, ValueError):
            continue
    return out


def _social_sentiment_status(disabled_categories: list[str] | None,
                             enabled_categories: list[str] | None) -> str:
    disabled = set(disabled_categories or [])
    enabled = set(enabled_categories or [])
    if "social_sentiment" in enabled:
        return SS_AVAILABLE
    if "social_sentiment" in disabled:
        # Lane B disables social_sentiment specifically because the FMP plan locks it.
        return SS_PLAN_LOCKED
    if disabled or enabled:
        return SS_DISABLED
    return SS_UNKNOWN


def _classify(
    *,
    social_present: bool,
    fmp_present: bool,
    r: float,
    f: float,
    confirmation: float,
    divergence: float,
    breadth_total: int,
    breadth_fmp: int,
) -> str:
    if not social_present and not fmp_present:
        return STATE_INSUFFICIENT_DATA
    # No meaningful signal on either side.
    if r < TAU_LO and f < TAU_LO:
        return STATE_INSUFFICIENT_DATA
    both_present = social_present and fmp_present
    if confirmation >= TAU_HI and breadth_total >= BREADTH_MULTI:
        return STATE_CONFIRMED_ATTENTION
    # "Divergent" means both lanes have data but disagree — not merely one-sided
    # coverage. A ticker only one lane covers falls through to retail/institutional.
    if both_present and divergence >= TAU_HI:
        return STATE_DIVERGENT_ATTENTION
    if r >= TAU_MID and f < TAU_MID:
        return STATE_RETAIL_ONLY
    if f >= TAU_MID and r < TAU_MID:
        return STATE_MARKET_CONTEXT_ONLY
    if breadth_fmp >= BREADTH_MULTI and r > 0.0:
        return STATE_BROAD_SUPPORT
    if breadth_total <= 1 and (r > 0.0 or f > 0.0):
        return STATE_CAUTION_LOW_BREADTH
    return STATE_INSUFFICIENT_DATA


_STATE_EXPLANATION = {
    STATE_CONFIRMED_ATTENTION: "Confirmed attention: retail velocity aligns with FMP attention/news context.",
    STATE_DIVERGENT_ATTENTION: "Divergent attention: one source is elevated while the other does not confirm.",
    STATE_RETAIL_ONLY: "Retail-only attention: ApeWisdom mentions rising, but FMP context is limited.",
    STATE_MARKET_CONTEXT_ONLY: "Market context only: FMP analyst/news/insider/congress context exists, but retail attention is quiet. NOT institutional 13F positioning.",
    STATE_BROAD_SUPPORT: "Broad context support: multiple FMP categories active with some retail attention.",
    STATE_CAUTION_LOW_BREADTH: "Caution low breadth: signal concentrated in a single source.",
    STATE_INSUFFICIENT_DATA: "Insufficient data: no usable crowd signal on either lane.",
}


def build_unified_row(
    ticker: str,
    *,
    generated_at: str,
    social: dict[str, Any] | None,
    fmp: dict[str, Any] | None,
    enabled_categories: list[str] | None,
    disabled_categories: list[str] | None,
    social_stale: bool = False,
    fmp_stale: bool = False,
) -> UnifiedCrowdRow:
    """Join one ticker's Lane A + Lane B records into a UnifiedCrowdRow."""
    warnings: list[str] = []
    evidence_refs: list[str] = []

    social_present = social is not None
    fmp_present = fmp is not None

    # --- Lane A (retail) ----------------------------------------------------
    r = 0.0
    conf_social = 0.0
    breadth_social = 0
    if social_present:
        r = normalize_retail_attention(social.get("mention_velocity"))
        if social_stale:
            r *= _STALE_SOCIAL_PENALTY
            warnings.append("social_lane_stale")
        conf_social = clamp01(social.get("confidence"))
        breadth_social = int(social.get("source_breadth") or 0)
        evidence_refs.append("social:crowd_multi_source_velocity.json")

    # --- Lane B (FMP context) ----------------------------------------------
    f = 0.0
    conf_fmp = 0.0
    breadth_fmp = 0
    news_score = analyst_score = insider_score = congress_score = None
    fmp_attention_score: float | None = None
    if fmp_present:
        f = _fmp_activation(fmp)
        if fmp_stale:
            f *= _STALE_FMP_PENALTY
            warnings.append("fmp_lane_stale")
        fmp_attention_score = f
        conf_fmp = clamp01(fmp.get("confidence"))
        scores = fmp.get("category_scores") or {}
        news_score = clamp_signed(scores.get("news"))
        analyst_score = clamp_signed(scores.get("analyst"))
        insider_score = clamp_signed(scores.get("insider"))
        congress_score = clamp_signed(scores.get("congress"))
        active_cats = _fmp_active_categories(fmp)
        # A present FMP symbol always has >=1 context dimension even when scores
        # are neutral (data was fetched); never let a live symbol read breadth 0.
        records = float(fmp.get("source_records_count") or 0)
        breadth_fmp = max(len(active_cats), 1 if records > 0 else 0)
        evidence_refs.append("fmp:crowd_intelligence.json")

    # --- single-lane handling ----------------------------------------------
    if social_present and not fmp_present:
        warnings.append("lane_a_only")
    elif fmp_present and not social_present:
        warnings.append("lane_b_only")

    # --- cross-source metrics ----------------------------------------------
    breadth_total = breadth_social + breadth_fmp
    breadth_factor = clamp01(breadth_total / BREADTH_MULTI)
    confirmation = clamp01(min(r, f) * breadth_factor)
    divergence = clamp01(abs(r - f))

    if social_present or fmp_present:
        delta: float | None = round(r - f, 4)
        if social_present != fmp_present:
            # one lane only — delta is informative but flagged
            pass
    else:
        delta = None

    confidence = (
        0.4 * breadth_factor
        + 0.3 * confirmation
        + 0.3 * max(conf_social, conf_fmp)
    )
    if social_present != fmp_present:  # exactly one lane present
        confidence *= SINGLE_LANE_CONFIDENCE_FACTOR
    if social_stale or fmp_stale:
        confidence *= _STALE_FMP_PENALTY
    confidence = clamp01(confidence)

    state = _classify(
        social_present=social_present,
        fmp_present=fmp_present,
        r=r,
        f=f,
        confirmation=confirmation,
        divergence=divergence,
        breadth_total=breadth_total,
        breadth_fmp=breadth_fmp,
    )

    ss_status = _social_sentiment_status(disabled_categories, enabled_categories)
    social_sentiment_score = None  # null unless AVAILABLE (PLAN_LOCKED => stays null)

    # enabled/disabled categories: lane-global context, surfaced per row for the UI.
    enabled = list(enabled_categories or []) if fmp_present else []
    disabled = list(disabled_categories or []) if fmp_present else []

    return UnifiedCrowdRow(
        ticker=ticker,
        generated_at=generated_at,
        source_lanes_present={
            "social_intelligence": social_present,
            "crowd_intelligence": fmp_present,
        },
        enabled_categories=enabled,
        disabled_categories=disabled,
        source_breadth_total=breadth_total,
        source_breadth_social=breadth_social,
        source_breadth_fmp=breadth_fmp,
        retail_attention_score=(r if social_present else None),
        fmp_attention_score=fmp_attention_score,
        news_score=news_score,
        analyst_score=analyst_score,
        insider_score=insider_score,
        congress_score=congress_score,
        social_sentiment_score=social_sentiment_score,
        social_sentiment_status=ss_status,
        cross_source_confirmation_score=confirmation,
        cross_source_divergence_score=divergence,
        retail_vs_fmp_attention_delta=delta,
        crowd_confidence=confidence,
        crowd_state=state,
        explanation=_STATE_EXPLANATION.get(state, ""),
        warnings=warnings,
        evidence_refs=evidence_refs,
    )


def build_unified_rows(
    *,
    social_records: list[dict[str, Any]] | None,
    fmp_by_symbol: dict[str, dict[str, Any]] | None,
    enabled_categories: list[str] | None,
    disabled_categories: list[str] | None,
    generated_at: str,
    social_stale: bool = False,
    fmp_stale: bool = False,
) -> list[UnifiedCrowdRow]:
    """Outer-join the two lanes by ticker; returns rows sorted by confidence desc."""
    social_by_ticker: dict[str, dict[str, Any]] = {}
    for rec in social_records or []:
        if not isinstance(rec, dict):
            continue
        tk = str(rec.get("ticker") or "").upper().strip()
        if tk:
            social_by_ticker[tk] = rec

    fmp_by_ticker: dict[str, dict[str, Any]] = {}
    for sym, sig in (fmp_by_symbol or {}).items():
        tk = str(sym or "").upper().strip()
        if tk and isinstance(sig, dict):
            fmp_by_ticker[tk] = sig

    tickers = sorted(set(social_by_ticker) | set(fmp_by_ticker))
    rows = [
        build_unified_row(
            tk,
            generated_at=generated_at,
            social=social_by_ticker.get(tk),
            fmp=fmp_by_ticker.get(tk),
            enabled_categories=enabled_categories,
            disabled_categories=disabled_categories,
            social_stale=social_stale,
            fmp_stale=fmp_stale,
        )
        for tk in tickers
    ]
    rows.sort(key=lambda x: x.crowd_confidence, reverse=True)
    return rows
