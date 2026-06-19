"""Shared FMP sector normalization.

FMP's company-profile `sector` field is *issuer-based*: it reports the sector of
the entity that issues a security, not the security's market exposure. Every ETF
/ fund therefore comes back as "Financial Services / Asset Management" regardless
of what it holds — folding an energy ETF, a tech ETF, and a financials ETF into
one bogus "Financial Services" bucket. For sector *attribution* that is wrong.

`normalize_sector` corrects it: sector-exposure ETFs map to their exposure
sector; every other fund buckets as "ETF/Index"; non-fund equities (including
crypto names FMP files under Financial Services — that is FMP-truth for an
operating company) keep their raw sector.

Pure function, no I/O — callers read the cache and pass the raw fields in.
"""
from __future__ import annotations

ETF_INDEX_SECTOR = "ETF/Index"

# Canonical sector-exposure ETFs FMP mis-files under the issuer's sector. Map
# them to the exposure an attribution reader actually means. Broad-market funds
# (QQQ/SPY/VTI/IWM) intentionally fall through to ETF/Index — they have no single
# sector exposure.
ETF_EXPOSURE_SECTOR = {
    "XLE": "Energy",
    "XLK": "Technology",
    "XLF": "Financial Services",
    "XLV": "Healthcare",
    "XLI": "Industrials",
    "XLU": "Utilities",
    "XLP": "Consumer Defensive",
    "XLY": "Consumer Cyclical",
    "XLB": "Basic Materials",
    "XLRE": "Real Estate",
    "XLC": "Communication Services",
    "SMH": "Technology",
    "SOXX": "Technology",
}


def normalize_sector(
    ticker: str,
    raw_sector: object,
    *,
    is_etf: bool = False,
    is_fund: bool = False,
    unknown: str = "Unknown",
) -> str:
    """Return the attribution-appropriate sector for a ticker.

    Funds (`is_etf`/`is_fund`) resolve to their exposure sector via
    `ETF_EXPOSURE_SECTOR`, else `ETF/Index`. Non-funds return the cleaned
    `raw_sector`, or `unknown` if it is missing/blank.
    """
    if is_etf or is_fund:
        safe = (ticker or "").strip().upper()
        return ETF_EXPOSURE_SECTOR.get(safe, ETF_INDEX_SECTOR)
    if isinstance(raw_sector, str) and raw_sector.strip():
        return raw_sector.strip()
    return unknown
