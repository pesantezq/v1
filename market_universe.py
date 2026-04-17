"""
Market Universe
===============
Defines the broad market universe available for scanning.

Groups
------
  nasdaq100    — Nasdaq-100 constituents (static list; refresh quarterly)
  sector_etfs  — SPDR and thematic sector ETFs
  portfolio    — Symbols currently held in the portfolio (passed in at runtime)
  sp500        — S&P 500 symbols (passed in from FMPClient at runtime)

Usage
-----
    from market_universe import get_universe_symbols, get_all_symbols

    symbols_by_group = get_universe_symbols(config, sp500_symbols=sp500_list,
                                            portfolio_symbols=["QQQ", "GLD"])
    flat_list = get_all_symbols(config, sp500_symbols=sp500_list)

Config key: ``market_universe``
  groups         — list of group names to include (default: ["nasdaq100", "sector_etfs"])
  max_symbols    — per-group ceiling (0 = unlimited, default: 300)
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

logger = logging.getLogger("portfolio_automation.market_universe")

# ---------------------------------------------------------------------------
# Static symbol lists
# ---------------------------------------------------------------------------

NASDAQ_100_SYMBOLS: List[str] = [
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "TSLA",
    # Semiconductors / hardware
    "AVGO", "AMD", "QCOM", "INTC", "AMAT", "KLAC", "LRCX", "MRVL",
    "MCHP", "NXPI", "ON", "SMCI", "ADI", "TXN",
    # Software / cloud
    "ADBE", "INTU", "CSCO", "CDNS", "SNPS", "PANW", "FTNT", "ZS",
    "CRWD", "DDOG", "TEAM", "WDAY", "OKTA", "NET", "SNOW", "PLTR",
    # Consumer / retail
    "COST", "SBUX", "AMGN", "MNST", "KHC", "PEP", "DLTR",
    # Healthcare / biotech
    "BIIB", "IDXX", "ILMN", "ISRG", "VRTX", "GILD", "REGN",
    # Travel / services
    "BKNG", "EBAY", "PYPL", "ABNB",
    # Industrial / infrastructure
    "HON", "CTAS", "PAYX", "FAST", "ODFL", "VRSK", "CTSH", "PCAR",
    "CSX", "AEP", "EXC", "XEL", "CEG",
    # Emerging / high-growth
    "MELI", "NTES", "ASML", "COIN", "ARM", "APP", "AXON", "TTD",
    "FICO", "ROP", "ANSS", "LULU", "ZM", "ORLY", "CPRT", "DXCM",
    "ROST", "CHTR", "CMCSA", "TMUS", "WBD",
    # EV / new mobility
    "LCID", "RIVN",
    # Financials / misc
    "FANG", "TTWO", "GEHC", "CSGP", "GFS",
]

SECTOR_ETF_SYMBOLS: List[str] = [
    # SPDR sector ETFs (full GICS coverage)
    "XLK",   # Technology
    "XLF",   # Financials
    "XLV",   # Health Care
    "XLE",   # Energy
    "XLI",   # Industrials
    "XLY",   # Consumer Discretionary
    "XLP",   # Consumer Staples
    "XLRE",  # Real Estate
    "XLB",   # Materials
    "XLU",   # Utilities
    "XLC",   # Communication Services
    # Thematic / sub-sector
    "SMH",   # Semiconductors (VanEck)
    "SOXX",  # Semiconductors (iShares)
    "IBB",   # Biotech (iShares)
    "XBI",   # Biotech small-cap (SPDR)
    "GDX",   # Gold miners (VanEck)
    "ARKK",  # Disruptive Innovation (ARK)
    "IYR",   # Real Estate (iShares)
]

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_universe_symbols(
    config: dict,
    sp500_symbols: Optional[List[str]] = None,
    portfolio_symbols: Optional[List[str]] = None,
) -> Dict[str, List[str]]:
    """
    Build the market universe symbol lists from config + static data.

    Args:
        config:            The full config dict or the ``market_universe`` sub-dict.
        sp500_symbols:     Pre-loaded S&P 500 symbol list (from FMPClient).
                           Required only when 'sp500' is in config groups.
        portfolio_symbols: Current portfolio holding symbols.
                           Required only when 'portfolio' is in config groups.

    Returns:
        Dict mapping group_name → [symbols].  May be empty if all groups
        are unknown or unavailable.
    """
    universe_cfg = config.get("market_universe", config)
    groups = _resolve_groups(universe_cfg.get("groups", ["nasdaq100", "sector_etfs"]))
    max_symbols = _resolve_max_symbols(universe_cfg.get("max_symbols", 300))

    result: Dict[str, List[str]] = {}

    for group in groups:
        if group == "nasdaq100":
            result["nasdaq100"] = list(NASDAQ_100_SYMBOLS)
        elif group == "sector_etfs":
            result["sector_etfs"] = list(SECTOR_ETF_SYMBOLS)
        elif group == "sp500":
            if sp500_symbols:
                result["sp500"] = _normalise_symbols(sp500_symbols)
            else:
                logger.warning(
                    "market_universe: 'sp500' group requested but sp500_symbols not provided"
                )
        elif group == "portfolio":
            if portfolio_symbols:
                result["portfolio"] = _normalise_symbols(portfolio_symbols)
            else:
                logger.debug(
                    "market_universe: 'portfolio' group requested but portfolio_symbols is empty"
                )
        else:
            logger.warning("market_universe: unknown group %r — skipping", group)

    # Apply per-group cap
    if max_symbols > 0:
        for grp in list(result):
            if len(result[grp]) > max_symbols:
                logger.info(
                    "market_universe: trimmed %s to %d symbols (max_symbols=%d)",
                    grp, max_symbols, max_symbols,
                )
                result[grp] = result[grp][:max_symbols]

    return result


def get_all_symbols(
    config: dict,
    sp500_symbols: Optional[List[str]] = None,
    portfolio_symbols: Optional[List[str]] = None,
) -> List[str]:
    """
    Return a deduplicated flat list of all symbols across configured groups.

    Insertion order: groups are iterated in the order they appear in config.
    Within each group, symbols appear in their list order.
    """
    groups = get_universe_symbols(
        config,
        sp500_symbols=sp500_symbols,
        portfolio_symbols=portfolio_symbols,
    )
    seen: set = set()
    result: List[str] = []
    for syms in groups.values():
        for s in syms:
            if s not in seen:
                seen.add(s)
                result.append(s)
    logger.debug(
        "market_universe: %d unique symbols across %d groups",
        len(result), len(groups),
    )
    return result


def _resolve_groups(groups: object) -> List[str]:
    if groups is None:
        return ["nasdaq100", "sector_etfs"]
    if isinstance(groups, str):
        groups = [groups]
    elif not isinstance(groups, list):
        try:
            groups = list(groups)
        except TypeError:
            groups = [groups]
    resolved: List[str] = []
    seen: set[str] = set()
    for group in groups:
        normalized = str(group or "").strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            resolved.append(normalized)
    return resolved


def _resolve_max_symbols(value: object) -> int:
    try:
        max_symbols = int(value)
    except (TypeError, ValueError):
        return 300
    return max(0, max_symbols)


def _normalise_symbols(symbols: List[str]) -> List[str]:
    result: List[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        normalized = str(symbol or "").strip().upper()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result
