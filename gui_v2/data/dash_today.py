"""Today cockpit: answers system-healthy? decision-core OK? review needed? what changed? memo?"""
from __future__ import annotations
from pathlib import Path
from gui_v2.data.shared import card, _read_json


def collect_today_view(root: Path) -> dict:
    latest = Path(root) / "outputs/latest"
    drs = _read_json(latest / "daily_run_status.json") or {}
    dp = _read_json(latest / "decision_plan.json")
    risk = _read_json(latest / "risk_delta.json") or {}
    cards = []

    # System health card
    drs_status = (drs.get("overall_status") or "unknown")
    if drs_status == "ok":
        health_status = "ok"
    elif drs_status == "ok_with_warnings":
        health_status = "warning"
    elif drs_status in ("failed", "partial"):
        health_status = "red"
    else:
        health_status = "unknown"

    stage_ok = (drs.get("stage_summary") or {}).get("ok", "?")
    cards.append(card(
        "System health",
        status=health_status,
        label=drs_status,
        summary=f"{stage_ok} stages OK",
        source_artifacts=["daily_run_status.json"],
        updated_at=drs.get("generated_at"),
    ))

    # Decision core card — check is not None (an empty dict {} is still "present")
    dp_present = dp is not None
    cards.append(card(
        "Decision core",
        status="ok" if dp_present else "red",
        label="present" if dp_present else "MISSING",
        summary=(
            "decision_plan present"
            if dp_present
            else "decision_plan.json absent — decisions not trustworthy"
        ),
        source_artifacts=["decision_plan.json"],
    ))

    # Risk card
    risk_overall = risk.get("overall_status") or "unknown"
    cards.append(card(
        "Risk",
        status="ok" if risk_overall == "ok" else "warning",
        label=risk_overall,
        summary="see Portfolio view",
        source_artifacts=["risk_delta.json"],
    ))

    return {"cards": cards, "persona": "today"}
