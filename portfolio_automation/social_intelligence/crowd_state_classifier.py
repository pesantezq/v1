"""
Crowd Knowledge State Classifier.

Maps a per-ticker :class:`TickerFeatures` vector into one of eight research
states (:class:`CrowdState`) with a confidence band, an explainable
``score_components`` breakdown, ``risk_flags``, a research-only
``recommended_next_step``, and a capped ``crowd_research_priority_score``.

Design: a **prioritized rule cascade**, evaluated most-dangerous / most-specific
first, so the layer fails toward caution and every output is explainable. No
network, no AI, deterministic.

GOVERNANCE: ``recommended_next_step`` is drawn only from :class:`NextStep`
(research verbs). The ``crowd_research_priority_score`` is clamped to
``research_priority_cap``. This module cannot and does not emit a trade action.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from portfolio_automation.social_intelligence.base import (
    CrowdState,
    NextStep,
    utc_now_iso,
)
from portfolio_automation.social_intelligence.feature_aggregation import TickerFeatures


@dataclass
class ClassifierThresholds:
    """Tunable thresholds. Defaults chosen to be conservative on thin data."""

    min_mentions: int = 3                 # below → dormant_noise (or contrarian_neglect)
    high_velocity_z: float = 2.0          # mention_velocity_zscore considered a spike
    moderate_velocity_z: float = 1.0
    strong_evidence: float = 0.5
    weak_evidence: float = 0.2
    high_meme: float = 0.4
    high_dispersion: float = 0.45
    high_author_concentration: float = 0.6
    multi_author: int = 3                 # >= this many distinct authors = "independent"
    elevated_options_si: float = 1.5      # options/short-interest context z


def _next_step_for(state: CrowdState, confidence: float) -> NextStep:
    mapping = {
        CrowdState.DORMANT_NOISE: NextStep.IGNORE,
        CrowdState.EMERGING_DD: NextStep.SEND_TO_DISCOVERY_REVIEW,
        CrowdState.CROWD_VALIDATION: NextStep.REQUIRES_NEWS_VALIDATION,
        CrowdState.HYPE_ACCELERATION: NextStep.FLAG_AS_HYPE_RISK,
        CrowdState.REFLEXIVE_SQUEEZE_RISK: NextStep.FLAG_AS_HYPE_RISK,
        CrowdState.KNOWN_NEWS_ECHO: NextStep.MONITOR,
        CrowdState.CROWD_EXHAUSTION: NextStep.MONITOR,
        CrowdState.CONTRARIAN_NEGLECT: NextStep.REQUIRES_NEWS_VALIDATION,
    }
    step = mapping[state]
    # Demote weak research-review signals to mere monitoring.
    if step == NextStep.SEND_TO_DISCOVERY_REVIEW and confidence < 0.45:
        return NextStep.MONITOR
    return step


def _research_priority(
    state: CrowdState,
    f: TickerFeatures,
    cap: float,
) -> float:
    """
    Capped, research-only priority score. Positive = look sooner. Hype / squeeze
    / exhaustion states are *suppressed* (they raise caution, not interest)."""
    base = 0.0
    # Useful states earn priority from velocity + evidence + author breadth.
    if state in (CrowdState.EMERGING_DD, CrowdState.CROWD_VALIDATION, CrowdState.CONTRARIAN_NEGLECT):
        base = (
            2.0 * max(0.0, f.mention_velocity_zscore)
            + 4.0 * f.evidence_score
            + 1.0 * min(1.0, f.unique_author_count / 10.0)
        )
        if state == CrowdState.CONTRARIAN_NEGLECT:
            base += 2.0  # neglected-but-supported is the most "early" signal
    elif state in (CrowdState.HYPE_ACCELERATION, CrowdState.REFLEXIVE_SQUEEZE_RISK):
        base = -2.0 * f.meme_language_score   # negative = de-prioritize / caution
    elif state in (CrowdState.KNOWN_NEWS_ECHO, CrowdState.CROWD_EXHAUSTION):
        base = -1.0
    # Clamp to [-cap, cap].
    return max(-cap, min(cap, round(base, 3)))


def classify_ticker(
    f: TickerFeatures,
    thresholds: ClassifierThresholds | None = None,
    *,
    research_priority_cap: float = 10.0,
) -> dict[str, Any]:
    """Classify a single ticker's features into a crowd-knowledge state dict."""
    t = thresholds or ClassifierThresholds()
    flags: list[str] = []

    # Explainable component signals (also persisted for audit).
    components = {
        "mention_count": f.mention_count,
        "velocity_z": round(f.mention_velocity_zscore, 3),
        "evidence_score": round(f.evidence_score, 3),
        "dd_density": round(f.dd_density, 3),
        "meme_score": round(f.meme_language_score, 3),
        "sentiment": round(f.sentiment_score, 3),
        "dispersion": round(f.sentiment_dispersion, 3),
        "unique_authors": f.unique_author_count,
        "author_concentration": round(f.author_concentration, 3),
        "external_news_match": f.external_news_match,
        "options_si_ctx": f.options_or_short_interest_context,
        "price_move_pre_spike": f.price_move_before_social_spike,
    }

    high_vel = f.mention_velocity_zscore >= t.high_velocity_z
    mod_vel = f.mention_velocity_zscore >= t.moderate_velocity_z
    strong_ev = f.evidence_score >= t.strong_evidence
    weak_ev = f.evidence_score <= t.weak_evidence
    high_meme = f.meme_language_score >= t.high_meme
    multi_author = f.unique_author_count >= t.multi_author
    si = f.options_or_short_interest_context
    elevated_si = si is not None and si >= t.elevated_options_si
    price_pre = f.price_move_before_social_spike

    state: CrowdState
    confidence: float

    # --- Cascade: dangerous / specific first ---------------------------------
    if f.mention_count < t.min_mentions:
        # Quiet. Either truly dormant, or a neglected good setup.
        if (f.external_news_match or strong_ev) and not high_meme:
            state = CrowdState.CONTRARIAN_NEGLECT
            confidence = 0.4 + 0.3 * f.evidence_score
            flags.append("low_attention_with_external_support")
        else:
            state = CrowdState.DORMANT_NOISE
            confidence = 0.6
    elif high_vel and elevated_si:
        state = CrowdState.REFLEXIVE_SQUEEZE_RISK
        confidence = 0.6 + 0.2 * min(1.0, (si or 0) / 3.0)
        flags.append("elevated_short_interest_or_options")
        flags.append("social_velocity_spike")
    elif f.external_news_match and price_pre is not None and price_pre >= 3.0:
        # News already public AND price already moved before the social spike.
        state = CrowdState.KNOWN_NEWS_ECHO
        confidence = 0.65
        flags.append("price_moved_before_social")
        flags.append("reacting_to_public_news")
    elif high_vel and high_meme and weak_ev:
        state = CrowdState.HYPE_ACCELERATION
        confidence = 0.55 + 0.2 * f.meme_language_score
        flags.append("fast_mention_growth_weak_evidence")
        if f.author_concentration >= t.high_author_concentration:
            flags.append("attention_concentrated_in_few_authors")
    elif (mod_vel or high_vel) and f.sentiment_dispersion >= t.high_dispersion and high_meme:
        # Peaked: fragmenting debate + meme-heavy + attention no longer rising cleanly.
        state = CrowdState.CROWD_EXHAUSTION
        confidence = 0.5 + 0.2 * f.sentiment_dispersion
        flags.append("fragmenting_sentiment")
        flags.append("late_stage_attention")
    elif multi_author and strong_ev and not high_meme:
        state = CrowdState.CROWD_VALIDATION
        confidence = 0.55 + 0.25 * f.evidence_score
        flags.append("independent_authors_converging")
        if f.external_news_match:
            flags.append("external_support_present")
    elif mod_vel and f.dd_density >= t.strong_evidence and not high_meme:
        state = CrowdState.EMERGING_DD
        confidence = 0.5 + 0.25 * f.dd_density
        flags.append("early_rising_attention_with_dd")
    else:
        state = CrowdState.DORMANT_NOISE
        confidence = 0.4
        if high_meme:
            flags.append("meme_language_present")

    confidence = round(max(0.0, min(1.0, confidence)), 3)
    next_step = _next_step_for(state, confidence)
    priority = _research_priority(state, f, research_priority_cap)

    evidence_summary = (
        f"{f.mention_count} mentions across {f.unique_author_count} authors; "
        f"velocity z={f.mention_velocity_zscore:.2f}, dd_density={f.dd_density:.2f}, "
        f"evidence={f.evidence_score:.2f}, meme={f.meme_language_score:.2f}"
    )

    return {
        "ticker": f.ticker,
        "crowd_state": state.value,
        "confidence": confidence,
        "score_components": components,
        "evidence_summary": evidence_summary,
        "risk_flags": flags,
        "recommended_next_step": next_step.value,
        "crowd_research_priority_score": priority,
        "created_at": utc_now_iso(),
    }


def classify_all(
    features: list[TickerFeatures],
    thresholds: ClassifierThresholds | None = None,
    *,
    research_priority_cap: float = 10.0,
) -> list[dict[str, Any]]:
    """Classify a list of feature vectors; sorted by research priority desc."""
    out = [
        classify_ticker(f, thresholds, research_priority_cap=research_priority_cap)
        for f in features
    ]
    out.sort(key=lambda d: d["crowd_research_priority_score"], reverse=True)
    return out
