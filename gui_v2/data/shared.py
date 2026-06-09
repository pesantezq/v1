"""Shared helpers for the persona dashboard: normalized card shape + json reader."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any

_STATUS_TO_SEVERITY = {
    "ok": "green",
    "warning": "yellow",
    "red": "red",
    "info": "blue",
    "unknown": "gray",
}


def _read_json(path: Path) -> Any | None:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def card(
    title: str,
    *,
    status: str = "unknown",
    label: str = "",
    summary: str = "",
    source_artifacts: list[str] | None = None,
    updated_at: str | None = None,
) -> dict:
    """Normalized dashboard card. status in ok|warning|red|info|unknown."""
    status = status if status in _STATUS_TO_SEVERITY else "unknown"
    return {
        "title": title,
        "status": status,
        "label": label,
        "summary": summary,
        "source_artifacts": source_artifacts or [],
        "updated_at": updated_at,
        "severity": _STATUS_TO_SEVERITY[status],
    }


# Old-route -> persona-route redirect map (Task 1 wires these in app.py).
REDIRECT_MAP = {
    "/portfolio": "/dashboard/portfolio",
    "/risk-impact": "/dashboard/portfolio",
    "/research": "/dashboard/quant",
    "/health": "/dashboard/system",
    "/operations": "/dashboard/system",
}
