"""Shared parse helpers for Northstar contract deserialization."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional


def parse_optional_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse the kernel's ISO-8601 Z encoding back to an aware datetime."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"expected ISO-8601 string, got {type(value).__name__}")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"naive datetime in serialized form: {value!r}")
    return dt.astimezone(timezone.utc)


def parse_optional_date(value: Optional[str]) -> Optional[date]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"expected ISO date string, got {type(value).__name__}")
    return date.fromisoformat(value)
