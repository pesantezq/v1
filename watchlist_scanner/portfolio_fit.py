"""
Portfolio-fit scoring: measures how well a scanned symbol fits the CURRENT portfolio.

Advisory only. Never modifies core signal scores.
All public functions are safe to call when the portfolio snapshot is absent or malformed.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("watchlist_scanner.portfolio_fit")

_PORTFOLIO_SNAPSHOT_REL = ("outputs", "portfolio", "portfolio_snapshot.json")

_LABEL_STRONG: float = 0.75
_LABEL_GOOD: float = 0.55
_LABEL_NEUTRAL: float = 0.35

# Well-known leveraged ETF tickers (covers common 2x/3x instruments)
_LEVERAGED_TICKERS: frozenset[str] = frozenset({
    "QLD", "TQQQ", "UPRO", "SOXL", "SQQQ", "SPXU", "SDOW", "TNA", "TZA",
    "LABU", "LABD", "JDST", "JNUG", "NUGT", "DUST", "UVXY", "SVXY",
    "DFEN", "NAIL", "WANT", "PILL", "CURE", "HIBL", "HIBS", "FNGU", "FNGD",
    "TECL", "TECS", "DPST", "RETL", "MIDU", "UDOW", "SPXL", "SPXS",
    "AGQ", "ZSL", "UCO", "SCO", "BOIL", "KOLD",
})

_WEIGHTS = {
    "existing_position": 0.25,
    "sector": 0.25,
    "diversification": 0.20,
    "leverage": 0.15,
    "cash_fit": 0.15,
}


def load_portfolio_snapshot(root: Path | str) -> dict[str, Any]:
    """
    Load outputs/portfolio/portfolio_snapshot.json relative to *root*.

    Returns {} when file is absent, malformed, or not a dict.
    """
    path = Path(root).joinpath(*_PORTFOLIO_SNAPSHOT_REL)
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("portfolio_fit: could not load %s — %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _extract_portfolio_context(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Extract structured context from a portfolio snapshot dict."""
    rows = snapshot.get("rows") or []
    holdings: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or "").upper()
        if ticker:
            holdings[ticker] = {
                "sector": str(row.get("sector") or "Unknown"),
                "conviction_score": float(row.get("conviction_score") or 0.0),
                "normalized_allocation": float(row.get("normalized_allocation") or 0.0),
            }

    cfg = snapshot.get("config") or {}
    sector_allocation: dict[str, float] = {
        str(k): float(v)
        for k, v in (snapshot.get("allocation_by_sector") or {}).items()
    }

    # Build sector → ticker-count map from groupings
    sector_tickers: dict[str, list[str]] = {}
    for grp in (snapshot.get("groupings") or {}).get("by_sector") or []:
        if not isinstance(grp, dict):
            continue
        sector_name = str(grp.get("name") or "Unknown")
        tickers = [str(t) for t in (grp.get("tickers") or []) if t]
        sector_tickers[sector_name] = tickers

    return {
        "holdings": holdings,
        "sector_allocation": sector_allocation,
        "sector_tickers": sector_tickers,
        "total_normalized_allocation": float(snapshot.get("total_normalized_allocation") or 0.0),
        "max_total_allocation": float(cfg.get("max_total_allocation") or 0.1),
        "max_sector_allocation": float(cfg.get("max_sector_allocation") or 0.04),
        "max_ticker_allocation": float(cfg.get("max_ticker_allocation") or 0.02),
        "regime_label": str(
            (snapshot.get("market_regime") or {}).get("regime_label") or "neutral"
        ),
    }


def _existing_position_score(symbol: str, ctx: dict[str, Any]) -> tuple[float, str]:
    holdings = ctx["holdings"]
    if symbol not in holdings:
        return 0.5, "new position opportunity"
    holding = holdings[symbol]
    alloc = holding["normalized_allocation"]
    max_alloc = ctx["max_ticker_allocation"]
    if max_alloc > 0 and alloc >= max_alloc:
        return 0.4, "existing position at or near maximum allocation"
    conv = holding["conviction_score"]
    score = round(0.4 + conv * 0.4, 4)
    return score, "reinforces existing position"


