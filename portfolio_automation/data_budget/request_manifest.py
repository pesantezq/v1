from __future__ import annotations
from typing import Any


def plan_quote_request(symbols: list[str], *, run_mode: str) -> dict[str, Any]:
    """Choose the cheapest quote endpoint. quote-short for single-symbol GUI;
    batch (cached per-symbol) for everything else."""
    if run_mode == "gui_refresh" and len(symbols) == 1:
        return {"method": "get_quote_short", "args": {"symbol": symbols[0]}}
    return {"method": "get_batch_quotes", "args": {"symbols": symbols}}


def plan_price_request(symbols: list[str], *, run_mode: str) -> dict[str, Any]:
    """Daily price updates: light EOD historical (ttl_days=1). Per-symbol full
    history only when cache missing (handled by FMPClient's own cache check)."""
    return {"method": "get_historical_prices", "args": {"symbols": symbols}, "ttl_days": 1}
