"""
Crowd-source health + factory + multi-source activation builder.

Only active free sources are registered here. Paid / partner-gated probes
(FMP social sentiment, Finnhub, Stocktwits, Quiver) were removed 2026-06-21
as they provided no runtime value — all returned inert statuses and cluttered
the health display.

Active sources:
  - ApeWisdom    — attention / mention counts (free, no auth)
  - Bluesky      — text / sentiment (free, AT Protocol public API)
  - Mastodon     — text / sentiment (free, instance allowlist)
  - Lemmy        — text / sentiment (free, RSS + API, instance/community allowlist)
"""
from __future__ import annotations

import os
from typing import Any

from portfolio_automation.social_intelligence.base import SourceStatus
from portfolio_automation.social_sources.apewisdom_connector import ApeWisdomConnector
from portfolio_automation.social_sources.base import SourceResult
from portfolio_automation.social_sources.dev_doc_audit import SOURCE_AUDIT

# Audit-derived classification (independent of runtime config).
_AUDIT_STATUS = {s["source_name"]: s["implementation_status"] for s in SOURCE_AUDIT}

# Lazy import helpers — Bluesky/Mastodon/Lemmy connectors only loaded when
# the crowd_radar feature is enabled, keeping import time minimal.
def _import_text_connectors():
    from portfolio_automation.social_sources.bluesky_connector import BlueskyConnector
    from portfolio_automation.social_sources.mastodon_connector import MastodonConnector
    from portfolio_automation.social_sources.lemmy_connector import LemmyConnector
    return BlueskyConnector, MastodonConnector, LemmyConnector


def build_sources(cfg: dict[str, Any]) -> dict[str, Any]:
    """Instantiate every active connector from the crowd_radar.source_policy config."""
    crowd_enabled = bool(cfg.get("enabled"))
    policy = cfg.get("source_policy") or {}

    sources: dict[str, Any] = {
        "apewisdom": ApeWisdomConnector(
            policy.get("apewisdom"), crowd_radar_enabled=crowd_enabled
        ),
    }

    try:
        BlueskyConnector, MastodonConnector, LemmyConnector = _import_text_connectors()
        sources["bluesky"] = BlueskyConnector(
            policy.get("bluesky"), crowd_radar_enabled=crowd_enabled
        )
        sources["mastodon"] = MastodonConnector(
            policy.get("mastodon"), crowd_radar_enabled=crowd_enabled
        )
        sources["lemmy"] = LemmyConnector(
            policy.get("lemmy"), crowd_radar_enabled=crowd_enabled
        )
    except ImportError:
        # Text connectors not yet installed — attention-only mode.
        pass

    return sources


def collect_health(sources: dict[str, Any]) -> list[SourceResult]:
    """Run health() on every source. Never raises (connectors are fail-safe)."""
    out: list[SourceResult] = []
    for name, conn in sources.items():
        try:
            out.append(conn.health())
        except Exception as exc:  # pragma: no cover - defensive; connectors shouldn't raise
            out.append(
                SourceResult(name, SourceStatus.ERROR,
                             warnings=[f"health_error:{type(exc).__name__}"])
            )
    return out


def classify_sources(cfg: dict[str, Any]) -> dict[str, list[str]]:
    """Split sources into active / probe-only / blocked using the dev-doc audit."""
    policy = cfg.get("source_policy") or {}
    active, probe, blocked = [], [], []
    for name, status in _AUDIT_STATUS.items():
        src_cfg = policy.get(name) or {}
        if status == "active" and src_cfg.get("enabled"):
            active.append(name)
        elif status == "probe_only":
            probe.append(name)
        else:
            blocked.append(name)
    return {"active": sorted(active), "probe_only": sorted(probe), "blocked": sorted(blocked)}


def credentials_present() -> dict[str, bool]:
    """Which source credentials are present in the environment (no network).

    All active free sources (ApeWisdom, Bluesky, Mastodon, Lemmy) are public
    APIs that need no credentials — they always return False/absent here, which
    is the expected and healthy state.
    """
    def _set(*keys: str) -> bool:
        return any((os.environ.get(k) or "").strip() for k in keys)
    return {
        # No active free source requires credentials.
        # This map is intentionally empty — the activation check displays it as
        # "no credentials required (all sources are public APIs)".
    }


def entitlements_from_health(health: list[SourceResult]) -> dict[str, bool]:
    """Map source → confirmed-entitled (True only when a probe returned OK + entitled)."""
    out: dict[str, bool] = {}
    for r in health:
        ent = r.meta.get("entitled")
        if ent is not None:
            out[r.source_name] = bool(ent)
    return out
