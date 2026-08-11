"""Canonical serialization and deterministic identity for Northstar contracts.

ONE serialization + ONE hash primitive for every Northstar contract, so no two
contract families can drift apart on separators, ordering, or coercion.

Relationship to the Intraday Lab primitive
------------------------------------------
``portfolio_automation/intraday_lab/identity.py`` established the repository's
identity discipline (sha256 over sorted, compact canonical JSON; explicit
schema/identity eras). This module inherits that algorithm family but is
deliberately STRICTER: intraday's ``canonical_json`` uses ``default=str``,
which silently coerces any unknown object to its repr. That is acceptable for
a closed, era-audited bar corpus; it is not acceptable for open-ended contract
payloads, where a silent coercion would make identity depend on Python object
reprs. Here, non-JSON-safe values are a hard error.

Canonical rules (identity-bearing — changing any of these mints a new
kernel schema version):

* JSON with ``sort_keys=True`` and compact separators ``(",", ":")``
* allowed scalar types: str, int, float, bool, None
  - floats must be finite (NaN/Infinity rejected — not valid JSON)
* ``datetime`` values must be timezone-aware; they are converted to UTC and
  encoded as ISO-8601 with a trailing ``Z`` and microsecond precision.
  Naive datetimes are REJECTED (PIT ambiguity is never acceptable).
* ``date`` values are encoded as ISO ``YYYY-MM-DD`` strings.
* enums are represented as plain strings by convention (the contracts use
  string constants + frozensets, per repo convention) — no Enum objects.
* mappings must have str keys; sequences (list/tuple) are encoded as arrays.
* anything else (sets, bytes, custom objects, Decimal) is a hard error —
  callers convert explicitly so the encoding decision is visible in code.
* no secrets: canonical payloads are persisted and hashed; see
  ``sources._reject_secret_material`` for the descriptor-level guard.

Hash: sha256 over the canonical UTF-8 bytes, full 64-char hex.
Deterministic ID: ``<prefix>_<first 32 hex chars>`` (128 bits, matching the
intraday truncation width).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime, timezone
from typing import Any


class CanonicalizationError(ValueError):
    """A value cannot be represented canonically (fail closed, never coerce)."""


# ── Identifier / hash / version formats (kernel-wide, centralized) ─────────
#
# deterministic_id defines the on-the-wire ID format, so this module also owns
# its validation: every contract ID is ``<prefix>_`` + exactly 32 lowercase
# hexadecimal characters, and every content hash is exactly 64 lowercase
# hexadecimal characters. Contracts call these validators instead of growing
# per-module startswith() checks.

_LOWER_HEX = re.compile(r"[0-9a-f]+")
_SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
ID_HEX_LENGTH = 32
HASH_HEX_LENGTH = 64


def validate_contract_id(name: str, value: Any, prefix: str) -> str:
    """Require ``<prefix>_`` + exactly 32 lowercase hex chars. Fail closed."""
    expected = f"{prefix}_"
    if not isinstance(value, str) or not value.startswith(expected):
        raise ValueError(f"{name} must start with {expected!r}, got {value!r}")
    digest = value[len(expected):]
    if len(digest) != ID_HEX_LENGTH or not _LOWER_HEX.fullmatch(digest):
        raise ValueError(
            f"{name} must be {expected!r} + exactly {ID_HEX_LENGTH} lowercase "
            f"hexadecimal characters, got {value!r}"
        )
    return value


def validate_content_hash(name: str, value: Any) -> str:
    """Require exactly 64 lowercase hex chars (a full sha256). Fail closed."""
    if (
        not isinstance(value, str)
        or len(value) != HASH_HEX_LENGTH
        or not _LOWER_HEX.fullmatch(value)
    ):
        raise ValueError(
            f"{name} must be exactly {HASH_HEX_LENGTH} lowercase hexadecimal "
            f"characters, got {value!r}"
        )
    return value


def schema_era(schema_version: str) -> int:
    """The identity era of a schema version: its MAJOR component.

    Kernel identity policy: ``schema_version`` participates in deterministic
    identity through its major version only. A major bump means the schema's
    SEMANTICS changed, so the same bytes under a new major version are a new
    object (new identity era — inherited from the Intraday Lab discipline).
    Minor/patch versions are additive/non-semantic by definition and must NOT
    fragment identity: re-stating identical information under 1.1.0 keeps the
    1.0.0 identity.
    """
    if not isinstance(schema_version, str) or not _SEMVER.fullmatch(schema_version):
        raise CanonicalizationError(
            f"invalid schema_version {schema_version!r} — expected MAJOR.MINOR.PATCH"
        )
    return int(schema_version.split(".", 1)[0])


def encode_datetime(value: datetime) -> str:
    """ISO-8601 UTC with trailing Z. Rejects naive datetimes."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise CanonicalizationError(
            "naive datetime rejected — point-in-time fields must be timezone-aware"
        )
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _canonicalize(value: Any, path: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError(f"non-finite float at {path}")
        return value
    if isinstance(value, datetime):
        return encode_datetime(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise CanonicalizationError(f"non-string mapping key at {path}: {k!r}")
            out[k] = _canonicalize(v, f"{path}.{k}")
        return out
    if isinstance(value, (list, tuple)):
        return [_canonicalize(v, f"{path}[{i}]") for i, v in enumerate(value)]
    raise CanonicalizationError(
        f"type {type(value).__name__} at {path} is not canonically representable; "
        "convert explicitly before building the contract"
    )


def canonical_dumps(payload: Any) -> str:
    """The single canonical serializer. Deterministic or a hard error."""
    return json.dumps(
        _canonicalize(payload, "$"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def content_hash(payload: Any) -> str:
    """sha256 hex over the canonical bytes — the single hash primitive."""
    return hashlib.sha256(canonical_dumps(payload).encode("utf-8")).hexdigest()


def deterministic_id(prefix: str, identity_payload: Any) -> str:
    """Deterministic ID: stable semantic fields → canonical JSON → sha256.

    Callers pass ONLY the documented identity-bearing fields of a contract
    (never acquisition metadata such as retrieved_at / provenance.recorded_at),
    so re-acquiring identical information yields the identical ID.
    """
    if not prefix.isidentifier():
        raise CanonicalizationError(f"invalid id prefix: {prefix!r}")
    return f"{prefix}_{content_hash(identity_payload)[:32]}"
