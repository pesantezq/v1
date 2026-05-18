from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any


# Tactical retune (operator-approved 2026-05-18): proportional bump matching
# the allocation_engine retune so the portfolio snapshot's "suggested
# deployment" headroom keeps pace with the wider per-position caps in the
# decision plan. Reverts cleanly by restoring prior values.
DEFAULT_PORTFOLIO_CONSTRUCTION_CONFIG = {
    "enabled": True,
    "observe_only": True,
    "baseline_position_pct": 0.04,
    "max_total_allocation": 0.30,
    "max_ticker_allocation": 0.05,
    "max_sector_allocation": 0.10,
    "top_sector_warning_threshold": 0.40,
    "top3_ticker_warning_threshold": 0.70,
    "high_conviction_theme_warning_count": 2,
}


def _cfg(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(base)
    if not isinstance(override, dict):
        return merged
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            nested = dict(merged[key])
            nested.update(value)
            merged[key] = nested
        else:
            merged[key] = value
    return merged


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _sector_of(row: dict[str, Any]) -> str:
    fundamentals = row.get("fundamentals")
    if isinstance(fundamentals, dict):
        sector = str(fundamentals.get("sector") or "").strip()
        if sector:
            return sector
    sector = str(row.get("sector") or "").strip()
    return sector or "Unknown"


def _themes_of(row: dict[str, Any]) -> list[str]:
    themes = row.get("themes")
    if isinstance(themes, list):
        cleaned = [str(theme).strip() for theme in themes if str(theme).strip()]
        return cleaned or ["Unspecified"]
    return ["Unspecified"]


def _market_cap_value(row: dict[str, Any]) -> float | None:
    fundamentals = row.get("fundamentals")
    if isinstance(fundamentals, dict):
        market_cap = _safe_float(fundamentals.get("market_cap"), default=-1.0)
        if market_cap >= 0:
            return market_cap
    market_cap = _safe_float(row.get("market_cap"), default=-1.0)
    return market_cap if market_cap >= 0 else None


def _market_cap_bucket(market_cap: float | None) -> str:
    if market_cap is None:
        return "unknown"
    if market_cap >= 200_000_000_000:
        return "mega"
    if market_cap >= 10_000_000_000:
        return "large"
    if market_cap >= 2_000_000_000:
        return "mid"
    return "small"


def _avg(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(values) / len(values), 4)


def _scale_rows(rows: list[dict[str, Any]], field: str, scale: float) -> None:
    for row in rows:
        row[field] = round(_safe_float(row.get(field)) * scale, 4)


def _group_summary(rows: list[dict[str, Any]], key_fn) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(key_fn(row))].append(row)
    summary: list[dict[str, Any]] = []
    for name, group_rows in sorted(groups.items()):
        summary.append(
            {
                "name": name,
                "count": len(group_rows),
                "avg_conviction_score": _avg(
                    [_safe_float(row.get("conviction_score")) for row in group_rows]
                ),
                "total_suggested_allocation": round(
                    sum(_safe_float(row.get("suggested_allocation")) for row in group_rows), 4
                ),
                "total_normalized_allocation": round(
                    sum(_safe_float(row.get("normalized_allocation")) for row in group_rows), 4
                ),
                "tickers": [str(row.get("ticker") or "") for row in group_rows],
            }
        )
    return summary


def _theme_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        for theme in _themes_of(row):
            groups[theme].append(row)
    summary: list[dict[str, Any]] = []
    for name, group_rows in sorted(groups.items()):
        summary.append(
            {
                "name": name,
                "count": len(group_rows),
                "avg_conviction_score": _avg(
                    [_safe_float(row.get("conviction_score")) for row in group_rows]
                ),
                "total_suggested_allocation": round(
                    sum(_safe_float(row.get("suggested_allocation")) for row in group_rows), 4
                ),
                "total_normalized_allocation": round(
                    sum(_safe_float(row.get("normalized_allocation")) for row in group_rows), 4
                ),
                "tickers": [str(row.get("ticker") or "") for row in group_rows],
            }
        )
    return summary


def _summary_label(warnings: list[str], top_sector_name: str, top_sector_pct: float) -> str:
    if any("high_conviction_theme" in warning for warning in warnings):
        return "high conviction concentration"
    if top_sector_pct >= 0.45 and top_sector_name and top_sector_name != "Unknown":
        return f"overweight {top_sector_name.lower()}"
    if warnings:
        return "skewed"
    return "balanced"


