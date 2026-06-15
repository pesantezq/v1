"""
FMP social-sentiment entitlement probe (paid Starter+ dataset → probe-only).

Verified 2026-06-14 against FMP's docs + example repo:
- Current path: ``/stable/historical/social-sentiment`` (legacy fallback
  ``/api/v4/historical/social-sentiment``), auth via ``apikey`` query param,
  pagination via ``page`` (0-indexed).
- Social sentiment requires a PAID Starter+ plan. A non-entitled key returns
  402/403, an ``{"Error Message": ...}`` body, or an empty list.
- Fields: date, symbol, stocktwits*/twitter* counts, sentiment, absoluteIndex,
  relativeIndex, generalPerception.

We will NOT buy a plan. This probes the EXISTING key once and stays dark unless
entitlement is confirmed. The endpoint path is read FROM fmp_endpoint_registry
(no bypass). Budget-exhaustion is surfaced distinctly, never as empty data.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable

from fmp_endpoint_registry import REGISTRY
from portfolio_automation.social_intelligence.base import SourceStatus
from portfolio_automation.social_sources.base import SourceResult

logger = logging.getLogger("stockbot.social_sources.fmp_social")

_FMP_BASE = "https://financialmodelingprep.com"
_REGISTRY_KEY = "social_sentiment"
_PROBE_SYMBOL = "AAPL"


def _registry_paths() -> tuple[str, str | None]:
    spec = REGISTRY.get(_REGISTRY_KEY, {})
    return spec.get("endpoint", "/stable/historical/social-sentiment"), spec.get("legacy_endpoint")


class FMPSocialSentimentConnector:
    """Entitlement-probed FMP social-sentiment source. Never raises."""

    source_name = "fmp_social_sentiment"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        crowd_radar_enabled: bool = False,
        api_key: str | None = None,
        http_get_status: Callable[[str], tuple[int, Any, str]] | None = None,
        budget_exhausted: Callable[[], bool] | None = None,
    ) -> None:
        cfg = config or {}
        self._enabled = bool(cfg.get("enabled", False))
        self._probe_only = bool(cfg.get("entitlement_probe_only_until_confirmed", True))
        self._crowd_radar_enabled = bool(crowd_radar_enabled)
        self._api_key = api_key if api_key is not None else (os.environ.get("FMP_API_KEY") or "").strip()
        self._http_get_status = http_get_status or _default_http_get_status
        self._budget_exhausted = budget_exhausted or (lambda: False)

    def is_configured(self) -> bool:
        return self._crowd_radar_enabled and self._enabled and bool(self._api_key)

    def probe(self) -> SourceResult:
        if not self._crowd_radar_enabled or not self._enabled:
            return SourceResult(self.source_name, SourceStatus.DISABLED, warnings=["crowd_radar/fmp_social disabled"])
        if not self._api_key:
            return SourceResult(self.source_name, SourceStatus.NO_CREDENTIALS,
                               warnings=["FMP_API_KEY not set"], meta={"entitled": False})
        # Budget guard FIRST — a spent quota must read as budget_exhausted, never
        # as "not entitled / empty" (the documented FMPClient daily_budget trap).
        if self._budget_exhausted():
            return SourceResult(self.source_name, SourceStatus.BUDGET_EXHAUSTED,
                               warnings=["FMP daily budget exhausted — probe deferred"],
                               meta={"entitled": None})

        stable, legacy = _registry_paths()
        status_code, body, message = self._call(stable)
        # 404 on stable → try the documented v4 legacy fallback once.
        if status_code == 404 and legacy:
            status_code, body, message = self._call(legacy)

        return self._classify(status_code, body, message)

    def _call(self, path: str) -> tuple[int, Any, str]:
        url = f"{_FMP_BASE}{path}?symbol={_PROBE_SYMBOL}&page=0&apikey={self._api_key}"
        try:
            return self._http_get_status(url)
        except Exception as exc:
            return -1, None, f"{type(exc).__name__}: {exc}"

    def _classify(self, status_code: int, body: Any, message: str) -> SourceResult:
        if status_code == 200 and isinstance(body, list) and body:
            return SourceResult(self.source_name, SourceStatus.OK,
                               meta={"entitled": True, "http_status": 200,
                                     "sample_fields": sorted(body[0].keys()) if isinstance(body[0], dict) else []},
                               warnings=["probe_only=true: not polling"] if self._probe_only else [])
        if status_code in (401, 402, 403):
            return SourceResult(self.source_name, SourceStatus.NOT_ENTITLED,
                               warnings=["paid Starter+ dataset; key not entitled (expected)"],
                               meta={"entitled": False, "http_status": status_code})
        if status_code == 429:
            return SourceResult(self.source_name, SourceStatus.RATE_LIMITED,
                               meta={"entitled": None, "http_status": 429})
        # 200-with-empty-list or an {"Error Message"} body → treat as not entitled.
        if (status_code == 200 and isinstance(body, list) and not body) or \
           (isinstance(body, dict) and ("Error Message" in body or "error" in body)):
            return SourceResult(self.source_name, SourceStatus.NOT_ENTITLED,
                               warnings=["empty/error body — key not entitled to social sentiment"],
                               meta={"entitled": False, "http_status": status_code})
        return SourceResult(self.source_name, SourceStatus.DEGRADED,
                           warnings=[f"unexpected_probe_status:{status_code}:{message[:80]}"],
                           meta={"entitled": False, "http_status": status_code})

    def fetch(self) -> SourceResult:
        # Probe-only until entitlement confirmed; we never poll history at no-extra-cost.
        return self.probe()

    def normalize(self, raw: SourceResult) -> SourceResult:
        return raw  # no records collected in probe-only mode

    def health(self) -> SourceResult:
        return self.probe()


def _default_http_get_status(url: str) -> tuple[int, Any, str]:  # pragma: no cover - network
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