def _sector_exposure_score(sector: str, ctx: dict[str, Any]) -> tuple[float, str]:
    if not sector or sector.upper() in {"UNKNOWN", ""}:
        return 0.5, "sector unknown"
    sector_max = ctx["max_sector_allocation"]
    current = float(ctx["sector_allocation"].get(sector, 0.0))
    if sector_max <= 0:
        return 0.5, "sector cap not configured"
    usage = min(1.0, current / sector_max)
    score = round(max(0.2, 1.0 - 0.8 * usage), 4)
    if usage >= 0.9:
        reason = f"sector near allocation cap ({current:.1%} of {sector_max:.1%} max)"
    elif usage >= 0.5:
        reason = f"sector moderately allocated ({current:.1%} used)"
    else:
        reason = f"sector has headroom ({current:.1%} of {sector_max:.1%} max)"
    return score, reason


def _diversification_score(
    symbol: str,
    sector: str,
    ctx: dict[str, Any],
) -> tuple[float, str]:
    if symbol in ctx["holdings"]:
        return 0.5, "already held, no new diversification"
    if not sector or sector.upper() in {"UNKNOWN", ""}:
        return 0.5, "sector unknown, diversification unclear"
    existing_count = len(ctx["sector_tickers"].get(sector, []))
    if existing_count == 0:
        return 0.8, "adds new sector to portfolio"
    if existing_count <= 2:
        return 0.65, "sector lightly represented in portfolio"
    if existing_count <= 4:
        return 0.45, "sector moderately represented in portfolio"
    return 0.3, "sector heavily represented in portfolio"


def _leverage_score(symbol: str, ctx: dict[str, Any]) -> tuple[float, str]:
    is_leveraged = symbol in _LEVERAGED_TICKERS
    if not is_leveraged:
        return 0.8, "non-leveraged instrument"
    existing_leveraged = [t for t in ctx["holdings"] if t in _LEVERAGED_TICKERS]
    if existing_leveraged:
        sample = ", ".join(sorted(existing_leveraged)[:2])
        return 0.3, f"adds leverage; portfolio already holds {sample}"
    return 0.5, "leveraged instrument; no existing leverage in portfolio"


def _cash_fit_score(ctx: dict[str, Any]) -> tuple[float, str]:
    available = round(
        max(0.0, ctx["max_total_allocation"] - ctx["total_normalized_allocation"]), 4
    )
    if available >= 0.03:
        return 0.8, f"{available:.1%} deployment room available"
    if available >= 0.01:
        return 0.6, f"limited deployment room ({available:.1%})"
    if available > 0:
        return 0.4, f"very limited deployment room ({available:.1%})"
    return 0.2, "portfolio at allocation ceiling"


