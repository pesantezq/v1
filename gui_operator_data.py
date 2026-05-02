from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from watchlist_scanner.daily_memo import (
    _build_memo_top_insight,
    _capital_action_summary,
    _change_items,
    _health_items,
    _risk_focus_items,
    _top_decision_rows,
)


CORE_ARTIFACTS = {
    "run_summary": ("outputs", "latest", "scraped_intel_run_summary.json"),
    "agent_bundle": ("outputs", "latest", "agent_bundle.json"),
    "agent_llm_metadata": ("outputs", "latest", "agent_llm_metadata.json"),
    "theme_engine_llm_metadata": ("outputs", "latest", "theme_engine_llm_metadata.json"),
    "watchlist_signals": ("outputs", "latest", "watchlist_signals.json"),
    "portfolio_snapshot": ("outputs", "portfolio", "portfolio_snapshot.json"),
    "policy_recommendation": ("outputs", "policy", "policy_recommendation.json"),
    "recommendation_evaluation": ("outputs", "policy", "recommendation_evaluation.json"),
    "recommendation_outcomes": ("outputs", "policy", "recommendation_outcomes.json"),
    "regime_performance": ("outputs", "regime", "regime_performance.json"),
}

DECISION_PLAN_RELATIVE_PATH = ("outputs", "latest", "decision_plan.json")
SYSTEM_DECISION_SUMMARY_RELATIVE_PATH = ("outputs", "latest", "system_decision_summary.json")
DECISION_EXPLANATIONS_RELATIVE_PATH = ("outputs", "latest", "decision_explanations.json")
DECISION_PERFORMANCE_ATTRIBUTION_RELATIVE_PATH = (
    "outputs", "policy", "decision_performance_attribution.json"
)
AI_DECISION_VALIDATION_RELATIVE_PATH = ("outputs", "latest", "ai_decision_validation.json")
DECISION_OUTCOME_SUMMARY_RELATIVE_PATH = ("outputs", "policy", "decision_outcome_summary.json")
DECISION_TRIAGE_RELATIVE_PATH = ("outputs", "latest", "decision_triage.json")
CONFIDENCE_CALIBRATION_RELATIVE_PATH = ("outputs", "policy", "confidence_calibration.json")
CONFIDENCE_CALIBRATION_LATEST_RELATIVE_PATH = ("outputs", "latest", "confidence_calibration.json")
DATA_QUALITY_RELATIVE_PATH = ("outputs", "latest", "data_quality_report.json")
AI_BUDGET_RELATIVE_PATH = ("outputs", "latest", "ai_budget_summary.json")
DISCOVERY_EMERGING_RELATIVE_PATH = ("outputs", "sandbox", "discovery", "emerging_candidates.json")
DISCOVERY_REJECTED_RELATIVE_PATH = ("outputs", "sandbox", "discovery", "rejected_candidates.json")
DISCOVERY_MEMORY_RELATIVE_PATH = ("outputs", "sandbox", "discovery", "discovery_memory.json")
DISCOVERY_MEMO_RELATIVE_PATH = ("outputs", "sandbox", "discovery", "discovery_memo_section.md")
DISCOVERY_APPROVAL_DECISIONS_RELATIVE_PATH = ("outputs", "sandbox", "discovery", "approval_decisions.jsonl")
DISCOVERY_APPROVAL_SUMMARY_RELATIVE_PATH = ("outputs", "sandbox", "discovery", "approval_summary.json")

ARTIFACT_META = {
    "run_summary": {
        "label": "Run Summary",
        "scope": "Latest",
        "timestamp_keys": ("timestamp", "generated_at"),
    },
    "agent_bundle": {
        "label": "Agent Bundle",
        "scope": "Latest",
        "timestamp_keys": ("generated_at",),
    },
    "agent_llm_metadata": {
        "label": "Agent LLM Metadata",
        "scope": "Latest",
        "timestamp_keys": ("completed_at", "generated_at", "started_at"),
    },
    "theme_engine_llm_metadata": {
        "label": "Theme Engine Metadata",
        "scope": "Latest",
        "timestamp_keys": ("completed_at", "generated_at", "started_at"),
    },
    "watchlist_signals": {
        "label": "Signals",
        "scope": "Latest",
        "timestamp_keys": ("generated_at", "run_date"),
    },
    "portfolio_snapshot": {
        "label": "Portfolio Snapshot",
        "scope": "Portfolio",
        "timestamp_keys": ("generated_at", "timestamp"),
    },
    "policy_recommendation": {
        "label": "Policy Recommendation",
        "scope": "Policy",
        "timestamp_keys": ("generated_at", "timestamp"),
    },
    "recommendation_evaluation": {
        "label": "Recommendation Evaluation",
        "scope": "Policy",
        "timestamp_keys": ("generated_at", "timestamp"),
    },
    "recommendation_outcomes": {
        "label": "Recommendation Outcomes",
        "scope": "Policy",
        "timestamp_keys": ("generated_at", "timestamp"),
    },
    "regime_performance": {
        "label": "Regime Performance",
        "scope": "Regime",
        "timestamp_keys": ("generated_at", "timestamp"),
    },
}

KEY_FRESHNESS_ARTIFACTS = [
    "run_summary",
    "watchlist_signals",
    "portfolio_snapshot",
    "policy_recommendation",
]

MEMO_CANDIDATES = [
    ("outputs", "latest", "monthly_memo.md"),
    ("outputs", "latest", "decision_memo.md"),
    ("decision_memo.md",),
    ("monthly_memo.md",),
]
WEEKLY_REPORT_RELATIVE_PATH = ("outputs", "reports", "weekly_summary.md")

MEMO_SECTION_TARGETS = [
    ("signals", "Signals", ("signal", "signals", "watchlist", "triage")),
    ("portfolio", "Portfolio", ("portfolio", "allocation", "construction", "position")),
    ("regime", "Regime", ("regime", "market regime", "market backdrop")),
    ("recommendation", "Recommendation", ("recommendation", "policy", "profile")),
]


