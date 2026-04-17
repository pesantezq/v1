"""Validation and normalization helpers for structured portfolio config."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


class ConfigValidationError(ValueError):
    """Raised when the resolved runtime config is invalid."""


REQUIRED_STRUCTURED_SECTIONS = (
    "portfolio",
    "signals",
    "execution",
    "data",
    "watchlist",
)

DEFAULT_CONFIDENCE_TIERS = {
    "high": 0.80,
    "medium": 0.65,
    "low": 0.50,
}

DEFAULT_TIER_COOLDOWN_HOURS = {
    "high": 6,
    "medium": 24,
    "low": 72,
}

DEFAULT_RANKING = {
    "signal_weight": 0.45,
    "confidence_weight": 0.30,
    "evidence_weight": 0.15,
    "freshness_weight": 0.10,
}


def normalize_symbol_list(symbols: Any, *, field_name: str) -> list[str]:
    """Normalize a symbol list to uppercase unique tickers in input order."""
    if symbols is None:
        return []
    if not isinstance(symbols, list):
        raise ConfigValidationError(f"{field_name} must be a list of ticker strings")

    normalized: list[str] = []
    seen: set[str] = set()
    for idx, raw in enumerate(symbols):
        if not isinstance(raw, str):
            raise ConfigValidationError(
                f"{field_name}[{idx}] must be a string ticker symbol"
            )
        symbol = raw.strip().upper()
        if not symbol:
            continue
        if symbol not in seen:
            normalized.append(symbol)
            seen.add(symbol)
    return normalized


def normalize_structured_config(data: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized copy of the structured config."""
    normalized = deepcopy(data)

    signals = normalized.setdefault("signals", {})
    if isinstance(signals, dict):
        signals.setdefault("min_signal_score", 0.50)
        signals.setdefault("min_confidence_score", 0.50)
        signals.setdefault("theme_score_threshold", 0.40)
        signals.setdefault("price_change_alert_pct", 3.0)
        signals.setdefault("volume_spike_factor", 1.5)
        signals.setdefault("cooldown_hours", 72)
        signals.setdefault("min_evidence_count", 2)
        confidence_tiers = signals.setdefault("confidence_tiers", {})
        if isinstance(confidence_tiers, dict):
            for tier, value in DEFAULT_CONFIDENCE_TIERS.items():
                confidence_tiers.setdefault(tier, value)
        cooldown_cfg = signals.setdefault("cooldown", {})
        if isinstance(cooldown_cfg, dict):
            for tier, value in DEFAULT_TIER_COOLDOWN_HOURS.items():
                cooldown_cfg.setdefault(tier, value)

    ranking = normalized.setdefault("ranking", {})
    if isinstance(ranking, dict):
        for key, value in DEFAULT_RANKING.items():
            ranking.setdefault(key, value)

    watchlist = normalized.setdefault("watchlist", {})
    if isinstance(watchlist, dict):
        for bucket in ("core", "tactical", "speculative"):
            watchlist[bucket] = normalize_symbol_list(
                watchlist.get(bucket, []),
                field_name=f"watchlist.{bucket}",
            )

    portfolio = normalized.get("portfolio")
    if isinstance(portfolio, dict):
        holdings = portfolio.get("holdings", [])
        if isinstance(holdings, list):
            for holding in holdings:
                if isinstance(holding, dict) and "symbol" in holding and isinstance(holding["symbol"], str):
                    holding["symbol"] = holding["symbol"].strip().upper()

    watchlist_scanner = normalized.get("watchlist_scanner")
    if isinstance(watchlist_scanner, dict) and "watchlist" in watchlist_scanner:
        watchlist_scanner["watchlist"] = normalize_symbol_list(
            watchlist_scanner.get("watchlist", []),
            field_name="watchlist_scanner.watchlist",
        )

    return normalized


