"""
Historical Backfill — weekend collector for 5y daily price history.

Pre-warms the FMP cache + writes a permanent HISTORICAL-namespace archive
so the historical_replay module (and any future backtest consumers) can
load 5-year price series without a live FMP call during weekday runs.

Step 2 of the FMP-capacity roadmap sequence (per
.agent/project_state.yaml:queued_after_n100_confirmation). Designed for
Sat/Sun cron — markets are closed, idle FMP capacity is available.

Hard guarantees (matches roadmap invariants):
  - observe_only=True hardcoded
  - Writes ONLY to HISTORICAL (outputs/backtest/) and LATEST (status artifact)
  - Never modifies portfolio / scoring / decision / recommendation state
  - Degrades safely when FMP budget is exhausted; the failure is logged
    and partial progress persists for the next weekend's catch-up run
  - Per-ticker error isolation — one symbol's failure can't block others

Public API:
  build_universe(root) -> list[str]
  archive_path_for(root, ticker) -> Path
  is_archive_fresh(path, max_age_days=7) -> bool
  run_historical_backfill(root, *, max_tickers=None, force=False,
                          write_files=True, dry_run=False) -> dict
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.data_governance import (
    OutputNamespace,
    safe_write_json,
    safe_write_text,
)

logger = logging.getLogger("stockbot.portfolio_automation.historical_backfill")

_SCHEMA_VERSION = "1"
_SOURCE_LABEL = "historical_backfill"
_OBSERVE_ONLY = True
_ARCHIVE_REL_DIR = ("backtest", "historical")  # under outputs/
_DEFAULT_YEARS = 5
_DEFAULT_FRESHNESS_DAYS = 7  # re-fetch if archive older than this

_DISCLAIMER = (
    "Observe-only weekend backfill collector. Pulls 5y daily price history "
    "via FMP and writes a permanent archive under outputs/backtest/historical/. "
    "Does not modify portfolio, allocation, scoring, or decision state."
)


# ---------------------------------------------------------------------------
# Universe + archive path helpers
# ---------------------------------------------------------------------------


def _load_static_watchlist(root: Path) -> list[str]:
    cfg_path = root / "config.json"
    if not cfg_path.exists():
        return []
    try:
        d = json.loads(cfg_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []
    return [
        s.upper()
        for s in (d.get("watchlist_scanner") or {}).get("watchlist") or []
        if isinstance(s, str)
    ]


def _load_extended_active(root: Path) -> list[str]:
    db_path = root / "data" / "portfolio.db"
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT symbol FROM extended_watchlist WHERE is_active=1"
        ).fetchall()
        conn.close()
        return [r[0].upper() for r in rows if r and r[0]]
    except Exception as exc:
        logger.debug("historical_backfill: extended_watchlist read failed: %s", exc)
        return []


def _load_top100_symbols(root: Path) -> list[str]:
    p = root / "data" / "fmp_cache" / "top100_watchlist.json"
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        cands = d.get("candidates") if isinstance(d, dict) else []
        return [
            (c.get("symbol") or "").upper()
            for c in cands
            if isinstance(c, dict) and c.get("symbol")
        ]
    except Exception:
        return []


def _load_simulation_price_universe(root: Path) -> list[str]:
    """Broad-market + sector ETFs the simulation suite prices off the archive.

    The portfolio_sim/strategy-lab tactics derive their universe from
    ``config/universe_lists.yaml`` (see portfolio_sim/universe.py), then load
    each ticker's 5y history from this backfill's archive — with no live-FMP
    fallback. So any ETF declared there but absent from the watchlist/top100
    universe (e.g. XLI) would never get an archive and silently drop out of a
    walk-forward fold. Including it here keeps the two universes coupled.
    Fail-safe: missing file / unreadable YAML / no PyYAML → empty list."""
    path = root / "config" / "universe_lists.yaml"
    if not path.exists():
        return []
    try:
        import yaml  # type: ignore
    except Exception:
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace")) or {}
    except Exception as exc:
        logger.debug("historical_backfill: universe_lists read failed: %s", exc)
        return []
    out: list[str] = []
    for key in ("broad_market_etfs", "sector_etfs"):
        for sym in data.get(key) or []:
            if isinstance(sym, str) and sym.strip():
                out.append(sym.strip().upper())
    return out


def build_universe(root: Path) -> list[str]:
    """Union of static watchlist + extended_watchlist active + top100 candidates
    + the simulation suite's broad/sector-ETF price universe.
    Deduplicated, sorted alphabetically for deterministic ordering."""
    seen: set[str] = set()
    ordered: list[str] = []
    for source in (
        _load_static_watchlist(root),
        _load_extended_active(root),
        _load_top100_symbols(root),
        _load_simulation_price_universe(root),
    ):
        for sym in source:
            if sym and sym not in seen:
                seen.add(sym)
                ordered.append(sym)
    ordered.sort()
    return ordered


def archive_path_for(root: Path, ticker: str, years: int = _DEFAULT_YEARS) -> Path:
    """Resolve the on-disk archive location for a ticker's N-year history."""
    sym = (ticker or "").strip().upper()
    return root / "outputs" / "backtest" / "historical" / f"{sym}_{years}y.json"


