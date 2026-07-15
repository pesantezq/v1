"""Read-only GUI reader for the one-shot operator approval packet."""
from __future__ import annotations

import json
from pathlib import Path

_EMPTY = {"available": False, "observe_only": True, "tier_sim": [],
          "tier_production": [], "counts": {"tier_sim_within_veto": 0,
                                            "tier_production_pending": 0},
          "approval_page_url": "/dashboard/governance"}


def load_packet_context(outputs_dir: str) -> dict:
    """Load the packet artifact for the governance page. Never raises."""
    path = Path(outputs_dir) / "promotion_review" / "operator_approval_packet.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(_EMPTY)
    return {
        "available": True,
        "observe_only": True,
        "tier_sim": data.get("tier_sim", []) or [],
        "tier_production": data.get("tier_production", []) or [],
        "counts": data.get("counts", {}) or _EMPTY["counts"],
        "approval_page_url": data.get("approval_page_url", "/dashboard/governance"),
        "generated_at": data.get("generated_at"),
    }
