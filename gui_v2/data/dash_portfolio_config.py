"""Portfolio Config cockpit — gated read/write view.

Reads current config.json portfolio (holdings + cash) and returns form data
for the edit surface, OR an "editing disabled" card when not edit_enabled.

SAFETY:
  - observe_only=True hardcoded on the view dict.
  - No trade/buy/sell/execute/order language.
  - edit_enabled is controlled exclusively by the route layer (_edit_enabled()
    in app.py); this module never sets it to True on its own.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gui_v2.data.shared import card, _read_json


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_config(root: Path) -> dict | None:
    config_path = Path(root) / "config.json"
    try:
        if not config_path.exists():
            return None
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _holdings_rows(portfolio: dict) -> list[dict[str, Any]]:
    """Normalize holdings for the form / table."""
    holdings = portfolio.get("holdings") or []
    if not isinstance(holdings, list):
        return []

    rows: list[dict[str, Any]] = []
    for h in holdings:
        if not isinstance(h, dict):
            continue
        rows.append({
            "symbol": str(h.get("symbol") or "").upper(),
            "shares": h.get("shares"),
            "target_weight": h.get("target_weight"),
            "asset_class": h.get("asset_class") or "us_equity",
            "is_leveraged": bool(h.get("is_leveraged") or False),
            "leverage_factor": h.get("leverage_factor") or 1,
        })
    return rows


# ---------------------------------------------------------------------------
# Public collector
# ---------------------------------------------------------------------------


def collect_portfolio_config_view(root: Path, edit_enabled: bool) -> dict[str, Any]:
    """
    Persona collector for /dashboard/portfolio-config.

    Parameters
    ----------
    root:
        Project root (contains config.json and outputs/).
    edit_enabled:
        True only when the route layer has confirmed both auth is configured
        AND ``GUI_V2_PORTFOLIO_EDIT=1`` is set.

    Returns
    -------
    ``{
        "cards": [ <card dicts> ],
        "persona": "portfolio_config",
        "edit_enabled": bool,
        "holdings": [ <holding row dicts> ],
        "cash": float,
        "growth_mode": dict,
        "config_available": bool,
        "observe_only": True,
    }``
    """
    root = Path(root)
    config = _load_config(root)
    config_available = config is not None

    portfolio: dict = {}
    growth_mode: dict = {}
    holdings: list[dict] = []
    cash: float = 0.0

    if config_available:
        portfolio = config.get("portfolio") or {}
        if not isinstance(portfolio, dict):
            portfolio = {}
        growth_mode = config.get("growth_mode") or {}
        if not isinstance(growth_mode, dict):
            growth_mode = {}
        holdings = _holdings_rows(portfolio)
        cash = float(portfolio.get("cash_available") or 0.0)

    # Build summary card
    if not config_available:
        status_card = card(
            "Portfolio Config",
            status="red",
            label="config unavailable",
            summary="config.json not found — cannot display or edit portfolio configuration.",
            source_artifacts=["config.json"],
        )
    elif not edit_enabled:
        status_card = card(
            "Portfolio Config",
            status="info",
            label="editing disabled",
            summary=(
                f"{len(holdings)} holding(s) loaded. "
                f"Cash: ${cash:,.2f}. "
                "Editing is disabled — set GUI_V2_AUTH_USER, GUI_V2_AUTH_PASS, "
                "and GUI_V2_PORTFOLIO_EDIT=1 to enable the write surface."
            ),
            source_artifacts=["config.json"],
        )
    else:
        status_card = card(
            "Portfolio Config",
            status="warning",
            label="edit enabled",
            summary=(
                f"{len(holdings)} holding(s) loaded. "
                f"Cash: ${cash:,.2f}. "
                "Write surface active — changes update local config only."
            ),
            source_artifacts=["config.json"],
        )

    return {
        "cards": [status_card],
        "persona": "portfolio_config",
        "edit_enabled": edit_enabled,
        "holdings": holdings,
        "cash": cash,
        "growth_mode": growth_mode,
        "config_available": config_available,
        # Hardcoded safety flag
        "observe_only": True,
    }
