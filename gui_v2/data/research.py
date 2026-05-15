"""Research stub — shallow read of sandbox discovery counts."""
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
