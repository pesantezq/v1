"""
Unified Crowd Intelligence — lane loaders + the consumer fallback reader.

Two responsibilities:

1. ``load_social_lane`` / ``load_fmp_lane`` — read each lane's on-disk artifact
   into the normalized inputs that :mod:`unified_bus` expects. Tolerant of
   missing / unreadable / empty / stale artifacts (never raises).

2. ``read_unified_crowd`` — the SINGLE entry point consumers use. Implements the
   documented fallback chain so a consumer always gets an honest answer:

       1. outputs/latest/unified_crowd_intelligence.json   (preferred)
       2. outputs/latest/crowd_intelligence.json           (FMP lane only)
       3. outputs/sandbox/discovery/crowd_multi_source_velocity.json (ApeWisdom only)
       4. honest empty state

   Existing direct readers keep working untouched; this is the additive
   "prefer unified" path.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portfolio_automation.crowd_intelligence.context_loader import load_crowd_context
from portfolio_automation.crowd_intelligence.unified_schema import STALE_AFTER_HOURS

_SOCIAL_REL = ("outputs", "sandbox", "discovery", "crowd_multi_source_velocity.json")
_FMP_STATUS_REL = ("outputs", "latest", "crowd_intelligence_status.json")
_UNIFIED_REL = ("outputs", "latest", "unified_crowd_intelligence.json")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def _age_hours(iso: str | None, *, now: datetime | None = None) -> float | None:
    if not iso:
        return None
    try:
        ts = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except Exception:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - ts).total_seconds() / 3600.0)


def load_social_lane(root: Path | str, *, now: datetime | None = None) -> dict[str, Any]:
    """Return {available, stale, records, generated_at, source_status}."""
    path = Path(root).joinpath(*_SOCIAL_REL)
    doc = _read_json(path)
    if not doc:
        return {"available": False, "stale": False, "records": [], "generated_at": None,
                "source_status": "missing"}
    gen = doc.get("created_at") or doc.get("run_id")
    age = _age_hours(gen, now=now)
    return {
        "available": True,
        "stale": (age is not None and age > STALE_AFTER_HOURS),
        "records": doc.get("records") or [],
        "generated_at": gen,
        "source_status": doc.get("source_status"),
    }


def load_fmp_lane(root: Path | str, *, now: datetime | None = None) -> dict[str, Any]:
    """Return {available, stale, by_symbol, enabled_categories, disabled_categories,
    overall_status, generated_at}. Reuses the existing Lane B context_loader."""
    ctx = load_crowd_context(root)
    status = _read_json(Path(root).joinpath(*_FMP_STATUS_REL)) or {}
    return {
        "available": bool(ctx.get("available")),
        "stale": bool(ctx.get("stale")),
        "by_symbol": ctx.get("by_symbol") or {},
        "enabled_categories": status.get("enabled_categories") or [],
        "disabled_categories": status.get("disabled_categories")
        or ctx.get("disabled_categories")
        or [],
        "overall_status": status.get("overall_status"),
        "generated_at": ctx.get("generated_at"),
    }


def read_unified_crowd(root: Path | str) -> dict[str, Any]:
    """Consumer entry point with the documented fallback chain.

    Always returns a dict with at least: {available, source, by_ticker, rows,
    generated_at, fallback_level}. ``by_ticker`` maps TICKER -> the unified row
    dict (or, in fallback modes, a best-effort partial row).
    """
    root = Path(root)

    # 1. Preferred: the unified artifact.
    unified = _read_json(root.joinpath(*_UNIFIED_REL))
    if unified and unified.get("records"):
        rows = unified.get("records") or []
        return {
            "available": True,
            "source": "unified",
            "fallback_level": 1,
            "generated_at": unified.get("generated_at"),
            "rows": rows,
            "by_ticker": {str(r.get("ticker", "")).upper(): r for r in rows if r.get("ticker")},
        }

    # 2. FMP lane only.
    fmp = load_fmp_lane(root)
    if fmp["available"] and fmp["by_symbol"]:
        by_ticker = {
            str(sym).upper(): {"ticker": str(sym).upper(), **sig}
            for sym, sig in fmp["by_symbol"].items()
        }
        return {
            "available": True,
            "source": "crowd_intelligence",
            "fallback_level": 2,
            "generated_at": fmp.get("generated_at"),
            "rows": list(by_ticker.values()),
            "by_ticker": by_ticker,
        }

    # 3. ApeWisdom social lane only.
    social = load_social_lane(root)
    if social["available"] and social["records"]:
        by_ticker = {
            str(r.get("ticker", "")).upper(): r
            for r in social["records"] if r.get("ticker")
        }
        return {
            "available": True,
            "source": "social_intelligence",
            "fallback_level": 3,
            "generated_at": social.get("generated_at"),
            "rows": social["records"],
            "by_ticker": by_ticker,
        }

    # 4. Honest empty state.
    return {
        "available": False,
        "source": "none",
        "fallback_level": 4,
        "generated_at": None,
        "rows": [],
        "by_ticker": {},
    }
