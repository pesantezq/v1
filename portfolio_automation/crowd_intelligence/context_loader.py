"""Artifact-only crowd-context loader (NO FMP, NO HTTP, NO governor).

Reads the Phase-2A artifacts and returns per-symbol context + a status summary,
degrading safely when artifacts are missing, unreadable, or stale.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _read(path: Path) -> Any | None:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _age_hours(generated_at: str | None, now: datetime) -> float | None:
    if not generated_at:
        return None
    try:
        ts = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now - ts).total_seconds() / 3600.0
    except Exception:
        return None


def load_crowd_context(root: Path | str, *, max_age_hours: float = 30.0,
                       now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    latest = Path(root) / "outputs" / "latest"
    doc = _read(latest / "crowd_intelligence.json")
    status = _read(latest / "crowd_intelligence_status.json") or {}

    if doc is None:
        reason = "unreadable" if (latest / "crowd_intelligence.json").exists() else "not_generated"
        return {"available": False, "stale": False, "generated_at": None,
                "by_symbol": {}, "social_disabled": True,
                "disabled_categories": [], "missing_reason": reason}

    generated_at = doc.get("generated_at")
    age = _age_hours(generated_at, now)
    stale = age is not None and age > max_age_hours
    disabled_categories = status.get("disabled_categories") or []
    by_symbol = {}
    for s in doc.get("symbols") or []:
        sym = str(s.get("symbol") or "").upper()
        if sym:
            s = dict(s)
            s["present"] = True
            by_symbol[sym] = s
    return {
        "available": True, "stale": bool(stale), "generated_at": generated_at,
        "age_hours": age, "by_symbol": by_symbol,
        "social_disabled": ("social_sentiment" in disabled_categories) or True,
        "disabled_categories": disabled_categories, "missing_reason": None,
    }
