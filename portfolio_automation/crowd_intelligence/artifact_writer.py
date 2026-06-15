"""Write crowd-intelligence artifacts + persist to SQLite, and the run() entrypoint.

Observe-only: writes only the 3 crowd artifacts + the 2 crowd_intelligence.db
tables. Never reads or mutates decision_plan.json / allocations / scoring.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.crowd_intelligence.capability_store import CapabilityStore
from portfolio_automation.crowd_intelligence import crowd_signal_builder as builder
from portfolio_automation.crowd_intelligence import normalization as norm
from portfolio_automation.data_governance import OutputNamespace, safe_write_text

_OBSERVE_ONLY = True


def _render_md(signals: list, status: dict) -> str:
    lines = [f"# Crowd Intelligence — {status.get('generated_at', '')}", "",
             "Observe-only crowd/market-attention context. **Not** a trade signal; never "
             "creates or changes a BUY/SELL/HOLD or allocation.", "",
             f"Enabled categories: {', '.join(status.get('enabled_categories') or []) or 'none'} · "
             f"Disabled: {', '.join(status.get('disabled_categories') or []) or 'none'}", "",
             "| Symbol | Crowd | Conf | news | analyst | insider | congress | attention | top reason |",
             "|---|---|---|---|---|---|---|---|---|"]
    for s in signals:
        cs = s.category_scores
        reason = (s.top_reasons[0] if s.top_reasons else "")[:60]
        lines.append(
            f"| {s.symbol} | {s.composite_crowd_score:+.2f} | {s.confidence:.2f} | "
            f"{cs.get('news', 0):+.2f} | {cs.get('analyst', 0):+.2f} | {cs.get('insider', 0):+.2f} | "
            f"{cs.get('congress', 0):+.2f} | {cs.get('attention', 0):+.2f} | {reason} |")
    return "\n".join(lines) + "\n"


def write_artifacts(signals: list, status: dict, *, base_dir: Path | str = "outputs") -> None:
    payload = {
        "observe_only": _OBSERVE_ONLY, "source": "crowd_intelligence",
        "generated_at": status.get("generated_at"), "weights": norm.WEIGHTS,
        "symbols": [asdict(s) for s in signals],
    }
    # OutputNamespace.LATEST governance (CLAUDE.md: all writes go through it).
    safe_write_text(OutputNamespace.LATEST, "crowd_intelligence.json",
                    json.dumps(payload, indent=2), base_dir=base_dir)
    safe_write_text(OutputNamespace.LATEST, "crowd_intelligence.md",
                    _render_md(signals, status), base_dir=base_dir)
    safe_write_text(OutputNamespace.LATEST, "crowd_intelligence_status.json",
                    json.dumps(status, indent=2), base_dir=base_dir)


import re as _re

# Real exchange tickers: uppercase, ≤7 chars, optional single . or - class suffix
# (AAPL, QQQ, BRK.B, BF-B). Rejects synthetic decision_plan entries like
# EMERGENCY_FUND_2026-06-15 / DRIFT_QQQ_2026-06-15 (underscores, date suffix) so the
# universe cap is spent only on symbols FMP can actually return data for.
_TICKER_RE = _re.compile(r"^[A-Z][A-Z0-9]{0,5}([.\-][A-Z]{1,4})?$")


def _is_ticker_like(sym: str) -> bool:
    return bool(_TICKER_RE.match(sym)) and "_" not in sym


def _load_universe(root: Path, *, max_symbols: int = 60) -> list[str]:
    """Holdings ∪ decision_plan advisory-picks ∪ daily watchlist single-names (all
    free artifacts), deduped, ticker-shape filtered, capped to bound governed FMP
    calls. Picks come first (they're what the GUI shows), then holdings, then the
    watchlist single-names that enrich coverage + build per-symbol history."""
    syms: list[str] = []
    seen: set[str] = set()

    def _add(sym):
        s = str(sym or "").upper().strip()
        if s and s not in seen and _is_ticker_like(s):
            seen.add(s)
            syms.append(s)

    latest = root / "outputs" / "latest"
    # 1. Advisory picks first — these are what the GUI attaches context to.
    try:
        dp = json.loads((latest / "decision_plan.json").read_text(encoding="utf-8"))
        for d in (dp.get("decisions") or []):
            if isinstance(d, dict):
                _add(d.get("symbol") or d.get("ticker"))
    except Exception:
        pass
    # 2. Holdings.
    try:
        cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
        for h in (cfg.get("portfolio", {}).get("holdings") or []):
            if isinstance(h, dict):
                _add(h.get("symbol"))
    except Exception:
        pass
    # 3. Daily watchlist single-names (free artifact) — enrich coverage + build history.
    try:
        ws = json.loads((latest / "watchlist_signals.json").read_text(encoding="utf-8"))
        for r in (ws.get("results") or ws.get("signals") or []):
            if isinstance(r, dict):
                _add(r.get("ticker") or r.get("symbol"))
    except Exception:
        pass
    return syms[:max_symbols]


def apply_trend(signals: list, prior_by_sym: dict[str, float]) -> None:
    """Set composite_trend + trend_label on each signal from prior-day composites.
    'building' when no prior history; flat/rising/falling at ±0.05. Pure."""
    for s in signals:
        prev = prior_by_sym.get(s.symbol)
        if prev is None:
            s.trend_label = "building"
            s.composite_trend = None
        else:
            s.composite_trend = round(s.composite_crowd_score - float(prev), 4)
            s.trend_label = ("rising" if s.composite_trend >= 0.05
                             else "falling" if s.composite_trend <= -0.05 else "flat")


def run(root: str | Path = ".", *, symbols: list[str] | None = None) -> dict:
    """Non-blocking entrypoint: build crowd context for the holdings universe and
    write artifacts + persist. Returns the status dict. Never raises."""
    try:
        root = Path(root)
        from portfolio_automation.data_budget.factory import governed_client
        client = governed_client("discovery")
        caps_path = root / "outputs" / "latest" / "fmp_endpoint_capabilities.json"
        capabilities = {}
        if caps_path.exists():
            capabilities = json.loads(caps_path.read_text(encoding="utf-8"))
        universe = symbols or _load_universe(root)
        now_iso = datetime.now(timezone.utc).isoformat()
        signals, events, status = builder.build_signals(
            universe, client=client, capabilities=capabilities, now_iso=now_iso)
        store = CapabilityStore(root / "data" / "crowd_intelligence.db")
        # Trend vs the most-recent PRIOR day — read history BEFORE writing today's row.
        today = status["signal_date"]
        prior_by_sym: dict[str, float] = {}
        for r in store.daily_rows():  # ordered by (symbol, signal_date) ascending
            if r.get("signal_date") and r["signal_date"] < today and r.get("composite_crowd_score") is not None:
                prior_by_sym[r["symbol"]] = r["composite_crowd_score"]
        apply_trend(signals, prior_by_sym)
        store.record_events(events)
        store.upsert_daily([{
            "symbol": s.symbol, "signal_date": status["signal_date"],
            "news_score": s.category_scores.get("news"), "analyst_score": s.category_scores.get("analyst"),
            "insider_score": s.category_scores.get("insider"), "congress_score": s.category_scores.get("congress"),
            "attention_score": s.category_scores.get("attention"),
            "social_sentiment_score": s.category_scores.get("social_sentiment"),
            "composite_crowd_score": s.composite_crowd_score, "confidence": s.confidence,
            "enabled_sources_json": json.dumps(s.enabled_sources),
            "disabled_sources_json": json.dumps(s.disabled_sources),
            "explanation_json": json.dumps({"top_reasons": s.top_reasons, "warnings": s.warnings}),
            "created_at": now_iso,
        } for s in signals])
        write_artifacts(signals, status, base_dir=root / "outputs")
        return status
    except Exception as exc:
        return {"observe_only": True, "overall_status": "error",
                "error": f"{type(exc).__name__}: {exc}"}


if __name__ == "__main__":
    print(json.dumps(run("."), indent=2))
