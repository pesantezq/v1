"""Portfolio stub — shallow read of portfolio_snapshot.json."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def collect_portfolio_stub(repo_root: Path) -> dict[str, Any]:
    path = Path(repo_root) / "outputs" / "portfolio" / "portfolio_snapshot.json"
    if not path.exists():
        return {
            "advisory_only": True,
            "no_trade": True,
            "available": False,
            "total_value": None,
            "cash_available": None,
            "generated_at": None,
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "advisory_only": True,
            "no_trade": True,
            "available": False,
            "error": f"parse_failed: {exc}",
        }
    return {
        "advisory_only": True,
        "no_trade": True,
        "available": True,
        "total_value": payload.get("total_value"),
        "cash_available": payload.get("cash_available"),
        "generated_at": payload.get("generated_at"),
    }
