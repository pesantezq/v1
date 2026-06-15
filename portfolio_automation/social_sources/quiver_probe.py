"""
Quiver Quantitative WSB probe — paid dataset; BLOCKED under no-extra-cost policy.

Verified 2026-06-14 against the official Quiver python-api source + pricing:
- WSB/Reddit mentions live at /beta/live/wallstreetbets and
  /beta/historical/wallstreetbets/{ticker}; auth is an ``Authorization: Token``
  header (docs prose says Bearer — the shipped client sends Token).
- It is PAID: the WSB dataset requires the Trader tier (~$75/mo); there is no
  free entitlement. A blocked call returns "Upgrade your subscription plan…".

No QUIVER_API_KEY exists in this repo, so this module ships INERT and makes NO
network call. Activation requires BOTH an existing QUIVER_API_KEY AND an explicit
config opt-in (allow_paid_sources=true) — neither is present.
"""
from __future__ import annotations

import os
from typing import Any

from portfolio_automation.social_intelligence.base import SourceStatus
from portfolio_automation.social_sources.base import SourceResult

_OFFICIAL = "https://api.quiverquant.com/ (paid — Trader tier ~$75/mo for WSB)"


class QuiverProbe:
    """Probe-only, no-network, blocked-by-default. Never auto-subscribes."""

    source_name = "quiver_wsb"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        crowd_radar_enabled: bool = False,
        allow_paid_sources: bool = False,
    ) -> None:
        cfg = config or {}
        self._blocked_flag = bool(cfg.get("blocked_no_extra_cost", True))
        self._crowd_radar_enabled = bool(crowd_radar_enabled)
        self._allow_paid = bool(allow_paid_sources)
        self._has_key = bool((os.environ.get("QUIVER_API_KEY") or "").strip())

    def is_configured(self) -> bool:
        # Only configurable if paid sources are explicitly allowed AND a key exists.
        return self._crowd_radar_enabled and self._allow_paid and self._has_key and not self._blocked_flag

    def probe(self) -> SourceResult:
        if not self._crowd_radar_enabled:
            return SourceResult(self.source_name, SourceStatus.DISABLED, warnings=["crowd_radar disabled"])
        if self.is_configured():
            # An existing key + explicit opt-in: we still only mark reference-ready,
            # never auto-call a paid endpoint without the operator wiring it.
            return SourceResult(
                self.source_name, SourceStatus.MANUAL_REFERENCE_ONLY,
                warnings=["QUIVER_API_KEY present + opt-in; paid endpoint not auto-called"],
                meta={"official_source": _OFFICIAL, "network_called": False},
            )
        return SourceResult(
            self.source_name, SourceStatus.BLOCKED_NO_EXTRA_COST,
            warnings=["paid dataset; blocked by no_extra_cost policy (no key / no opt-in)"],
            meta={"official_source": _OFFICIAL, "network_called": False},
        )

    def fetch(self) -> SourceResult:
        return self.probe()  # never collects; no network

    def normalize(self, raw: SourceResult) -> SourceResult:
        return raw

    def health(self) -> SourceResult:
        return self.probe()
