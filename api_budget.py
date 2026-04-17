"""
Shared Alpha Vantage daily call budget.

Prevents the two independent AV clients (market_data.py and
watchlist_scanner/alpha_vantage_client.py) from collectively exceeding
the free-tier 25 calls/day limit.

Backed by data/av_budget.json — a lightweight JSON file that auto-resets at
midnight. Thread-safety is not needed (single-process sequential execution).

Policy
------
- ``daily_limit`` (default 25): total calls allowed per calendar day.
- ``holdings_reserve`` (default 7): calls set aside for portfolio price fetches
  (5 holdings + 2 headroom). The watchlist scanner may only use the remainder.

Usage
-----
    from api_budget import AVDailyBudget

    budget = AVDailyBudget()
    if budget.can_reserve("holdings"):
        budget.reserve("holdings")
        # ... make API call ...
    else:
        # fall back to stale cache
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Optional

logger = logging.getLogger("portfolio_automation.api_budget")

_DEFAULT_PATH = Path("data/av_budget.json")
DAILY_LIMIT: int = 25
HOLDINGS_RESERVE: int = 7   # 5 holdings + 2 headroom


class AVDailyBudget:
    """
    Shared daily quota manager for all Alpha Vantage consumers.

    Args:
        path:             JSON file that persists today's usage.
        daily_limit:      Total calls allowed per day (AV free tier = 25).
        holdings_reserve: Calls reserved for portfolio holdings fetches.
                          Scanner is capped at ``daily_limit - holdings_reserve``.
    """

    def __init__(
        self,
        path: Path = _DEFAULT_PATH,
        daily_limit: int = DAILY_LIMIT,
        holdings_reserve: int = HOLDINGS_RESERVE,
    ) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.daily_limit = daily_limit
        self.holdings_reserve = holdings_reserve

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> dict:
        today = date.today().isoformat()
        if self._path.exists():
            try:
                d = json.loads(self._path.read_text(encoding="utf-8"))
                if d.get("date") == today:
                    return d
            except Exception:
                pass
        return {"date": today, "holdings": 0, "scanner": 0}

    def _save(self, d: dict) -> None:
        try:
            self._path.write_text(json.dumps(d, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("av_budget.json write failed: %s", exc)

    # ── Public API ────────────────────────────────────────────────────────────

    def used_today(self, source: Optional[str] = None) -> int:
        """Total calls used today (or by a specific source if provided)."""
        d = self._load()
        if source:
            return d.get(source, 0)
        return d.get("holdings", 0) + d.get("scanner", 0)

    def remaining(self) -> int:
        """Remaining calls in the shared daily pool."""
        return max(0, self.daily_limit - self.used_today())

    def scanner_remaining(self) -> int:
        """
        Remaining calls available to the watchlist scanner.

        Capped at ``daily_limit - holdings_reserve`` regardless of actual
        holdings usage; further limited by overall remaining budget.
        """
        scanner_cap = self.daily_limit - self.holdings_reserve
        scanner_used = self.used_today("scanner")
        return min(
            max(0, scanner_cap - scanner_used),
            self.remaining(),
        )

    def can_reserve(self, source: str, units: int = 1) -> bool:
        """
        Return True if ``source`` can spend ``units`` more calls without
        exceeding its allocation.

        Sources: ``"holdings"`` (uses total remaining),
                 ``"scanner"`` (uses scanner_remaining).
        """
        if source == "scanner":
            return self.scanner_remaining() >= units
        return self.remaining() >= units

    def reserve(self, source: str, units: int = 1) -> bool:
        """
        Deduct ``units`` calls for ``source``.

        Returns True on success, False if the budget is exhausted (and logs
        a warning). The budget file is only written on success.
        """
        if not self.can_reserve(source, units):
            logger.warning(
                "AV budget exhausted for %s: cannot reserve %d call(s) "
                "(total_remaining=%d, scanner_remaining=%d)",
                source, units, self.remaining(), self.scanner_remaining(),
            )
            return False
        d = self._load()
        d[source] = d.get(source, 0) + units
        self._save(d)
        holdings = d.get("holdings", 0)
        scanner = d.get("scanner", 0)
        logger.debug(
            "AV budget: %s −%d → holdings=%d scanner=%d total=%d/%d",
            source, units, holdings, scanner,
            holdings + scanner, self.daily_limit,
        )
        return True

    def status_line(self) -> str:
        """One-line summary for logging / email degraded-mode banner."""
        d = self._load()
        h = d.get("holdings", 0)
        s = d.get("scanner", 0)
        total = h + s
        return (
            f"AV budget: {total}/{self.daily_limit} calls used today "
            f"(holdings={h}, scanner={s}, scanner_remaining={self.scanner_remaining()})"
        )
