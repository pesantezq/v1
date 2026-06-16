"""GUI loader: per-advisory-pick Flock Context (simulation-only, artifact-only).

Reads outputs/simulation/flock_advisory_context.json (written by the Flock
Intelligence producer). No FMP / HTTP calls. Honest empty/missing states.
Observe-only: this never feeds the decision plan; production display of flock
context requires an approved promotion proposal.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from gui_v2.data.shared import _read_json

_SEVERITY = {
    "flock_forming": "blue", "flock_confirmed": "green",
    "flock_exhaustion": "yellow", "flock_dispersing": "yellow",
    "flock_broken": "red", "insufficient_data": "gray",
}


def flock_context_for(root: Path | str, symbols: list[str]) -> dict[str, Any]:
    """Return {status, by_symbol} flock context for the given advisory symbols."""
    root = Path(root)
    doc = _read_json(root / "outputs" / "simulation" / "flock_advisory_context.json") or {}
    available = bool(doc) and bool(doc.get("by_symbol"))
    src = doc.get("by_symbol") or {}
    by_symbol: dict[str, Any] = {}
    for sym in {str(s).upper() for s in (symbols or [])}:
        ctx = src.get(sym)
        if not available or ctx is None:
            by_symbol[sym] = {"present": False, "label": "Insufficient flock data",
                              "severity": "gray", "state": "insufficient_data",
                              "meaning": "No flock context available for this symbol yet."}
            continue
        state = ctx.get("flock_state", "insufficient_data")
        by_symbol[sym] = {
            "present": True,
            "label": ctx.get("label") or state.replace("_", " "),
            "severity": _SEVERITY.get(state, "gray"),
            "state": state,
            "group": ctx.get("group"),
            "flock_score": ctx.get("flock_score"),
            "dispersion_score": ctx.get("dispersion_score"),
            "confidence": ctx.get("confidence"),
            "meaning": ctx.get("meaning", ""),
        }
    return {
        "status": {
            "available": available,
            "generated_at": doc.get("generated_at"),
            "simulation_only": True,
            "banner": (None if available
                       else "Flock context unavailable — simulation artifact not generated yet."),
        },
        "by_symbol": by_symbol,
    }
