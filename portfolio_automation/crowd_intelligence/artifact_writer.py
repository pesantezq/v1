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
    latest = Path(base_dir) / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    payload = {
        "observe_only": _OBSERVE_ONLY, "source": "crowd_intelligence",
        "generated_at": status.get("generated_at"), "weights": norm.WEIGHTS,
        "symbols": [asdict(s) for s in signals],
    }
    (latest / "crowd_intelligence.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (latest / "crowd_intelligence.md").write_text(_render_md(signals, status), encoding="utf-8")
    (latest / "crowd_intelligence_status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")


def _load_holdings(root: Path) -> list[str]:
    try:
        cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
        return [str(h["symbol"]).upper() for h in (cfg.get("portfolio", {}).get("holdings") or [])
                if isinstance(h, dict) and h.get("symbol")]
    except Exception:
        return []


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
        universe = symbols or _load_holdings(root)
        now_iso = datetime.now(timezone.utc).isoformat()
        signals, events, status = builder.build_signals(
            universe, client=client, capabilities=capabilities, now_iso=now_iso)
        store = CapabilityStore(root / "data" / "crowd_intelligence.db")
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
