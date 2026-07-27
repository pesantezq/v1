"""
Load + validate the curated ETF bundle configuration.

Fail-closed: any structural or semantic error raises WeeklyEtfConfigError so a
run aborts before it can freeze predictions against a broken configuration.

The content hash is a sha256 over the NORMALIZED semantic content (parsed +
canonicalized), so comment/whitespace edits do not churn it but any change to
bundle membership, benchmarks, weights, or defaults does. Every prediction and
artifact records this hash for full traceability.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import yaml

from portfolio_automation.weekly_etf_bundles.models import (
    Bundle,
    BundleMember,
    WeeklyEtfConfig,
    WeeklyEtfConfigError,
)

logger = logging.getLogger("stockbot.weekly_etf_bundles.config")

_DEFAULT_REL = ("config", "weekly_etf_bundles.yaml")
_SUPPORTED_SCHEMA = 1
_WEIGHT_SUM_TOL = 1e-6
_VALID_WEIGHTING = {"equal", "custom"}

_DEFAULT_DEFAULTS: dict[str, Any] = {
    "benchmark": "SPY",
    "weighting_method": "equal",
    "minimum_history_days": 200,
    "minimum_bundle_coverage": 0.80,
}


def default_config_path(root: str | Path = ".") -> Path:
    return Path(root).resolve().joinpath(*_DEFAULT_REL)


def _canonical_hash(schema_version: int, defaults: dict[str, Any],
                    bundles: list[dict[str, Any]]) -> str:
    payload = {
        "schema_version": schema_version,
        "defaults": defaults,
        "bundles": bundles,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _norm_symbol(sym: Any) -> str:
    s = str(sym or "").strip().upper()
    if not s:
        raise WeeklyEtfConfigError("empty symbol in bundle membership")
    return s


def _parse_member(raw: Any, *, bundle_id: str) -> BundleMember:
    if isinstance(raw, str):
        return BundleMember(symbol=_norm_symbol(raw))
    if not isinstance(raw, dict):
        raise WeeklyEtfConfigError(
            f"bundle {bundle_id!r}: member must be a mapping or symbol string, got {type(raw).__name__}"
        )
    sym = _norm_symbol(raw.get("symbol"))
    weight = raw.get("weight")
    if weight is not None:
        try:
            weight = float(weight)
        except (TypeError, ValueError):
            raise WeeklyEtfConfigError(
                f"bundle {bundle_id!r} symbol {sym}: weight {weight!r} is not a number"
            )
        if weight < 0:
            raise WeeklyEtfConfigError(
                f"bundle {bundle_id!r} symbol {sym}: weight must be non-negative"
            )
    return BundleMember(symbol=sym, role=str(raw.get("role", "") or ""), weight=weight)


def _parse_bundle(raw: Any, defaults: dict[str, Any]) -> Bundle:
    if not isinstance(raw, dict):
        raise WeeklyEtfConfigError(f"each bundle must be a mapping, got {type(raw).__name__}")
    bundle_id = str(raw.get("id") or "").strip()
    if not bundle_id:
        raise WeeklyEtfConfigError("bundle is missing a non-empty 'id'")

    members_raw = raw.get("members")
    if not isinstance(members_raw, list) or not members_raw:
        raise WeeklyEtfConfigError(f"bundle {bundle_id!r}: 'members' must be a non-empty list")
    members = tuple(_parse_member(m, bundle_id=bundle_id) for m in members_raw)

    seen: set[str] = set()
    for m in members:
        if m.symbol in seen:
            raise WeeklyEtfConfigError(f"bundle {bundle_id!r}: duplicate symbol {m.symbol}")
        seen.add(m.symbol)

    weighting = str(raw.get("weighting_method", defaults.get("weighting_method", "equal"))).lower()
    if weighting not in _VALID_WEIGHTING:
        raise WeeklyEtfConfigError(
            f"bundle {bundle_id!r}: weighting_method must be one of {sorted(_VALID_WEIGHTING)}, got {weighting!r}"
        )
    if weighting == "custom":
        if any(m.weight is None for m in members):
            raise WeeklyEtfConfigError(
                f"bundle {bundle_id!r}: weighting_method=custom requires an explicit weight on every member"
            )
        total = sum(float(m.weight) for m in members)
        if abs(total - 1.0) > _WEIGHT_SUM_TOL:
            raise WeeklyEtfConfigError(
                f"bundle {bundle_id!r}: custom weights must sum to 1.0 (got {total:.6f})"
            )

    return Bundle(
        id=bundle_id,
        name=str(raw.get("name", bundle_id)),
        description=str(raw.get("description", "") or ""),
        benchmark=_norm_symbol(raw.get("benchmark", defaults.get("benchmark", "SPY"))),
        enabled=bool(raw.get("enabled", True)),
        display_order=int(raw.get("display_order", 1000)),
        weighting_method=weighting,
        members=members,
    )


def load_config(path: str | Path | None = None, *, root: str | Path = ".") -> WeeklyEtfConfig:
    """Load + validate the bundle config. Raises WeeklyEtfConfigError on any
    problem (fail-closed)."""
    cfg_path = Path(path).resolve() if path else default_config_path(root)
    if not cfg_path.exists():
        raise WeeklyEtfConfigError(f"config not found: {cfg_path}")

    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise WeeklyEtfConfigError(f"YAML parse error in {cfg_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise WeeklyEtfConfigError(f"config root must be a mapping, got {type(raw).__name__}")

    schema_version = raw.get("schema_version")
    if schema_version != _SUPPORTED_SCHEMA:
        raise WeeklyEtfConfigError(
            f"unsupported schema_version {schema_version!r} (this build supports {_SUPPORTED_SCHEMA})"
        )

    defaults = {**_DEFAULT_DEFAULTS, **(raw.get("defaults") or {})}
    try:
        defaults["minimum_history_days"] = int(defaults["minimum_history_days"])
        defaults["minimum_bundle_coverage"] = float(defaults["minimum_bundle_coverage"])
    except (TypeError, ValueError) as exc:
        raise WeeklyEtfConfigError(f"invalid defaults: {exc}") from exc
    if not (0.0 <= defaults["minimum_bundle_coverage"] <= 1.0):
        raise WeeklyEtfConfigError("defaults.minimum_bundle_coverage must be in [0, 1]")

    bundles_raw = raw.get("bundles")
    if not isinstance(bundles_raw, list) or not bundles_raw:
        raise WeeklyEtfConfigError("config must define a non-empty 'bundles' list")

    bundles = tuple(_parse_bundle(b, defaults) for b in bundles_raw)

    ids = [b.id for b in bundles]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise WeeklyEtfConfigError(f"duplicate bundle id(s): {dupes}")

    if not any(b.enabled for b in bundles):
        raise WeeklyEtfConfigError("no enabled bundles — nothing to analyze (fail-closed)")

    content_hash = _canonical_hash(
        int(schema_version), defaults, [b.to_dict() for b in bundles]
    )

    return WeeklyEtfConfig(
        schema_version=int(schema_version),
        defaults=defaults,
        bundles=bundles,
        content_hash=content_hash,
        source_path=str(cfg_path),
    )
