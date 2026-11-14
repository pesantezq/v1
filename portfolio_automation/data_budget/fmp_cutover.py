"""FMP-4 — fail-closed cutover guard + pure Basic provider-window budget model.

GUARD / UTILITY ONLY. This module performs no network I/O, reads no secrets,
reads no cache/data artifacts, writes nothing, and is not a data pipeline, a
controller, a task register or a rights registry.

Why it exists
-------------
The paid FMP Starter subscription ends 2026-11-27 and the account transitions to
the Free/Basic tier. Neither (a) Free/Basic entitlement for Northstar symbols nor
(b) the right to keep using cached/derived paid-tier data after the plan ends is
confirmed. Until a human operator records a scoped grant AND an independent
deployment gate reopens access, the only safe posture is a universal HOLD:

* every outbound FMP HTTP attempt is refused (direct, strict-live, generic
  dynamic ``get_json``, legacy v3/v4, and every retry), even when the daily
  budget is 0 / uncapped;
* the FMP client disk cache serves NOTHING (fresh or stale) — subscribed data
  is never served silently under unresolved rights;
* the scheduled daily / monthly entrypoints refuse early with a non-zero exit
  (see ``scripts/fmp_cutover_guard.sh``) before ``.env`` credential loading.

Cutoff choice
-------------
``FMP_CUTOVER_HOLD_UTC = 2026-11-26T00:00:00Z`` — a full 24h before
2026-11-27T00:00Z. "Nov 27" in the earliest civil timezone (UTC+14) begins at
2026-11-26T10:00Z, so this margin is effective before ANY wall-clock
interpretation of the provider switch, independent of DST or account timezone.

Bypass policy
-------------
There is deliberately NO environment-variable, config or flag bypass. The clock
is injectable for deterministic tests only, and an injected clock that is naive
(timezone-ambiguous), non-datetime, or raises is treated as POST-cutover (HOLD).
Reopening requires a code change reviewed against a recorded scoped rights
grant plus a separate deployment gate (docs/FMP_CUTOVER_FAILCLOSED.md).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Cutover hold
# ---------------------------------------------------------------------------

#: Provider plan change (Starter → Free) as communicated: 2026-11-27.
FMP_STARTER_END_UTC = datetime(2026, 11, 27, 0, 0, 0, tzinfo=timezone.utc)
#: Effective universal HOLD instant (24h safety margin; see module docstring).
FMP_CUTOVER_HOLD_UTC = datetime(2026, 11, 26, 0, 0, 0, tzinfo=timezone.utc)
#: Same instant as POSIX epoch seconds (mirrored in scripts/fmp_cutover_guard.sh).
FMP_CUTOVER_HOLD_EPOCH = int(FMP_CUTOVER_HOLD_UTC.timestamp())

#: Exit status used by entrypoint guards when the HOLD is active.
CUTOVER_HOLD_EXIT_CODE = 3

Clock = Callable[[], datetime]


def _system_clock() -> datetime:
    return datetime.now(timezone.utc)


def resolve_now(clock: Optional[Clock] = None) -> Tuple[Optional[datetime], str]:
    """Return ``(aware_utc_now, "")`` or ``(None, reason)`` when time is unknowable.

    ``None`` means "cannot prove we are pre-cutover" and every caller must treat
    it as HOLD. Naive datetimes are rejected rather than guessed (wall-clock /
    DST ambiguity must never resolve toward allowing traffic).
    """
    # The injected test clock may only advance the observed time, never rewind
    # a real post-cutover process into a fictitious pre-cutover state.
    try:
        real_now = _system_clock()
        now = real_now if clock is None else clock()
    except Exception as exc:  # noqa: BLE001 — any clock failure is a HOLD
        return None, f"clock_error:{type(exc).__name__}"
    if not isinstance(now, datetime):
        return None, "clock_not_datetime"
    if now.tzinfo is None or now.utcoffset() is None:
        return None, "clock_naive_ambiguous"
    if (not isinstance(real_now, datetime)
            or real_now.tzinfo is None or real_now.utcoffset() is None):
        return None, "system_clock_invalid"
    return max(real_now.astimezone(timezone.utc),
               now.astimezone(timezone.utc)), ""


def cutover_hold_reason(clock: Optional[Clock] = None) -> Optional[str]:
    """``None`` when FMP traffic is still permitted (pre-cutover), else a reason."""
    now, err = resolve_now(clock)
    if now is None:
        return f"fmp_cutover_hold:{err}"
    if now >= FMP_CUTOVER_HOLD_UTC:
        return "fmp_cutover_hold:post_cutover_rights_unresolved"
    return None


def is_cutover_hold(clock: Optional[Clock] = None) -> bool:
    return cutover_hold_reason(clock) is not None


class FMPCutoverHold(RuntimeError):
    """Raised by non-client callers (entrypoint guard). ``fmp_client`` raises its
    own ``FMPCutoverRefused`` (an ``FMPError`` subclass) so existing ``except
    FMPError`` handlers fail closed instead of crashing."""


def check_entrypoint(entrypoint: str, clock: Optional[Clock] = None) -> int:
    """Pure decision for a scheduled entrypoint: 0 = proceed, 3 = HOLD."""
    return CUTOVER_HOLD_EXIT_CODE if is_cutover_hold(clock) else 0


# ---------------------------------------------------------------------------
# Legacy endpoint classification
# ---------------------------------------------------------------------------

_LEGACY_MARKERS = ("/api/v3/", "/api/v4/", "api/v3/", "api/v4/")
_LEGACY_PREFIXES = ("v3/", "v4/")


def is_legacy_endpoint(endpoint: str, base_url: str = "") -> bool:
    """True for legacy ``/api/v3`` or ``/api/v4`` paths (not on Basic/unverified)."""
    ep = (endpoint or "").strip().lstrip("/").lower()
    full = f"{(base_url or '').rstrip('/').lower()}/{ep}"
    if ep.startswith(_LEGACY_PREFIXES):
        return True
    return any(m in full or m in ep for m in _LEGACY_MARKERS)


# ---------------------------------------------------------------------------
# Pure Basic provider-window budget model (INACTIVE while Basic is prohibited)
# ---------------------------------------------------------------------------

BASIC_DAILY_CALL_LIMIT = 250
BASIC_ROLLING_WINDOW = timedelta(hours=24)
BASIC_BANDWIDTH_LIMIT_BYTES = 500_000_000   # 500 MB (decimal = the smaller reading)
BASIC_BANDWIDTH_WINDOW = timedelta(days=30)


@dataclass(frozen=True)
class Admission:
    allowed: bool
    reason: str


@dataclass
class BasicProviderWindowBudget:
    """Deterministic, in-memory model of a future FMP Basic window.

    * Rolling 24h attempt count ``<= 250`` — no reliance on the disputed fixed
      daily reset (3 PM EST per FAQ, not guaranteed per account).
    * 30-day raw bandwidth ``<= 500 MB`` and HARD fail-closed while upstream
      account usage / balance is unknown (``upstream_usage_bytes is None``).
    * Every ATTEMPT counts, recorded before HTTP, including failed attempts and
      each retry (``record_attempt`` is per HTTP attempt, not per success).
    * Cached/stale data is never admissible: rights unresolved.
    * Legacy v3/v4 endpoints are refused outright.

    ``basic_use_authorized`` defaults to False: the model refuses everything
    until a human scoped grant exists. It is NOT wired into ``fmp_client``; the
    universal cutover HOLD takes precedence over any admission it would grant.
    """

    basic_use_authorized: bool = False
    upstream_usage_bytes: Optional[int] = None  # None == unknown → fail closed
    daily_limit: int = BASIC_DAILY_CALL_LIMIT
    bandwidth_limit_bytes: int = BASIC_BANDWIDTH_LIMIT_BYTES
    attempts: List[datetime] = field(default_factory=list)
    byte_events: List[Tuple[datetime, int]] = field(default_factory=list)

    @staticmethod
    def _aware(ts: datetime) -> datetime:
        if not isinstance(ts, datetime) or ts.tzinfo is None or ts.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware (no wall-clock guessing)")
        return ts.astimezone(timezone.utc)

    def attempts_in_window(self, now: datetime) -> int:
        now = self._aware(now)
        lo = now - BASIC_ROLLING_WINDOW
        return sum(1 for t in self.attempts if lo < t <= now)

    def bytes_in_window(self, now: datetime) -> int:
        now = self._aware(now)
        lo = now - BASIC_BANDWIDTH_WINDOW
        return sum(n for t, n in self.byte_events if lo < t <= now)

    def admit(self, now: datetime, endpoint: str, base_url: str = "") -> Admission:
        try:
            now = self._aware(now)
        except ValueError:
            return Admission(False, "clock_naive_ambiguous")
        if not self.basic_use_authorized:
            return Admission(False, "basic_use_not_authorized")
        if is_legacy_endpoint(endpoint, base_url):
            return Admission(False, "legacy_v3_v4_rejected")
        # Configurable test limits can tighten provider limits, never increase.
        if self.attempts_in_window(now) + 1 > min(self.daily_limit, BASIC_DAILY_CALL_LIMIT):
            return Admission(False, "rolling_24h_call_limit")
        if self.upstream_usage_bytes is None:
            return Admission(False, "bandwidth_upstream_usage_unknown")
        if (isinstance(self.upstream_usage_bytes, bool)
                or not isinstance(self.upstream_usage_bytes, int)
                or self.upstream_usage_bytes < 0):
            return Admission(False, "bandwidth_upstream_usage_invalid")
        used = self.upstream_usage_bytes + self.bytes_in_window(now)
        if used >= min(self.bandwidth_limit_bytes, BASIC_BANDWIDTH_LIMIT_BYTES):
            return Admission(False, "bandwidth_30d_limit")
        return Admission(True, "ok")

    def record_attempt(self, now: datetime) -> int:
        """Count ONE outbound attempt (call BEFORE the HTTP request, every retry)."""
        self.attempts.append(self._aware(now))
        return len(self.attempts)

    def record_bytes(self, now: datetime, n_bytes: int) -> None:
        if isinstance(n_bytes, bool) or not isinstance(n_bytes, int) or n_bytes < 0:
            raise ValueError("raw bandwidth bytes must be a non-negative integer")
        self.byte_events.append((self._aware(now), n_bytes))

    @staticmethod
    def cached_data_admission() -> Admission:
        return Admission(False, "cached_data_rights_unresolved")


# ---------------------------------------------------------------------------
# CLI (manual / operator use; scheduled scripts use the bash guard, which needs
# no virtualenv and runs before any .env load)
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="FMP-4 cutover HOLD check (no network).")
    p.add_argument("--entrypoint", default="manual")
    a = p.parse_args(argv)
    reason = cutover_hold_reason()
    rc = check_entrypoint(a.entrypoint)
    if rc:
        print(f"FMP_CUTOVER_HOLD entrypoint={a.entrypoint} reason={reason} "
              f"hold_utc={FMP_CUTOVER_HOLD_UTC.isoformat()} exit={rc}", file=sys.stderr)
    else:
        print(f"fmp_cutover: pre-cutover entrypoint={a.entrypoint} "
              f"hold_utc={FMP_CUTOVER_HOLD_UTC.isoformat()}")
    return rc


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
