"""
Resolve the simulable universe for the backtest/projection engines.

Dynamic, not hardcoded (honors the repo's no-static-variables rule): the universe
is the operator's holdings ∪ a proxy-ETF set (so Defensive/Income profiles are
honest) ∪ optionally broad/sector ETFs from config/universe_lists.yaml. Fail-safe.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

logger = logging.getLogger("stockbot.portfolio_sim.universe")

DEFAULT_PROXY_ETFS = ["BND", "TLT", "SCHD", "USMV"]


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def resolve_simulable_universe(root: str | Path) -> dict[str, dict[str, Any]]:
    """
    Return ``{TICKER: {"source": holding|proxy|universe_list, "in_holdings": bool}}``.

    Always includes the operator's holdings when config is readable; proxies and
    universe-list ETFs are additive and config-gated. Never raises.
    """
    root = Path(root)
    cfg = _load_json(root / "config.json") or {}
    sim_cfg = (cfg.get("portfolio_sim") or {}).get("universe") or {}
    universe: dict[str, dict[str, Any]] = {}

    # 1. Operator holdings.
    for h in (cfg.get("portfolio", {}) or {}).get("holdings", []) or []:
        sym = str(h.get("symbol", "")).upper().strip()
        if sym:
            universe[sym] = {"source": "holding", "in_holdings": True}

    # 2. Proxy ETFs (so Defensive/Income are honest).
    for sym in sim_cfg.get("proxy_etfs", DEFAULT_PROXY_ETFS):
        sym = str(sym).upper().strip()
        if sym and sym not in universe:
            universe[sym] = {"source": "proxy", "in_holdings": False}

    # 3. Optional broad/sector ETFs from universe_lists.yaml.
    if sim_cfg.get("include_universe_lists") and yaml is not None:
        try:
            data = yaml.safe_load((root / "config" / "universe_lists.yaml").read_text(encoding="utf-8")) or {}
            for key in ("broad_market_etfs", "sector_etfs"):
                for sym in data.get(key, []) or []:
                    sym = str(sym).upper().strip()
                    if sym and sym not in universe:
                        universe[sym] = {"source": "universe_list", "in_holdings": False}
        except Exception as exc:
            logger.debug("portfolio_sim universe: universe_lists read failed (%s)", exc)

    return universe
