"""Research page — sandbox lane.

The stub (`collect_research_stub`) is kept for backward compatibility — it
returns a tiny dict of discovery candidate counts.

The full view (`collect_research_view`) ports the Streamlit Automatic
Promotion Review page: candidate triage grouped by status, safety-boundary
panel, recent decision log, governance gates, producer summary.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _count(path: Path, key: str = "candidates") -> int:
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data.get(key) if isinstance(data, dict) else None
        return len(items) if isinstance(items, list) else 0
    except Exception:
        return 0


def collect_research_stub(repo_root: Path) -> dict[str, Any]:
    base = Path(repo_root) / "outputs" / "sandbox" / "discovery"
    return {
        "advisory_only": True,
        "no_trade": True,
        "discovery_only": True,
        "sandbox_only": True,
        "counts": {
            "emerging": _count(base / "emerging_candidates.json"),
            "rejected": _count(base / "rejected_candidates.json"),
            "promotion": _count(base / "automatic_promotion_candidates.json", "decisions"),
        },
    }


def collect_research_view(repo_root: Path) -> dict[str, Any]:
    """
    Full Research page data — Automatic Promotion Review migrated from
    gui/page_automatic_promotion. Reuses the existing
    gui_operator_data.load_automatic_promotion_data loader which has a
    stable, never-raises contract.

    Returns the same dict shape as the stub plus an `auto_promotion` block
    carrying the full review data.
    """
    base = collect_research_stub(repo_root)
    try:
        from gui_operator_data import load_automatic_promotion_data
        ap = load_automatic_promotion_data(Path(repo_root))
    except Exception as exc:
        ap = {
            "available": False,
            "error": f"loader_failed: {exc}",
        }
    base["auto_promotion"] = ap
    return base
