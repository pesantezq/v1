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

    status = _read_json(latest / "pipeline_run_status.json")
    plan = _read_json(latest / "decision_plan.json")
    opps = _read_json(latest / "market_opportunities.json")
    memo_md = _read_text(latest / "daily_memo.md")

    return {
        "advisory_only": True,
        "no_trade": True,
        "repo_root": str(repo_root),
        "header": _header_from_status(status),
        "decisions": _decisions(plan),
        "capital_actions": _capital_actions(plan),
        "risk_focus": _risk_focus(plan),
        "top_movers": _top_movers(opps),
        "memo_html": _render_markdown(memo_md),
    }
