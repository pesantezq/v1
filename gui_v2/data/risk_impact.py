"""
Risk & Impact view — consolidates risk_delta + retune_impact + daily_run_status
into a single read-only template payload.

Pure data layer: reads JSON artifacts under outputs/latest/, returns a dict
the Jinja2 template renders. No side effects, no API calls, no mutation.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _safe_load(path: Path) -> dict[str, Any] | None:
    """Return the parsed JSON, or None on any read/parse failure."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _classify_top_status(*sub_statuses: str | None) -> str:
    """Take the worst across the three sub-statuses; ok < ok_with_warnings < near_cap < breach < failed."""
    priority = {
        "ok": 0,
        "ok_with_warnings": 1,
        "news_empty": 1,
        "near_cap": 2,
        "exhausted": 3,
        "breach": 3,
        "partial": 3,
        "failed": 4,
    }
    worst = "ok"
    for s in sub_statuses:
        if s and priority.get(s, 0) > priority.get(worst, 0):
            worst = s
    return worst


def collect_risk_impact_view(repo_root: Path) -> dict[str, Any]:
    """
    Return a dict consumable by templates/risk_impact.html.

    Shape:
      {
        "risk_delta":          (dict or None) — outputs/latest/risk_delta.json
        "retune_impact":       (dict or None) — outputs/latest/retune_impact.json
        "daily_run_status":    (dict or None) — outputs/latest/daily_run_status.json
        "fmp_budget_status":   (dict or None) — outputs/latest/fmp_budget_status.json
        "overall_status":      str — derived
        "missing_artifacts":   list[str] — names of missing JSON files
      }
    """
    artifact_root = Path(repo_root) / "outputs" / "latest"
    sources = {
        "risk_delta":        artifact_root / "risk_delta.json",
        "retune_impact":     artifact_root / "retune_impact.json",
        "daily_run_status":  artifact_root / "daily_run_status.json",
        "fmp_budget_status": artifact_root / "fmp_budget_status.json",
    }
    loaded: dict[str, dict[str, Any] | None] = {
        name: _safe_load(path) for name, path in sources.items()
    }
    missing = [name for name, value in loaded.items() if value is None]

    overall_status = _classify_top_status(
        (loaded["risk_delta"] or {}).get("overall_status"),
        (loaded["daily_run_status"] or {}).get("overall_status"),
        (loaded["fmp_budget_status"] or {}).get("overall_status"),
    )

    return {
        "risk_delta":        loaded["risk_delta"],
        "retune_impact":     loaded["retune_impact"],
        "daily_run_status":  loaded["daily_run_status"],
        "fmp_budget_status": loaded["fmp_budget_status"],
        "overall_status":    overall_status,
        "missing_artifacts": missing,
    }