def _safe_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_text(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _safe_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _safe_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _non_empty_rows(rows: list[dict[str, Any]], key: str = "count") -> list[dict[str, Any]]:
    output = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if key not in row or row.get(key) not in (None, 0, "", []):
            output.append(row)
    return output


def _iso_to_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _format_timestamp(value: Any) -> str:
    dt = _iso_to_dt(value)
    if dt is None:
        return "Unknown"
    return dt.strftime("%Y-%m-%d %H:%M")


def _relative_age_from_dt(value: datetime | None, now: datetime) -> str:
    if value is None:
        return "Unknown"
    age_seconds = max(0, int((now - value).total_seconds()))
    if age_seconds < 60:
        return "just now"
    if age_seconds < 3600:
        return f"{age_seconds // 60}m ago"
    if age_seconds < 86400:
        return f"{age_seconds // 3600}h ago"
    return f"{age_seconds // 86400}d ago"


def _relative_age(path: Path, now: datetime) -> str:
    if not path.exists():
        return "missing"
    return _relative_age_from_dt(datetime.fromtimestamp(path.stat().st_mtime), now)


def classify_freshness(updated_at: datetime | None, now: datetime | None = None) -> str:
    if updated_at is None:
        return "missing"
    now = now or datetime.now()
    age_seconds = max(0, int((now - updated_at).total_seconds()))
    if age_seconds < 6 * 3600:
        return "fresh"
    if age_seconds < 24 * 3600:
        return "stale"
    return "old"


def _extract_timestamp(payload: dict[str, Any], timestamp_keys: tuple[str, ...]) -> datetime | None:
    for key in timestamp_keys:
        value = payload.get(key)
        parsed = _iso_to_dt(value)
        if parsed is not None:
            return parsed
    return None


def _build_output_target(name: str, path: Path, root: Path) -> dict[str, Any]:
    meta = ARTIFACT_META.get(name, {})
    relative_path = str(path.relative_to(root))
    return {
        "label": meta.get("label", name.replace("_", " ").title()),
        "scope": meta.get("scope"),
        "file_name": path.name,
        "path": str(path),
        "relative_path": relative_path,
    }


def _artifact_status(
    *,
    root: Path,
    name: str,
    rel_parts: tuple[str, ...],
    payload: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    path = root.joinpath(*rel_parts)
    exists = path.exists()
    meta = ARTIFACT_META.get(name, {})
    artifact_dt = _extract_timestamp(payload, meta.get("timestamp_keys", ("generated_at", "timestamp")))
    file_dt = datetime.fromtimestamp(path.stat().st_mtime) if exists else None
    updated_dt = artifact_dt or file_dt
    updated_source = "artifact" if artifact_dt is not None else ("file" if file_dt is not None else "missing")
    return {
        "name": name,
        "label": meta.get("label", name.replace("_", " ").title()),
        "scope": meta.get("scope", "Latest"),
        "path": str(path.relative_to(root)),
        "exists": exists,
        "updated_at": updated_dt.isoformat() if updated_dt else None,
        "updated_display": _format_timestamp(updated_dt),
        "updated_source": updated_source,
        "age_label": _relative_age_from_dt(updated_dt, now) if updated_dt else "missing",
        "freshness_status": classify_freshness(updated_dt, now),
        "size_bytes": path.stat().st_size if exists else 0,
        "output_target": _build_output_target(name, path, root),
    }


def _build_missing_memo_section(key: str, title: str) -> dict[str, Any]:
    return {
        "key": key,
        "title": title,
        "found": False,
        "heading": None,
        "content": f"_No {title.lower()} section found in this memo._",
    }


def _sample_warning(count: Any, threshold: int = 5) -> str | None:
    numeric = int(_coerce_float(count, 0))
    if numeric <= 0:
        return "No attributed records"
    if numeric < threshold:
        return f"Small sample ({numeric})"
    return None


def _split_markdown_sections(markdown: str) -> list[dict[str, Any]]:
    text = (markdown or "").strip()
    if not text:
        return []

    lines = text.splitlines()
    sections: list[dict[str, Any]] = []
    current_title = "Overview"
    current_heading = None
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_lines, current_title, current_heading
        content = "\n".join(current_lines).strip()
        if content:
            sections.append(
                {
                    "title": current_title,
                    "heading": current_heading,
                    "content": content,
                }
            )
        current_lines = []

    for line in lines:
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            flush()
            current_heading = line
            current_title = match.group(2).strip()
            current_lines = [line]
        else:
            current_lines.append(line)
    flush()
    return sections


def _memo_sections(markdown: str) -> list[dict[str, Any]]:
    sections = _split_markdown_sections(markdown)
    if not sections and markdown.strip():
        sections = [{"title": "Overview", "heading": None, "content": markdown.strip()}]

    matched: list[dict[str, Any]] = []
    used_indexes: set[int] = set()
    for key, title, terms in MEMO_SECTION_TARGETS:
        found = None
        for index, section in enumerate(sections):
            if index in used_indexes:
                continue
            haystack = f"{section.get('title', '')}\n{section.get('content', '')}".lower()
            if any(term in haystack for term in terms):
                found = {
                    "key": key,
                    "title": title,
                    "found": True,
                    "heading": section.get("heading"),
                    "content": section.get("content", ""),
                }
                used_indexes.add(index)
                break
        matched.append(found or _build_missing_memo_section(key, title))

    return matched


def _pick_latest_memo(root: Path, now: datetime) -> dict[str, Any]:
    candidates = []
    for rel_parts in MEMO_CANDIDATES:
        path = root.joinpath(*rel_parts)
        if path.exists():
            candidates.append(path)
    if not candidates:
        return {
            "available": False,
            "path": None,
            "title": "No memo available",
            "updated_at": None,
            "age_label": "missing",
            "simple_markdown": "_No memo artifact found._",
            "full_markdown": "",
            "section_index": [],
            "sections": [
                _build_missing_memo_section(key, title)
                for key, title, _terms in MEMO_SECTION_TARGETS
            ],
        }

    memo_path = max(candidates, key=lambda item: item.stat().st_mtime)
    full_text = _safe_text(memo_path).strip()
    sections = _memo_sections(full_text)
    simple_lines = []
    for line in full_text.splitlines():
        if line.strip():
            simple_lines.append(line)
        if len(simple_lines) >= 18:
            break
    simple_markdown = "\n".join(simple_lines).strip()
    if simple_markdown and simple_markdown != full_text.strip():
        simple_markdown += "\n\n_Excerpt shown. Switch to Full view for the complete memo._"
    return {
        "available": True,
        "path": str(memo_path.relative_to(root)),
        "title": memo_path.name,
        "updated_at": datetime.fromtimestamp(memo_path.stat().st_mtime).isoformat(),
        "age_label": _relative_age(memo_path, now),
        "simple_markdown": simple_markdown or "_Memo file is empty._",
        "full_markdown": full_text,
        "section_index": [
            {
                "key": section["key"],
                "title": section["title"],
                "found": section["found"],
            }
            for section in sections
        ],
        "sections": sections,
    }


def _load_weekly_report(root: Path, now: datetime) -> dict[str, Any]:
    path = root.joinpath(*WEEKLY_REPORT_RELATIVE_PATH)
    markdown = _safe_text(path)
    exists = path.exists()
    updated_dt = datetime.fromtimestamp(path.stat().st_mtime) if exists else None
    return {
        "available": exists,
        "path": str(path.relative_to(root)),
        "markdown": markdown,
        "updated_at": updated_dt.isoformat() if updated_dt else None,
        "updated_display": _format_timestamp(updated_dt),
        "age_label": _relative_age_from_dt(updated_dt, now) if updated_dt else "missing",
        "output_target": {
            "label": "Weekly Summary",
            "scope": "Reports",
            "file_name": "weekly_summary.md",
            "path": str(path),
            "relative_path": str(path.relative_to(root)),
        },
    }


def _flatten_llm_tasks(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = _safe_list(metadata.get("tasks"))
    llm_metadata = _safe_dict(metadata.get("llm_metadata"))
    if tasks:
        return [task for task in tasks if isinstance(task, dict)]
    if llm_metadata:
        return [llm_metadata]
    if metadata:
        return [metadata]
    return []


def _latest_provider_snapshot(
    run_summary: dict[str, Any],
    agent_metadata: dict[str, Any],
    theme_metadata: dict[str, Any],
) -> dict[str, Any]:
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for source_name, payload in (
        ("agent", agent_metadata),
        ("theme_engine", theme_metadata),
    ):
        for task in _flatten_llm_tasks(payload):
            completed_at = _iso_to_dt(task.get("completed_at") or task.get("generated_at"))
            if completed_at is None:
                continue
            snapshot = {
                "source": source_name,
                "provider": task.get("resolved_provider") or task.get("provider") or "Unknown",
                "actual_provider": task.get("actual_provider") or task.get("resolved_provider") or "Unknown",
                "model": task.get("actual_model") or task.get("model") or "Unknown",
                "fallback_triggered": _coerce_bool(task.get("fallback_triggered") or task.get("llm_fallback_triggered")),
                "llm_fallback_triggered": _coerce_bool(task.get("llm_fallback_triggered") or task.get("fallback_triggered")),
                "data_fallback_triggered": _coerce_bool(task.get("data_fallback_triggered")),
                "degraded_mode": _coerce_bool(task.get("degraded_mode")),
                "degraded_reason": task.get("degraded_reason"),
                "completed_at": completed_at.isoformat(),
            }
            candidates.append((completed_at, snapshot))

    if not candidates:
        scanner = _safe_dict(run_summary.get("scanner"))
        return {
            "source": "system",
            "provider": "Unknown",
            "actual_provider": "Unknown",
            "model": "Unknown",
            "fallback_triggered": False,
            "llm_fallback_triggered": False,
            "data_fallback_triggered": _coerce_bool(
                scanner.get("data_fallback_triggered") or run_summary.get("data_fallback_triggered")
            ),
            "degraded_mode": _coerce_bool(run_summary.get("degraded_mode")),
            "degraded_reason": run_summary.get("degraded_reason"),
            "completed_at": run_summary.get("timestamp"),
        }

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _normalize_overview(
    *,
    now: datetime,
    run_summary: dict[str, Any],
    watchlist: dict[str, Any],
    portfolio_snapshot: dict[str, Any],
    policy_recommendation: dict[str, Any],
    provider_snapshot: dict[str, Any],
    health_warnings: list[str],
    artifact_statuses: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    current_context = _safe_dict(policy_recommendation.get("current_context"))
    recommendation = _safe_dict(policy_recommendation.get("recommendation"))
    market_regime = _safe_dict(run_summary.get("market_regime") or watchlist.get("market_regime"))
    portfolio_view = portfolio_snapshot or _safe_dict(watchlist.get("portfolio_construction"))

    run_ts = run_summary.get("timestamp") or run_summary.get("generated_at")
    last_updated_dt = _iso_to_dt(run_ts)
    if last_updated_dt is None:
        existing_updates = [
            _iso_to_dt(status.get("updated_at"))
            for status in artifact_statuses.values()
            if status.get("exists")
        ]
        last_updated_dt = max([dt for dt in existing_updates if dt is not None], default=None)

    warning_pool = []
    warning_pool.extend(health_warnings)
    warning_pool.extend(_safe_list(portfolio_view.get("warnings")))
    quality_note = recommendation.get("recommendation_quality_note")
    if quality_note:
        warning_pool.append(str(quality_note))

    deduped_warnings = []
    seen = set()
    for warning in warning_pool:
        normalized = str(warning).strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            deduped_warnings.append(normalized)

    return {
        "latest_run_status": _safe_dict(run_summary.get("scan_summary")).get("scan_status", "available" if run_summary else "missing"),
        "last_updated": _format_timestamp(last_updated_dt),
        "last_updated_age": _relative_age_from_dt(last_updated_dt, now),
        "run_mode": run_summary.get("run_mode") or run_summary.get("mode") or "Unknown",
        "data_mode": run_summary.get("data_mode") or watchlist.get("data_mode") or provider_snapshot.get("data_mode") or "Unknown",
        "degraded_mode": _coerce_bool(run_summary.get("degraded_mode") or current_context.get("degraded_mode")),
        "degraded_reason": run_summary.get("degraded_reason") or current_context.get("degraded_reason"),
        "market_regime": market_regime.get("regime_label") or current_context.get("regime_label") or "Unknown",
        "market_regime_confidence": market_regime.get("regime_confidence") or current_context.get("regime_confidence"),
        "policy": recommendation.get("recommended_policy") or "Unavailable",
        "profile": recommendation.get("recommended_profile") or "Unavailable",
        "recommendation_confidence": recommendation.get("recommendation_confidence"),
        "provider_source": provider_snapshot.get("source") or "Unknown",
        "top_warnings": deduped_warnings[:5],
        "freshness_strip": [
            artifact_statuses[name]
            for name in KEY_FRESHNESS_ARTIFACTS
            if name in artifact_statuses
        ],
        "status_badges": {
            "degraded_mode": _coerce_bool(run_summary.get("degraded_mode") or current_context.get("degraded_mode")),
            "fallback_triggered": _coerce_bool(
                _safe_dict(run_summary.get("scanner")).get("data_fallback_triggered")
                or provider_snapshot.get("llm_fallback_triggered")
                or provider_snapshot.get("fallback_triggered")
            ),
            "low_recommendation_confidence": _coerce_float(recommendation.get("recommendation_confidence"), 1.0) < 0.4,
        },
    }


def _normalize_run_status(
    *,
    now: datetime,
    run_summary: dict[str, Any],
    provider_snapshot: dict[str, Any],
    artifact_statuses: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    scanner = _safe_dict(run_summary.get("scanner"))
    missing_artifacts = [
        status["path"] for status in artifact_statuses.values() if not status.get("exists")
    ]
    freshness_rows = []
    for name, status in artifact_statuses.items():
        freshness_rows.append(
            {
                "artifact": name,
                "path": status["path"],
                "available": status["exists"],
                "updated_at": status["updated_display"],
                "updated_source": status["updated_source"],
                "age": status["age_label"],
                "freshness_status": status["freshness_status"],
            }
        )
    freshness_rows.sort(key=lambda row: (not row["available"], row["artifact"]))

    last_success_dt = _iso_to_dt(run_summary.get("timestamp") or run_summary.get("generated_at"))
    return {
        "last_successful_run": _format_timestamp(last_success_dt),
        "last_successful_run_age": _relative_age_from_dt(last_success_dt, now),
        "latest_run_mode": run_summary.get("run_mode") or run_summary.get("mode") or "Unknown",
        "provider_used": provider_snapshot.get("provider") or "Unknown",
        "actual_provider": provider_snapshot.get("actual_provider") or "Unknown",
        "model": provider_snapshot.get("model") or "Unknown",
        "provider_source": provider_snapshot.get("source") or "Unknown",
        "data_mode": run_summary.get("data_mode") or "Unknown",
        "data_sources_used": _safe_list(run_summary.get("data_sources_used")),
        "watchlist_source": scanner.get("watchlist_source") or "Unknown",
        "data_fallback_triggered": _coerce_bool(
            scanner.get("data_fallback_triggered") or provider_snapshot.get("data_fallback_triggered")
        ),
        "llm_fallback_triggered": _coerce_bool(provider_snapshot.get("llm_fallback_triggered")),
        "fallback_occurred": provider_snapshot.get("provider") != provider_snapshot.get("actual_provider"),
        "degraded_mode": _coerce_bool(run_summary.get("degraded_mode") or provider_snapshot.get("degraded_mode")),
        "degraded_reason": run_summary.get("degraded_reason") or provider_snapshot.get("degraded_reason"),
        "artifact_freshness": freshness_rows,
        "missing_artifact_warnings": missing_artifacts,
        "key_artifact_freshness": [
            artifact_statuses[name]
            for name in KEY_FRESHNESS_ARTIFACTS
            if name in artifact_statuses
        ],
    }


def _normalize_signal_triage(watchlist: dict[str, Any]) -> dict[str, Any]:
    results = _safe_list(watchlist.get("results") or watchlist.get("signals"))
    portfolio_rows = _safe_list(_safe_dict(watchlist.get("portfolio_construction")).get("rows"))
    triage_rows = []

    if results:
        for row in results:
            if not isinstance(row, dict):
                continue
            sector = row.get("portfolio_sector")
            if not sector:
                sector = _safe_dict(row.get("fundamentals")).get("sector")
            triage_rows.append(
                {
                    "ticker": row.get("ticker") or row.get("symbol") or "Unknown",
                    "conviction_band": row.get("conviction_band") or row.get("sizing_recommendation") or "unknown",
                    "conviction_score": row.get("conviction_score"),
                    "effective_score": row.get("effective_score"),
                    "normalized_allocation": row.get("normalized_allocation"),
                    "sector": sector or row.get("sector") or "Unknown",
                    "cooldown_active": _coerce_bool(row.get("cooldown_active")),
                    "degraded_impact": _coerce_float(row.get("degraded_confidence_penalty")),
                    "signal_reliability": row.get("signal_reliability") or row.get("data_quality") or "Unknown",
                    "actionable_signal": _coerce_bool(row.get("actionable_signal")),
                    # Theme alignment fields (present only when theme_discovery has run)
                    "theme_alignment_label": row.get("theme_alignment_label") or "none",
                    "theme_top_name": row.get("theme_top_name"),
                    "theme_match_count": int(row.get("theme_match_count") or 0),
                    "augmented_signal_score": row.get("augmented_signal_score"),
                    "theme_reason": row.get("theme_reason") or "",
                    # Portfolio fit fields (present only when portfolio snapshot exists)
                    "portfolio_fit_label": row.get("portfolio_fit_label") or "neutral",
                    "portfolio_fit_score": row.get("portfolio_fit_score"),
                    "portfolio_fit_reason": row.get("portfolio_fit_reason") or "",
                    "final_rank_score": row.get("final_rank_score"),
                    "raw": row,
                }
            )
    else:
        for row in portfolio_rows:
            if not isinstance(row, dict):
                continue
            triage_rows.append(
                {
                    "ticker": row.get("ticker") or row.get("symbol") or "Unknown",
                    "conviction_band": row.get("conviction_band") or row.get("sizing_recommendation") or "unknown",
                    "conviction_score": row.get("conviction_score"),
                    "effective_score": row.get("effective_score"),
                    "normalized_allocation": row.get("normalized_allocation"),
                    "sector": row.get("sector") or "Unknown",
                    "cooldown_active": False,
                    "degraded_impact": 0.0,
                    "signal_reliability": row.get("data_mode") or "Unknown",
                    "actionable_signal": row.get("normalized_allocation", 0) not in (0, None, ""),
                    "theme_alignment_label": "none",
                    "theme_top_name": None,
                    "theme_match_count": 0,
                    "augmented_signal_score": None,
                    "theme_reason": "",
                    "portfolio_fit_label": "neutral",
                    "portfolio_fit_score": None,
                    "portfolio_fit_reason": "",
                    "final_rank_score": None,
                    "raw": row,
                }
            )

    triage_rows.sort(
        key=lambda item: (
            _coerce_float(item.get("conviction_score"), -1.0),
            _coerce_float(item.get("effective_score"), -1.0),
        ),
        reverse=True,
    )

    band_counts: dict[str, int] = {}
    for row in triage_rows:
        band = str(row.get("conviction_band") or "unknown")
        band_counts[band] = band_counts.get(band, 0) + 1

    return {
        "available": bool(triage_rows),
        "rows": triage_rows,
        "counts_by_band": band_counts,
        "summary_line": _safe_dict(watchlist.get("scan_summary")).get("conviction_summary_line")
        or _safe_dict(watchlist.get("conviction")).get("summary_line")
        or "",
        "output_target": {
            "label": "Signals",
            "scope": "Latest",
            "file_name": "watchlist_signals.json",
            "path": "outputs/latest/watchlist_signals.json",
            "relative_path": "outputs/latest/watchlist_signals.json",
        },
    }


def _normalize_portfolio_view(
    watchlist: dict[str, Any],
    portfolio_snapshot: dict[str, Any],
) -> dict[str, Any]:
    snapshot = portfolio_snapshot or _safe_dict(watchlist.get("portfolio_construction"))
    if not snapshot:
        return {
            "available": False,
            "summary_line": "Portfolio construction artifact is missing.",
            "total_suggested_allocation": None,
            "total_normalized_allocation": None,
            "allocation_by_sector": {},
            "warnings": [],
            "capped_positions": 0,
            "portfolio_fit_vs_regime": "Unknown",
            "regime_commentary": "",
            "rows": [],
            "output_target": {
                "label": "Portfolio Snapshot",
                "scope": "Portfolio",
                "file_name": "portfolio_snapshot.json",
                "path": "outputs/portfolio/portfolio_snapshot.json",
                "relative_path": "outputs/portfolio/portfolio_snapshot.json",
            },
        }

    regime_payload = _safe_dict(snapshot.get("market_regime") or watchlist.get("market_regime"))
    return {
        "available": True,
        "summary_line": snapshot.get("summary_line") or "Portfolio construction snapshot available.",
        "total_suggested_allocation": snapshot.get("total_suggested_allocation"),
        "total_normalized_allocation": snapshot.get("total_normalized_allocation"),
        "allocation_by_sector": _safe_dict(snapshot.get("allocation_by_sector")),
        "warnings": _safe_list(snapshot.get("warnings")),
        "capped_positions": snapshot.get("capped_positions", 0),
        "portfolio_fit_vs_regime": regime_payload.get("regime_portfolio_fit")
        or snapshot.get("summary_label")
        or "Unknown",
        "regime_commentary": regime_payload.get("regime_portfolio_commentary") or "",
        "top_sector": _safe_dict(snapshot.get("top_sector")),
        "rows": _safe_list(snapshot.get("rows")),
        "degraded_mode_impact": _safe_dict(snapshot.get("degraded_mode_impact")),
        "groupings": _safe_dict(snapshot.get("groupings")),
        "output_target": {
            "label": "Portfolio Snapshot",
            "scope": "Portfolio",
            "file_name": "portfolio_snapshot.json",
            "path": "outputs/portfolio/portfolio_snapshot.json",
            "relative_path": "outputs/portfolio/portfolio_snapshot.json",
        },
    }


def _normalize_strategy_view(
    policy_recommendation: dict[str, Any],
    recommendation_evaluation: dict[str, Any],
    recommendation_outcomes: dict[str, Any],
) -> dict[str, Any]:
    recommendation = _safe_dict(policy_recommendation.get("recommendation"))
    if not recommendation:
        return {
            "available": False,
            "recommended_policy": "Unavailable",
            "recommended_profile": "Unavailable",
            "confidence": None,
            "source": "missing_artifact",
            "reasoning": ["Policy recommendation artifact is missing."],
            "alternatives": {},
            "data_quality": "missing",
            "evaluation": recommendation_evaluation,
            "outcomes": recommendation_outcomes,
            "why": {},
            "output_target": {
                "label": "Policy Recommendation",
                "scope": "Policy",
                "file_name": "policy_recommendation.json",
                "path": "outputs/policy/policy_recommendation.json",
                "relative_path": "outputs/policy/policy_recommendation.json",
            },
        }

    alternatives = _safe_dict(policy_recommendation.get("alternatives"))
    return {
        "available": True,
        "recommended_policy": recommendation.get("recommended_policy") or "Unavailable",
        "recommended_profile": recommendation.get("recommended_profile") or "Unavailable",
        "confidence": recommendation.get("recommendation_confidence"),
        "score": recommendation.get("recommendation_score"),
        "source": recommendation.get("recommendation_source") or "Unknown",
        "reasoning": _safe_list(recommendation.get("recommendation_reasoning")),
        "alternatives": alternatives,
        "data_quality": recommendation.get("recommendation_data_quality") or "Unknown",
        "quality_note": recommendation.get("recommendation_quality_note"),
        "evaluation": recommendation_evaluation,
        "outcomes": recommendation_outcomes,
        "why": {
            "inputs": _safe_dict(recommendation.get("recommendation_inputs")),
            "source": recommendation.get("recommendation_source"),
            "quality_note": recommendation.get("recommendation_quality_note"),
        },
        "output_target": {
            "label": "Policy Recommendation",
            "scope": "Policy",
            "file_name": "policy_recommendation.json",
            "path": "outputs/policy/policy_recommendation.json",
            "relative_path": "outputs/policy/policy_recommendation.json",
        },
    }


def _normalize_performance_view(recommendation_outcomes: dict[str, Any]) -> dict[str, Any]:
    overall = _safe_dict(recommendation_outcomes.get("overall"))
    coverage = _safe_dict(recommendation_outcomes.get("coverage_by_horizon"))
    by_tier = _safe_dict(recommendation_outcomes.get("by_confidence_tier"))
    calibration = _safe_dict(recommendation_outcomes.get("confidence_calibration"))

    calibration_rows = []
    for tier in ("low", "medium", "high"):
        bucket = _safe_dict(by_tier.get(tier))
        if not bucket:
            continue
        calibration_rows.append(
            {
                "bucket": tier,
                "count": int(_coerce_float(bucket.get("count"), 0)),
                "attributable_count": int(_coerce_float(bucket.get("attributable_count"), 0)),
                "hit_rate": bucket.get("hit_rate"),
                "avg_return_5d": bucket.get("avg_forward_return_5d"),
                "median_return_5d": bucket.get("median_forward_return_5d"),
                "strong_win_rate": bucket.get("strong_win_rate"),
                "adverse_rate": bucket.get("adverse_rate"),
                "small_sample": _coerce_bool(bucket.get("small_sample")),
                "sample_warning": _sample_warning(bucket.get("attributable_count")),
            }
        )

    coverage_rows = [
        {
            "horizon": "1d",
            "count": int(_coerce_float(coverage.get("count_1d"), 0)),
        },
        {
            "horizon": "3d",
            "count": int(_coerce_float(coverage.get("count_3d"), 0)),
        },
        {
            "horizon": "5d",
            "count": int(_coerce_float(coverage.get("count_5d"), 0)),
        },
        {
            "horizon": "10d",
            "count": int(_coerce_float(coverage.get("count_10d"), 0)),
        },
    ]

    notes = []
    notes.extend(_safe_list(recommendation_outcomes.get("data_quality_notes")))
    notes.extend(_safe_list(calibration.get("notes")))
    deduped_notes = []
    seen = set()
    for note in notes:
        normalized = str(note).strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            deduped_notes.append(normalized)

    available = bool(calibration_rows or overall or any(row["count"] > 0 for row in coverage_rows))
    return {
        "available": available,
        "calibration_rows": calibration_rows,
        "return_distribution": {
            "avg_return_1d": overall.get("avg_forward_return_1d"),
            "avg_return_3d": overall.get("avg_forward_return_3d"),
            "avg_return_5d": overall.get("avg_forward_return_5d"),
            "avg_return_10d": overall.get("avg_forward_return_10d"),
            "median_return_5d": overall.get("median_forward_return_5d"),
            "strong_win_rate": overall.get("strong_win_rate"),
            "adverse_rate": overall.get("adverse_rate"),
            "hit_rate": overall.get("hit_rate"),
        },
        "coverage_rows": coverage_rows,
        "sample_size": int(_coerce_float(_safe_dict(recommendation_outcomes.get("coverage")).get("attributable_records"), 0)),
        "sample_quality": recommendation_outcomes.get("sample_quality") or "unknown",
        "notes": deduped_notes,
        "output_target": {
            "label": "Recommendation Outcomes",
            "scope": "Policy",
            "file_name": "recommendation_outcomes.json",
            "path": "outputs/policy/recommendation_outcomes.json",
            "relative_path": "outputs/policy/recommendation_outcomes.json",
        },
    }


def _normalize_regime_analytics(regime_performance: dict[str, Any]) -> dict[str, Any]:
    by_regime = _safe_dict(regime_performance.get("by_regime"))
    rows = []
    for regime, payload in sorted(by_regime.items()):
        bucket = _safe_dict(payload)
        rows.append(
            {
                "regime": regime,
                "total_signals": int(_coerce_float(bucket.get("total_signals"), 0)),
                "resolved_signals": int(_coerce_float(bucket.get("resolved_signals"), 0)),
                "win_rate": bucket.get("win_rate"),
                "avg_return_pct": bucket.get("avg_return_pct"),
                "best_conviction_band": bucket.get("best_conviction_band") or "n/a",
                "worst_conviction_band": bucket.get("worst_conviction_band") or "n/a",
                "degraded_note": bucket.get("degraded_data_impact_note") or "",
            }
        )

    observability = _safe_dict(regime_performance.get("observability"))
    notes = []
    for row in rows:
        if row["degraded_note"]:
            notes.append(f"{row['regime']}: {row['degraded_note']}")
    if not rows and observability:
        notes.append("Regime observability artifact exists, but no resolved regime buckets are available yet.")

    return {
        "available": bool(rows),
        "rows": rows,
        "resolved_signals": int(_coerce_float(regime_performance.get("resolved_signals"), 0)),
        "primary_window_days": regime_performance.get("primary_window_days"),
        "notes": notes,
        "output_target": {
            "label": "Regime Performance",
            "scope": "Regime",
            "file_name": "regime_performance.json",
            "path": "outputs/regime/regime_performance.json",
            "relative_path": "outputs/regime/regime_performance.json",
        },
    }


def _monotonicity_label(monotonicity: dict[str, Any]) -> str:
    overall = monotonicity.get("overall")
    if overall is True:
        return "monotonic"
    hit_checks = _safe_list(monotonicity.get("hit_rate_checks"))
    avg_checks = _safe_list(monotonicity.get("avg_return_5d_checks"))
    combined = hit_checks + avg_checks
    if not combined:
        return "unavailable"
    if all(check.get("monotonic") is False for check in combined):
        return "inverted"
    return "mixed"


def _bucket_rows_from_mapping(
    buckets: dict[str, Any],
    *,
    order: list[str] | None = None,
) -> list[dict[str, Any]]:
    rows = []
    keys = order or list(buckets.keys())
    for key in keys:
        bucket = _safe_dict(buckets.get(key))
        if not bucket:
            continue
        rows.append(
            {
                "bucket": key,
                "count": int(_coerce_float(bucket.get("count"), bucket.get("total"))),
                "attributable_count": int(_coerce_float(bucket.get("attributable_count"), bucket.get("resolved"))),
                "hit_rate": bucket.get("hit_rate"),
                "avg_return_5d": bucket.get("avg_forward_return_5d"),
                "median_return_5d": bucket.get("median_forward_return_5d"),
                "strong_win_rate": bucket.get("strong_win_rate"),
                "adverse_rate": bucket.get("adverse_rate"),
                "small_sample": _coerce_bool(bucket.get("small_sample")),
                "sample_warning": _sample_warning(bucket.get("attributable_count", bucket.get("resolved"))),
            }
        )
    return _non_empty_rows(rows, key="count")


def _normalize_recommendation_quality(
    recommendation_outcomes: dict[str, Any],
    recommendation_evaluation: dict[str, Any],
) -> dict[str, Any]:
    outcomes_monotonicity = _safe_dict(
        _safe_dict(recommendation_outcomes.get("confidence_calibration")).get("monotonicity")
    )
    evaluation_calibration = _safe_dict(recommendation_evaluation.get("confidence_calibration"))
    deciles = []
    for bucket in _safe_list(recommendation_outcomes.get("by_score_decile")):
        if not isinstance(bucket, dict):
            continue
        deciles.append(
            {
                "bucket": bucket.get("label") or "unknown",
                "count": int(_coerce_float(bucket.get("count"), 0)),
                "attributable_count": int(_coerce_float(bucket.get("attributable_count"), 0)),
                "hit_rate": bucket.get("hit_rate"),
                "avg_return_5d": bucket.get("avg_forward_return_5d"),
                "median_return_5d": bucket.get("median_forward_return_5d"),
                "small_sample": _coerce_bool(bucket.get("small_sample")),
                "sample_warning": _sample_warning(bucket.get("attributable_count")),
            }
        )

    available = bool(
        recommendation_outcomes
        or recommendation_evaluation
        or deciles
    )
    return {
        "available": available,
        "by_degraded_mode": _bucket_rows_from_mapping(
            _safe_dict(recommendation_outcomes.get("by_degraded_mode")),
            order=["normal", "degraded"],
        ),
        "by_action_level": _bucket_rows_from_mapping(
            _safe_dict(recommendation_outcomes.get("by_action_level")),
            order=["Action Required", "Recommended", "Monitor", "FYI", "unknown"],
        ),
        "by_impact_area": _bucket_rows_from_mapping(
            _safe_dict(recommendation_outcomes.get("by_impact_area")),
        ),
        "by_score_decile": _non_empty_rows(deciles, key="count"),
        "monotonicity_label": _monotonicity_label(outcomes_monotonicity),
        "monotonicity_checks": outcomes_monotonicity,
        "notes": _safe_list(_safe_dict(recommendation_outcomes.get("confidence_calibration")).get("notes")),
        "evaluation_fallback": {
            "hit_rate_by_mode": _safe_dict(recommendation_evaluation.get("hit_rate_by_mode")),
            "confidence_calibration": evaluation_calibration,
        },
        "output_targets": {
            "outcomes": {
                "label": "Recommendation Outcomes",
                "scope": "Policy",
                "file_name": "recommendation_outcomes.json",
                "path": "outputs/policy/recommendation_outcomes.json",
                "relative_path": "outputs/policy/recommendation_outcomes.json",
            },
            "evaluation": {
                "label": "Recommendation Evaluation",
                "scope": "Policy",
                "file_name": "recommendation_evaluation.json",
                "path": "outputs/policy/recommendation_evaluation.json",
                "relative_path": "outputs/policy/recommendation_evaluation.json",
            },
        },
    }


def _normalize_health(
    artifact_statuses: dict[str, dict[str, Any]],
    run_summary: dict[str, Any],
    provider_snapshot: dict[str, Any],
    watchlist: dict[str, Any],
) -> dict[str, Any]:
    scanner = _safe_dict(run_summary.get("scanner"))
    warnings = []
    if _coerce_bool(run_summary.get("degraded_mode")):
        warnings.append(
            f"Degraded mode active: {run_summary.get('degraded_reason') or 'reason unavailable'}."
        )
    if _coerce_bool(scanner.get("data_fallback_triggered")):
        warnings.append("Data fallback triggered in the latest scanner run.")
    if _coerce_bool(provider_snapshot.get("llm_fallback_triggered")):
        warnings.append("LLM fallback triggered in the latest provider task.")
    for name, status in artifact_statuses.items():
        if not status.get("exists"):
            warnings.append(f"Missing optional artifact: {status['path']}.")
    warnings.extend(_safe_list(_safe_dict(watchlist.get("portfolio_construction")).get("warnings")))

    deduped = []
    seen = set()
    for warning in warnings:
        if warning not in seen:
            seen.add(warning)
            deduped.append(warning)

    return {
        "degraded_mode": _coerce_bool(run_summary.get("degraded_mode")),
        "degraded_reason": run_summary.get("degraded_reason"),
        "fallback_usage": {
            "data_mode": run_summary.get("data_mode") or watchlist.get("data_mode") or "Unknown",
            "data_sources_used": _safe_list(run_summary.get("data_sources_used")),
            "data_fallback_triggered": _coerce_bool(
                scanner.get("data_fallback_triggered") or provider_snapshot.get("data_fallback_triggered")
            ),
            "llm_fallback_triggered": _coerce_bool(provider_snapshot.get("llm_fallback_triggered")),
        },
        "artifact_availability": artifact_statuses,
        "warnings": deduped,
    }


_STRUCTURAL_PREFIX_RE = re.compile(r"^STRUCTURAL:\s*", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)%")
_SUFFIX_COUNT_RE = re.compile(r"\s*\.\.\.\(\+\d+[^)]*\)")


def _compact_decision_reason(row: dict[str, Any], max_len: int = 80) -> str:
    """
    Return a compact, single-sentence reason for the Decision Center compact summary.

    This is presentation-only compression. It must never mutate the source
    decision row or recompute the decision itself.
    """
    def _normalize(text: str) -> str:
        return _WHITESPACE_RE.sub(" ", text).strip()

    def _first_segment(text: str) -> str:
        return _normalize(text.split("|")[0])

    def _first_sentence(text: str) -> str:
        match = re.match(r"^(.+?[.!?])(?:\s|$)", text)
        return match.group(1).strip() if match else text.strip()

    def _strip_prefixes(text: str) -> str:
        cleaned = _STRUCTURAL_PREFIX_RE.sub("", text).strip()
        cleaned = re.sub(r"^(structural|portfolio|market|finance|watchlist)\s*:\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = _SUFFIX_COUNT_RE.sub("", cleaned).strip()
        return cleaned.strip()

    def _format_pct(value: Any) -> str | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if 0 <= number <= 1:
            number *= 100.0
        rendered = f"{number:.1f}".rstrip("0").rstrip(".")
        return f"{rendered}%"

    def _extract_pct_pair(text: str, inputs_used: dict[str, Any]) -> tuple[str, str] | None:
        current = (
            _format_pct(row.get("current_pct"))
            or _format_pct(inputs_used.get("current_pct"))
            or _format_pct(inputs_used.get("current"))
        )
        cap = (
            _format_pct(row.get("cap_pct"))
            or _format_pct(inputs_used.get("cap_pct"))
            or _format_pct(inputs_used.get("cap"))
        )
        if current and cap:
            return current, cap

        percents = _PERCENT_RE.findall(text)
        if len(percents) >= 2:
            return f"{percents[0]}%", f"{percents[1]}%"
        return None

    def _cap_sentence(text: str) -> str:
        text = _normalize(text)
        if not text:
            return "No rationale provided."
        text = text.rstrip(" ,;:")
        if not re.search(r"[.!?]$", text):
            text += "."
        if len(text) <= max_len:
            return text

        body = text.rstrip(".!?")
        words = body.split()
        kept: list[str] = []
        for word in words:
            candidate_body = " ".join([*kept, word])
            candidate = candidate_body + "."
            if len(candidate) > max_len:
                break
            kept.append(word)
        if kept:
            return " ".join(kept).rstrip(" ,;:") + "."
        return text[: max_len - 1].rstrip(" ,;:.") + "."

    inputs_used = row.get("inputs_used") if isinstance(row.get("inputs_used"), dict) else {}
    risk_flags = {str(flag).lower() for flag in (row.get("risk_flags") or [])}
    source = str(row.get("source") or "").lower()
    short = str(row.get("short_reason") or "").strip()
    reason = str(row.get("reason") or "").strip()
    source_text = short or reason
    if not source_text:
        return "No rationale provided."

    segment = _strip_prefixes(_first_segment(source_text))
    sentence = _strip_prefixes(_first_sentence(segment))
    lowered = sentence.lower()
    violation_type = str(inputs_used.get("violation_type") or "").lower()

    is_structural = source == "structural"
    if is_structural and (
        violation_type == "leverage"
        or "leverage_breach" in risk_flags
        or "leverage" in lowered
    ):
        pct_pair = _extract_pct_pair(reason or segment, inputs_used)
        if pct_pair:
            return _cap_sentence(f"Leverage exceeds cap ({pct_pair[0]} vs {pct_pair[1]})")
        return "Leverage exceeds cap."

    if is_structural and (
        violation_type == "concentration"
        or "concentration_breach" in risk_flags
        or "concentration" in lowered
    ):
        pct_pair = _extract_pct_pair(reason or segment, inputs_used)
        if pct_pair:
            return _cap_sentence(f"Concentration exceeds cap ({pct_pair[0]} vs {pct_pair[1]})")
        return "Concentration exceeds cap."

    if any(token in lowered for token in ("rebalance", "drift", "underweight", "overweight")):
        return "Drift exceeds rebalance threshold."

    if "relative strength" in lowered or re.search(r"\brs\b", lowered):
        return "Relative strength near highs."

    if any(token in lowered for token in ("momentum", "breakout", "near highs", "market signal")):
        return "Momentum breakout near highs."

    deduped = re.sub(r"\b(\w+)(?:\s+\1\b)+", r"\1", sentence, flags=re.IGNORECASE)
    return _cap_sentence(deduped)


_AI_BADGE_MAP: dict[str, str] = {
    "boost": "↑ boost",
    "neutral": "• neutral",
    "caution": "⚠ caution",
}


def _ai_validation_badge(label: str) -> str:
    return _AI_BADGE_MAP.get(str(label).lower().strip(), "• neutral")


def load_decision_explanations(root: Path | str) -> dict[str, Any]:
    path = Path(root).joinpath(*DECISION_EXPLANATIONS_RELATIVE_PATH)
    if not path.exists():
        return {"available": False, "explanations": [], "summary_line": "No AI explanations available."}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {"available": False, "explanations": [], "summary_line": "AI explanation file could not be read."}
    if not isinstance(payload, dict):
        return {"available": False, "explanations": [], "summary_line": "AI explanation file is malformed."}
    if not payload.get("available"):
        return {
            "available": False,
            "explanations": [],
            "summary_line": payload.get("summary_line") or "No AI explanations available.",
        }
    return payload


def load_ai_decision_validation(root: Path | str) -> dict[str, Any]:
    path = Path(root).joinpath(*AI_DECISION_VALIDATION_RELATIVE_PATH)
    if not path.exists():
        return {
            "available": False,
            "validations": [],
            "summary_line": "AI validation artifact not available yet.",
            "total_validated": 0,
            "aligned_count": 0,
            "caution_count": 0,
            "contradiction_count": 0,
            "insufficient_context_count": 0,
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {
            "available": False,
            "validations": [],
            "summary_line": "AI validation file could not be read.",
            "total_validated": 0,
            "aligned_count": 0,
            "caution_count": 0,
            "contradiction_count": 0,
            "insufficient_context_count": 0,
        }
    if not isinstance(payload, dict):
        return {
            "available": False,
            "validations": [],
            "summary_line": "AI validation file is malformed.",
            "total_validated": 0,
            "aligned_count": 0,
            "caution_count": 0,
            "contradiction_count": 0,
            "insufficient_context_count": 0,
        }
    if not payload.get("available"):
        return {
            "available": False,
            "validations": [],
            "summary_line": payload.get("summary_line") or "AI validation artifact not available yet.",
            "total_validated": 0,
            "aligned_count": 0,
            "caution_count": 0,
            "contradiction_count": 0,
            "insufficient_context_count": 0,
        }
    return payload


def load_decision_outcome_summary(root: Path | str) -> dict[str, Any]:
    _empty: dict[str, Any] = {
        "available": False,
        "total_decisions": 0,
        "resolved": 0,
        "unresolved": 0,
        "hit_rate": None,
        "avg_return_pct": None,
        "by_decision": {},
        "by_validation_status": {},
        "last_10_resolved": [],
        "summary_line": "No decision performance data yet.",
    }
    path = Path(root).joinpath(*DECISION_OUTCOME_SUMMARY_RELATIVE_PATH)
    if not path.exists():
        return _empty
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {**_empty, "summary_line": "Decision performance file could not be read."}
    if not isinstance(payload, dict):
        return {**_empty, "summary_line": "Decision performance file is malformed."}
    payload.setdefault("available", True)
    payload.setdefault("summary_line", f"{payload.get('resolved', 0)} resolved decisions.")
    return payload


def load_decision_triage(root: Path | str) -> dict[str, Any]:
    _empty_buckets = {
        "critical_action": [],
        "action_candidate": [],
        "monitor": [],
        "ignore_for_now": [],
    }
    _empty_counts = {k: 0 for k in _empty_buckets}
    _empty: dict[str, Any] = {
        "available": False,
        "total_decisions": 0,
        "bucket_counts": _empty_counts,
        "top_actions": [],
        "buckets": _empty_buckets,
        "summary_line": "Decision triage artifact not available yet.",
    }
    path = Path(root).joinpath(*DECISION_TRIAGE_RELATIVE_PATH)
    if not path.exists():
        return _empty
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {**_empty, "summary_line": "Decision triage file could not be read."}
    if not isinstance(payload, dict):
        return {**_empty, "summary_line": "Decision triage file is malformed."}
    payload.setdefault("available", True)
    payload.setdefault(
        "summary_line",
        f"{payload.get('total_decisions', 0)} decisions triaged.",
    )
    return payload


def load_confidence_calibration(root: Path | str) -> dict[str, Any]:
    _empty: dict[str, Any] = {
        "available": False,
        "insufficient_data": True,
        "total_resolved": 0,
        "overall_hit_rate": None,
        "overall_avg_return": None,
        "confidence_buckets": {},
        "validation_analysis": {},
        "decision_analysis": {},
        "insights": [],
        "summary_line": "Confidence calibration artifact not available yet.",
    }
    path = Path(root).joinpath(*CONFIDENCE_CALIBRATION_RELATIVE_PATH)
    if not path.exists():
        return _empty
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {**_empty, "summary_line": "Confidence calibration file could not be read."}
    if not isinstance(payload, dict):
        return {**_empty, "summary_line": "Confidence calibration file is malformed."}
    payload.setdefault("available", True)
    payload.setdefault(
        "summary_line",
        f"{payload.get('total_resolved', 0)} resolved decisions analyzed.",
    )
    return payload


def _get_insight_cards(data: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or not data.get("available"):
        return []
    return (data.get("explanations") or [])[:5]


def load_decision_performance_attribution(root: Path | str) -> dict[str, Any]:
    path = Path(root).joinpath(*DECISION_PERFORMANCE_ATTRIBUTION_RELATIVE_PATH)
    if not path.exists():
        return {
            "available": False,
            "insufficient_data": True,
            "summary_line": "No performance attribution data available.",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {
            "available": False,
            "insufficient_data": True,
            "summary_line": "Performance attribution file could not be read.",
        }
    if not isinstance(payload, dict):
        return {
            "available": False,
            "insufficient_data": True,
            "summary_line": "Performance attribution file is malformed.",
        }
    return payload


def _normalize_decision_brief(
    *,
    decision_plan: dict[str, Any],
    system_summary: dict[str, Any],
) -> dict[str, Any]:
    summary = dict(system_summary or {})
    if decision_plan:
        summary["_decision_plan"] = decision_plan

    top_rows = _top_decision_rows(summary, limit=5)
    capital_counts, capital_total = _capital_action_summary(top_rows)
    risk_items = _risk_focus_items(top_rows)[:3]
    change_items = _change_items(_safe_dict(summary.get("changes")))[:3]
    health_items = _health_items(_safe_dict(summary.get("data_health")))[:3]

    formatted_rows = []
    for row in top_rows:
        formatted_rows.append(
            {
                "decision": str(row.get("decision") or "-"),
                "symbol": str(row.get("symbol") or "-"),
                "priority": _coerce_float(row.get("priority")),
                "source": str(row.get("source") or "-"),
                "urgency": str(row.get("urgency") or "-"),
                "reason": str(row.get("reason") or "").strip() or "No decision rationale provided.",
                "compact_reason": _compact_decision_reason(row),
                "risk_flags": [str(flag) for flag in _safe_list(row.get("risk_flags")) if str(flag).strip()],
                "raw": row,
            }
        )

    return {
        "available": bool(decision_plan),
        "path": "/".join(DECISION_PLAN_RELATIVE_PATH),
        "top_insight": _build_memo_top_insight(
            _safe_dict(summary.get("top_theme")),
            _safe_dict(summary.get("top_opportunity")),
            top_rows,
        ),
        "top_decisions": formatted_rows,
        "capital_actions": {
            "sell": capital_counts.get("SELL", 0),
            "scale": capital_counts.get("SCALE", 0),
            "buy": capital_counts.get("BUY", 0),
            "total_recommended_capital": capital_total,
        },
        "risk_focus": risk_items,
        "what_changed": change_items,
        "system_data_health": health_items,
        "summary_line": "Decision plan unavailable." if not decision_plan else "",
        "full_decisions": _safe_list(decision_plan.get("decisions")) if decision_plan else [],
        "observe_only": _coerce_bool(decision_plan.get("observe_only")) if decision_plan else True,
        "output_target": {
            "label": "Decision Plan",
            "scope": "Latest",
            "file_name": "decision_plan.json",
            "path": "outputs/latest/decision_plan.json",
            "relative_path": "outputs/latest/decision_plan.json",
        },
    }


def load_data_quality_report(root: Path | str) -> dict[str, Any]:
    _empty: dict[str, Any] = {
        "available": False,
        "observe_only": True,
        "total_symbols": 0,
        "healthy_symbols": 0,
        "warning_symbols": 0,
        "critical_symbols": 0,
        "missing_price_count": 0,
        "stale_price_count": 0,
        "fallback_count": 0,
        "issues": [],
        "summary_line": "Data quality report not available yet.",
    }
    path = Path(root).joinpath(*DATA_QUALITY_RELATIVE_PATH)
    if not path.exists():
        return _empty
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {**_empty, "summary_line": "Data quality report could not be read."}
    if not isinstance(payload, dict):
        return {**_empty, "summary_line": "Data quality report is malformed."}
    payload.setdefault("available", True)
    payload.setdefault("summary_line", f"{payload.get('total_symbols', 0)} symbols evaluated.")
    return payload


def load_ai_budget_summary(root: Path | str) -> dict[str, Any]:
    _empty: dict[str, Any] = {
        "available": False,
        "observe_only": True,
        "enabled": False,
        "daily_token_total": 0,
        "daily_cost_total_usd": 0.0,
        "monthly_cost_total_usd": 0.0,
        "daily_cost_limit_usd": None,
        "monthly_cost_limit_usd": None,
        "warning": False,
        "blocked": False,
        "warnings": [],
        "event_count": 0,
        "summary_line": "AI budget summary not available yet.",
    }
    path = Path(root).joinpath(*AI_BUDGET_RELATIVE_PATH)
    if not path.exists():
        return _empty
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {**_empty, "summary_line": "AI budget summary could not be read."}
    if not isinstance(payload, dict):
        return {**_empty, "summary_line": "AI budget summary is malformed."}
    payload.setdefault("available", True)
    payload.setdefault("summary_line", f"{payload.get('event_count', 0)} AI calls tracked today.")
    return payload


def load_confidence_calibration_latest(root: Path | str) -> dict[str, Any]:
    _empty: dict[str, Any] = {
        "available": False,
        "observe_only": True,
        "insufficient_data": True,
        "total_resolved": 0,
        "overall_hit_rate": None,
        "overall_avg_return": None,
        "buckets_5": [],
        "signal_results": [],
        "dq_warnings": [],
        "summary_line": "Confidence calibration (latest) not available yet.",
    }
    path = Path(root).joinpath(*CONFIDENCE_CALIBRATION_LATEST_RELATIVE_PATH)
    if not path.exists():
        return _empty
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {**_empty, "summary_line": "Confidence calibration file could not be read."}
    if not isinstance(payload, dict):
        return {**_empty, "summary_line": "Confidence calibration file is malformed."}
    payload.setdefault("available", True)
    payload.setdefault(
        "summary_line",
        f"{payload.get('total_resolved', 0)} resolved decisions analyzed.",
    )
    return payload


def load_discovery_approval_decisions(root: Path | str) -> list[dict[str, Any]]:
    """
    Load sandbox discovery approval decisions from approval_decisions.jsonl.

    Malformed lines are silently skipped.
    Returns [] when the file is absent or unreadable.
    Read-only — never writes or modifies any artifact.
    """
    path = Path(root).joinpath(*DISCOVERY_APPROVAL_DECISIONS_RELATIVE_PATH)
    if not path.exists():
        return []
    decisions: list[dict[str, Any]] = []
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    decisions.append(obj)
            except json.JSONDecodeError:
                pass
    except Exception:
        pass
    return decisions


def load_discovery_approval_summary(root: Path | str) -> dict[str, Any]:
    """
    Build an in-memory approval summary from the JSONL decisions file.

    Always includes governance flags (observe_only, sandbox_only, no_trade,
    no_official_promotion) regardless of artifact state.
    Returns a safe empty summary when no decisions exist.
    Read-only — never writes or modifies any artifact.
    """
    _base = {
        "total_decisions": 0,
        "unique_symbols_reviewed": 0,
        "decision_counts": {},
        "latest_per_symbol": {},
        "observe_only": True,
        "sandbox_only": True,
        "no_trade": True,
        "no_official_promotion": True,
        "disclaimer": (
            "Approval decisions are sandbox research notes only. "
            "They do not update the official watchlist, portfolio, or recommendations."
        ),
    }
    decisions = load_discovery_approval_decisions(root)
    if not decisions:
        return _base

    counts: dict[str, int] = {}
    by_symbol: dict[str, dict[str, Any]] = {}
    for d in decisions:
        val = d.get("decision", "unknown")
        counts[val] = counts.get(val, 0) + 1
        sym = d.get("symbol", "?")
        by_symbol[sym] = d  # last decision per symbol wins

    return {
        **_base,
        "total_decisions": len(decisions),
        "unique_symbols_reviewed": len(by_symbol),
        "decision_counts": counts,
        "latest_per_symbol": by_symbol,
    }


def load_discovery_sandbox_status(root: Path | str) -> dict[str, Any]:
    root_path = Path(root)
    emerging = _safe_json(root_path.joinpath(*DISCOVERY_EMERGING_RELATIVE_PATH))
    rejected = _safe_json(root_path.joinpath(*DISCOVERY_REJECTED_RELATIVE_PATH))
    memory = _safe_json(root_path.joinpath(*DISCOVERY_MEMORY_RELATIVE_PATH))
    memo_path = root_path.joinpath(*DISCOVERY_MEMO_RELATIVE_PATH)
    memo_md = _safe_text(memo_path)

    available = bool(emerging or rejected or memory)
    candidates = _safe_list(emerging.get("candidates"))
    watch_candidates = [c for c in candidates if isinstance(c, dict) and c.get("status") == "watch"]
    discovered_candidates = [c for c in candidates if isinstance(c, dict) and c.get("status") == "discovered"]
    rejected_list = _safe_list(rejected.get("rejected_candidates"))

    approval_decisions = load_discovery_approval_decisions(root_path)
    approval_summary = load_discovery_approval_summary(root_path)

    return {
        "available": available,
        "discovery_only": True,
        "sandbox_only": True,
        "observe_only": True,
        "can_execute_trades": False,
        "official_watchlist_modified": False,
        "disclaimer": emerging.get("disclaimer") or "Discovery candidates are not buy/sell recommendations.",
        "run_id": emerging.get("run_id"),
        "generated_at": emerging.get("generated_at"),
        "watch_count": len(watch_candidates),
        "discovered_count": len(discovered_candidates),
        "total_candidates": len(candidates),
        "total_rejected": len(rejected_list),
        "watch_candidates": watch_candidates,
        "discovered_candidates": discovered_candidates,
        "rejected_candidates": rejected_list,
        "memory_entry_count": memory.get("entry_count", 0),
        "memo_md": memo_md,
        "approval_decisions": approval_decisions,
        "approval_summary": approval_summary,
        "artifacts": {
            "emerging_candidates": str(root_path.joinpath(*DISCOVERY_EMERGING_RELATIVE_PATH)),
            "rejected_candidates": str(root_path.joinpath(*DISCOVERY_REJECTED_RELATIVE_PATH)),
            "discovery_memory": str(root_path.joinpath(*DISCOVERY_MEMORY_RELATIVE_PATH)),
            "discovery_memo_section": str(memo_path),
            "approval_decisions": str(root_path.joinpath(*DISCOVERY_APPROVAL_DECISIONS_RELATIVE_PATH)),
        },
    }


def load_operator_dashboard_data(root: Path | str) -> dict[str, Any]:
    root_path = Path(root)
    now = datetime.now()

    run_summary = _safe_json(root_path.joinpath(*CORE_ARTIFACTS["run_summary"]))
    system_decision_summary = _safe_json(root_path.joinpath(*SYSTEM_DECISION_SUMMARY_RELATIVE_PATH))
    decision_plan = _safe_json(root_path.joinpath(*DECISION_PLAN_RELATIVE_PATH))
    agent_bundle = _safe_json(root_path.joinpath(*CORE_ARTIFACTS["agent_bundle"]))
    agent_llm_metadata = _safe_json(root_path.joinpath(*CORE_ARTIFACTS["agent_llm_metadata"]))
    theme_llm_metadata = _safe_json(root_path.joinpath(*CORE_ARTIFACTS["theme_engine_llm_metadata"]))
    watchlist = _safe_json(root_path.joinpath(*CORE_ARTIFACTS["watchlist_signals"]))
    portfolio_snapshot = _safe_json(root_path.joinpath(*CORE_ARTIFACTS["portfolio_snapshot"]))
    policy_recommendation = _safe_json(root_path.joinpath(*CORE_ARTIFACTS["policy_recommendation"]))
    recommendation_evaluation = _safe_json(root_path.joinpath(*CORE_ARTIFACTS["recommendation_evaluation"]))
    recommendation_outcomes = _safe_json(root_path.joinpath(*CORE_ARTIFACTS["recommendation_outcomes"]))
    regime_performance = _safe_json(root_path.joinpath(*CORE_ARTIFACTS["regime_performance"]))

    artifact_payloads = {
        "run_summary": run_summary,
        "agent_bundle": agent_bundle,
        "agent_llm_metadata": agent_llm_metadata,
        "theme_engine_llm_metadata": theme_llm_metadata,
        "watchlist_signals": watchlist,
        "portfolio_snapshot": portfolio_snapshot,
        "policy_recommendation": policy_recommendation,
        "recommendation_evaluation": recommendation_evaluation,
        "recommendation_outcomes": recommendation_outcomes,
        "regime_performance": regime_performance,
    }
    artifact_statuses = {
        name: _artifact_status(
            root=root_path,
            name=name,
            rel_parts=rel_parts,
            payload=artifact_payloads.get(name, {}),
            now=now,
        )
        for name, rel_parts in CORE_ARTIFACTS.items()
    }

    provider_snapshot = _latest_provider_snapshot(
        run_summary=run_summary,
        agent_metadata=agent_llm_metadata,
        theme_metadata=theme_llm_metadata,
    )
    health = _normalize_health(
        artifact_statuses=artifact_statuses,
        run_summary=run_summary,
        provider_snapshot=provider_snapshot,
        watchlist=watchlist,
    )
    weekly_review = _load_weekly_report(root_path, now)

    return {
        "artifacts": {
            "run_summary": run_summary,
            "agent_bundle": agent_bundle,
            "agent_llm_metadata": agent_llm_metadata,
            "theme_engine_llm_metadata": theme_llm_metadata,
            "watchlist_signals": watchlist,
            "portfolio_snapshot": portfolio_snapshot,
            "policy_recommendation": policy_recommendation,
            "recommendation_evaluation": recommendation_evaluation,
            "recommendation_outcomes": recommendation_outcomes,
            "regime_performance": regime_performance,
        },
        "artifact_statuses": artifact_statuses,
        "overview": _normalize_overview(
            now=now,
            run_summary=run_summary,
            watchlist=watchlist,
            portfolio_snapshot=portfolio_snapshot,
            policy_recommendation=policy_recommendation,
            provider_snapshot=provider_snapshot,
            health_warnings=health["warnings"],
            artifact_statuses=artifact_statuses,
        ),
        "run_status": _normalize_run_status(
            now=now,
            run_summary=run_summary,
            provider_snapshot=provider_snapshot,
            artifact_statuses=artifact_statuses,
        ),
        "memo": _pick_latest_memo(root_path, now),
        "signal_triage": _normalize_signal_triage(watchlist),
        "portfolio_view": _normalize_portfolio_view(watchlist, portfolio_snapshot),
        "strategy_view": _normalize_strategy_view(
            policy_recommendation=policy_recommendation,
            recommendation_evaluation=recommendation_evaluation,
            recommendation_outcomes=recommendation_outcomes,
        ),
        "performance_view": _normalize_performance_view(recommendation_outcomes),
        "regime_analytics_view": _normalize_regime_analytics(regime_performance),
        "recommendation_quality_view": _normalize_recommendation_quality(
            recommendation_outcomes=recommendation_outcomes,
            recommendation_evaluation=recommendation_evaluation,
        ),
        "weekly_review": weekly_review,
        "health": health,
        "provider_snapshot": provider_snapshot,
        "decision_brief": _normalize_decision_brief(
            decision_plan=decision_plan,
            system_summary=system_decision_summary,
        ),
        "decision_explanations": load_decision_explanations(root_path),
        "ai_decision_validation": load_ai_decision_validation(root_path),
        "decision_outcome_summary": load_decision_outcome_summary(root_path),
        "decision_triage": load_decision_triage(root_path),
        "confidence_calibration": load_confidence_calibration(root_path),
        "decision_performance_attribution": load_decision_performance_attribution(root_path),
        "data_quality_report": load_data_quality_report(root_path),
        "ai_budget_summary": load_ai_budget_summary(root_path),
        "confidence_calibration_latest": load_confidence_calibration_latest(root_path),
        "discovery_sandbox_status": load_discovery_sandbox_status(root_path),
    }


# ---------------------------------------------------------------------------
# Attribution / Rotation loaders (read-only, no side effects)
# ---------------------------------------------------------------------------

def load_profit_attribution(root: Path | str) -> dict[str, Any]:
    """
    Load outputs/policy/profit_attribution.json.

    Returns {} when the file is absent or malformed.
    Read-only — never writes or modifies any artifact.
    """
    return _safe_json(Path(root) / "outputs" / "policy" / "profit_attribution.json")


def load_rotation_events(root: Path | str) -> list[dict[str, Any]]:
    """
    Load outputs/policy/rotation_events.jsonl (line-delimited JSON).

    Returns [] when the file is absent, empty, or unreadable.
    Malformed lines are silently skipped.
    Read-only — never writes or modifies any artifact.
    """
    path = Path(root) / "outputs" / "policy" / "rotation_events.jsonl"
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    records.append(obj)
            except json.JSONDecodeError:
                pass
    except Exception:
        pass
    return records
