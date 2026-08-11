"""Strict unordered-collection normalization for Milestone-2 contracts.

Container discipline (0B.2 hardening, repair 7): unordered collection fields
accept ONLY ``list`` and ``tuple``. Strings, bytes, mappings, sets, frozensets,
generators, and arbitrary iterables are rejected — ``entity_ids="IBM"`` must
raise instead of silently becoming ``("B", "I", "M")``, and a generator must
not be silently consumed to emptiness. Entries are validated, duplicates are
rejected, and the result is the canonical SORTED tuple (unordered-set
semantics: equality ≡ identity ≡ serialization).

Wildcard semantics: on fields where ``"*"`` already means "explicitly
unrestricted", it must be the SOLE value — mixing the wildcard with concrete
entries is contradictory and rejected. Fields without an existing wildcard
meaning simply never pass ``wildcard=True``.
"""
from __future__ import annotations

from typing import Any, Tuple


def _require_list_or_tuple(name: str, value: Any) -> tuple:
    if isinstance(value, (str, bytes)):
        raise ValueError(
            f"{name} must be a list or tuple, not a {type(value).__name__} — "
            f"a bare string would be iterated character-by-character"
        )
    if not isinstance(value, (list, tuple)):
        raise ValueError(
            f"{name} must be a list or tuple, got {type(value).__name__}"
        )
    return tuple(value)


def normalize_string_set(
    name: str,
    value: Any,
    *,
    allow_empty: bool,
    wildcard: bool = False,
) -> Tuple[str, ...]:
    """Validate + normalize a string collection to its sorted canonical tuple."""
    items = _require_list_or_tuple(name, value)
    if not items:
        if allow_empty:
            return ()
        raise ValueError(f"{name} must not be empty")
    for v in items:
        if not v or not isinstance(v, str):
            raise ValueError(f"{name} entries must be non-empty strings")
    if len(items) != len(set(items)):
        raise ValueError(f"{name} must not contain duplicates (unordered set semantics)")
    if "*" in items:
        if not wildcard:
            raise ValueError(f"{name} does not define wildcard semantics; '*' is invalid here")
        if len(items) != 1:
            raise ValueError(
                f"{name}: '*' means explicitly unrestricted and must be the sole "
                f"value — mixing it with concrete entries is contradictory"
            )
    return tuple(sorted(items))


def normalize_ref_set(name: str, value: Any, ref_type: type, key: str) -> tuple:
    """Validate + normalize a reference collection to a sorted canonical tuple.

    ``key`` is the attribute providing the sort/dedup identity (e.g.
    ``snapshot_id``).
    """
    items = _require_list_or_tuple(name, value)
    if not all(isinstance(r, ref_type) for r in items):
        raise ValueError(f"every {name} entry must be an {ref_type.__name__}")
    ids = [getattr(r, key) for r in items]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{name} must not contain duplicate {key}s")
    return tuple(sorted(items, key=lambda r: getattr(r, key)))