def is_archive_fresh(path: Path, max_age_days: int = _DEFAULT_FRESHNESS_DAYS) -> bool:
    """True iff archive exists AND mtime is within max_age_days."""
    if not path.exists():
        return False
    try:
        age_days = (datetime.now(timezone.utc).timestamp() - path.stat().st_mtime) / 86400
    except Exception:
        return False
    return age_days < max_age_days


# ---------------------------------------------------------------------------
# Per-ticker fetch + write
# ---------------------------------------------------------------------------


def _persist_archive(root: Path, ticker: str, rows: list[dict[str, Any]],
                     years: int = _DEFAULT_YEARS) -> Path:
    """Write the HISTORICAL-namespace archive. Returns the written path."""
    payload = {
        "symbol": ticker.upper(),
        "years": years,
        "row_count": len(rows),
        "stored_at": datetime.now(timezone.utc).isoformat(),
        "rows": rows,
        "schema_version": _SCHEMA_VERSION,
        "source": _SOURCE_LABEL,
        "observe_only": _OBSERVE_ONLY,
    }
    # safe_write_json maps HISTORICAL → outputs/backtest/<filename>
    return safe_write_json(
        OutputNamespace.HISTORICAL,
        f"historical/{ticker.upper()}_{years}y.json",
        payload,
        base_dir=root / "outputs",
    )


def _budget_exhausted(fmp_client: Any) -> bool:
    """
    True when the client's daily call budget would be exceeded.

    ``FMPClient.get_historical_prices`` returns an empty list (not an exception)
    when the daily budget is spent and no cache is available — indistinguishable
    from a genuinely-empty history. Consulting the client's own counter lets the
    caller surface ``budget_exhausted`` instead of a misleading empty result.
    """
    try:
        counter = getattr(fmp_client, "_counter", None)
        budget = getattr(fmp_client, "_budget", None)
        if counter is None or budget is None:
            return False
        return bool(counter.would_exceed(budget))
    except Exception:
        return False


