"""
ApeWisdom connector — free, no-auth, read-only Reddit/social mention aggregator.

Verified against the official docs (https://apewisdom.io/api/) and a live fetch
on 2026-06-14:
- Endpoints: ``/api/v1.0/filter/{filter}`` and ``/api/v1.0/filter/{filter}/page/{n}``
- No authentication required.
- Top-level response: ``count``, ``pages``, ``current_page``, ``results[]``.
- Each result: ``rank, ticker, name, mentions, upvotes, rank_24h_ago,
  mentions_24h_ago`` (~100 results/page).
- No published rate limit → be polite client-side (bounded pages + caching).

We store only aggregate mention counts — never raw Reddit/4chan post text.
Observe-only, sandbox-only; output adjusts research priority only.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Any, Callable

from portfolio_automation.social_intelligence.base import SourceStatus
from portfolio_automation.social_sources.base import SourceResult

logger = logging.getLogger("stockbot.social_sources.apewisdom")

_BASE = "https://apewisdom.io/api/v1.0/filter"
_DEFAULT_FILTERS = ["wallstreetbets"]
_RESULT_FIELDS = ("rank", "ticker", "name", "mentions", "upvotes",
                  "rank_24h_ago", "mentions_24h_ago")
_USER_AGENT = "stockbot-crowd-radar/1.0 (observe-only research; contact: operator)"
_POLITE_DELAY_S = 1.0  # client-side politeness — no server rate limit is published


def _default_http_get(url: str) -> dict[str, Any]:  # pragma: no cover - network
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


class ApeWisdomConnector:
    """Read-only ApeWisdom source. Never raises; returns SourceResult."""

    source_name = "apewisdom"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        crowd_radar_enabled: bool = False,
        http_get: Callable[[str], dict[str, Any]] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        cfg = config or {}
        self._enabled = bool(cfg.get("enabled", False))
        self._crowd_radar_enabled = bool(crowd_radar_enabled)
        self._max_pages = max(1, int(cfg.get("max_pages", 1)))
        self._filters = list(cfg.get("filters") or _DEFAULT_FILTERS)
        self._http_get = http_get or _default_http_get
        self._sleep = sleep or time.sleep

    # -- interface ----------------------------------------------------------

    def is_configured(self) -> bool:
        """No credentials needed — only the feature flag + source flag."""
        return self._crowd_radar_enabled and self._enabled

    def probe(self) -> SourceResult:
        """One page-1 request against the default filter to confirm reachability."""
        if not self.is_configured():
            return self._inert()
        flt = self._filters[0] if self._filters else _DEFAULT_FILTERS[0]
        try:
            data = self._http_get(f"{_BASE}/{urllib.parse.quote(flt)}/page/1")
            ok = isinstance(data, dict) and isinstance(data.get("results"), list)
            return SourceResult(
                self.source_name,
                SourceStatus.OK if ok else SourceStatus.DEGRADED,
                meta={"probe_filter": flt, "pages": data.get("pages") if isinstance(data, dict) else None},
                warnings=[] if ok else ["unexpected_probe_shape"],
            )
        except Exception as exc:
            return SourceResult(self.source_name, SourceStatus.ERROR,
                               warnings=[f"probe_error:{type(exc).__name__}"])

    def fetch(self) -> SourceResult:
        """Pull bounded aggregate rows across configured filters. Polite + bounded."""
        if not self.is_configured():
            return self._inert()
        raw_rows: list[dict[str, Any]] = []
        warnings: list[str] = []
        first = True
        for flt in self._filters:
            for page in range(1, self._max_pages + 1):
                if not first:
                    self._sleep(_POLITE_DELAY_S)
                first = False
                url = f"{_BASE}/{urllib.parse.quote(flt)}/page/{page}"
                try:
                    data = self._http_get(url)
                except Exception as exc:
                    warnings.append(f"fetch_error:{flt}:p{page}:{type(exc).__name__}")
                    break  # stop paging this filter; keep what we have
                if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                    warnings.append(f"bad_shape:{flt}:p{page}")
                    break
                for r in data["results"]:
                    if not isinstance(r, dict):
                        continue
                    row = {k: r.get(k) for k in _RESULT_FIELDS}
                    row["source_filter"] = flt
                    raw_rows.append(row)
                total_pages = data.get("pages")
                if isinstance(total_pages, int) and page >= total_pages:
                    break  # don't request past the last page

        if not raw_rows:
            status = SourceStatus.DEGRADED if warnings else SourceStatus.INSUFFICIENT_DATA
            return SourceResult(self.source_name, status, warnings=warnings or ["no_rows_returned"])
        status = SourceStatus.DEGRADED if warnings else SourceStatus.OK
        return SourceResult(self.source_name, status, records=raw_rows, warnings=warnings,
                           meta={"filters": self._filters, "max_pages": self._max_pages})

    def normalize(self, raw: SourceResult) -> SourceResult:
        """
        Collapse raw rows to one record per ticker with derived velocity metrics.
        A ticker seen under multiple filters is merged (mentions summed, best rank
        kept) and ``source_breadth`` counts how many filters mentioned it.
        """
        if raw.status in (SourceStatus.ERROR,):
            return raw
        by_ticker: dict[str, dict[str, Any]] = {}
        for r in raw.records:
            tk = str(r.get("ticker") or "").upper()
            if not tk:
                continue
            mentions = _num(r.get("mentions"))
            mentions_24h = _num(r.get("mentions_24h_ago"))
            upvotes = _num(r.get("upvotes"))
            rank = _num(r.get("rank"))
            rank_24h = _num(r.get("rank_24h_ago"))
            agg = by_ticker.get(tk)
            if agg is None:
                by_ticker[tk] = {
                    "ticker": tk,
                    "name": r.get("name"),
                    "mentions": mentions,
                    "mentions_24h_ago": mentions_24h,
                    "upvotes": upvotes,
                    "rank": rank,
                    "rank_24h_ago": rank_24h,
                    "filters": {r.get("source_filter")},
                }
            else:
                agg["mentions"] += mentions
                agg["mentions_24h_ago"] += mentions_24h
                agg["upvotes"] += upvotes
                agg["rank"] = min(agg["rank"], rank) if rank else agg["rank"]
                agg["filters"].add(r.get("source_filter"))

        records: list[dict[str, Any]] = []
        for tk, a in by_ticker.items():
            mentions = a["mentions"]
            mentions_24h = a["mentions_24h_ago"]
            records.append({
                "ticker": tk,
                "name": a["name"],
                "source": self.source_name,
                "mentions": mentions,
                "mentions_24h_ago": mentions_24h,
                "upvotes": a["upvotes"],
                "rank": a["rank"],
                "rank_24h_ago": a["rank_24h_ago"],
                "mention_delta_24h": mentions - mentions_24h,
                "mention_velocity_ratio": round(mentions / mentions_24h, 4) if mentions_24h else None,
                "rank_change_24h": (a["rank_24h_ago"] - a["rank"]) if (a["rank"] and a["rank_24h_ago"]) else None,
                "upvote_per_mention": round(a["upvotes"] / mentions, 4) if mentions else None,
                "source_filter": ",".join(sorted(str(f) for f in a["filters"] if f)),
                "source_breadth": len([f for f in a["filters"] if f]),
            })
        records.sort(key=lambda x: (x["mention_delta_24h"] is None, -(x["mention_delta_24h"] or 0)))
        return SourceResult(self.source_name, raw.status, records=records,
                           warnings=raw.warnings, meta=raw.meta)

    def health(self) -> SourceResult:
        if not self._crowd_radar_enabled or not self._enabled:
            return SourceResult(self.source_name, SourceStatus.DISABLED,
                               warnings=["crowd_radar or apewisdom disabled"])
        return self.probe()

    # -- helpers ------------------------------------------------------------

    def _inert(self) -> SourceResult:
        return SourceResult(self.source_name, SourceStatus.DISABLED,
                           warnings=["crowd_radar or apewisdom disabled"])


def _num(v: Any) -> float | int:
    try:
        if v is None:
            return 0
        return float(v) if not isinstance(v, (int, float)) else v
    except (TypeError, ValueError):
        return 0
