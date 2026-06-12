"""Read-only dashboard loader for the tax/strategy panel. Never raises."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _read(base: Path, ns: str, name: str) -> dict[str, Any]:
    p = Path(base) / ns / name
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


def load_strategy_tax_context(base_dir: str | Path = "outputs") -> dict[str, Any]:
    base = Path(base_dir)
    scorecard = _read(base, "sandbox", "strategy_tax_scorecard.json")
    harvest = _read(base, "latest", "tax_harvest_advisor.json")
    strategy = _read(base, "sandbox", "strategy_comparison.json")
    lots = _read(base, "latest", "schwab_tax_lots.json")
    available = bool(scorecard or harvest or strategy)
    return {
        "available": available,
        "observe_only": True,
        "scorecard": scorecard,
        "harvest": harvest,
        "strategy": strategy,
        "lots": lots,
    }
