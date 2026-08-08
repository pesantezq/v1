"""Provider-neutral intraday bar access. Research-only, HISTORICAL namespace.

Session-1 scope: normalize + status. No bulk backfill, no strategy, no fills.

FMP FINDINGS (probed 2026-08-08, configured account, read-only):
  endpoint          /stable/historical-chart/{timeframe}
  5min              ENTITLED   (SPY, AAPL; 78 bars per full session)
  1min              NOT ENTITLED — HTTP 402 Payment Required
  depth             >= 2017-08 verified (2017/2020/2023/2025/2026 all full)
  fields            date, open, high, low, close, volume
  timestamp         BAR_OPEN, naive US/Eastern wall-clock (no offset supplied)
  session           REGULAR ONLY; early closes real (2025-11-28 -> 42 bars,
                    last 12:55 for a 13:00 close)
  adjustment        SPLIT-ADJUSTED (AAPL 2020-08-27 closes ~125, not the ~500
                    that actually printed pre-4:1-split)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

from portfolio_automation.intraday_lab.models import (
    IntradayBar, BarValidationError, TIMEFRAMES,
)

SCHEMA_VERSION = "1"

FMP_INTRADAY_ENDPOINT = "/stable/historical-chart/{timeframe}"
FMP_SOURCE = "fmp"

# The provider returns naive wall-clock strings. They are US/Eastern; attaching
# the wrong zone silently shifts every session boundary by 4-5 hours.
PROVIDER_TZ = ZoneInfo("America/New_York")

# What the provider's `date` field means. Proven, not assumed: a 13:00 early
# close yields a final bar of 12:55, which is only consistent with BAR_OPEN.
TIMESTAMP_SEMANTIC = "BAR_OPEN"

# Split back-adjusted. Material for point-in-time work: the adjustment uses a
# corporate action that had NOT occurred at the bar's own timestamp. Safe for
# return-based research; UNSAFE for any rule keyed to absolute price levels.
ADJUSTMENT_STATE = "split_adjusted"

# Explicit no-data states — never a silent empty list.
STATUS_OK = "OK"
STATUS_NO_DATA = "NO_DATA"
STATUS_DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
STATUS_NOT_ENTITLED = "NOT_ENTITLED"
STATUS_MALFORMED = "MALFORMED_RESPONSE"
STATUS_SESSION_CLOSED = "SESSION_CLOSED"


class IntradayDataError(RuntimeError):
    pass


def parse_provider_timestamp(raw: str) -> datetime:
    """`'2026-08-03 09:30:00'` (US/Eastern wall-clock) -> aware UTC.

    Uses a real IANA zone so DST is handled by the calendar rather than a fixed
    offset — a fixed -5 would misplace every bar for eight months of the year.
    """
    try:
        naive = datetime.strptime(str(raw).strip(), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError) as exc:
        raise BarValidationError(f"unparseable provider timestamp {raw!r}") from exc
    return naive.replace(tzinfo=PROVIDER_TZ).astimezone(timezone.utc)


def normalize_fmp_rows(rows: Any, *, symbol: str, timeframe: str,
                       retrieved_at: datetime | None = None) -> list[IntradayBar]:
    """Provider payload -> validated IntradayBar list.

    Raises on a malformed payload rather than returning fewer bars: a partially
    parsed dataset that silently drops rows would corrupt the fingerprint and
    quietly change research results.
    """
    if timeframe not in TIMEFRAMES:
        raise IntradayDataError(
            f"timeframe {timeframe!r} unsupported (1min is NOT entitled on this "
            f"account — HTTP 402); supported: {sorted(TIMEFRAMES)}")
    if not isinstance(rows, list):
        raise IntradayDataError(f"expected a list payload, got {type(rows).__name__}")

    endpoint = FMP_INTRADAY_ENDPOINT.format(timeframe=timeframe)
    out: list[IntradayBar] = []
    for row in rows:
        if not isinstance(row, dict):
            raise IntradayDataError(f"expected dict rows, got {type(row).__name__}")
        missing = {"date", "open", "high", "low", "close", "volume"} - set(row)
        if missing:
            raise IntradayDataError(f"row missing fields: {sorted(missing)}")
        out.append(IntradayBar(
            symbol=symbol,
            timeframe=timeframe,
            bar_start_at=parse_provider_timestamp(row["date"]),
            open=float(row["open"]), high=float(row["high"]),
            low=float(row["low"]), close=float(row["close"]),
            volume=float(row["volume"]),
            source=FMP_SOURCE,
            source_endpoint=endpoint,
            retrieved_at=retrieved_at,
            adjustment_state=ADJUSTMENT_STATE,
        ))
    return out


def fetch_status(rows: Any, *, http_status: int | None = None) -> str:
    """Classify a provider response into an explicit state. No silent zeros."""
    if http_status == 402:
        return STATUS_NOT_ENTITLED
    if http_status is not None and http_status >= 400:
        return STATUS_DATA_UNAVAILABLE
    if rows is None:
        return STATUS_DATA_UNAVAILABLE
    if not isinstance(rows, list):
        return STATUS_MALFORMED
    if not rows:
        return STATUS_NO_DATA
    return STATUS_OK