def _fetch_one(
    fmp_client: Any,
    ticker: str,
    years: int,
) -> tuple[str, list[dict[str, Any]] | None, str | None]:
    """Return (ticker, rows, error). rows=None on failure with error string."""
    try:
        rows = fmp_client.get_historical_prices(ticker, years=years)
        if not isinstance(rows, list):
            return (ticker, None, "fmp_returned_non_list")
        if not rows:
            # Distinguish a budget-exhausted empty from a genuinely-empty history
            # so callers do not mistake a spent quota for "no data exists".
            if _budget_exhausted(fmp_client):
                return (ticker, None, "budget_exhausted")
            return (ticker, [], "fmp_returned_empty")
        return (ticker, rows, None)
    except Exception as exc:
        return (ticker, None, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_historical_backfill(
    *,
    root: str | Path = ".",
    max_tickers: int | None = None,
    force: bool = False,
    freshness_days: int = _DEFAULT_FRESHNESS_DAYS,
    years: int = _DEFAULT_YEARS,
    write_files: bool = True,
    dry_run: bool = False,
    fmp_client: Any | None = None,
) -> dict[str, Any]:
    """Top-level entry. Never raises — returns a status dict."""
    root_path = Path(root).resolve()
    ts = datetime.now(timezone.utc).isoformat()

    universe = build_universe(root_path)
    if max_tickers is not None:
        universe = universe[:max_tickers]

    per_ticker: list[dict[str, Any]] = []
    fetched = 0
    skipped_fresh = 0
    skipped_budget = 0
    errored = 0
    written_paths: list[str] = []

    # Init FMP client (caller may inject a stub for tests)
    if fmp_client is None and not dry_run and universe:
        try:
            import sys as _sys
            _sys.path.insert(0, str(root_path))
            from portfolio_automation.data_budget.factory import governed_client
            fmp_client = governed_client("historical_replay")
        except Exception as exc:
            logger.error("historical_backfill: FMP client init failed: %s", exc)
            payload = _build_payload(
                ts, universe, per_ticker, fetched, skipped_fresh,
                skipped_budget, errored, years, freshness_days,
                error=f"fmp_init_failed: {type(exc).__name__}: {exc}",
            )
            if write_files and not dry_run:
                _write_status(root_path, payload)
            return payload

    for sym in universe:
        archive = archive_path_for(root_path, sym, years=years)
        if not force and is_archive_fresh(archive, max_age_days=freshness_days):
            skipped_fresh += 1
            per_ticker.append({"symbol": sym, "status": "skipped_fresh"})
            continue

        if dry_run:
            per_ticker.append({"symbol": sym, "status": "dry_run"})
            continue

        # Budget guard — FMP client's own counter is authoritative
        try:
            if fmp_client._counter.would_exceed(fmp_client._budget):
                skipped_budget += 1
                per_ticker.append({"symbol": sym, "status": "skipped_budget"})
                continue
        except AttributeError:
            # Test stub may not implement the counter — assume ok
            pass

        sym_done, rows, err = _fetch_one(fmp_client, sym, years)
        if err:
            errored += 1
            per_ticker.append({"symbol": sym_done, "status": "error", "error": err})
            continue
        if not rows:
            errored += 1
            per_ticker.append({"symbol": sym_done, "status": "empty"})
            continue

        try:
            written = _persist_archive(root_path, sym_done, rows, years=years)
            fetched += 1
            written_paths.append(str(written))
            per_ticker.append({
                "symbol": sym_done,
                "status": "ok",
                "row_count": len(rows),
                "archive_path": str(written),
            })
        except Exception as exc:
            errored += 1
            per_ticker.append({
                "symbol": sym_done,
                "status": "write_error",
                "error": f"{type(exc).__name__}: {exc}",
            })

    payload = _build_payload(
        ts, universe, per_ticker, fetched, skipped_fresh,
        skipped_budget, errored, years, freshness_days,
    )

    if write_files and not dry_run:
        _write_status(root_path, payload)

    return payload


def _build_payload(
    ts: str,
    universe: list[str],
    per_ticker: list[dict[str, Any]],
    fetched: int,
    skipped_fresh: int,
    skipped_budget: int,
    errored: int,
    years: int,
    freshness_days: int,
    *,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "generated_at": ts,
        "observe_only": _OBSERVE_ONLY,
        "schema_version": _SCHEMA_VERSION,
        "source": _SOURCE_LABEL,
        "years": years,
        "freshness_days": freshness_days,
        "universe_size": len(universe),
        "fetched": fetched,
        "skipped_fresh": skipped_fresh,
        "skipped_budget": skipped_budget,
        "errored": errored,
        "per_ticker": per_ticker,
        "error": error,
        "disclaimer": _DISCLAIMER,
    }


# ---------------------------------------------------------------------------
# Rendering + status writer
# ---------------------------------------------------------------------------


def _render_md(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    a = lines.append
    a(f"# Historical Backfill — {payload.get('generated_at', '')[:19]}")
    a("")
    a(f"**Universe size:** {payload.get('universe_size', 0)} ticker(s)  ")
    a(f"**Years per ticker:** {payload.get('years', 5)}  ")
    a(f"**Freshness window:** {payload.get('freshness_days', 7)} days")
    a("")
    a(f"> {payload.get('disclaimer', _DISCLAIMER)}")
    a("")
    if payload.get("error"):
        a(f"## ⚠ Run aborted")
        a("")
        a(f"`{payload['error']}`")
        return "\n".join(lines)

    a("## Summary")
    a("")
    a(f"- fetched: **{payload.get('fetched', 0)}**")
    a(f"- skipped (fresh archive): {payload.get('skipped_fresh', 0)}")
    a(f"- skipped (budget exhausted): {payload.get('skipped_budget', 0)}")
    a(f"- errored: {payload.get('errored', 0)}")
    a("")

    pt = payload.get("per_ticker") or []
    if pt:
        a("## Per-ticker outcomes")
        a("")
        a("| Symbol | Status | Rows | Notes |")
        a("|---|---|---|---|")
        for row in pt:
            note = row.get("error") or ""
            rc = row.get("row_count", "")
            a(f"| `{row.get('symbol')}` | {row.get('status')} | {rc} | {note} |")
        a("")
    a("---")
    a("_Observe-only weekend backfill collector._")
    return "\n".join(lines)


def _write_status(root: Path, payload: dict[str, Any]) -> None:
    try:
        safe_write_json(
            OutputNamespace.LATEST,
            "historical_backfill_status.json",
            payload,
            base_dir=root / "outputs",
        )
        safe_write_text(
            OutputNamespace.LATEST,
            "historical_backfill_status.md",
            _render_md(payload),
            base_dir=root / "outputs",
        )
    except Exception as exc:
        logger.warning("historical_backfill: status write failed: %s", exc)


if __name__ == "__main__":
    import sys
    root_arg = Path(__file__).resolve().parents[1]
    try:
        sys.path.insert(0, str(root_arg))
        from utils import load_env
        load_env(str(root_arg / ".env"))
    except Exception:
        pass
    r = run_historical_backfill(root=root_arg)
    print(
        f"historical_backfill: universe={r.get('universe_size', 0)} "
        f"fetched={r.get('fetched', 0)} "
        f"skipped_fresh={r.get('skipped_fresh', 0)} "
        f"skipped_budget={r.get('skipped_budget', 0)} "
        f"errored={r.get('errored', 0)}"
    )
    sys.exit(0)
