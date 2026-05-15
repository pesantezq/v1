"""Today page — daily-use operator surface."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    import markdown as _markdown  # type: ignore
except Exception:
    _markdown = None  # graceful degrade


def _read_json(path: Path) -> Any | None:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_text(path: Path) -> str:
    try:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _render_markdown(src: str) -> str:
    if not src:
        return ""
    if _markdown is None:
        from html import escape
        return f"<pre>{escape(src)}</pre>"
    try:
        return _markdown.markdown(src, extensions=["extra", "sane_lists"])
    except Exception:
        from html import escape
        return f"<pre>{escape(src)}</pre>"


def _header_from_status(status: dict | None) -> dict[str, Any]:
    if not isinstance(status, dict):
        return {"run_id": None, "run_mode": None, "success": None,
                "generated_at": None}
    return {
        "run_id": status.get("run_id"),
        "run_mode": status.get("run_mode"),
        "success": status.get("success"),
        "generated_at": status.get("generated_at"),
    }


def _decisions(plan: dict | None, top_n: int = 5) -> list[dict[str, Any]]:
    if not isinstance(plan, dict):
        return []
    rows = plan.get("decisions")
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows[:top_n]:
        if not isinstance(row, dict):
            continue
        out.append({
            "symbol": row.get("symbol"),
            "decision": row.get("decision"),
            "priority": row.get("priority"),
            "urgency": row.get("urgency"),
            "source": row.get("source"),
            "reason": row.get("reason") or row.get("decision_reason") or "",
        })
    return out


def _capital_actions(plan: dict | None) -> dict[str, float]:
    out = {"SELL": 0.0, "SCALE": 0.0, "BUY": 0.0}
    if not isinstance(plan, dict):
        return out
    for row in plan.get("decisions", []) or []:
        if not isinstance(row, dict):
            continue
        action = str(row.get("decision") or "").upper()
        amt = row.get("recommended_amount")
        if action in out and isinstance(amt, (int, float)):
            out[action] += float(amt)
    return out


def _risk_focus(plan: dict | None) -> list[dict[str, Any]]:
    if not isinstance(plan, dict):
        return []
    out: list[dict[str, Any]] = []
    for row in plan.get("decisions", []) or []:
        if not isinstance(row, dict):
            continue
        flags = row.get("risk_flags") or []
        if flags:
            out.append({
                "symbol": row.get("symbol"),
                "flags": flags,
            })
        if len(out) >= 5:
            break
    return out


def _full_decisions(plan: dict | None) -> list[dict[str, Any]]:
    """Every decision row, normalised to a small projection for the queue view."""
    if not isinstance(plan, dict):
        return []
    rows = plan.get("decisions")
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append({
            "symbol": row.get("symbol"),
            "decision": row.get("decision"),
            "priority": row.get("priority"),
            "urgency": row.get("urgency"),
            "source": row.get("source"),
            "reason": row.get("reason") or row.get("decision_reason") or "",
            "recommended_amount": row.get("recommended_amount"),
            "recommended_allocation_pct": row.get("recommended_allocation_pct"),
            "confidence": row.get("confidence"),
            "risk_flags": row.get("risk_flags") or [],
        })
    return out


def _validations_by_symbol(validation: dict | None) -> dict[str, dict[str, Any]]:
    """Map symbol -> {validation_status, plain_english_summary} for join with decisions."""
    if not isinstance(validation, dict):
        return {}
    rows = validation.get("validations")
    if not isinstance(rows, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        sym = row.get("symbol")
        if not sym:
            continue
        out[sym] = {
            "status": row.get("validation_status"),
            "summary": row.get("plain_english_summary"),
            "contradictions": row.get("contradictions") or [],
            "watch_next": row.get("watch_next") or [],
        }
    return out


def _explanations_by_symbol(expl: dict | None) -> dict[str, dict[str, Any]]:
    if not isinstance(expl, dict):
        return {}
    rows = expl.get("explanations")
    if not isinstance(rows, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        sym = row.get("symbol")
        if not sym:
            continue
        out[sym] = {
            "concise": row.get("concise_explanation"),
            "risks": row.get("risks") or [],
            "what_to_watch_next": row.get("what_to_watch_next") or [],
        }
    return out


def _validation_counts(validation: dict | None) -> dict[str, Any]:
    if not isinstance(validation, dict):
        return {"available": False}
    return {
        "available": bool(validation.get("available")),
        "total": validation.get("total_validated", 0),
        "aligned": validation.get("aligned_count", 0),
        "caution": validation.get("caution_count", 0),
        "contradiction": validation.get("contradiction_count", 0),
        "insufficient": validation.get("insufficient_context_count", 0),
        "ai_used": bool(validation.get("ai_used")),
        "summary_line": validation.get("summary_line", ""),
    }


def _market_narrative(payload: dict | None) -> dict[str, Any]:
    """Project a single market_narrative_*.json to the fields Today renders."""
    if not isinstance(payload, dict):
        return {"available": False}
    if payload.get("data_available") is False:
        return {"available": False}
    return {
        "available": True,
        "period": payload.get("narrative_period"),
        "generated_at": payload.get("generated_at"),
        "top_headline": payload.get("top_headline") or "",
        "executive_summary": payload.get("executive_summary") or "",
        "key_themes": [t for t in (payload.get("key_themes") or []) if t][:6],
        "risks_to_watch": [r for r in (payload.get("risks_to_watch") or []) if r][:6],
        "catalysts_to_watch": [c for c in (payload.get("catalysts_to_watch") or []) if c][:6],
        "operator_watchlist": [
            w for w in (payload.get("operator_watchlist") or []) if w
        ][:8],
    }


def _market_narratives_summary(
    daily: dict | None, weekly: dict | None, monthly: dict | None,
) -> dict[str, Any]:
    """Aggregate the three market_narrative_*.json artifacts. Never raises."""
    sections = {
        "daily": _market_narrative(daily),
        "weekly": _market_narrative(weekly),
        "monthly": _market_narrative(monthly),
    }
    available = any(s.get("available") for s in sections.values())
    return {"available": available, **sections}


def _news_evidence_summary(news: dict | None) -> dict[str, Any]:
    """
    Project outputs/latest/news_evidence_layer.json down to what Today
    actually renders.  Returns ``available=False`` when the artifact is
    missing or carries ``data_available=False``.  Never raises.
    """
    if not isinstance(news, dict):
        return {"available": False}
    if news.get("data_available") is False:
        return {"available": False, "missing_inputs": news.get("missing_inputs") or []}

    def _events(rows: list | None, max_rows: int = 8) -> list[dict[str, Any]]:
        if not isinstance(rows, list):
            return []
        out: list[dict[str, Any]] = []
        for r in rows[:max_rows]:
            if not isinstance(r, dict):
                continue
            out.append({
                "label": r.get("label"),
                "count": r.get("count"),
                "tickers": r.get("tickers") or [],
                "description": r.get("description") or "",
            })
        return out

    bullets = [b for b in (news.get("memo_bullets") or []) if isinstance(b, str)][:6]
    return {
        "available": True,
        "generated_at": news.get("generated_at"),
        "influence_cap": news.get("influence_cap") or "context_only",
        "portfolio_context": news.get("portfolio_context") or "",
        "memo_bullets": bullets,
        "catalyst_evidence": _events(news.get("catalyst_evidence")),
        "risk_evidence": _events(news.get("risk_evidence")),
        "operator_review_flags": news.get("operator_review_flags") or [],
        "discovery_context_summary": news.get("discovery_context_summary") or "",
    }


def _decision_performance(outcome_summary: dict | None) -> dict[str, Any]:
    if not isinstance(outcome_summary, dict):
        return {"available": False}
    return {
        "available": True,
        "total_decisions": outcome_summary.get("total_decisions"),
        "resolved": outcome_summary.get("resolved"),
        "unresolved": outcome_summary.get("unresolved"),
        "hit_rate": outcome_summary.get("hit_rate"),
        "avg_return_pct": outcome_summary.get("avg_return_pct"),
        "last_10_resolved": outcome_summary.get("last_10_resolved", []),
        "best_decision": outcome_summary.get("best_decision"),
        "worst_decision": outcome_summary.get("worst_decision"),
    }


def _top_movers(opps: dict | None, max_rows: int = 8) -> list[dict[str, Any]]:
    if not isinstance(opps, dict):
        return []
    rows = opps.get("symbols") or opps.get("opportunities") or []
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows[:max_rows]:
        if not isinstance(row, dict):
            continue
        out.append({
            "symbol": row.get("symbol") or row.get("ticker"),
            "change_pct": row.get("change_pct") or row.get("daily_change"),
            "sector": row.get("sector"),
        })
    return out


def collect_today_view(repo_root: Path) -> dict[str, Any]:
    """
    Aggregate every input the Today page needs into a single dict.

    Pure function: no Streamlit, no FastAPI, no HTML. Reads outputs/* and
    returns a dict; never raises.
    """
    latest = Path(repo_root) / "outputs" / "latest"
    policy = Path(repo_root) / "outputs" / "policy"

    status = _read_json(latest / "pipeline_run_status.json")
    plan = _read_json(latest / "decision_plan.json")
    opps = _read_json(latest / "market_opportunities.json")
    memo_md = _read_text(latest / "daily_memo.md")

    # Decision Center inputs (migrated from gui/page_decision_center)
    validation = _read_json(latest / "ai_decision_validation.json")
    explanations = _read_json(latest / "decision_explanations.json")
    outcome_summary = _read_json(policy / "decision_outcome_summary.json")

    # News evidence + market narrative
    news_evidence = _read_json(latest / "news_evidence_layer.json")
    narrative_daily = _read_json(latest / "market_narrative_daily.json")
    narrative_weekly = _read_json(latest / "market_narrative_weekly.json")
    narrative_monthly = _read_json(latest / "market_narrative_monthly.json")

    return {
        "advisory_only": True,
        "no_trade": True,
        "repo_root": str(repo_root),
        "header": _header_from_status(status),
        "decisions": _decisions(plan),
        "full_decisions": _full_decisions(plan),
        "capital_actions": _capital_actions(plan),
        "risk_focus": _risk_focus(plan),
        "top_movers": _top_movers(opps),
        "memo_html": _render_markdown(memo_md),
        # Decision Center sections
        "validation_counts": _validation_counts(validation),
        "validations_by_symbol": _validations_by_symbol(validation),
        "explanations_by_symbol": _explanations_by_symbol(explanations),
        "decision_performance": _decision_performance(outcome_summary),
        # News evidence (context-only)
        "news_evidence": _news_evidence_summary(news_evidence),
        # Market narratives (deterministic; daily / weekly / monthly)
        "market_narratives": _market_narratives_summary(
            narrative_daily, narrative_weekly, narrative_monthly,
        ),
    }
