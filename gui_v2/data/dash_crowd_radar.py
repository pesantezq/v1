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

# Source status -> (plain-English reason, next-step action). Display-only mapping
# derived from the connector's reported status; invents no runtime behavior.
_SOURCE_REASON_ACTION: dict[str, tuple[str, str]] = {
    "ok": ("Active — mention velocity flowing", ""),
    "active": ("Active — mention velocity flowing", ""),
    "not_entitled": ("Plan does not include this endpoint", "Enable FMP entitlement"),
    "no_credentials": ("Missing API key / credentials", "Add API credentials"),
    "not_configured": ("Missing config or token", "Configure source"),
    "blocked_no_extra_cost": ("Disabled — would incur extra cost", "Out of scope (no paid sources)"),
    "manual_reference_only": ("Manual reference only", ""),
    "requires_manual_review": ("Terms-of-service review needed", "Review source ToS"),
    "rate_limited": ("Rate limited by source", "Retry later"),
    "budget_exhausted": ("Source budget exhausted", "Wait for budget reset"),
    "degraded": ("Degraded — partial data", "Check connector logs"),
    "disabled": ("Disabled in config", "Enable in config"),
    "error": ("Error contacting source", "Check connector logs"),
}

# data_quality_status -> (display label, hero-stat severity).
_QUALITY_DISPLAY: dict[str, tuple[str, str]] = {
    "ok": ("OK", "green"),
    "degraded": ("Degraded", "yellow"),
    "insufficient_data": ("Insufficient Data", "yellow"),
    "disabled": ("Disabled", "gray"),
    "unknown": ("Unavailable", "gray"),
}


# Flock Intelligence section ordering: (state value, display label, card status).
_FLOCK_SECTIONS: list[tuple[str, str, str]] = [
    ("flock_forming", "Forming Flocks", "info"),
    ("flock_confirmed", "Confirmed Flocks", "ok"),
    ("flock_exhaustion", "Exhaustion Risk", "warning"),
    ("flock_dispersing", "Dispersion Risk", "warning"),
    ("flock_broken", "Broken Flocks", "red"),
    ("insufficient_data", "Insufficient Data", "unknown"),
]


def _collect_flock(root: Path) -> dict[str, Any]:
    """Build the simulation-only Flock Intelligence view (observe-only).

    Reads outputs/simulation/flock_intelligence.json; degrades to an honest
    empty/not-produced state when the artifact is missing.
    """
    doc = _read_json(root / "outputs" / "simulation" / "flock_intelligence.json") or {}
    groups = doc.get("groups") or []
    by_state: dict[str, list[dict]] = {}
    for g in groups:
        by_state.setdefault(g.get("flock_state", "insufficient_data"), []).append(g)

    sections: list[dict[str, Any]] = []
    for key, label, status in _FLOCK_SECTIONS:
        rows = sorted(by_state.get(key, []),
                      key=lambda r: r.get("flock_score", 0), reverse=True)
        if rows:
            sections.append({"key": key, "label": label, "status": status,
                             "rows": [{
                                 "group": r.get("group"),
                                 "group_kind": r.get("group_kind"),
                                 "state": r.get("flock_state"),
                                 "flock_score": r.get("flock_score"),
                                 "dispersion_score": r.get("dispersion_score"),
                                 "breadth": r.get("crowd_breadth"),
                                 "velocity": r.get("crowd_velocity"),
                                 "concentration": r.get("mention_concentration"),
                                 "correlation": r.get("price_correlation_to_group"),
                                 "confidence": r.get("confidence"),
                                 "explanation": r.get("explanation"),
                             } for r in rows[:8]]})
    return {
        "has_data": bool(groups),
        "data_quality_status": doc.get("data_quality_status", "unknown"),
        "group_count": doc.get("group_count", 0),
        "ticker_count": doc.get("ticker_count", 0),
        "sections": sections,
        "generated_at": doc.get("generated_at"),
        "disclaimer": doc.get("disclaimer",
                              "Flock Intelligence is simulation-only research context; "
                              "never affects trades or allocation."),
    }


