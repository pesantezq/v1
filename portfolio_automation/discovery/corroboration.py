"""
Deterministic corroboration scoring for discovery candidates.

All scoring is compute-only — no file I/O, no LLM calls, no API calls.
This module is sandbox/research-lane only.

Weighted components:
    source_diversity : 35%  (unique sources, cap = 4)
    mention          : 20%  (mention count, log-scaled, cap = 7 mentions)
    event_strength   : 25%  (event confidence, direct 0.0–1.0)
    persistence      : 20%  (seen_runs from discovery memory, cap = 3)
    risk_penalty     : -0.20 when risk_flag is True

Level thresholds:
    none     : [0.00, 0.30)
    weak     : [0.30, 0.50)
    moderate : [0.50, 0.65)
    strong   : [0.65, 1.00]

corroboration_met = True when score >= CORROBORATION_MET_THRESHOLD (0.65).
WATCH status requires corroboration_met = True.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

CORROBORATION_MET_THRESHOLD: float = 0.65

_W_SOURCE: float = 0.35
_W_MENTION: float = 0.20
_W_EVENT: float = 0.25
_W_PERSISTENCE: float = 0.20
_RISK_PENALTY: float = 0.20

_SOURCE_CAP: int = 4
_MENTION_LOG_CAP: float = 3.0   # log2(8) ≈ 3.0 → 7 mentions saturates
_PERSISTENCE_CAP: int = 3


@dataclass
class CorroborationResult:
    """Deterministic corroboration scoring result for a single discovery candidate."""

    score: float            # 0.0–1.0 composite, clamped
    level: str              # "none" | "weak" | "moderate" | "strong"
    corroboration_met: bool # True when score >= CORROBORATION_MET_THRESHOLD

    # Weighted sub-components (already multiplied by weight)
    source_diversity_component: float
    mention_component: float
    event_strength_component: float
    persistence_component: float
    risk_penalty_applied: float

    # Source names that contributed evidence
    corroboration_sources: list[str] = field(default_factory=list)


def _score_to_level(score: float) -> str:
    if score >= 0.65:
        return "strong"
    if score >= 0.50:
        return "moderate"
    if score >= 0.30:
        return "weak"
    return "none"


def compute_corroboration(
    *,
    unique_source_count: int,
    mention_count: int,
    event_confidence: float,
    risk_flag: bool,
    seen_runs: int = 0,
    source_names: list[str] | None = None,
) -> CorroborationResult:
    """
    Compute deterministic corroboration score for a discovery candidate.

    Parameters
    ----------
    unique_source_count:
        Number of distinct sources that mentioned this ticker.
    mention_count:
        Total mentions across all records in this run.
    event_confidence:
        Classification confidence from event_classifier (0.0–1.0).
    risk_flag:
        True if the event classifier flagged this as a risk event.
    seen_runs:
        Number of prior discovery runs (from DiscoveryMemory.seen_runs).
    source_names:
        Optional list of source name strings for artifact metadata.
    """
    source_component = min(unique_source_count / _SOURCE_CAP, 1.0) * _W_SOURCE

    raw_mention = math.log2(mention_count + 1) / _MENTION_LOG_CAP
    mention_component = min(raw_mention, 1.0) * _W_MENTION

    event_component = max(0.0, min(float(event_confidence), 1.0)) * _W_EVENT

    persistence_component = min(seen_runs / _PERSISTENCE_CAP, 1.0) * _W_PERSISTENCE

    risk_penalty = _RISK_PENALTY if risk_flag else 0.0

    raw = source_component + mention_component + event_component + persistence_component - risk_penalty
    score = round(max(0.0, min(1.0, raw)), 4)

    return CorroborationResult(
        score=score,
        level=_score_to_level(score),
        corroboration_met=score >= CORROBORATION_MET_THRESHOLD,
        source_diversity_component=round(source_component, 4),
        mention_component=round(mention_component, 4),
        event_strength_component=round(event_component, 4),
        persistence_component=round(persistence_component, 4),
        risk_penalty_applied=round(risk_penalty, 4),
        corroboration_sources=list(source_names or []),
    )
