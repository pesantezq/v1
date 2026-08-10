"""Shared parse helpers for Northstar contract deserialization."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional


def require_schema_version(data: dict, *, expected: str, contract: str) -> str:
    """Fail-closed schema-version gate for every persisted v1 contract.

    Kernel v1 has NO migration/compatibility mechanism (deliberately — see
    docs/NORTHSTAR_CONTRACTS.md §9), so deserialization requires the exact
    supported version string. Missing, empty, non-string, and unknown/future
    versions are all rejected; accepting an unknown version would mean
    interpreting bytes under semantics this code cannot know.
    """
    value = data.get("schema_version")
    if value is None:
        raise ValueError(f"{contract}: schema_version is required")
    if not isinstance(value, str) or not value:
        raise ValueError(
            f"{contract}: schema_version must be a non-empty string, got {value!r}"
        )
    if value != expected:
        raise ValueError(
            f"{contract}: unsupported schema_version {value!r} — this kernel "
            f"supports exactly {expected!r} (fail closed; no migration framework)"
        )
    return value


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
