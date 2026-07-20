"""
Manager registry loader + registry-level validation.

Loads the version-controlled YAML registry (default
``config/institutional_managers.yaml``), validates each entry via
:mod:`schemas`, and enforces registry-level invariants:

  * ``schema_version`` present and supported
  * no duplicate CIK across managers
  * no overlapping effective windows for the same ``internal_id`` (a manager's
    metadata may be re-versioned over time, but the windows must not overlap so
    a point-in-time query resolves to exactly one record)

Provides point-in-time queries so a backtest at a given date only sees the
manager metadata effective on that date.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from .schemas import ManagerRecord, RegistryValidationError, validate_manager

SUPPORTED_SCHEMA_VERSIONS = frozenset({1})
DEFAULT_REGISTRY_PATH = Path("config/institutional_managers.yaml")


class ManagerRegistry:
    """A validated, point-in-time-queryable set of manager records."""

    def __init__(self, records: list[ManagerRecord], *, schema_version: int) -> None:
        self.schema_version = schema_version
        self._records = list(records)

    # -- point-in-time queries ------------------------------------------
    def all_records(self) -> list[ManagerRecord]:
        return list(self._records)

    def effective_on(self, as_of: date, *, enabled_only: bool = True) -> list[ManagerRecord]:
        """Records whose effective window covers ``as_of``.

        With ``enabled_only`` (default) only enabled managers are returned —
        this is the set an evaluation may actually ingest against.
        """
        out = [r for r in self._records if r.is_effective_on(as_of)]
        if enabled_only:
            out = [r for r in out if r.enabled]
        return out

    def enabled_records(self) -> list[ManagerRecord]:
        return [r for r in self._records if r.enabled]

    def by_cik(self, cik: str) -> ManagerRecord | None:
        for r in self._records:
            if r.cik == cik:
                return r
        return None


def _load_yaml(path: Path) -> dict[str, Any]:
    import yaml  # local import; repo already depends on PyYAML

    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise RegistryValidationError(
            f"registry {path}: top-level document must be a mapping"
        )
    return data


def validate_registry_dict(data: dict[str, Any]) -> ManagerRegistry:
    """Validate an already-parsed registry mapping into a ManagerRegistry."""
    schema_version = data.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise RegistryValidationError(
            f"registry: unsupported schema_version {schema_version!r} "
            f"(supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)})"
        )

    managers = data.get("managers")
    if not isinstance(managers, dict):
        raise RegistryValidationError(
            "registry: 'managers' must be a mapping of internal_id -> entry"
        )

    records: list[ManagerRecord] = []
    for internal_id, raw in managers.items():
        if not isinstance(internal_id, str) or not internal_id.strip():
            raise RegistryValidationError(
                f"registry: invalid manager key {internal_id!r}"
            )
        records.append(validate_manager(raw, internal_id))

    _check_duplicate_ciks(records)
    _check_overlapping_windows(records)

    return ManagerRegistry(records, schema_version=int(schema_version))


def load_registry(path: str | Path | None = None) -> ManagerRegistry:
    """Load + validate the registry YAML. Raises RegistryValidationError."""
    p = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
    if not p.exists():
        raise RegistryValidationError(f"registry file not found: {p}")
    return validate_registry_dict(_load_yaml(p))


def _check_duplicate_ciks(records: list[ManagerRecord]) -> None:
    seen: dict[str, str] = {}
    for r in records:
        prior = seen.get(r.cik)
        if prior is not None and prior != r.internal_id:
            raise RegistryValidationError(
                f"registry: duplicate CIK {r.cik} used by both "
                f"'{prior}' and '{r.internal_id}'"
            )
        # Same internal_id may repeat the CIK across re-versioned windows.
        seen.setdefault(r.cik, r.internal_id)


def _check_overlapping_windows(records: list[ManagerRecord]) -> None:
    by_id: dict[str, list[ManagerRecord]] = {}
    for r in records:
        by_id.setdefault(r.internal_id, []).append(r)
    for internal_id, group in by_id.items():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda r: r.effective_from)
        for a, b in zip(ordered, ordered[1:]):
            a_end = a.effective_to or date.max
            if a_end >= b.effective_from:
                raise RegistryValidationError(
                    f"registry: overlapping effective windows for "
                    f"'{internal_id}' ({a.effective_from}..{a.effective_to} "
                    f"overlaps {b.effective_from}..{b.effective_to})"
                )
