"""
Signal Registry — config-driven catalog of known signal types.

Loads signal definitions from config/signal_registry.yaml (by default) and
provides lookup, filtering, and annotation helpers.

This module is observe/lookup-only in v1. It does NOT change scoring weights,
alert ranking, or recommendation outcomes. Use it to:
  - look up metadata for a known signal_id
  - check whether a signal is actionable, discovery-only, or requires corroboration
  - annotate output records with governance metadata
  - validate that a signal_id is known before treating it as actionable

Unknown signals are non-actionable by design.  Call require() to raise on
unknown, or get() to receive None.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Supported vocabulary (informational — not enforced at load time so that
# new categories / domains can be added to the YAML before the code is updated)
# ---------------------------------------------------------------------------

VALID_CATEGORIES: frozenset[str] = frozenset({
    "price_action",
    "momentum",
    "volume",
    "fundamentals",
    "news",
    "macro",
    "theme",
    "portfolio_risk",
    "exit_risk",
    "replay_only",
    "unknown",
})

VALID_SOURCE_DOMAINS: frozenset[str] = frozenset({
    "scanner",
    "watchlist",
    "theme_engine",
    "discovery",
    "historical_replay",
    "portfolio",
    "policy",
    "manual",
    "unknown",
})

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SignalRegistryError(Exception):
    """Raised for invalid registry configuration or unknown required signal."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class SignalDefinition:
    signal_id: str
    display_name: str
    category: str
    source_domain: str
    actionable: bool
    discovery_only: bool
    requires_corroboration: bool
    default_weight: float
    confidence_floor: float | None
    description: str
    enabled: bool = True


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class SignalRegistry:
    """
    Immutable lookup table over a set of SignalDefinition objects.

    Constructed by load_signal_registry(); not instantiated directly.
    """

    def __init__(self, definitions: list[SignalDefinition]) -> None:
        seen: set[str] = set()
        for d in definitions:
            if d.signal_id in seen:
                raise SignalRegistryError(
                    f"Duplicate signal_id in registry: {d.signal_id!r}"
                )
            seen.add(d.signal_id)
        self._definitions: dict[str, SignalDefinition] = {
            d.signal_id: d for d in definitions
        }

    # ── Lookup ──────────────────────────────────────────────────────────────

    def get(self, signal_id: str) -> SignalDefinition | None:
        """Return the SignalDefinition or None if unknown."""
        return self._definitions.get(signal_id)

    def require(self, signal_id: str) -> SignalDefinition:
        """Return the SignalDefinition or raise SignalRegistryError if unknown."""
        definition = self._definitions.get(signal_id)
        if definition is None:
            raise SignalRegistryError(
                f"Unknown signal_id: {signal_id!r}. "
                "Register it in config/signal_registry.yaml before using it."
            )
        return definition

    # ── Enumeration ─────────────────────────────────────────────────────────

    def all(self) -> list[SignalDefinition]:
        """Return all definitions (including disabled)."""
        return list(self._definitions.values())

    def enabled(self) -> list[SignalDefinition]:
        """Return only definitions where enabled=True."""
        return [d for d in self._definitions.values() if d.enabled]

    def by_category(self, category: str) -> list[SignalDefinition]:
        """Return all definitions with the given category (including disabled)."""
        return [d for d in self._definitions.values() if d.category == category]

    def by_source_domain(self, source_domain: str) -> list[SignalDefinition]:
        """Return all definitions with the given source_domain (including disabled)."""
        return [d for d in self._definitions.values() if d.source_domain == source_domain]

    # ── Boolean predicates ───────────────────────────────────────────────────

    def is_actionable(self, signal_id: str) -> bool:
        """Return True only if the signal is known AND marked actionable."""
        d = self._definitions.get(signal_id)
        return bool(d and d.actionable)

    def is_discovery_only(self, signal_id: str) -> bool:
        """Return True if discovery_only=True, or if the signal is unknown."""
        d = self._definitions.get(signal_id)
        if d is None:
            return True  # unknown → treat as discovery-only
        return d.discovery_only

    def requires_corroboration(self, signal_id: str) -> bool:
        """Return True if the signal requires corroboration, or if unknown."""
        d = self._definitions.get(signal_id)
        if d is None:
            return True  # unknown → always require corroboration
        return d.requires_corroboration

    def validate_signal_id(self, signal_id: str) -> bool:
        """Return True if the signal_id is known in the registry."""
        return signal_id in self._definitions

    # ── Annotation helper ────────────────────────────────────────────────────

    def annotate_signal(
        self,
        signal_id: str,
        metadata: dict | None = None,
    ) -> dict[str, Any]:
        """
        Return a governance metadata dict for signal_id.

        For known signals: all SignalDefinition fields plus known=True.
        For unknown signals: safe non-actionable defaults plus known=False.
        Extra metadata (if provided) is merged in after registry fields.
        """
        d = self._definitions.get(signal_id)
        if d is not None:
            result: dict[str, Any] = {
                "signal_id": d.signal_id,
                "known": True,
                "category": d.category,
                "source_domain": d.source_domain,
                "actionable": d.actionable,
                "discovery_only": d.discovery_only,
                "requires_corroboration": d.requires_corroboration,
                "default_weight": d.default_weight,
                "confidence_floor": d.confidence_floor,
                "description": d.description,
            }
        else:
            result = {
                "signal_id": signal_id,
                "known": False,
                "category": "unknown",
                "source_domain": "unknown",
                "actionable": False,
                "discovery_only": True,
                "requires_corroboration": True,
                "default_weight": 0.0,
                "confidence_floor": None,
                "description": f"Unknown signal: {signal_id!r}. Not in registry.",
            }
        if metadata:
            result.update(metadata)
        return result


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_definition(d: SignalDefinition) -> None:
    if not d.signal_id or not d.signal_id.strip():
        raise SignalRegistryError("signal_id must be non-empty")
    if d.actionable and d.discovery_only:
        raise SignalRegistryError(
            f"Signal {d.signal_id!r}: actionable and discovery_only cannot both be "
            "True. A discovery-only signal cannot directly drive portfolio actions."
        )
    if not (0.0 <= d.default_weight <= 1.0):
        raise SignalRegistryError(
            f"Signal {d.signal_id!r}: default_weight {d.default_weight} is out of "
            "range [0.0, 1.0]."
        )
    if d.discovery_only and not d.requires_corroboration:
        raise SignalRegistryError(
            f"Signal {d.signal_id!r}: discovery_only=True requires "
            "requires_corroboration=True. Discovery-only signals must not be "
            "promoted to actionable without corroboration."
        )


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG_PATH = Path("config") / "signal_registry.yaml"


