"""
Crowd-source health + factory + multi-source activation builder.

One place to: (1) instantiate every source connector from config, (2) collect a
per-source health snapshot, and (3) compute the multi-source activation
checklist. Pure orchestration — connectors themselves never raise; probes only
hit the network when a source is configured with credentials (none in this repo
by default, so the steady state is no-network).
"""
from __future__ import annotations

import os
from typing import Any

from portfolio_automation.social_intelligence.base import SourceStatus
from portfolio_automation.social_sources.apewisdom_connector import ApeWisdomConnector
from portfolio_automation.social_sources.base import SourceResult
from portfolio_automation.social_sources.dev_doc_audit import SOURCE_AUDIT
from portfolio_automation.social_sources.finnhub_social_probe import FinnhubSocialProbe
from portfolio_automation.social_sources.fmp_social_sentiment_connector import (
    FMPSocialSentimentConnector,
)
from portfolio_automation.social_sources.quiver_probe import QuiverProbe
from portfolio_automation.social_sources.stocktwits_probe import StocktwitsProbe

# Audit-derived classification (independent of runtime config).
_AUDIT_STATUS = {s["source_name"]: s["implementation_status"] for s in SOURCE_AUDIT}


def credentials_present() -> dict[str, bool]:
    """Which source credentials exist in the environment (no network)."""
    def _set(*keys: str) -> bool:
        return any((os.environ.get(k) or "").strip() for k in keys)
    return {
        "reddit": _set("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT"),
        "fmp_social_sentiment": _set("FMP_API_KEY"),
        "finnhub_social": _set("FINNHUB_API_KEY"),
        "quiver_wsb": _set("QUIVER_API_KEY"),
        "stocktwits": _set("STOCKTWITS_TOKEN", "STOCKTWITS_API_KEY"),
    }


def build_sources(cfg: dict[str, Any]) -> dict[str, Any]:
    """Instantiate every connector from the crowd_radar.source_policy config."""
    crowd_enabled = bool(cfg.get("enabled"))
    allow_paid = bool(cfg.get("allow_paid_sources", False))
    policy = cfg.get("source_policy") or {}
    return {
        "apewisdom": ApeWisdomConnector(policy.get("apewisdom"), crowd_radar_enabled=crowd_enabled),
        "fmp_social_sentiment": FMPSocialSentimentConnector(
            policy.get("fmp_social_sentiment"), crowd_radar_enabled=crowd_enabled),
        "stocktwits": StocktwitsProbe(policy.get("stocktwits"), crowd_radar_enabled=crowd_enabled),
        "finnhub_social": FinnhubSocialProbe(policy.get("finnhub_social"), crowd_radar_enabled=crowd_enabled),
        "quiver_wsb": QuiverProbe(policy.get("quiver_wsb"), crowd_radar_enabled=crowd_enabled,
                                  allow_paid_sources=allow_paid),
    }


def collect_health(sources: dict[str, Any]) -> list[SourceResult]:
    """Run health() on every source. Never raises (connectors are fail-safe)."""
    out: list[SourceResult] = []
    for name, conn in sources.items():
        try:
            out.append(conn.health())
        except Exception as exc:  # pragma: no cover - defensive; connectors shouldn't raise
            out.append(SourceResult(name, SourceStatus.ERROR, warnings=[f"health_error:{type(exc).__name__}"]))
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
        else:  # blocked_no_extra_cost / requires_manual_review
            blocked.append(name)
    return {"active": sorted(active), "probe_only": sorted(probe), "blocked": sorted(blocked)}


def entitlements_from_health(health: list[SourceResult]) -> dict[str, bool]:
    """Map source -> confirmed-entitled (True only when a probe returned OK + entitled)."""
    out: dict[str, bool] = {}
    for r in health:
        ent = r.meta.get("entitled")
        if ent is not None:
            out[r.source_name] = bool(ent)
    return out