def apply_portfolio_construction_layer(
    scan_result: dict[str, Any],
    *,
    portfolio_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = _cfg(DEFAULT_PORTFOLIO_CONSTRUCTION_CONFIG, portfolio_config)
    if not cfg.get("enabled", True):
        return scan_result

    results = list(scan_result.get("results") or [])
    actionable = [row for row in results if _safe_float(row.get("sizing_multiplier")) > 0.0]

    baseline_position_pct = _safe_float(cfg.get("baseline_position_pct"), 0.02)
    max_total_allocation = _safe_float(cfg.get("max_total_allocation"), 0.10)
    max_ticker_allocation = _safe_float(cfg.get("max_ticker_allocation"), 0.02)
    max_sector_allocation = _safe_float(cfg.get("max_sector_allocation"), 0.04)

    for row in results:
        sector = _sector_of(row)
        market_cap_bucket = _market_cap_bucket(_market_cap_value(row))
        suggested = round(baseline_position_pct * _safe_float(row.get("sizing_multiplier")), 4)
        row["portfolio_sector"] = sector
        row["portfolio_themes"] = _themes_of(row)
        row["market_cap_bucket"] = market_cap_bucket
        row["suggested_allocation"] = suggested
        row["normalized_allocation"] = suggested
        row["allocation_capped"] = False
        row["allocation_cap_reason"] = ""

    positive_rows = [row for row in actionable if _safe_float(row.get("suggested_allocation")) > 0.0]
    total_suggested = round(
        sum(_safe_float(row.get("suggested_allocation")) for row in positive_rows), 4
    )
    normalization_scale = 1.0
    if total_suggested > max_total_allocation > 0:
        normalization_scale = max_total_allocation / total_suggested
        for row in positive_rows:
            row["normalized_allocation"] = round(
                _safe_float(row.get("suggested_allocation")) * normalization_scale,
                4,
            )
            row["allocation_capped"] = True
            row["allocation_cap_reason"] = "total_allocation_cap"

    capped_positions = 0
    sectors_capped: set[str] = set()

    for row in positive_rows:
        current = _safe_float(row.get("normalized_allocation"))
        if current > max_ticker_allocation > 0:
            row["normalized_allocation"] = round(max_ticker_allocation, 4)
            row["allocation_capped"] = True
            row["allocation_cap_reason"] = (
                f"{row['allocation_cap_reason']},ticker_cap".strip(",")
                if row.get("allocation_cap_reason")
                else "ticker_cap"
            )

    sector_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in positive_rows:
        sector_groups[str(row.get("portfolio_sector") or "Unknown")].append(row)
    for sector, sector_rows in sector_groups.items():
        sector_total = sum(_safe_float(row.get("normalized_allocation")) for row in sector_rows)
        if sector_total > max_sector_allocation > 0:
            sectors_capped.add(sector)
            scale = max_sector_allocation / sector_total if sector_total else 1.0
            for row in sector_rows:
                row["normalized_allocation"] = round(
                    _safe_float(row.get("normalized_allocation")) * scale,
                    4,
                )
                row["allocation_capped"] = True
                row["allocation_cap_reason"] = (
                    f"{row['allocation_cap_reason']},sector_cap".strip(",")
                    if row.get("allocation_cap_reason")
                    else "sector_cap"
                )

    for row in positive_rows:
        if row.get("allocation_capped"):
            capped_positions += 1

    total_normalized = round(
        sum(_safe_float(row.get("normalized_allocation")) for row in positive_rows), 4
    )
    allocation_by_sector = {
        sector: round(sum(_safe_float(row.get("normalized_allocation")) for row in sector_rows), 4)
        for sector, sector_rows in sector_groups.items()
    }
    allocation_by_conviction_band = {
        band: round(
            sum(
                _safe_float(row.get("normalized_allocation"))
                for row in positive_rows
                if str(row.get("conviction_band") or "") == band
            ),
            4,
        )
        for band in ("defer", "observe", "starter", "normal", "high_conviction")
    }

    top_sector_name = ""
    top_sector_pct = 0.0
    if total_normalized > 0 and allocation_by_sector:
        top_sector_name, top_sector_value = max(
            allocation_by_sector.items(),
            key=lambda item: item[1],
        )
        top_sector_pct = round(top_sector_value / total_normalized, 4)

    top3_rows = sorted(
        positive_rows,
        key=lambda row: _safe_float(row.get("normalized_allocation")),
        reverse=True,
    )[:3]
    top3_pct = round(
        sum(_safe_float(row.get("normalized_allocation")) for row in top3_rows) / total_normalized,
        4,
    ) if total_normalized > 0 else 0.0

    high_conviction_theme_counts: Counter[str] = Counter()
    for row in positive_rows:
        if row.get("conviction_band") != "high_conviction":
            continue
        for theme in _themes_of(row):
            high_conviction_theme_counts[theme] += 1

    warnings: list[str] = []
    if top_sector_pct >= _safe_float(cfg.get("top_sector_warning_threshold"), 0.40):
        warnings.append(
            f"overconcentration_top_sector:{top_sector_name}:{top_sector_pct:.1%}"
        )
    if top3_pct >= _safe_float(cfg.get("top3_ticker_warning_threshold"), 0.70):
        warnings.append(f"top3_ticker_concentration:{top3_pct:.1%}")
    threshold = int(cfg.get("high_conviction_theme_warning_count", 2) or 2)
    for theme, count in sorted(high_conviction_theme_counts.items()):
        if count >= threshold:
            warnings.append(f"high_conviction_theme:{theme}:{count}")
    if bool(scan_result.get("degraded_mode")) and total_normalized > 0:
        warnings.append("degraded_mode_exposure_risk")

    snapshot_rows = []
    for row in sorted(
        results,
        key=lambda item: (
            _safe_float(item.get("normalized_allocation")),
            _safe_float(item.get("conviction_score")),
            _safe_float(item.get("effective_score")),
        ),
        reverse=True,
    ):
        snapshot_rows.append(
            {
                "ticker": row.get("ticker", ""),
                "sector": row.get("portfolio_sector", "Unknown"),
                "themes": list(row.get("portfolio_themes") or []),
                "market_cap_bucket": row.get("market_cap_bucket", "unknown"),
                "conviction_score": round(_safe_float(row.get("conviction_score")), 3),
                "conviction_band": row.get("conviction_band", ""),
                "sizing_recommendation": row.get("sizing_recommendation", ""),
                "suggested_allocation": round(_safe_float(row.get("suggested_allocation")), 4),
                "normalized_allocation": round(_safe_float(row.get("normalized_allocation")), 4),
                "allocation_capped": bool(row.get("allocation_capped", False)),
                "allocation_cap_reason": row.get("allocation_cap_reason", ""),
                "data_mode": row.get("data_mode", scan_result.get("data_mode", "live")),
                "degraded_mode": bool(scan_result.get("degraded_mode", False)),
            }
        )

    scan_result.setdefault("scan_summary", {})
    scan_result["scan_summary"]["portfolio_construction_summary_line"] = (
        f"Portfolio view: {len(positive_rows)} actionable signals, "
        f"{total_suggested:.1%} suggested, {total_normalized:.1%} normalized, "
        f"{capped_positions} capped"
    )
    scan_result["scan_summary"]["portfolio_construction_label"] = _summary_label(
        warnings,
        top_sector_name,
        top_sector_pct,
    )

    scan_result["portfolio_construction"] = {
        "enabled": True,
        "observe_only": bool(cfg.get("observe_only", True)),
        "summary_label": scan_result["scan_summary"]["portfolio_construction_label"],
        "summary_line": scan_result["scan_summary"]["portfolio_construction_summary_line"],
        "total_suggested_allocation": total_suggested,
        "total_normalized_allocation": total_normalized,
        "normalization_scale": round(normalization_scale, 4),
        "capped_positions": capped_positions,
        "sectors_capped": sorted(sectors_capped),
        "allocation_by_sector": allocation_by_sector,
        "allocation_by_conviction_band": allocation_by_conviction_band,
        "top_sector": {
            "name": top_sector_name or "Unknown",
            "allocation_pct": top_sector_pct,
        },
        "top_3_ticker_concentration_pct": top3_pct,
        "warnings": warnings,
        "degraded_mode_impact": {
            "degraded_mode": bool(scan_result.get("degraded_mode", False)),
            "data_mode": scan_result.get("data_mode", "live"),
            "risk_flagged": "degraded_mode_exposure_risk" in warnings,
        },
        "groupings": {
            "by_sector": _group_summary(snapshot_rows, lambda row: row.get("sector", "Unknown")),
            "by_theme": _theme_summary(snapshot_rows),
            "by_market_cap": _group_summary(snapshot_rows, lambda row: row.get("market_cap_bucket", "unknown")),
        },
        "rows": snapshot_rows,
        "config": {
            "baseline_position_pct": baseline_position_pct,
            "max_total_allocation": max_total_allocation,
            "max_ticker_allocation": max_ticker_allocation,
            "max_sector_allocation": max_sector_allocation,
        },
    }
    rows_by_ticker = {
        str(row.get("ticker") or ""): row
        for row in results
    }
    for alert in list(scan_result.get("alerts") or []):
        source = rows_by_ticker.get(str(alert.get("ticker") or ""))
        if not source:
            continue
        for key in (
            "portfolio_sector",
            "portfolio_themes",
            "market_cap_bucket",
            "suggested_allocation",
            "normalized_allocation",
            "allocation_capped",
            "allocation_cap_reason",
        ):
            alert[key] = source.get(key)
    return scan_result
