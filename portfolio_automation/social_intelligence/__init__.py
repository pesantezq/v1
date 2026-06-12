"""
Public Knowledge Velocity Layer (Crowd Intelligence Radar).

Sandbox-only, observe-only social-discussion signal layer. Classifies the state
of public knowledge around tickers; never trades, recommends a trade, or mutates
the official portfolio. See docs/PUBLIC_KNOWLEDGE_VELOCITY_LAYER.md.
"""
from __future__ import annotations

from portfolio_automation.social_intelligence.base import (
    CrowdState,
    NextStep,
    RawPost,
    SourceStatus,
)
from portfolio_automation.social_intelligence.public_knowledge_velocity import (
    run_public_knowledge_velocity,
)

__all__ = [
    "CrowdState",
    "NextStep",
    "RawPost",
    "SourceStatus",
    "run_public_knowledge_velocity",
]
