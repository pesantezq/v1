from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("stockbot.portfolio_automation.historical_replay.data_loader")

_CONFIG_DEFAULT = Path("config.json")


def load_holdings_symbols(config_path: Path | str | None = None) -> list[str]:
    """Return holding symbols from config.json portfolio.holdings."""
    path = Path(config_path) if config_path else _CONFIG_DEFAULT
    if not path.exists():
        logger.warning("data_loader: config not found at %s", path)
        return []
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
        holdings = cfg.get("portfolio", {}).get("holdings", [])
        return [h["symbol"].upper() for h in holdings if h.get("symbol")]
    except Exception as exc:
        logger.warning("data_loader: failed to load config — %s", exc)
        return []


def load_extra_symbols(symbols_str: str | None) -> list[str]:
    """Parse comma-separated extra symbols string."""
    if not symbols_str:
        return []
    return [s.strip().upper() for s in symbols_str.split(",") if s.strip()]


def load_universe(
    config_path: Path | str | None = None,
    extra_symbols: list[str] | None = None,
) -> list[str]:
    """Build deduplicated symbol universe: holdings + extra."""
    seen: set[str] = set()
    result: list[str] = []
    for sym in (load_holdings_symbols(config_path) + (extra_symbols or [])):
        if sym not in seen:
            seen.add(sym)
            result.append(sym)
    return result


def normalize_prices(raw_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Normalize FMP historical rows to oldest-first sorted list.

    FMP returns newest-first; we sort to get chronological order.
    Output fields: date (str YYYY-MM-DD), open, high, low, close, volume.
    """
    normalized: list[dict[str, Any]] = []
    for row in raw_rows:
        try:
            d = str(row.get("date") or "")
            close = float(row.get("close") or row.get("adjClose") or 0.0)
            if not d or close <= 0:
                continue
            normalized.append({
                "date": d,
                "open": float(row.get("open") or close),
                "high": float(row.get("high") or close),
                "low": float(row.get("low") or close),
                "close": close,
                "volume": int(row.get("volume") or 0),
            })
        except (TypeError, ValueError):
            continue
    normalized.sort(key=lambda r: r["date"])
    return normalized


def load_historical_prices(
    symbols: list[str],
    fmp_client: Any,
    *,
    days: int = 90,
) -> dict[str, list[dict[str, Any]]]:
    """
    Fetch and normalize historical EOD prices for each symbol.

    Returns {symbol: rows_oldest_first}. Missing or failed symbols are omitted.
    Requests enough history to cover SMA20 warmup (20 days) plus forward
    resolution window (7 days) on top of the requested replay days.
    """
    # 90 trading days ≈ 126 calendar days; add headroom for SMA warmup + forward window
    years_needed = max(1, (days + 60) // 252 + 1)

    result: dict[str, list[dict[str, Any]]] = {}
    for sym in symbols:
        try:
            raw = fmp_client.get_historical_prices(sym, years=years_needed)
            normalized = normalize_prices(raw if isinstance(raw, list) else [])
            if normalized:
                result[sym] = normalized
            else:
                logger.warning("data_loader: no usable price data for %s", sym)
        except Exception as exc:
            logger.warning("data_loader: price fetch failed for %s — %s", sym, exc)
    return result