def _collect_unified_crowd(root: Path) -> dict[str, Any]:
    """Build the Unified Crowd Intelligence display view (read-only display of the
    simulation-active, production-gated unified bus).

    Joins the ApeWisdom retail-attention lane and the FMP market/context lane.
    Reads outputs/latest/unified_crowd_intelligence_status.json; degrades to an
    honest {has_data: False} on any error so the page never crashes.
    """
    try:
        doc = _read_json(
            root / "outputs" / "latest" / "unified_crowd_intelligence_status.json"
        ) or {}
        if not doc:
            return {"has_data": False}

        def _top(key: str) -> list[dict[str, Any]]:
            rows = doc.get(key) or []
            return [{
                "ticker": r.get("ticker"),
                "crowd_confidence": r.get("crowd_confidence"),
                "retail_attention_score": r.get("retail_attention_score"),
                "fmp_attention_score": r.get("fmp_attention_score"),
                "confirmation": r.get("cross_source_confirmation_score"),
                "divergence": r.get("cross_source_divergence_score"),
                "explanation": r.get("explanation"),
            } for r in rows[:10]]

        return {
            "has_data": True,
            "source": doc.get("source") or "unified_crowd_intelligence_status",
            "generated_at": doc.get("generated_at"),
            "total_tickers": doc.get("total_tickers", 0),
            "lane_a_tickers": doc.get("lane_a_tickers", 0),
            "lane_b_tickers": doc.get("lane_b_tickers", 0),
            "overlap_tickers": doc.get("overlap_tickers", 0),
            "source_breadth_max": doc.get("source_breadth_max", 0),
            "enabled_categories": doc.get("enabled_categories") or [],
            "disabled_categories": doc.get("disabled_categories") or [],
            "social_sentiment_status": doc.get("social_sentiment_status"),
            "crowd_confidence_avg": doc.get("crowd_confidence_avg"),
            "top_confirmed": _top("top_confirmed_attention"),
            "top_retail_only": _top("top_retail_only_attention"),
            "top_divergent": _top("top_divergent_attention"),
            "top_institutional": _top("top_institutional_context_only"),
        }
    except Exception:
        return {"has_data": False}


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
    source_health_rows = []
    for r in (health_doc.get("records") or []):
        st = r.get("status") or "unknown"
        reason, action = _SOURCE_REASON_ACTION.get(
            st, (st.replace("_", " ").title(), "Review"))
        source_health_rows.append({
            "source": r.get("source_name"),
            "status": st,
            "badge": _STATUS_BADGE.get(st, "unknown"),
            "reason": reason,
            "action": action,
            "warnings": r.get("warnings") or [],
        })
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

    # --- Summary status strip (derived, display-only) ---
    active_source_count = sum(1 for r in source_health_rows if r["status"] in ("ok", "active"))
    total_source_count = len(source_health_rows)
    if total_source_count == 0:
        # Fall back to the compliance artifact's counts when health rows are absent.
        active_source_count = compliance_doc.get("active_sources", 0) or 0
        total_source_count = compliance_doc.get("total_sources", 0) or 0
    if active_source_count >= 1:
        active_source_severity = "green"
    elif total_source_count > 0:
        active_source_severity = "red"
    else:
        active_source_severity = "gray"
    quality_label, quality_severity = _QUALITY_DISPLAY.get(data_quality, ("Unavailable", "gray"))

    # --- Mention velocity ranked rows (sorted by velocity desc; display-only) ---
    velocity_rows = []
    for i, r in enumerate(sorted(multi_source["top_mention_velocity"],
                                 key=lambda x: x.get("mention_velocity") or 0,
                                 reverse=True), start=1):
        velocity_rows.append({
            "rank": i,
            "ticker": r.get("ticker"),
            "velocity": r.get("mention_velocity") or 0,
            "breadth": r.get("source_breadth"),
            "hype": r.get("hype_risk_score") or 0,
            "confidence": r.get("confidence") or 0,
            "signal": " · ".join(r.get("labels") or []) or "—",
        })
    # Low-confidence banner: only one governed source can corroborate breadth.
    velocity_low_conf = bool(velocity_rows) and active_source_count <= 1

    # --- Advisory output: did the layer produce usable research today? ---
    advisory_produced = bool(records) and data_quality == "ok"
    why: list[str] = []
    next_steps: list[str] = []
    if not advisory_produced:
        ent = [r for r in source_health_rows if r["status"] == "not_entitled"]
        nocred = [r for r in source_health_rows if r["status"] == "no_credentials"]
        active_names = [r["source"] for r in source_health_rows if r["status"] in ("ok", "active")]
        if source_status == "disabled":
            why.append("Crowd Radar is disabled in config.")
        if ent:
            why.append(f"{', '.join(r['source'] for r in ent)} is not entitled on the current plan.")
        if active_names and len(active_names) == 1:
            why.append(f"Only {active_names[0]} is active (single governed source).")
        if data_quality == "insufficient_data":
            why.append("Confidence is below the advisory threshold.")
        if not records:
            why.append("No tickers crossed the mention-velocity threshold.")
        if not why:
            why.append("Insufficient governed source coverage.")
        if ent:
            next_steps.append("Enable FMP social-sentiment entitlement.")
        if nocred:
            next_steps.append(f"Add credentials for {', '.join(r['source'] for r in nocred)}.")
        next_steps.append("Run the discovery lane with the current sandbox source (ApeWisdom).")
    advisory = {"produced": advisory_produced, "why": why, "next_steps": next_steps}

    flock = _collect_flock(root)
    unified_crowd = _collect_unified_crowd(root)

    return {
        "persona": "crowd_radar",
        "flock": flock,
        "unified_crowd": unified_crowd,
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
        # --- Redesign (2026-06-15): derived display-only fields ---
        "active_source_count": active_source_count,
        "total_source_count": total_source_count,
        "active_source_severity": active_source_severity,
        "data_quality_label": quality_label,
        "data_quality_severity": quality_severity,
        "ticker_count": len(records),
        "state_updated_at": state_doc.get("created_at"),
        "velocity_rows": velocity_rows,
        "velocity_low_conf": velocity_low_conf,
        "advisory": advisory,
    }