def validate_structured_config(data: dict[str, Any]) -> None:
    """Validate the structured config sections required by the phase-1 loader."""
    issues: list[str] = []

    for section in REQUIRED_STRUCTURED_SECTIONS:
        if section not in data:
            issues.append(f"Missing required section: {section}")
        elif not isinstance(data.get(section), dict):
            issues.append(f"Section {section} must be an object")

    if issues:
        raise ConfigValidationError("\n".join(issues))

    portfolio = data["portfolio"]
    signals = data["signals"]
    execution = data["execution"]
    data_cfg = data["data"]
    watchlist = data["watchlist"]
    ranking = data.get("ranking", {})

    def _check_float(
        section: dict[str, Any],
        key: str,
        *,
        min_value: float | None = None,
        max_value: float | None = None,
    ) -> None:
        raw = section.get(key)
        if raw is None:
            issues.append(f"Missing required value: {key}")
            return
        try:
            value = float(raw)
        except (TypeError, ValueError):
            issues.append(f"{key} must be numeric")
            return
        if min_value is not None and value < min_value:
            issues.append(f"{key} must be >= {min_value}")
        if max_value is not None and value > max_value:
            issues.append(f"{key} must be <= {max_value}")

    def _check_int(
        section: dict[str, Any],
        key: str,
        *,
        min_value: int | None = None,
        max_value: int | None = None,
    ) -> None:
        raw = section.get(key)
        if raw is None:
            issues.append(f"Missing required value: {key}")
            return
        try:
            value = int(raw)
        except (TypeError, ValueError):
            issues.append(f"{key} must be an integer")
            return
        if min_value is not None and value < min_value:
            issues.append(f"{key} must be >= {min_value}")
        if max_value is not None and value > max_value:
            issues.append(f"{key} must be <= {max_value}")

    _check_float(portfolio, "cash_reserve_pct", min_value=0.0, max_value=1.0)
    _check_float(portfolio, "max_position_pct", min_value=0.0, max_value=1.0)
    _check_float(portfolio, "rebalance_band_pct", min_value=0.0, max_value=1.0)

    _check_float(signals, "min_signal_score", min_value=0.0, max_value=1.0)
    _check_float(signals, "min_confidence_score", min_value=0.0, max_value=1.0)
    _check_float(signals, "theme_score_threshold", min_value=0.0, max_value=1.0)
    _check_float(signals, "price_change_alert_pct", min_value=0.0, max_value=100.0)
    _check_float(signals, "volume_spike_factor", min_value=1.0, max_value=20.0)
    _check_float(signals, "cooldown_hours", min_value=1.0, max_value=24.0 * 30.0)
    _check_int(signals, "min_evidence_count", min_value=0, max_value=20)

    confidence_tiers = signals.get("confidence_tiers", {})
    if not isinstance(confidence_tiers, dict):
        issues.append("signals.confidence_tiers must be an object")
    else:
        for tier in ("high", "medium", "low"):
            try:
                value = float(confidence_tiers.get(tier))
            except (TypeError, ValueError):
                issues.append(f"signals.confidence_tiers.{tier} must be numeric")
                continue
            if value < 0.0 or value > 1.0:
                issues.append(f"signals.confidence_tiers.{tier} must be between 0.0 and 1.0")
        try:
            high = float(confidence_tiers.get("high"))
            medium = float(confidence_tiers.get("medium"))
            low = float(confidence_tiers.get("low"))
            if not (high >= medium >= low):
                issues.append("signals.confidence_tiers must satisfy high >= medium >= low")
        except (TypeError, ValueError):
            pass

    cooldown_cfg = signals.get("cooldown", {})
    if not isinstance(cooldown_cfg, dict):
        issues.append("signals.cooldown must be an object")
    else:
        for tier in ("high", "medium", "low"):
            try:
                value = int(cooldown_cfg.get(tier))
            except (TypeError, ValueError):
                issues.append(f"signals.cooldown.{tier} must be an integer hour value")
                continue
            if value < 0 or value > 24 * 30:
                issues.append(f"signals.cooldown.{tier} must be between 0 and {24 * 30}")
        try:
            high = int(cooldown_cfg.get("high"))
            medium = int(cooldown_cfg.get("medium"))
            low = int(cooldown_cfg.get("low"))
            if not (high <= medium <= low):
                issues.append("signals.cooldown must satisfy high <= medium <= low")
        except (TypeError, ValueError):
            pass

    recommend_only = execution.get("recommend_only")
    if not isinstance(recommend_only, bool):
        issues.append("execution.recommend_only must be true or false")
    _check_int(execution, "max_new_positions_per_day", min_value=0, max_value=20)
    _check_float(execution, "max_capital_per_day", min_value=0.0)

    _check_int(data_cfg, "max_daily_calls", min_value=1, max_value=500)

    for bucket in ("core", "tactical", "speculative"):
        try:
            normalize_symbol_list(watchlist.get(bucket, []), field_name=f"watchlist.{bucket}")
        except ConfigValidationError as exc:
            issues.append(str(exc))

    if not isinstance(portfolio.get("holdings", []), list):
        issues.append("portfolio.holdings must be a list")

    if ranking is not None and not isinstance(ranking, dict):
        issues.append("ranking must be an object")
    elif isinstance(ranking, dict):
        for key in DEFAULT_RANKING:
            _check_float(ranking, key, min_value=0.0, max_value=1.0)
        try:
            total_weight = sum(float(ranking.get(key, 0.0)) for key in DEFAULT_RANKING)
            if abs(total_weight - 1.0) > 0.001:
                issues.append(f"ranking weights must sum to 1.0 (got {total_weight:.3f})")
        except (TypeError, ValueError):
            pass

    if issues:
        raise ConfigValidationError("\n".join(issues))
