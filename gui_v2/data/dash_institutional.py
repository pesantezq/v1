"""Institutional Intelligence — observe-only 13F research view.

Reads outputs/latest/institutional_intelligence*.json + institutional_consensus*.json
and renders overview / consensus / manager-detail / strategy-comparison cards.
Tolerant of absent / disabled / degraded artifacts — a missing file renders a
neutral "not yet produced" state and never crashes. Display-only: nothing here
feeds the decision engine or mutates production.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from gui_v2.data.shared import _read_json, card

# User-facing limitations banner — always shown so the delayed/incomplete nature
# of 13F is never mistaken for a live trade signal.
LIMITATIONS = [
    "Disclosures are delayed — 13F is filed weeks after quarter-end.",
    "Holdings are incomplete: long US 13(f) securities only; shorts are not visible.",
    "Options cannot be fully reconstructed and are never read as directional.",
    "A filing is evidence, not a live trade instruction.",
]


def collect_institutional_view(root: Path) -> dict[str, Any]:
    latest = root / "outputs" / "latest"
    status = _read_json(latest / "institutional_intelligence_status.json") or {}
    intel = _read_json(latest / "institutional_intelligence.json") or {}
    records = intel.get("records") or []

    overall = status.get("overall_status", "disabled")
    if not status and not records:
        return {
            "persona": "institutional", "observe_only": True,
            "feeds_decision_engine": False, "limitations": LIMITATIONS,
            "has_data": False,
            "cards": [card("Institutional Intelligence", status="unknown",
                           label="not produced",
                           summary="No institutional (13F) artifact yet. The layer "
                                   "ships inert until live SEC ingestion is enabled "
                                   "and manager CIKs are verified.")],
            "consensus_rows": [], "manager_rows": [],
        }

    _sev = {"ok": "ok", "degraded": "warning", "insufficient_data": "warning",
            "stale": "warning", "failed": "red", "disabled": "unknown"}.get(overall, "unknown")

    cards = [
        card("Source status", status=_sev, label=overall,
             summary=f"{status.get('symbols_covered', len(records))} symbols · "
                     f"{status.get('stale_symbols', 0)} stale · "
                     f"{status.get('unresolved_symbols', 0)} unresolved · "
                     f"live ingestion {'ready' if status.get('live_ingestion_ready') else 'off'}"),
    ]

    consensus_rows = []
    for r in sorted(records, key=lambda x: (x.get("consensus_confidence") or 0.0),
                    reverse=True):
        consensus_rows.append({
            "symbol": r.get("symbol"),
            "state": r.get("consensus_state"),
            "score": r.get("consensus_score"),
            "confidence": r.get("consensus_confidence"),
            "effective_independent": r.get("effective_independent_managers"),
            "crowding": r.get("crowding_score"),
            "filing_age_days": r.get("filing_age_days"),  # shown so delay is explicit
            "warnings": r.get("warnings") or [],
        })

    manager_rows = []
    for r in records:
        for sig in (r.get("manager_signals") or []):
            manager_rows.append({"symbol": r.get("symbol"), **sig})

    return {
        "persona": "institutional", "observe_only": True,
        "feeds_decision_engine": False, "limitations": LIMITATIONS,
        "has_data": True, "overall_status": overall, "cards": cards,
        "consensus_rows": consensus_rows, "manager_rows": manager_rows,
        "data_as_of": status.get("data_as_of") or intel.get("data_as_of"),
    }
