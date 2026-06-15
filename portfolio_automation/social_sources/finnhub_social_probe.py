"""
Finnhub social-sentiment entitlement probe (premium-gated → probe-only).

Verified against Finnhub's official SDK + issue tracker (2026-06-14):
- Endpoint: ``GET /api/v1/stock/social-sentiment?symbol=AAPL&token=KEY``
- Auth: ``token`` query param.
- Social sentiment is **PREMIUM-only**. A free/non-entitled key returns HTTP 403
  with body ``"You don't have access to this resource."`` — that 403 is the
  EXPECTED non-entitled signal, not an error.
- Response (entitled): ``{symbol, data:[{atTime, mention, positiveMention,
  negativeMention, positiveScore, negativeScore, score}]}`` (legacy split
  ``reddit[]``/``twitter[]`` also accepted defensively).

We will NOT buy premium. This module makes at most ONE probe call, only when
FINNHUB_API_KEY is set, and never activates polling unless entitlement confirms.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable

from portfolio_automation.social_intelligence.base import SourceStatus
from portfolio_automation.social_sources.base import SourceResult

logger = logging.getLogger("stockbot.social_sources.finnhub_social")

_PROBE_URL = "https://finnhub.io/api/v1/stock/social-sentiment?symbol=AAPL&token={token}"
_NOT_ENTITLED_MARKER = "don't have access"


class FinnhubSocialProbe:
    """Probe-only Finnhub social-sentiment source. Never raises."""

    source_name = "finnhub_social"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        crowd_radar_enabled: bool = False,
        api_key: str | None = None,
        http_get_status: Callable[[str], tuple[int, dict | None, str]] | None = None,
    ) -> None:
        cfg = config or {}
        self._enabled = bool(cfg.get("enabled", False))
        self._probe_only = bool(cfg.get("probe_only", True))
        self._crowd_radar_enabled = bool(crowd_radar_enabled)
        # api_key seam: explicit > env. Tests pass api_key + http_get_status.
        self._api_key = api_key if api_key is not None else (os.environ.get("FINNHUB_API_KEY") or "").strip()
        self._http_get_status = http_get_status or _default_http_get_status

    def is_configured(self) -> bool:
        """Probe is only meaningful when the feature is on AND a key exists."""
        return self._crowd_radar_enabled and bool(self._api_key)

    def probe(self) -> SourceResult:
        if not self._crowd_radar_enabled:
            return SourceResult(self.source_name, SourceStatus.DISABLED,
                               warnings=["crowd_radar disabled"])
        if not self._api_key:
            return SourceResult(self.source_name, SourceStatus.NO_CREDENTIALS,
                               warnings=["FINNHUB_API_KEY not set"],
                               meta={"entitled": False})
        try:
            status_code, body, message = self._http_get_status(_PROBE_URL.format(token=self._api_key))
        except Exception as exc:
            return SourceResult(self.source_name, SourceStatus.ERROR,
                               warnings=[f"probe_error:{type(exc).__name__}"],
                               meta={"entitled": False})

        if status_code == 200 and _has_rows(body):
            return SourceResult(self.source_name, SourceStatus.OK,
                               meta={"entitled": True, "http_status": 200})
        if status_code in (401, 403) or (message and _NOT_ENTITLED_MARKER in message.lower()):
            return SourceResult(self.source_name, SourceStatus.NOT_ENTITLED,
                               warnings=["premium-gated (expected on free key)"],
                               meta={"entitled": False, "http_status": status_code})
        if status_code == 429:
            return SourceResult(self.source_name, SourceStatus.RATE_LIMITED,
                               meta={"entitled": None, "http_status": 429})
        return SourceResult(self.source_name, SourceStatus.DEGRADED,
                           warnings=[f"unexpected_probe_status:{status_code}"],
                           meta={"entitled": False, "http_status": status_code})

    def fetch(self) -> SourceResult:
        """Probe-only: never polls until entitlement is confirmed and opted in."""
        if self._probe_only:
            res = self.probe()
            # Demote any OK to "probe confirmed" — we still don't actively poll v1.
            res.warnings.append("probe_only=true: not polling")
            return res
        return self.probe()

    def normalize(self, raw: SourceResult) -> SourceResult:
        return raw  # no records collected in probe-only mode

    def health(self) -> SourceResult:
        return self.probe()


def _has_rows(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    if isinstance(body.get("data"), list) and body["data"]:
        return True
    return bool(body.get("reddit") or body.get("twitter"))


def _default_http_get_status(url: str) -> tuple[int, dict | None, str]:  # pragma: no cover - network
    import json
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "stockbot-crowd-radar/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8")), ""
    except urllib.error.HTTPError as exc:
        try:
            msg = exc.read().decode("utf-8", errors="replace")
        except Exception:
            msg = ""
        return exc.code, None, msg
