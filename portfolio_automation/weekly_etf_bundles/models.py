"""
Typed models for the weekly ETF bundle subsystem.

Kept deliberately small and dependency-free (dataclasses only) so they can be
imported by config, analysis, scoring, predictions, and tests without pulling in
market-data or governance modules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class WeeklyEtfConfigError(ValueError):
    """Raised when config/weekly_etf_bundles.yaml is invalid. Fail-closed."""


@dataclass(frozen=True)
class BundleMember:
    symbol: str
    role: str = ""
    weight: float | None = None  # None → equal-weighted at materialization time

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"symbol": self.symbol, "role": self.role}
        if self.weight is not None:
            d["weight"] = self.weight
        return d


@dataclass(frozen=True)
class Bundle:
    id: str
    name: str
    benchmark: str
    members: tuple[BundleMember, ...]
    description: str = ""
    enabled: bool = True
    display_order: int = 1000
    weighting_method: str = "equal"

    @property
    def symbols(self) -> list[str]:
        return [m.symbol for m in self.members]

    def resolved_weights(self) -> dict[str, float]:
        """Materialize target weights. Equal by default; custom weights are
        used verbatim (validated to sum to 1.0 at load time). Never mutates
        membership — this is a read-only projection used for bundle-level
        weighted metrics only, NOT an allocation."""
        n = len(self.members)
        if n == 0:
            return {}
        if self.weighting_method == "custom" and all(m.weight is not None for m in self.members):
            return {m.symbol: float(m.weight) for m in self.members}
        return {m.symbol: 1.0 / n for m in self.members}

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "benchmark": self.benchmark,
            "enabled": self.enabled,
            "display_order": self.display_order,
            "weighting_method": self.weighting_method,
            "members": [m.to_dict() for m in self.members],
        }


@dataclass(frozen=True)
class WeeklyEtfConfig:
    schema_version: int
    defaults: dict[str, Any]
    bundles: tuple[Bundle, ...]
    content_hash: str
    source_path: str = ""

    @property
    def enabled_bundles(self) -> list[Bundle]:
        """Bundles that appear in current outputs, ordered for display.
        Disabled bundles are intentionally excluded."""
        return sorted(
            (b for b in self.bundles if b.enabled),
            key=lambda b: (b.display_order, b.name),
        )

    @property
    def all_symbols(self) -> list[str]:
        """Every symbol needed for a run (enabled bundle members + their
        benchmarks + the default benchmark), de-duplicated, sorted."""
        syms: set[str] = set()
        default_bm = str(self.defaults.get("benchmark", "SPY"))
        syms.add(default_bm)
        for b in self.enabled_bundles:
            syms.add(b.benchmark)
            syms.update(b.symbols)
        return sorted(syms)

    def bundle(self, bundle_id: str) -> Bundle | None:
        return next((b for b in self.bundles if b.id == bundle_id), None)