def compute_portfolio_fit(
    symbol: str,
    sector: str,
    portfolio_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """
    Compute portfolio fit fields for a single symbol.

    Returns a dict with:
      portfolio_fit_score, portfolio_fit_label, portfolio_fit_reason,
      portfolio_fit_context (sub-scores for explainability).

    When portfolio_snapshot is empty, returns neutral defaults (score=0.5).
    """
    if not portfolio_snapshot:
        return _empty_portfolio_fit_fields()

    ctx = _extract_portfolio_context(portfolio_snapshot)
    symbol = (symbol or "").upper()
    sector = (sector or "").upper()

    ep_score, ep_reason = _existing_position_score(symbol, ctx)
    sec_score, sec_reason = _sector_exposure_score(sector, ctx)
    div_score, div_reason = _diversification_score(symbol, sector, ctx)
    lev_score, lev_reason = _leverage_score(symbol, ctx)
    cash_score, cash_reason = _cash_fit_score(ctx)

    raw = (
        _WEIGHTS["existing_position"] * ep_score
        + _WEIGHTS["sector"] * sec_score
        + _WEIGHTS["diversification"] * div_score
        + _WEIGHTS["leverage"] * lev_score
        + _WEIGHTS["cash_fit"] * cash_score
    )
    fit_score = round(min(max(raw, 0.0), 1.0), 4)
    fit_label = _fit_label(fit_score)
    fit_reason = _build_reason(
        symbol, sector, ctx,
        ep_score, ep_reason,
        sec_score, sec_reason,
        div_score, div_reason,
        lev_score, lev_reason,
        cash_score, cash_reason,
    )

    return {
        "portfolio_fit_score": fit_score,
        "portfolio_fit_label": fit_label,
        "portfolio_fit_reason": fit_reason,
        "portfolio_fit_context": {
            "existing_position_score": round(ep_score, 4),
            "sector_score": round(sec_score, 4),
            "diversification_score": round(div_score, 4),
            "leverage_score": round(lev_score, 4),
            "cash_fit_score": round(cash_score, 4),
            "regime_label": ctx["regime_label"],
        },
    }


def enrich_row_with_portfolio_fit(
    row: dict[str, Any],
    portfolio_snapshot: dict[str, Any],
) -> None:
    """
    Add portfolio_fit_* fields to *row* in-place.

    Never raises — all errors are silently logged.
    """
    try:
        symbol = str(row.get("ticker") or "")
        sector = str(
            row.get("sector")
            or (row.get("fundamentals") or {}).get("sector")
            or ""
        )
        fields = compute_portfolio_fit(symbol, sector, portfolio_snapshot)
        row.update(fields)
    except Exception as exc:
        logger.warning("portfolio_fit: error enriching %s — %s", row.get("ticker"), exc)
        _apply_empty_fallback(row)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _fit_label(score: float) -> str:
    if score >= _LABEL_STRONG:
        return "strong"
    if score >= _LABEL_GOOD:
        return "good"
    if score >= _LABEL_NEUTRAL:
        return "neutral"
    return "poor"


def _build_reason(
    symbol: str,
    sector: str,
    ctx: dict[str, Any],
    ep_score: float, ep_reason: str,
    sec_score: float, sec_reason: str,
    div_score: float, div_reason: str,
    lev_score: float, lev_reason: str,
    cash_score: float, cash_reason: str,
) -> str:
    parts: list[str] = []

    if cash_score <= 0.4:
        parts.append("Limited by low available deployment room")
    elif cash_score <= 0.6:
        parts.append("Moderate deployment room remaining")

    if lev_score <= 0.3:
        parts.append(lev_reason.capitalize())

    if sec_score <= 0.4:
        label = f" ({sector})" if sector and sector.upper() not in {"UNKNOWN", ""} else ""
        parts.append(f"Overlaps with heavily allocated sector{label}")

    if ep_score >= 0.6 and symbol in ctx["holdings"]:
        parts.append("Reinforces existing high-conviction position")
    elif div_score >= 0.75:
        parts.append("Adds diversification to underweight sector")
    elif div_score <= 0.35:
        parts.append("Overlaps heavily with existing sector exposure")

    if not parts:
        if ep_score >= 0.5 and div_score >= 0.5 and cash_score >= 0.6:
            parts.append("Portfolio fit is neutral to positive")
        else:
            parts.append("Portfolio fit is neutral")

    return "; ".join(parts)


def _empty_portfolio_fit_fields() -> dict[str, Any]:
    return {
        "portfolio_fit_score": 0.5,
        "portfolio_fit_label": "neutral",
        "portfolio_fit_reason": "No portfolio snapshot available",
        "portfolio_fit_context": {},
    }


def _apply_empty_fallback(row: dict[str, Any]) -> None:
    for k, v in _empty_portfolio_fit_fields().items():
        row.setdefault(k, v)
