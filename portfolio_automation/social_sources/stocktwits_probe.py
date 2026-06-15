"""
Stocktwits probe — official access is partner-gated; NO network calls.

Verified 2026-06-14 against official Stocktwits properties:
- The free public developer program is SUSPENDED ("won't be accepting new
  registrations…"). The current official API (Firestream / Sentiment v2 /
  Firehose) is a commercial, approval-gated partner product with no published
  free tier.
- The legacy public REST endpoints (/api/2/streams/symbol/{S}.json,
  /api/2/trending/symbols.json) still respond but their docs now 404 — using
  them would be unsanctioned/private-endpoint use, which policy forbids.

Therefore this module NEVER makes a network call. It records an honest
not_configured / requires_manual_review status so the operator knows the only
path forward is a manual partner inquiry (developers@stocktwits.com).
"""
from __future__ import annotations

import os
from typing import Any

from portfolio_automation.social_intelligence.base import SourceStatus
from portfolio_automation.social_sources.base import SourceResult

_OFFICIAL = "https://api.stocktwits.com/developers (registrations closed; partner-gated)"


class StocktwitsProbe:
    """Probe-only, no-network. Honest status; never scrapes, never private endpoints."""

    source_name = "stocktwits"

    def __init__(self, config: dict[str, Any] | None = None, *, crowd_radar_enabled: bool = False) -> None:
        cfg = config or {}
        self._enabled = bool(cfg.get("enabled", False))
        self._crowd_radar_enabled = bool(crowd_radar_enabled)
        self._has_token = bool((os.environ.get("STOCKTWITS_TOKEN") or os.environ.get("STOCKTWITS_API_KEY") or "").strip())

    def is_configured(self) -> bool:
        # Even WITH a token we do not auto-activate: terms are unverified and the
        # official product is partner-gated. Activation requires manual review.
        return False

    def probe(self) -> SourceResult:
        if not self._crowd_radar_enabled:
            return SourceResult(self.source_name, SourceStatus.DISABLED, warnings=["crowd_radar disabled"])
        if self._has_token:
            return SourceResult(
                self.source_name, SourceStatus.REQUIRES_MANUAL_REVIEW,
                warnings=["token present but official terms/entitlement unverified — manual review required"],
                meta={"official_source": _OFFICIAL, "network_called": False},
            )
        return SourceResult(
            self.source_name, SourceStatus.NOT_CONFIGURED,
            warnings=["no Stocktwits credentials; official API partner-gated (requires_manual_review)"],
            meta={"official_source": _OFFICIAL, "network_called": False},
        )

    def fetch(self) -> SourceResult:
        return self.probe()  # never collects; no network

    def normalize(self, raw: SourceResult) -> SourceResult:
        return raw

    def health(self) -> SourceResult:
        return self.probe()
