"""
News Intelligence Runner — pipeline entry point.

Wires the FMP news intelligence producer (portfolio_automation.news.
fmp_news_intelligence) into the daily pipeline by:

  1. Collecting the active ticker universe (holdings + watchlist signals +
     decision-plan symbols + sandbox discovery candidates).
  2. Calling FMPClient.get_stock_news to fetch raw articles for that universe.
  3. Invoking run_fmp_news_intelligence to normalize, dedupe, theme-classify,
     and write outputs/latest/news_intelligence.json + .md.

All writes are observe-only artifacts; no decision, allocation, score, or
recommendation state is mutated by this layer.

Safe degradation:
    - If FMP is unavailable or the budget is exhausted, raw_articles will be
      empty and the producer still writes a valid empty artifact.
    - Any unexpected exception is caught and logged; the run reports an
      error in its summary dict but does not propagate.

Public API:
    collect_ticker_universe(root, max_total=50) -> tuple[holdings, watchlist, discovery]
    fetch_news_articles(tickers, fmp_client=None, limit=50) -> list[dict]
    run(root="/opt/stockbot", limit=50, max_universe=50) -> dict
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("portfolio_automation.news.run_news_intelligence")

_CONFIG_REL          = ("config.json",)
_WATCHLIST_REL       = ("outputs", "latest", "watchlist_signals.json")
_DECISION_PLAN_REL   = ("outputs", "latest", "decision_plan.json")
_DISCOVERY_REL       = ("outputs", "sandbox", "discovery", "emerging_candidates.json")


def _safe_load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
        return json.loads(raw) if raw else None
    except Exception as exc:
        logger.debug("news runner: failed to load %s — %s", path, exc)
        return None


def _normalize_ticker(value: Any) -> str:
    if not value:
        return ""
    return str(value).upper().strip()


def _load_fmp_budget() -> int | None:
    """Read fmp_daily_calls_budget from config.json; return None on any error."""
    try:
        cfg_path = Path("config.json")
        if not cfg_path.exists():
            return None
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        limits = (cfg.get("api_limits") or {}) if isinstance(cfg, dict) else {}
        return int(limits.get("fmp_daily_calls_budget", 0)) or None
    except Exception:
        return None


def collect_ticker_universe(
    root: Path,
    max_total: int = 50,
) -> tuple[list[str], list[str], list[str]]:
    """
    Return (holdings, watchlist, discovery_candidates) as deduplicated lists.

    Holdings come from config.json (the operator-maintained portfolio).
    Watchlist comes from outputs/latest/watchlist_signals.json.
    Discovery candidates come from outputs/sandbox/discovery/emerging_candidates.json.
    The combined universe is capped at max_total to respect the FMP daily budget;
    holdings are never trimmed.
    """
    holdings: list[str] = []
    watchlist: list[str] = []
    discovery: list[str] = []

    cfg = _safe_load_json(root.joinpath(*_CONFIG_REL))
    if isinstance(cfg, dict):
        portfolio = cfg.get("portfolio") or {}
        for h in (portfolio.get("holdings") or []):
            if isinstance(h, dict):
                sym = _normalize_ticker(h.get("symbol"))
                if sym and sym not in holdings:
                    holdings.append(sym)

    ws = _safe_load_json(root.joinpath(*_WATCHLIST_REL))
    if isinstance(ws, dict):
        for row in (ws.get("results") or []):
            if isinstance(row, dict):
                sym = _normalize_ticker(row.get("ticker"))
                if sym and sym not in watchlist and sym not in holdings:
                    watchlist.append(sym)

    plan = _safe_load_json(root.joinpath(*_DECISION_PLAN_REL))
    if isinstance(plan, dict):
        for d in (plan.get("decisions") or []):
            if isinstance(d, dict):
                sym = _normalize_ticker(d.get("symbol"))
                if sym and sym not in watchlist and sym not in holdings:
                    watchlist.append(sym)

    disc = _safe_load_json(root.joinpath(*_DISCOVERY_REL))
    if isinstance(disc, dict):
        for c in (disc.get("candidates") or []):
            if isinstance(c, dict):
                sym = _normalize_ticker(c.get("ticker"))
                if sym and sym not in discovery and sym not in holdings and sym not in watchlist:
                    discovery.append(sym)

    # Budget the watchlist + discovery lists if the combined size is large.
    remaining = max(0, max_total - len(holdings))
    watchlist = watchlist[:remaining]
    remaining = max(0, remaining - len(watchlist))
    discovery = discovery[:remaining]

    return holdings, watchlist, discovery


def fetch_news_articles(
    tickers: list[str],
    fmp_client: Any = None,
    limit: int = 50,
) -> list[dict]:
    """
    Fetch FMP news articles for the given tickers. Returns the raw normalized
    article list from FMPClient.get_stock_news (or an empty list on any error).
    Safe to call with an empty ticker list.
    """
    if not tickers:
        return []

    if fmp_client is None:
        try:
            from fmp_client import FMPClient
            budget = _load_fmp_budget()
            fmp_client = FMPClient(daily_budget=budget) if budget else FMPClient()
        except Exception as exc:
            logger.warning("news runner: could not instantiate FMPClient — %s", exc)
            return []

    try:
        articles = fmp_client.get_stock_news(tickers, limit=limit)
    except Exception as exc:
        logger.warning("news runner: get_stock_news failed — %s", exc)
        return []

    return articles if isinstance(articles, list) else []


def run(
    root: str | Path = ".",
    limit: int = 50,
    max_universe: int = 50,
    fmp_client: Any = None,
) -> dict[str, Any]:
    """
    Top-level orchestrator. Returns the producer summary dict on success,
    or a degraded-state dict on any unhandled exception.
    """
    root_path = Path(root).resolve()

    try:
        holdings, watchlist, discovery = collect_ticker_universe(
            root_path, max_total=max_universe
        )
        universe = list(dict.fromkeys(holdings + watchlist + discovery))
        articles = fetch_news_articles(universe, fmp_client=fmp_client, limit=limit)

        from portfolio_automation.news.fmp_news_intelligence import (
            run_fmp_news_intelligence,
        )
        result = run_fmp_news_intelligence(
            raw_articles=articles,
            holdings=holdings,
            watchlist=watchlist,
            discovery_candidates=discovery,
            base_dir=root_path / "outputs",
            run_mode="daily",
            write_files=True,
        )
        result.setdefault("universe_size", len(universe))
        result.setdefault("articles_fetched", len(articles))
        return result
    except Exception as exc:
        logger.error("news runner: unexpected failure — %s", exc, exc_info=True)
        return {
            "error": str(exc),
            "articles_fetched": 0,
            "universe_size": 0,
            "observe_only": True,
            "no_trade": True,
            "not_recommendation": True,
        }


if __name__ == "__main__":
    import sys

    summary = run(root=Path(__file__).resolve().parents[2])
    print(
        f"news_intelligence: universe={summary.get('universe_size', 0)} "
        f"articles={summary.get('articles_fetched', 0)} "
        f"packets={summary.get('evidence_packet_count', 0)} "
        f"error={summary.get('error', 'none')}"
    )
    sys.exit(0)
