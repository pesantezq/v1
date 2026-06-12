"""
Shared types and constants for the Public Knowledge Velocity Layer (Crowd Radar).

This package is **sandbox-only** and **observe-only**. It classifies the state of
public knowledge around tickers from API-compliant public-discussion sources. It
never trades, recommends a trade, or mutates the official portfolio.

The flags below are imported by every module and stamped into every artifact so
that the observe-only / no-trade / sandbox-only guarantees are visible in the
output, not just the code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

# ---------------------------------------------------------------------------
# Hardcoded invariants (stamped into every artifact)
# ---------------------------------------------------------------------------

OBSERVE_ONLY: bool = True
NO_TRADE: bool = True
NOT_RECOMMENDATION: bool = True
SANDBOX_ONLY: bool = True
DISCOVERY_ONLY: bool = True
SCHEMA_VERSION: str = "1"
SOURCE_LABEL: str = "public_knowledge_velocity_layer"

DISCLAIMER: str = (
    "Sandbox research intelligence only. Not a trade recommendation. "
    "Crowd-knowledge state may adjust research priority only; it cannot trigger "
    "BUY/SELL/HOLD/REBALANCE/TRIM/SCALE/PROMOTE or any allocation change."
)

# Kill-switch surfaces (mirrors backtesting/auto_apply.py convention).
KILL_SWITCH_FILE: str = "config/crowd_radar.DISABLED"
KILL_SWITCH_ENV: str = "STOCKBOT_CROWD_RADAR_DISABLED"


# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------

class SourceStatus(str, Enum):
    """Operational status of a discussion source / the layer as a whole."""

    OK = "ok"
    DISABLED = "disabled"
    DEGRADED = "degraded"
    NO_CREDENTIALS = "no_credentials"
    RATE_LIMITED = "rate_limited"
    SOURCE_TERMS_BLOCKED = "source_terms_blocked"
    INSUFFICIENT_DATA = "insufficient_data"
    ERROR = "error"


class CrowdState(str, Enum):
    """The eight crowd-knowledge research states."""

    DORMANT_NOISE = "dormant_noise"
    EMERGING_DD = "emerging_dd"
    CROWD_VALIDATION = "crowd_validation"
    HYPE_ACCELERATION = "hype_acceleration"
    REFLEXIVE_SQUEEZE_RISK = "reflexive_squeeze_risk"
    KNOWN_NEWS_ECHO = "known_news_echo"
    CROWD_EXHAUSTION = "crowd_exhaustion"
    CONTRARIAN_NEGLECT = "contrarian_neglect"


class NextStep(str, Enum):
    """Research-only next-step vocabulary. **No trade verbs by construction.**"""

    IGNORE = "ignore"
    MONITOR = "monitor"
    SEND_TO_DISCOVERY_REVIEW = "send_to_discovery_review"
    REQUIRES_NEWS_VALIDATION = "requires_news_validation"
    REQUIRES_BACKTEST = "requires_backtest"
    FLAG_AS_HYPE_RISK = "flag_as_hype_risk"


# Verbs that this layer must NEVER emit. Used by guard tests and by a runtime
# assertion in the orchestrator so a future edit cannot smuggle a trade verb in.
FORBIDDEN_TRADE_VERBS: frozenset[str] = frozenset({
    "buy", "sell", "hold", "rebalance", "trim", "scale", "promote",
    "allocate", "exit", "enter", "long", "short", "add", "reduce",
})


# ---------------------------------------------------------------------------
# Raw post (transient — only derived features are persisted)
# ---------------------------------------------------------------------------

@dataclass
class RawPost:
    """
    Minimal-field representation of a single public-discussion post.

    Raw ``title``/``body`` are held transiently for ticker extraction and DD
    scoring. They are persisted to disk **only** when the originating source's
    ``raw_text_storage_allowed`` is True; otherwise only derived features survive.
    """

    post_id: str
    source: str
    community: str
    created_utc: float
    title: str = ""
    body: str = ""
    flair: str | None = None
    score: int = 0
    comment_count: int = 0
    upvote_ratio: float | None = None
    url: str = ""
    author_hash: str = ""
    collection_timestamp: str = ""

    def text(self) -> str:
        """Combined searchable text (transient)."""
        return f"{self.title} {self.body}".strip()


def utc_now_iso() -> str:
    """ISO-8601 UTC timestamp. Centralized so tests can monkeypatch if needed."""
    return datetime.now(timezone.utc).isoformat()


def base_envelope(
    *,
    run_id: str,
    run_mode: str,
    source_status: str,
    data_quality_status: str,
    warnings: list[str] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """
    Build the shared artifact envelope every Crowd Radar JSON carries.

    Centralizing this guarantees the observe-only / no-trade / sandbox-only flags
    are stamped identically across all five artifacts.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE_LABEL,
        "run_id": run_id,
        "run_mode": run_mode,
        "created_at": created_at or utc_now_iso(),
        "source_status": source_status,
        "data_quality_status": data_quality_status,
        "observe_only": OBSERVE_ONLY,
        "no_trade": NO_TRADE,
        "not_recommendation": NOT_RECOMMENDATION,
        "sandbox_only": SANDBOX_ONLY,
        "discovery_only": DISCOVERY_ONLY,
        "disclaimer": DISCLAIMER,
        "warnings": list(warnings or []),
    }