def load_signal_registry(
    path: Path | str | None = None,
) -> SignalRegistry:
    """
    Load a SignalRegistry from a YAML config file.

    path: Path to the YAML file.  Defaults to config/signal_registry.yaml
          resolved relative to the current working directory.

    Raises:
        SignalRegistryError: If the YAML is structurally invalid, a required
            field is missing, a duplicate signal_id is present, or a
            validation rule is violated.
        FileNotFoundError: If the config file does not exist.
    """
    import yaml  # local import — keeps module usable even if pyyaml is absent

    resolved = Path(path) if path is not None else _DEFAULT_CONFIG_PATH

    try:
        raw = resolved.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Signal registry config not found at {resolved!r}. "
            "Create config/signal_registry.yaml or pass an explicit path."
        )

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise SignalRegistryError(f"Failed to parse {resolved}: {exc}") from exc

    if not isinstance(data, dict) or "signals" not in data:
        raise SignalRegistryError(
            f"{resolved}: expected top-level 'signals' key, "
            f"got {type(data).__name__}"
        )

    raw_signals = data["signals"]
    if not isinstance(raw_signals, list):
        raise SignalRegistryError(
            f"{resolved}: 'signals' must be a list, "
            f"got {type(raw_signals).__name__}"
        )

    definitions: list[SignalDefinition] = []
    for i, entry in enumerate(raw_signals):
        if not isinstance(entry, dict):
            raise SignalRegistryError(
                f"{resolved}: signals[{i}] must be a dict, "
                f"got {type(entry).__name__}"
            )
        try:
            d = _build_definition(entry, i)
        except SignalRegistryError:
            raise
        except Exception as exc:
            sid = entry.get("signal_id", f"<entry {i}>")
            raise SignalRegistryError(
                f"{resolved}: error loading signal {sid!r}: {exc}"
            ) from exc
        _validate_definition(d)
        definitions.append(d)

    return SignalRegistry(definitions)


def _build_definition(entry: dict, index: int) -> SignalDefinition:
    def _require(key: str) -> Any:
        if key not in entry:
            sid = entry.get("signal_id", f"<entry {index}>")
            raise SignalRegistryError(
                f"Signal {sid!r} is missing required field {key!r}"
            )
        return entry[key]

    signal_id = str(_require("signal_id")).strip()
    display_name = str(_require("display_name")).strip()
    category = str(_require("category")).strip()
    source_domain = str(_require("source_domain")).strip()
    actionable = bool(_require("actionable"))
    discovery_only = bool(_require("discovery_only"))
    requires_corroboration = bool(_require("requires_corroboration"))

    raw_weight = _require("default_weight")
    try:
        default_weight = float(raw_weight)
    except (TypeError, ValueError):
        raise SignalRegistryError(
            f"Signal {signal_id!r}: default_weight {raw_weight!r} is not a number"
        )

    raw_floor = entry.get("confidence_floor")
    if raw_floor is not None:
        try:
            confidence_floor: float | None = float(raw_floor)
        except (TypeError, ValueError):
            raise SignalRegistryError(
                f"Signal {signal_id!r}: confidence_floor {raw_floor!r} is not a number"
            )
    else:
        confidence_floor = None

    description = str(_require("description")).strip()
    enabled = bool(entry.get("enabled", True))

    return SignalDefinition(
        signal_id=signal_id,
        display_name=display_name,
        category=category,
        source_domain=source_domain,
        actionable=actionable,
        discovery_only=discovery_only,
        requires_corroboration=requires_corroboration,
        default_weight=default_weight,
        confidence_floor=confidence_floor,
        description=description,
        enabled=enabled,
    )
