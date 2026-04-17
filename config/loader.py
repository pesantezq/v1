"""Structured config loader with profile overlays and history snapshots."""

from __future__ import annotations

import json
import math
import os
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from config.schema import (
    DEFAULT_CONFIDENCE_TIERS,
    DEFAULT_RANKING,
    DEFAULT_TIER_COOLDOWN_HOURS,
    normalize_structured_config,
    validate_structured_config,
)


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge overlay values into a copy of base."""
    merged = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def load_runtime_config_dict(
    config_path: str | os.PathLike[str] | None = None,
    *,
    profile: str | None = None,
    record_history: bool = True,
) -> dict[str, Any]:
    """
    Load legacy or structured config and return one resolved runtime config dict.

    Supported inputs:
    - `config.json` legacy file
    - `config/` structured directory with `base.json` + `profiles/*.json`
    - `config/base.json` direct path
    """
    source = Path(config_path or os.environ.get("CONFIG_PATH", "config.json"))
    source = source.resolve()

    if source.is_dir():
        return _load_structured_from_dir(source, profile=profile, record_history=record_history)

    if source.name == "base.json" and source.parent.name == "config":
        return _load_structured_from_dir(source.parent, profile=profile, record_history=record_history)

    if not source.exists():
        raise FileNotFoundError(f"Configuration path not found: {source}")

    data = _read_json(source)
    if _looks_structured(data):
        resolved = _sync_legacy_fields(_normalize_and_validate(data))
        resolved["config_runtime"] = {
            "source_mode": "structured_file",
            "config_path": str(source),
            "profile": None,
            "profile_diff": [],
            "history_snapshot": None,
        }
        return resolved

    resolved = _sync_legacy_fields(_legacy_to_structured(data))
    resolved["config_runtime"] = {
        "source_mode": "legacy_file",
        "config_path": str(source),
        "profile": None,
        "profile_diff": [],
        "history_snapshot": None,
    }
    return resolved


def _load_structured_from_dir(
    config_dir: Path,
    *,
    profile: str | None,
    record_history: bool,
) -> dict[str, Any]:
    base_path = config_dir / "base.json"
    if not base_path.exists():
        raise FileNotFoundError(f"Structured config missing base.json: {base_path}")

    base_data = _read_json(base_path)
    selected_profile = profile or os.environ.get("CONFIG_PROFILE") or None
    profile_diff: list[str] = []

    resolved = deepcopy(base_data)
    profile_path: Path | None = None
    if selected_profile:
        profile_path = config_dir / "profiles" / f"{selected_profile}.json"
        if not profile_path.exists():
            raise FileNotFoundError(f"Profile not found: {profile_path}")
        profile_data = _read_json(profile_path)
        resolved = deep_merge(base_data, profile_data)
        profile_diff = _diff_paths(base_data, resolved)

    resolved = _sync_legacy_fields(_normalize_and_validate(resolved))

    history_snapshot: str | None = None
    if record_history:
        history_snapshot = _write_history_snapshot(
            config_dir=config_dir,
            resolved=resolved,
            profile=selected_profile,
            source_files=[str(base_path)] + ([str(profile_path)] if profile_path else []),
            profile_diff=profile_diff,
        )

    resolved["config_runtime"] = {
        "source_mode": "structured_dir",
        "config_path": str(config_dir),
        "profile": selected_profile,
        "profile_diff": profile_diff,
        "history_snapshot": history_snapshot,
    }
    return resolved


def _normalize_and_validate(data: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_structured_config(data)
    validate_structured_config(normalized)
    return normalized


def _legacy_to_structured(data: dict[str, Any]) -> dict[str, Any]:
    """Add the new structured sections to a legacy config dict."""
    migrated = deepcopy(data)

    portfolio = migrated.setdefault("portfolio", {})
    rebalance_rules = migrated.setdefault("rebalance_rules", {})
    growth_mode = migrated.setdefault("growth_mode", {})
    watchlist_scanner = migrated.setdefault("watchlist_scanner", {})
    speculative_sleeve = migrated.setdefault("speculative_sleeve", {})

    portfolio.setdefault("cash_reserve_pct", float(portfolio.get("target_cash_weight", 0.05)))
    portfolio.setdefault("max_position_pct", float(growth_mode.get("concentration_cap", 0.40)))
    portfolio.setdefault("rebalance_band_pct", float(rebalance_rules.get("band_threshold", 0.07)))

    migrated.setdefault(
        "signals",
        {
            "min_signal_score": float(watchlist_scanner.get("min_signal_score", 0.50)),
            "min_confidence_score": float(watchlist_scanner.get("min_confidence_score", 0.50)),
            "theme_score_threshold": float(watchlist_scanner.get("theme_score_threshold", 0.40)),
            "price_change_alert_pct": float(watchlist_scanner.get("price_change_alert_pct", 3.0)),
            "volume_spike_factor": float(watchlist_scanner.get("volume_spike_factor", 1.5)),
            "cooldown_hours": float(int(watchlist_scanner.get("alert_cooldown_days", 3)) * 24),
            "min_evidence_count": int(watchlist_scanner.get("min_evidence_count", 2)),
            "confidence_tiers": deepcopy(DEFAULT_CONFIDENCE_TIERS),
            "cooldown": deepcopy(DEFAULT_TIER_COOLDOWN_HOURS),
        },
    )
    migrated.setdefault(
        "execution",
        {
            "recommend_only": True,
            "max_new_positions_per_day": int(speculative_sleeve.get("max_new_positions_per_month", 1)),
            "max_capital_per_day": float(portfolio.get("monthly_contribution", 0.0)),
        },
    )
    migrated.setdefault(
        "data",
        {
            "max_daily_calls": int(watchlist_scanner.get("max_daily_calls", 20)),
        },
    )
    migrated.setdefault(
        "watchlist",
        {
            "core": watchlist_scanner.get("watchlist", []),
            "tactical": [],
            "speculative": [],
        },
    )
    migrated.setdefault("ranking", deepcopy(DEFAULT_RANKING))
    return _normalize_and_validate(migrated)


def _sync_legacy_fields(data: dict[str, Any]) -> dict[str, Any]:
    """
    Synchronize the new structured sections back into the existing runtime keys.

    This keeps current consumers working without forcing a full refactor.
    """
    resolved = deepcopy(data)

    portfolio = resolved.setdefault("portfolio", {})
    growth_mode = resolved.setdefault("growth_mode", {})
    rebalance_rules = resolved.setdefault("rebalance_rules", {})
    signals = resolved.setdefault("signals", {})
    execution = resolved.setdefault("execution", {})
    data_cfg = resolved.setdefault("data", {})
    watchlist = resolved.setdefault("watchlist", {})
    ranking = resolved.setdefault("ranking", {})
    watchlist_scanner = resolved.setdefault("watchlist_scanner", {})

    cash_reserve_pct = float(portfolio.get("cash_reserve_pct", portfolio.get("target_cash_weight", 0.05)))
    max_position_pct = float(portfolio.get("max_position_pct", growth_mode.get("concentration_cap", 0.40)))
    rebalance_band_pct = float(portfolio.get("rebalance_band_pct", rebalance_rules.get("band_threshold", 0.07)))

    portfolio["cash_reserve_pct"] = cash_reserve_pct
    portfolio["max_position_pct"] = max_position_pct
    portfolio["rebalance_band_pct"] = rebalance_band_pct
    portfolio["target_cash_weight"] = cash_reserve_pct

    growth_mode["concentration_cap"] = max_position_pct
    rebalance_rules.setdefault("use_cash_before_selling", True)
    rebalance_rules.setdefault("direct_contributions_first", True)
    rebalance_rules.setdefault("trim_leverage_before_core", True)
    rebalance_rules.setdefault("avoid_taxable_sales", True)
    rebalance_rules.setdefault("panic_sell_protection", True)
    rebalance_rules["band_threshold"] = rebalance_band_pct

    signals["min_signal_score"] = float(signals.get("min_signal_score", watchlist_scanner.get("min_signal_score", 0.50)))
    signals["min_confidence_score"] = float(signals.get("min_confidence_score", watchlist_scanner.get("min_confidence_score", 0.50)))
    signals["theme_score_threshold"] = float(signals.get("theme_score_threshold", watchlist_scanner.get("theme_score_threshold", 0.40)))
    signals["price_change_alert_pct"] = float(signals.get("price_change_alert_pct", watchlist_scanner.get("price_change_alert_pct", 3.0)))
    signals["volume_spike_factor"] = float(signals.get("volume_spike_factor", watchlist_scanner.get("volume_spike_factor", 1.5)))
    signals["cooldown_hours"] = float(signals.get("cooldown_hours", int(watchlist_scanner.get("alert_cooldown_days", 3)) * 24))
    signals["min_evidence_count"] = int(signals.get("min_evidence_count", watchlist_scanner.get("min_evidence_count", 2)))
    confidence_tiers = signals.setdefault("confidence_tiers", {})
    for tier, value in DEFAULT_CONFIDENCE_TIERS.items():
        confidence_tiers[tier] = float(confidence_tiers.get(tier, value))
    cooldown_cfg = signals.setdefault("cooldown", {})
    for tier, value in DEFAULT_TIER_COOLDOWN_HOURS.items():
        cooldown_cfg[tier] = int(cooldown_cfg.get(tier, value))

    execution["recommend_only"] = bool(execution.get("recommend_only", True))
    execution["max_new_positions_per_day"] = int(execution.get("max_new_positions_per_day", 1))
    execution["max_capital_per_day"] = float(execution.get("max_capital_per_day", portfolio.get("monthly_contribution", 0.0)))

    data_cfg["max_daily_calls"] = int(data_cfg.get("max_daily_calls", watchlist_scanner.get("max_daily_calls", 20)))
    for key, value in DEFAULT_RANKING.items():
        ranking[key] = float(ranking.get(key, value))

    watchlist["core"] = watchlist.get("core", [])
    watchlist["tactical"] = watchlist.get("tactical", [])
    watchlist["speculative"] = watchlist.get("speculative", [])
    flattened_watchlist = []
    for bucket in ("core", "tactical", "speculative"):
        flattened_watchlist.extend(watchlist.get(bucket, []))

    watchlist_scanner.setdefault("enabled", True)
    watchlist_scanner["watchlist"] = flattened_watchlist
    watchlist_scanner["max_daily_calls"] = data_cfg["max_daily_calls"]
    watchlist_scanner["price_change_alert_pct"] = signals["price_change_alert_pct"]
    watchlist_scanner["volume_spike_factor"] = signals["volume_spike_factor"]
    watchlist_scanner["theme_score_threshold"] = signals["theme_score_threshold"]
    watchlist_scanner["min_signal_score"] = signals["min_signal_score"]
    watchlist_scanner["min_confidence_score"] = signals["min_confidence_score"]
    watchlist_scanner["min_evidence_count"] = signals["min_evidence_count"]
    watchlist_scanner["alert_cooldown_days"] = max(1, math.ceil(signals["cooldown_hours"] / 24.0))
    watchlist_scanner["confidence_tiers"] = deepcopy(confidence_tiers)
    watchlist_scanner["cooldown"] = deepcopy(cooldown_cfg)
    watchlist_scanner["ranking"] = deepcopy(ranking)
    watchlist_scanner.setdefault("output_dir", "outputs/latest")
    watchlist_scanner.setdefault("cache_dir", "data/watchlist_cache")

    resolved.setdefault("api_limits", {})
    resolved["api_limits"].setdefault("fmp_daily_calls_budget", 230)

    return resolved


def _write_history_snapshot(
    *,
    config_dir: Path,
    resolved: dict[str, Any],
    profile: str | None,
    source_files: list[str],
    profile_diff: list[str],
) -> str | None:
    """Write a timestamped resolved-config snapshot if it changed."""
    history_dir = config_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)

    serializable = deepcopy(resolved)
    config_hash = _hash_config(serializable)
    latest = _latest_history_snapshot(history_dir)
    if latest and latest.get("config_hash") == config_hash:
        return str(latest.get("snapshot_path") or "")

    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    label = (profile or "base").replace(" ", "_")
    file_path = history_dir / f"{timestamp}_{label}_{config_hash[:8]}.json"
    payload = {
        "saved_at": datetime.now().isoformat(),
        "profile": profile,
        "config_hash": config_hash,
        "source_files": source_files,
        "profile_diff": profile_diff,
        "resolved_config": serializable,
    }
    file_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return str(file_path)


def _latest_history_snapshot(history_dir: Path) -> dict[str, Any] | None:
    snapshots = sorted(history_dir.glob("*.json"))
    if not snapshots:
        return None
    latest = snapshots[-1]
    payload = _read_json(latest)
    payload["snapshot_path"] = str(latest)
    return payload


def _diff_paths(base: dict[str, Any], other: dict[str, Any], prefix: str = "") -> list[str]:
    changes: list[str] = []
    keys = sorted(set(base) | set(other))
    for key in keys:
        dotted = f"{prefix}.{key}" if prefix else str(key)
        left = base.get(key)
        right = other.get(key)
        if isinstance(left, dict) and isinstance(right, dict):
            changes.extend(_diff_paths(left, right, dotted))
        elif left != right:
            changes.append(dotted)
    return changes


def _hash_config(data: dict[str, Any]) -> str:
    import hashlib

    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _looks_structured(data: dict[str, Any]) -> bool:
    return all(key in data for key in ("portfolio", "signals", "execution", "data", "watchlist"))


def _read_json(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Config file must contain a top-level object: {path}")
    return data
