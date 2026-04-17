from __future__ import annotations

from typing import Any


_INDEX_TICKERS = {"SPY", "QQQ", "DIA", "IWM"}


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def detect_market_regime(
    *,
    results: list[dict[str, Any]] | None = None,
    portfolio_construction: dict[str, Any] | None = None,
    data_health: dict[str, Any] | None = None,
    regime_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows = list(results or [])
    portfolio_view = portfolio_construction if isinstance(portfolio_construction, dict) else {}
    data_ctx = data_health if isinstance(data_health, dict) else {}
    override_inputs = regime_inputs if isinstance(regime_inputs, dict) else {}

    breadth_sma50 = override_inputs.get("breadth_sma50")
    breadth_sma20 = override_inputs.get("breadth_sma20")
    avg_price_change = override_inputs.get("avg_price_change_pct")
    volatility_proxy = override_inputs.get("volatility_proxy")
    sector_leadership_concentration = override_inputs.get("sector_leadership_concentration")
    index_trend_state = override_inputs.get("index_trend_state")

    if breadth_sma50 is None and rows:
        valid = [row for row in rows if row.get("above_sma50") is not None]
        if valid:
            breadth_sma50 = sum(1 for row in valid if row.get("above_sma50")) / len(valid)
    if breadth_sma20 is None and rows:
        valid = [row for row in rows if row.get("above_sma20") is not None]
        if valid:
            breadth_sma20 = sum(1 for row in valid if row.get("above_sma20")) / len(valid)
    if avg_price_change is None and rows:
        pct_values = [_safe_float(row.get("price_change_pct")) for row in rows]
        pct_values = [value for value in pct_values if value is not None]
        if pct_values:
            avg_price_change = _avg(pct_values)
    if volatility_proxy is None and rows:
        pct_values = [_safe_float(row.get("price_change_pct")) for row in rows]
        pct_values = [abs(value) for value in pct_values if value is not None]
        if pct_values:
            volatility_proxy = _avg(pct_values)
    if sector_leadership_concentration is None and portfolio_view:
        top_sector = portfolio_view.get("top_sector") or {}
        sector_leadership_concentration = _safe_float(top_sector.get("allocation_pct"))

    if index_trend_state is None and rows:
        index_rows = [row for row in rows if str(row.get("ticker") or "").upper() in _INDEX_TICKERS]
        if index_rows:
            index_breadth = _avg([
                1.0 if row.get("above_sma50") else 0.0
                for row in index_rows
                if row.get("above_sma50") is not None
            ])
            if index_breadth is not None:
                if index_breadth >= 0.67:
                    index_trend_state = "up"
                elif index_breadth <= 0.33:
                    index_trend_state = "down"
                else:
                    index_trend_state = "mixed"

    if index_trend_state is None:
        breadth_anchor = _safe_float(breadth_sma50)
        if breadth_anchor is not None:
            if breadth_anchor >= 0.65:
                index_trend_state = "up"
            elif breadth_anchor <= 0.40:
                index_trend_state = "down"
            else:
                index_trend_state = "mixed"
        else:
            index_trend_state = "unknown"

    breadth_sma50 = _safe_float(breadth_sma50)
    breadth_sma20 = _safe_float(breadth_sma20)
    avg_price_change = _safe_float(avg_price_change)
    volatility_proxy = _safe_float(volatility_proxy)
    sector_leadership_concentration = _safe_float(sector_leadership_concentration)

    available_inputs = {
        "index_trend_state": index_trend_state != "unknown",
        "breadth_sma50": breadth_sma50 is not None,
        "breadth_sma20": breadth_sma20 is not None,
        "avg_price_change_pct": avg_price_change is not None,
        "volatility_proxy": volatility_proxy is not None,
        "sector_leadership_concentration": sector_leadership_concentration is not None,
    }
    input_count = sum(1 for value in available_inputs.values() if value)

    reasons: list[str] = []
    confidence = 0.45 + min(input_count, 4) * 0.08

    high_volatility = volatility_proxy is not None and volatility_proxy >= 3.0
    strong_breadth = (
        breadth_sma50 is not None and breadth_sma50 >= 0.65
        and breadth_sma20 is not None and breadth_sma20 >= 0.60
    )
    weak_breadth = (
        breadth_sma50 is not None and breadth_sma50 <= 0.40
        and breadth_sma20 is not None and breadth_sma20 <= 0.45
    )

    label = "neutral"
    if high_volatility:
        label = "high_volatility"
        reasons.append("elevated cross-signal volatility")
        confidence += 0.06
    elif index_trend_state == "up" and strong_breadth:
        label = "risk_on"
        reasons.append("broad uptrend with supportive breadth")
        confidence += 0.08
    elif index_trend_state == "down" and weak_breadth:
        label = "risk_off"
        reasons.append("weak breadth and trend pressure")
        confidence += 0.08
    else:
        reasons.append("mixed trend and breadth signals")

    if sector_leadership_concentration is not None:
        if sector_leadership_concentration >= 0.50:
            reasons.append("leadership is concentrated in one sector")
            if label == "risk_on":
                confidence -= 0.04
        else:
            reasons.append("leadership is reasonably distributed")

    if avg_price_change is not None:
        if avg_price_change >= 1.0 and label in {"risk_on", "neutral"}:
            reasons.append("average price change is positive")
            confidence += 0.03
        elif avg_price_change <= -1.0 and label in {"risk_off", "neutral", "high_volatility"}:
            reasons.append("average price change is negative")
            confidence += 0.03

    degraded_mode = bool(data_ctx.get("degraded_mode", False))
    if degraded_mode:
        reasons.append(
            f"data is degraded ({data_ctx.get('degraded_reason') or 'fallback'})"
        )
        confidence -= 0.10

    if input_count <= 2:
        reasons.append("limited regime inputs available")
        confidence -= 0.08

    regime_data_quality = "full"
    if degraded_mode:
        regime_data_quality = "degraded"
    elif input_count <= 2:
        regime_data_quality = "limited"
    elif input_count <= 4:
        regime_data_quality = "partial"

    confidence = round(_clamp(confidence), 2)
    reasoning = "; ".join(dict.fromkeys(reasons))
    summary_line = (
        f"Market regime: {label} (confidence {confidence:.2f}) - {reasoning}"
    )

    return {
        "regime_label": label,
        "regime_confidence": confidence,
        "regime_reasoning": reasoning,
        "regime_summary_line": summary_line,
        "regime_data_quality": regime_data_quality,
        "regime_inputs": {
            "index_trend_state": index_trend_state,
            "breadth_sma50": round(breadth_sma50, 3) if breadth_sma50 is not None else None,
            "breadth_sma20": round(breadth_sma20, 3) if breadth_sma20 is not None else None,
            "avg_price_change_pct": round(avg_price_change, 3) if avg_price_change is not None else None,
            "volatility_proxy": round(volatility_proxy, 3) if volatility_proxy is not None else None,
            "sector_leadership_concentration": (
                round(sector_leadership_concentration, 3)
                if sector_leadership_concentration is not None else None
            ),
            "available_input_count": input_count,
            "degraded_mode": degraded_mode,
        },
    }


def regime_fit_commentary(
    *,
    regime: dict[str, Any],
    portfolio_construction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    portfolio_view = portfolio_construction if isinstance(portfolio_construction, dict) else {}
    label = str(regime.get("regime_label") or "neutral")
    top_sector = portfolio_view.get("top_sector") or {}
    top_sector_pct = _safe_float(top_sector.get("allocation_pct")) or 0.0
    top3_pct = _safe_float(portfolio_view.get("top_3_ticker_concentration_pct")) or 0.0
    total_normalized = _safe_float(portfolio_view.get("total_normalized_allocation")) or 0.0
    warnings = list(portfolio_view.get("warnings") or [])

    if label == "risk_on":
        aligned = top_sector_pct <= 0.50 and top3_pct <= 0.75
        commentary = (
            "Current normalized allocations look broadly aligned with a constructive regime."
            if aligned
            else "Portfolio view looks more concentrated than a broad risk-on regime would usually justify."
        )
    elif label == "risk_off":
        aligned = total_normalized <= 0.05 and top_sector_pct <= 0.40
        commentary = (
            "Normalized allocations look appropriately restrained for a risk-off backdrop."
            if aligned
            else "Current normalized allocations look aggressive relative to a risk-off backdrop."
        )
    elif label == "high_volatility":
        aligned = top3_pct <= 0.65
        commentary = (
            "Portfolio construction remains reasonably balanced despite elevated volatility."
            if aligned
            else "Elevated volatility makes the current concentration profile worth monitoring closely."
        )
    else:
        aligned = top_sector_pct <= 0.45
        commentary = (
            "Portfolio view appears balanced for a neutral regime."
            if aligned
            else "Neutral conditions make current sector concentration worth watching."
        )

    if warnings:
        commentary += f" Concentration warnings active: {', '.join(warnings[:2])}."

    return {
        "regime_portfolio_fit": "aligned" if aligned else "stretched",
        "regime_portfolio_commentary": commentary,
    }
