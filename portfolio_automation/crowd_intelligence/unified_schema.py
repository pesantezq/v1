"""
Unified Crowd Intelligence — schema, constants, and the per-ticker row contract.

This module is the single source of truth for the *shape* of the unified crowd
artifact. It is pure data + deterministic helpers (no I/O, no network), so the
join logic in :mod:`unified_bus` and the tests can depend on it freely.

Design notes (see docs/superpowers/specs/2026-06-16-unified-crowd-intelligence-bus-design.md):

- The unified bus joins two pre-existing, disjoint crowd lanes by ticker:
    * Lane A — ``social_intelligence`` / ApeWisdom (retail mention attention).
    * Lane B — ``crowd_intelligence`` / FMP Starter (analyst/attention/congress/
      insider/news market context, entitlement-aware).
- Both lanes are PRESERVED; this is an additive normalization layer.
- ``social_sentiment_score`` is ``None`` (not 0.0) when the FMP plan locks the
  paid social-sentiment endpoints: null means "not measured (entitlement-locked)",
  whereas 0.0 would falsely assert a measured-neutral reading. ``social_sentiment_status``
  carries the reason (``PLAN_LOCKED`` / ``AVAILABLE`` / ``disabled`` / ``unknown``).
- Simulation-active, production-gated: the unified artifact never feeds the
  decision engine (production trade execution), but the simulation/test lane is
  ACTIVE and MAY consume it to change simulation outputs. Production behavior
  changes only via a human-approved promotion proposal
  (human_approval_required_for_production); AI/product review may recommend
  readiness but can never self-approve.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

SCHEMA_VERSION = "1"
SOURCE_LABEL = "unified_crowd_intelligence"

# --- normalization scales (documented rationale) --------------------------------
# Lane A mention_velocity is a ratio (mentions / mentions_24h_ago); 1.0 == flat.
# We map the *excess* over flat onto 0..1 so a non-moving ticker reads ~0 attention
# and a 5x+ surge reads full attention. 5.0 chosen because ApeWisdom velocity above
# ~5x is already firmly "trending" in observed data (e.g. HIMS at 13x -> 1.0).
RETAIL_ATTENTION_FLAT = 1.0
RETAIL_ATTENTION_FULL_SCALE = 5.0

# Lane B exposes per-symbol source_records_count + data_freshness but not a clean
# 0..1 "how much FMP context" measure. We derive one: freshness * (records / 20).
# 20 records is a healthy full-context symbol in observed data (e.g. SOXX = 20).
FMP_RECORDS_FULL_SCALE = 20.0

# Count of FMP context categories that contribute breadth (excludes social_sentiment,
# which is plan-locked, and is counted separately only when AVAILABLE).
FMP_CONTEXT_CATEGORIES = ("news", "analyst", "insider", "congress", "attention")

# --- classification thresholds (documented rationale) ---------------------------
# Deliberately coarse + deterministic. TAU_HI gates the "strong" states
# (confirmed / divergent); TAU_MID gates "present and meaningful"; TAU_LO is the
# floor below which a side counts as "quiet/absent" for divergence purposes.
TAU_LO = 0.15
TAU_MID = 0.30
TAU_HI = 0.50

# breadth_total at/above this counts as multi-source (lights up confirmation).
BREADTH_MULTI = 2

# Single-lane confidence penalty (a one-lane read is inherently less trustworthy).
SINGLE_LANE_CONFIDENCE_FACTOR = 0.70

# Staleness: a lane older than this many hours is treated as stale (matches the
# FMP context_loader default of 30h).
STALE_AFTER_HOURS = 30.0

# --- crowd_state vocabulary (unified) -------------------------------------------
STATE_INSUFFICIENT_DATA = "insufficient_data"
STATE_CONFIRMED_ATTENTION = "confirmed_attention"
STATE_DIVERGENT_ATTENTION = "divergent_attention"
STATE_RETAIL_ONLY = "retail_only_attention"
STATE_INSTITUTIONAL_ONLY = "institutional_context_only"
STATE_BROAD_SUPPORT = "broad_context_support"
STATE_CAUTION_LOW_BREADTH = "caution_low_breadth"

CROWD_STATES = (
    STATE_CONFIRMED_ATTENTION,
    STATE_DIVERGENT_ATTENTION,
    STATE_RETAIL_ONLY,
    STATE_INSTITUTIONAL_ONLY,
    STATE_BROAD_SUPPORT,
    STATE_CAUTION_LOW_BREADTH,
    STATE_INSUFFICIENT_DATA,
)

# social_sentiment_status values
SS_PLAN_LOCKED = "PLAN_LOCKED"
SS_AVAILABLE = "AVAILABLE"
SS_DISABLED = "disabled"
SS_UNKNOWN = "unknown"


def clamp01(x: float | None) -> float:
    """Clamp to [0, 1]; None -> 0.0."""
    if x is None:
        return 0.0
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


def clamp_signed(x: float | None) -> float:
    """Clamp to [-1, 1]; None -> 0.0."""
    if x is None:
        return 0.0
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return max(-1.0, min(1.0, v))


@dataclass
class UnifiedCrowdRow:
    """One joined crowd row per ticker. All scores are rounded at serialization."""

    ticker: str
    generated_at: str
    source_lanes_present: dict[str, bool] = field(
        default_factory=lambda: {"social_intelligence": False, "crowd_intelligence": False}
    )
    enabled_categories: list[str] = field(default_factory=list)
    disabled_categories: list[str] = field(default_factory=list)
    source_breadth_total: int = 0
    source_breadth_social: int = 0
    source_breadth_fmp: int = 0
    # normalized 0..1 activation/attention
    retail_attention_score: float | None = None
    fmp_attention_score: float | None = None
    # signed [-1, 1] directional FMP category scores (faithful passthrough)
    news_score: float | None = None
    analyst_score: float | None = None
    insider_score: float | None = None
    congress_score: float | None = None
    # null when PLAN_LOCKED (see module docstring)
    social_sentiment_score: float | None = None
    social_sentiment_status: str = SS_UNKNOWN
    # cross-source features (0..1 except delta which is -1..1)
    cross_source_confirmation_score: float = 0.0
    cross_source_divergence_score: float = 0.0
    retail_vs_fmp_attention_delta: float | None = None
    crowd_confidence: float = 0.0
    crowd_state: str = STATE_INSUFFICIENT_DATA
    explanation: str = ""
    warnings: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Round float-ish fields for stable artifacts; preserve None.
        for k in (
            "retail_attention_score", "fmp_attention_score", "news_score",
            "analyst_score", "insider_score", "congress_score",
            "social_sentiment_score", "cross_source_confirmation_score",
            "cross_source_divergence_score", "retail_vs_fmp_attention_delta",
            "crowd_confidence",
        ):
            v = d.get(k)
            if isinstance(v, float):
                d[k] = round(v, 4)
        return d
