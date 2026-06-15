from __future__ import annotations
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from portfolio_automation.data_budget.usage_ledger import UsageLedger
from portfolio_automation.data_budget.scheduler import RunModeScheduler, DEFAULT_RUN_MODES

_DISABLE_FILE = Path("config/fmp_governor.DISABLED")


class RunMode:
    GUI_REFRESH = "gui_refresh"
    DAILY = "daily"
    WEEKLY_REVIEW = "weekly_review"
    MONTHLY = "monthly"
    DISCOVERY = "discovery"
    HISTORICAL_REPLAY = "historical_replay"


class _TokenBucket:
    """In-process token bucket. rate_per_min sustained, burst hard cap."""

    def __init__(self, *, rate_per_min: int, burst: int,
                 now: Callable[[], float] = time.monotonic) -> None:
        self._rate = rate_per_min / 60.0
        self._burst = burst
        self._now = now
        self._tokens = float(burst)
        self._last = now()

    def _refill(self) -> None:
        t = self._now()
        self._tokens = min(self._burst, self._tokens + (t - self._last) * self._rate)
        self._last = t

    def try_consume(self, n: int = 1) -> bool:
        self._refill()
        if self._tokens >= n:
            self._tokens -= n
            return True
        return False


def _killed(config: dict) -> bool:
    if not config.get("enabled", True):
        return True
    if os.environ.get("STOCKBOT_FMP_GOVERNOR_DISABLED") == "1":
        return True
    if _DISABLE_FILE.exists():
        return True
    return False


def _extract_symbols(args, kwargs) -> list[str]:
    val = kwargs.get("symbols") or kwargs.get("symbol")
    if val is None and args:
        val = args[0]
    if isinstance(val, str):
        return [val]
    if isinstance(val, (list, tuple)):
        return [str(s) for s in val]
    return []


def _empty_like(method_name: str):
    # Mirror the shape callers expect from skipped calls: quote methods return a
    # dict ({sym: {...}} / single quote); everything else (history, bulk/batch
    # profiles, ratios, key-metrics — all List[Dict]) returns an empty list.
    return {} if "quote" in method_name else []


def _wait_for_token(bucket: "_TokenBucket", *, max_wait_s: float = 2.0) -> None:
    waited = 0.0
    while waited < max_wait_s and not bucket.try_consume(1):
        time.sleep(0.05)
        waited += 0.05


class GovernedFMPClient:
    """Proxies FMPClient methods by name; enforces token bucket + run-mode budget
    + bandwidth guard, records every call to the ledger. Never raises into callers."""

    def __init__(self, *, fmp_client: Any, run_mode: str, ledger: UsageLedger,
                 scheduler: RunModeScheduler, bucket: _TokenBucket,
                 monthly_bandwidth_bytes: int, now_month: Optional[str] = None) -> None:
        self._c = fmp_client
        self._run_mode = run_mode
        self._ledger = ledger
        self._sched = scheduler
        self._bucket = bucket
        self._bw_guard = monthly_bandwidth_bytes
        self._month = now_month or datetime.now(timezone.utc).strftime("%Y-%m")
        self._calls_this_run = 0

    def _bandwidth_exhausted(self) -> bool:
        try:
            return self._ledger.monthly_bytes(month=self._month) >= self._bw_guard
        except Exception:
            return False

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def __getattr__(self, name: str) -> Any:
        # Only proxy public callables; dunders/privates fall through normally.
        target = getattr(self._c, name)
        if not callable(target) or name.startswith("_"):
            return target

        def _wrapped(*args, **kwargs):
            symbols = _extract_symbols(args, kwargs)
            skip = self._sched.should_skip(self._run_mode,
                                           bandwidth_exhausted=self._bandwidth_exhausted())
            over = self._sched.over_run_budget(self._run_mode, calls_so_far=self._calls_this_run)
            if skip or over:
                reason = "bandwidth_guard" if skip else "run_budget"
                self._ledger.record(run_mode=self._run_mode, endpoint=name, symbols=symbols,
                                    cache_hit=False, bytes_=0, skipped_reason=reason,
                                    ts=self._now_iso())
                return _empty_like(name)
            # cache_only modes (historical_replay) fall through to the cache-first
            # inner client: a cache hit makes 0 live calls; a miss makes 1 (spec:
            # "cache-only by default; live only if cache missing").
            if not self._bucket.try_consume(1):
                # high-priority waits briefly; low-priority skips
                if self._sched.priority(self._run_mode) == "low":
                    self._ledger.record(run_mode=self._run_mode, endpoint=name, symbols=symbols,
                                        cache_hit=False, bytes_=0, skipped_reason="rate_limited",
                                        ts=self._now_iso())
                    return _empty_like(name)
                _wait_for_token(self._bucket)
            before = getattr(self._c, "last_response_bytes", 0)
            result = target(*args, **kwargs)
            after = getattr(self._c, "last_response_bytes", 0)
            # A fresh HTTP fetch changes last_response_bytes; an unchanged value
            # means the method served from cache (it never reached _raw_get).
            # (Method-level granularity: a multi-symbol batch counts as one unit;
            # the authoritative per-HTTP count remains FMPClient._CallCounter.)
            made_call = after != before
            self._calls_this_run += 1 if made_call else 0
            self._ledger.record(run_mode=self._run_mode, endpoint=name, symbols=symbols,
                                cache_hit=not made_call, bytes_=(after if made_call else 0),
                                skipped_reason=None, ts=self._now_iso())
            return result

        return _wrapped


class FMPBudgetGovernor:
    """Single factory. client(run_mode) -> GovernedFMPClient (enabled) or plain FMPClient."""

    def __init__(self, *, db_path: Path | str, cache_dir: Path | str,
                 config: Optional[dict] = None) -> None:
        self._config = config or {}
        self.ledger = UsageLedger(db_path)
        self._cache_dir = Path(cache_dir)
        run_modes = self._config.get("run_modes") or DEFAULT_RUN_MODES
        self._sched = RunModeScheduler(run_modes)
        self._bw_bytes = int(float(self._config.get("monthly_bandwidth_gb", 20)) * 1024**3)
        self._rate = int(self._config.get("rate_per_min", 240))
        self._burst = int(self._config.get("burst", 300))

    def client(self, *, run_mode: str, fmp_client: Any = None,
               now_month: Optional[str] = None) -> Any:
        if fmp_client is None:
            from fmp_client import FMPClient
            # daily_budget=0 => the inner client is uncapped; the GOVERNOR is the
            # budget authority (token bucket + run-mode budget + monthly bandwidth).
            # Letting the inner FMPClient also enforce its 230 default would
            # double-cap and silently throttle under the governor.
            fmp_client = FMPClient(cache_dir=self._cache_dir, daily_budget=0)
        if _killed(self._config):
            return fmp_client  # kill-switch: today's proven behavior
        bucket = _TokenBucket(rate_per_min=self._rate, burst=self._burst)
        return GovernedFMPClient(
            fmp_client=fmp_client, run_mode=run_mode, ledger=self.ledger,
            scheduler=self._sched, bucket=bucket,
            monthly_bandwidth_bytes=self._bw_bytes, now_month=now_month)
