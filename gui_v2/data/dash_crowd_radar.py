"""Crowd Radar — observe-only sandbox research view.

Reads the Public Knowledge Velocity Layer artifacts under
``outputs/sandbox/discovery/`` and renders summary cards + per-state ticker
buckets. Tolerant of absent / disabled / degraded artifacts: a missing file
renders a neutral "not yet produced" state rather than crashing.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from gui_v2.data.shared import _read_json, card

# state value -> (display label, card status)
_STATE_SECTIONS: list[tuple[str, str, str]] = [
    ("emerging_dd", "Top Emerging DD", "info"),
    ("crowd_validation", "Crowd Validation Candidates", "ok"),
    ("hype_acceleration", "Hype Acceleration Warnings", "warning"),
    ("reflexive_squeeze_risk", "Reflexive Squeeze Risk", "red"),
    ("known_news_echo", "Known News Echoes", "info"),
    ("crowd_exhaustion", "Crowd Exhaustion", "warning"),
    ("contrarian_neglect", "Contrarian Neglect", "info"),
]

_STATUS_FROM_QUALITY = {
    "ok": "ok",
    "disabled": "unknown",
    "insufficient_data": "warning",
    "degraded": "warning",
}


def collect_crowd_radar_view(root: Path) -> dict[str, Any]:
    root = Path(root)
    disc = root / "outputs" / "sandbox" / "discovery"

    state_doc = _read_json(disc / "crowd_knowledge_state.json") or {}
    velocity_doc = _read_json(disc / "public_knowledge_velocity.json") or {}
    backtest_doc = _read_json(disc / "social_signal_backtest.json") or {}
    compliance_doc = _read_json(disc / "social_source_compliance.json") or {}
    # Multi-source no-extra-cost lane (Stage 9c1).
    health_doc = _read_json(disc / "crowd_source_health.json") or {}
    activation_doc = _read_json(disc / "crowd_radar_activation_check.json") or {}
    multi_doc = _read_json(disc / "crowd_multi_source_velocity.json") or {}

    records = state_doc.get("records") or []
    source_status = state_doc.get("source_status") or "unknown"
    data_quality = state_doc.get("data_quality_status") or "unknown"
    compliance_status = (
        "review_needed" if (compliance_doc.get("review_needed_count") or 0) > 0 else "ok"
    )
    if source_status == "disabled":
        compliance_status = "disabled"

    # Group records by state for the section buckets.
    by_state: dict[str, list[dict]] = {}
    for rec in records:
        by_state.setdefault(rec.get("crowd_state", "dormant_noise"), []).append(rec)

    # Summary cards.
    cards: list[dict] = []
    cards.append(card(
        "Crowd Radar status",
        status=_STATUS_FROM_QUALITY.get(data_quality, "unknown"),
        label=f"{source_status} · {data_quality}",
        summary=f"{len(records)} classified tickers from "
                f"{velocity_doc.get('post_count', 0)} posts.",
        source_artifacts=["crowd_knowledge_state.json", "public_knowledge_velocity.json"],
        updated_at=state_doc.get("created_at"),
    ))
    bt_matured = backtest_doc.get("states_matured") or []
    cards.append(card(
        "Backtest confidence",
        status="ok" if bt_matured else "warning",
        label=f"{len(bt_matured)} states matured" if bt_matured else "insufficient data",
        summary=f"{backtest_doc.get('total_observations', 0)} resolved observations; "
                f"min sample {backtest_doc.get('min_sample', '?')}.",
        source_artifacts=["social_signal_backtest.json"],
        updated_at=backtest_doc.get("created_at"),
    ))
    cards.append(card(
        "Source compliance",
        status="ok" if compliance_status == "ok" else "warning",
        label=compliance_status,
        summary=f"{compliance_doc.get('active_sources', 0)}/"
                f"{compliance_doc.get('total_sources', 0)} active sources governed.",
        source_artifacts=["social_source_compliance.json"],
        updated_at=compliance_doc.get("created_at"),
    ))

    warnings = list(state_doc.get("warnings") or [])

    # Build the per-state sections (only non-empty ones).
    sections: list[dict[str, Any]] = []
    for key, label, status in _STATE_SECTIONS:
        rows = sorted(
            by_state.get(key, []),
            key=lambda r: r.get("crowd_research_priority_score", 0),
            reverse=True,
        )
        if rows:
            sections.append({"key": key, "label": label, "status": status, "rows": rows[:8]})

    # --- Multi-source readiness: render source health BEFORE ticker states ---
    # (per spec: Source Health / Active / Probe-Only / Blocked by No-Extra-Cost).
    _STATUS_BADGE = {
        "ok": "ok", "disabled": "unknown", "no_credentials": "unknown",
        "not_configured": "unknown", "not_entitled": "warning",
        "requires_manual_review": "warning", "blocked_no_extra_cost": "unknown",
        "manual_reference_only": "unknown", "rate_limited": "warning",
        "budget_exhausted": "warning", "degraded": "warning", "error": "red",
    }
    source_health_rows = [
        {
            "source": r.get("source_name"),
            "status": r.get("status"),
            "badge": _STATUS_BADGE.get(r.get("status"), "unknown"),
            "warnings": r.get("warnings") or [],
        }
        for r in (health_doc.get("records") or [])
    ]
    multi_source = {
        "ready_to_collect": activation_doc.get("ready_to_collect"),
        "cost_policy": activation_doc.get("cost_policy"),
        "allow_paid_sources": activation_doc.get("allow_paid_sources"),
        "active_sources": activation_doc.get("active_sources") or [],
        "probe_only_sources": activation_doc.get("probe_only_sources") or [],
        "blocked_sources": activation_doc.get("blocked_sources") or [],
        "entitlement_warnings": [
            f"{r['source']}: {r['status']}" for r in source_health_rows
            if r["status"] in ("not_entitled", "requires_manual_review")
        ],
        "labels": multi_doc.get("labels") or [],
        "top_mention_velocity": (multi_doc.get("records") or [])[:10],
        "disclaimer": "Sandbox research intelligence only. Not a trade recommendation. "
                      "No paid data sources enabled.",
    }

    return {
        "persona": "crowd_radar",
        "observe_only": True,
        "cards": cards,
        "sections": sections,
        "source_status": source_status,
        "data_quality_status": data_quality,
        "compliance_status": compliance_status,
        "warnings": warnings,
        "has_data": bool(records),
        # Multi-source source-health (shown above ticker states in the template).
        "source_health_rows": source_health_rows,
        "multi_source": multi_source,
    }
