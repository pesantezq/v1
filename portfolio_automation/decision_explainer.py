from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


DECISION_PLAN_RELATIVE_PATH = ("outputs", "latest", "decision_plan.json")
SYSTEM_SUMMARY_RELATIVE_PATH = ("outputs", "latest", "system_decision_summary.json")
EXPLANATIONS_JSON_RELATIVE_PATH = ("outputs", "latest", "decision_explanations.json")
EXPLANATIONS_MD_RELATIVE_PATH = ("outputs", "latest", "decision_explanations.md")

AI_VALIDATION_BOOST = "boost"
AI_VALIDATION_NEUTRAL = "neutral"
AI_VALIDATION_CAUTION = "caution"

MAX_EXPLANATIONS = 5
MAX_RISKS = 3
MAX_WATCH_ITEMS = 3
MAX_BASIS_ITEMS = 5

_STRUCTURAL_PREFIX_RE = re.compile(r"^STRUCTURAL:\s*", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)%")


def _safe_json_with_status(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        return {}, "missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}, "malformed"
    if not isinstance(payload, dict):
        return {}, "malformed"
    return payload, "ok"


def _safe_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _normalize_text(text: Any) -> str:
    return _WHITESPACE_RE.sub(" ", str(text or "")).strip()


def _first_segment(text: str) -> str:
    return _normalize_text(text.split("|")[0])


def _first_sentence(text: str) -> str:
    match = re.match(r"^(.+?[.!?])(?:\s|$)", text)
    return match.group(1).strip() if match else text.strip()


def _strip_prefixes(text: str) -> str:
    cleaned = _STRUCTURAL_PREFIX_RE.sub("", text).strip()
    cleaned = re.sub(
        r"^(structural|portfolio|market|finance|watchlist)\s*:\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned.strip()


def _cap_sentence(text: str, max_len: int = 100) -> str:
    sentence = _normalize_text(text).rstrip(" ,;:")
    if not sentence:
        return "No explanation available."
    if not re.search(r"[.!?]$", sentence):
        sentence += "."
    if len(sentence) <= max_len:
        return sentence

    body = sentence.rstrip(".!?")
    kept: list[str] = []
    for word in body.split():
        candidate = (" ".join([*kept, word]).rstrip(" ,;:") + ".").strip()
        if len(candidate) > max_len:
            break
        kept.append(word)
    if kept:
        return " ".join(kept).rstrip(" ,;:") + "."
    return sentence[: max_len - 1].rstrip(" ,;:.") + "."


def _format_pct(value: Any) -> str | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if 0 <= number <= 1:
        number *= 100.0
    rendered = f"{number:.1f}".rstrip("0").rstrip(".")
    return f"{rendered}%"


def _extract_pct_pair(decision: dict[str, Any]) -> tuple[str, str] | None:
    inputs_used = _safe_dict(decision.get("inputs_used"))
    current = (
        _format_pct(decision.get("current_pct"))
        or _format_pct(inputs_used.get("current_pct"))
        or _format_pct(inputs_used.get("current"))
    )
    cap = (
        _format_pct(decision.get("cap_pct"))
        or _format_pct(inputs_used.get("cap_pct"))
        or _format_pct(inputs_used.get("cap"))
    )
    if current and cap:
        return current, cap

    reason = _normalize_text(decision.get("reason"))
    percents = _PERCENT_RE.findall(reason)
    if len(percents) >= 2:
        return f"{percents[0]}%", f"{percents[1]}%"
    return None


def _decision_slug(index: int, decision: dict[str, Any]) -> str:
    existing = _normalize_text(decision.get("decision_id"))
    if existing:
        return existing

    parts = [
        str(index + 1),
        _normalize_text(decision.get("symbol") or "unknown"),
        _normalize_text(decision.get("decision") or "unknown"),
        _normalize_text(decision.get("source") or "unknown"),
    ]
    slug = "-".join(
        re.sub(r"[^a-z0-9]+", "-", part.lower()).strip("-") or "unknown"
        for part in parts
    )
    return slug


def _decision_explanation_sentence(decision: dict[str, Any]) -> str:
    inputs_used = _safe_dict(decision.get("inputs_used"))
    reason = _normalize_text(decision.get("reason"))
    short_reason = _normalize_text(decision.get("short_reason"))
    source = _normalize_text(decision.get("source")).lower()
    risk_flags = {str(flag).lower() for flag in _safe_list(decision.get("risk_flags"))}
    text = _strip_prefixes(_first_sentence(_first_segment(short_reason or reason)))
    lowered = text.lower()
    violation_type = _normalize_text(inputs_used.get("violation_type")).lower()

    if source == "structural" and (
        violation_type == "leverage" or "leverage_breach" in risk_flags or "leverage" in lowered
    ):
        pct_pair = _extract_pct_pair(decision)
        if pct_pair:
            return f"Leverage exceeds cap ({pct_pair[0]} vs {pct_pair[1]})."
        return "Leverage exceeds cap."

    if source == "structural" and (
        violation_type == "concentration"
        or "concentration_breach" in risk_flags
        or "concentration" in lowered
    ):
        pct_pair = _extract_pct_pair(decision)
        if pct_pair:
            return f"Concentration exceeds cap ({pct_pair[0]} vs {pct_pair[1]})."
        return "Concentration exceeds cap."

    if any(token in lowered for token in ("rebalance", "drift", "underweight", "overweight")):
        return "Drift exceeds rebalance threshold."

    if "relative strength" in lowered or re.search(r"\brs\b", lowered):
        return "Relative strength near highs."

    if any(token in lowered for token in ("momentum", "breakout", "near highs", "market signal")):
        return "Momentum breakout near highs."

    return _cap_sentence(text, max_len=100)


def _explanation_risks(decision: dict[str, Any], system_summary: dict[str, Any]) -> list[str]:
    risks = [str(flag).strip() for flag in _safe_list(decision.get("risk_flags")) if str(flag).strip()]
    inputs_used = _safe_dict(decision.get("inputs_used"))
    violation_type = _normalize_text(inputs_used.get("violation_type")).lower()
    if violation_type == "leverage" and "leverage_breach" not in risks:
        risks.insert(0, "leverage_breach")
    if violation_type == "concentration" and "concentration_breach" not in risks:
        risks.insert(0, "concentration_breach")

    data_health = _safe_dict(system_summary.get("data_health"))
    degraded = bool(data_health.get("degraded_mode")) or _normalize_text(data_health.get("data_mode")).lower() == "fallback"
    if degraded and "degraded_data" not in risks:
        risks.append("degraded_data")
    return risks[:MAX_RISKS]


def _what_to_watch_next(decision: dict[str, Any], system_summary: dict[str, Any]) -> list[str]:
    action = _normalize_text(decision.get("decision")).upper()
    inputs_used = _safe_dict(decision.get("inputs_used"))
    source = _normalize_text(decision.get("source")).lower()
    risk_flags = {str(flag).lower() for flag in _safe_list(decision.get("risk_flags"))}
    violation_type = _normalize_text(inputs_used.get("violation_type")).lower()

    items: list[str] = []
    if violation_type == "leverage" or "leverage_breach" in risk_flags:
        items.append("Leverage exposure after the next trim.")
        items.append("Guardrail status on the next run.")
    elif violation_type == "concentration" or "concentration_breach" in risk_flags:
        items.append("Position weight after the next trim.")
        items.append("Concentration cap status on the next run.")
    elif action in {"BUY", "SCALE"}:
        items.append("Position size versus allocation caps.")
        items.append("Whether conviction improves on the next run.")
    elif action in {"WAIT", "HOLD", "AVOID"}:
        items.append("Whether evidence strengthens on the next run.")
        items.append("Any change in risk or guardrail context.")
    else:
        items.append("Whether the action condition clears on the next run.")

    if source in {"market", "watchlist"}:
        items.append("Price strength and follow-through.")
    elif source == "finance":
        items.append("Whether the underlying finance trigger persists.")
    elif source == "portfolio":
        items.append("Drift versus the rebalance band.")

    data_health = _safe_dict(system_summary.get("data_health"))
    degraded = bool(data_health.get("degraded_mode")) or _normalize_text(data_health.get("data_mode")).lower() == "fallback"
    if degraded:
        items.append("Data quality recovery from fallback mode.")

    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = _normalize_text(item)
        key = normalized.lower()
        if normalized and key not in seen:
            deduped.append(_cap_sentence(normalized, max_len=90))
            seen.add(key)
        if len(deduped) >= MAX_WATCH_ITEMS:
            break
    return deduped


def _explanation_basis(decision: dict[str, Any], system_summary: dict[str, Any]) -> list[str]:
    basis = [
        f"source:{_normalize_text(decision.get('source') or 'unknown')}",
        f"decision:{_normalize_text(decision.get('decision') or 'unknown')}",
        f"urgency:{_normalize_text(decision.get('urgency') or 'unknown')}",
    ]
    try:
        priority = float(decision.get("priority"))
        basis.append(f"priority:{priority:.3f}")
    except (TypeError, ValueError):
        pass

    inputs_used = _safe_dict(decision.get("inputs_used"))
    for key in ("violation_type", "action_level", "conviction_band", "signal_type", "opportunity_type"):
        value = _normalize_text(inputs_used.get(key))
        if value:
            basis.append(f"{key}:{value}")

    data_health = _safe_dict(system_summary.get("data_health"))
    data_mode = _normalize_text(data_health.get("data_mode"))
    if data_mode:
        basis.append(f"data_mode:{data_mode}")

    deduped: list[str] = []
    seen: set[str] = set()
    for item in basis:
        key = item.lower()
        if key not in seen:
            deduped.append(item)
            seen.add(key)
        if len(deduped) >= MAX_BASIS_ITEMS:
            break
    return deduped


def _ai_validation_label(decision: dict[str, Any], system_summary: dict[str, Any]) -> str:
    source = _normalize_text(decision.get("source")).lower()
    action = _normalize_text(decision.get("decision")).upper()
    risk_flags = [str(flag).strip() for flag in _safe_list(decision.get("risk_flags")) if str(flag).strip()]
    data_health = _safe_dict(system_summary.get("data_health"))
    degraded = bool(data_health.get("degraded_mode")) or _normalize_text(data_health.get("data_mode")).lower() == "fallback"
    confidence = decision.get("confidence")
    try:
        confidence_value = float(confidence)
    except (TypeError, ValueError):
        confidence_value = None

    if source == "structural" or risk_flags or degraded or action == "SELL":
        return AI_VALIDATION_CAUTION
    if action in {"BUY", "SCALE"} and (confidence_value is None or confidence_value >= 0.75):
        return AI_VALIDATION_BOOST
    return AI_VALIDATION_NEUTRAL


def build_decision_explanations(
    decision_plan: dict[str, Any],
    system_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    system_summary = _safe_dict(system_summary)
    plan_copy = copy.deepcopy(_safe_dict(decision_plan))
    decisions = _safe_list(plan_copy.get("decisions"))
    observe_only = bool(plan_copy.get("observe_only", True))

    if not decisions:
        return {
            "generated_at": datetime.now().isoformat(),
            "available": bool(plan_copy),
            "observe_only": observe_only,
            "summary_line": "Decision plan unavailable." if not plan_copy else "No decisions available for explanation.",
            "source_artifacts": {
                "decision_plan": "outputs/latest/decision_plan.json",
                "system_decision_summary": "outputs/latest/system_decision_summary.json",
            },
            "explanations": [],
        }

    explanation_rows: list[dict[str, Any]] = []
    for index, row in enumerate(decisions[:MAX_EXPLANATIONS]):
        explanation_rows.append(
            {
                "decision_id": _decision_slug(index, row),
                "symbol": _normalize_text(row.get("symbol") or "UNKNOWN"),
                "action": _normalize_text(row.get("decision") or "UNKNOWN").upper(),
                "priority": row.get("priority"),
                "urgency": _normalize_text(row.get("urgency") or "unknown"),
                "source": _normalize_text(row.get("source") or "unknown"),
                "source_attribution": f"{_normalize_text(row.get('source') or 'unknown')} decision record",
                "concise_explanation": _decision_explanation_sentence(row),
                "risks": _explanation_risks(row, system_summary),
                "what_to_watch_next": _what_to_watch_next(row, system_summary),
                "explanation_basis": _explanation_basis(row, system_summary),
                "ai_validation": _ai_validation_label(row, system_summary),
            }
        )

    return {
        "generated_at": datetime.now().isoformat(),
        "available": True,
        "observe_only": observe_only,
        "summary_line": f"{len(explanation_rows)} compact decision explanations generated.",
        "source_artifacts": {
            "decision_plan": "outputs/latest/decision_plan.json",
            "system_decision_summary": "outputs/latest/system_decision_summary.json",
        },
        "explanations": explanation_rows,
    }


def render_decision_explanations_md(payload: dict[str, Any]) -> str:
    lines = [
        "# Decision Explanations",
        "",
        "Observe-only. No trades are executed.",
        "",
    ]

    summary_line = _normalize_text(payload.get("summary_line"))
    if summary_line:
        lines.append(summary_line)
        lines.append("")

    if not payload.get("available"):
        return "\n".join(lines).strip() + "\n"

    explanations = _safe_list(payload.get("explanations"))
    if not explanations:
        return "\n".join(lines).strip() + "\n"

    for index, row in enumerate(explanations, 1):
        priority = row.get("priority")
        try:
            priority_text = f"{float(priority):.3f}"
        except (TypeError, ValueError):
            priority_text = "-"
        lines.append(
            f"## {index}. {row.get('action', 'UNKNOWN')} {row.get('symbol', 'UNKNOWN')} | "
            f"{row.get('source', 'unknown')} | {row.get('urgency', 'unknown')} | pri {priority_text}"
        )
        lines.append("")
        lines.append(f"- Why: {row.get('concise_explanation', 'No explanation available.')}")
        risks = row.get("risks") or []
        lines.append(f"- Risks: {', '.join(risks) if risks else 'None'}")
        watch_items = row.get("what_to_watch_next") or []
        lines.append(f"- Watch next: {'; '.join(watch_items) if watch_items else 'None'}")
        basis = row.get("explanation_basis") or []
        lines.append(f"- Basis: {', '.join(basis) if basis else 'None'}")
        lines.append(f"- AI validation: {row.get('ai_validation', AI_VALIDATION_NEUTRAL)}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def generate_decision_explanations(
    root: Path | str | None = None,
    *,
    write_files: bool = True,
) -> tuple[dict[str, Any], str]:
    root_path = Path(root) if root is not None else Path(".")
    plan_path = root_path.joinpath(*DECISION_PLAN_RELATIVE_PATH)
    summary_path = root_path.joinpath(*SYSTEM_SUMMARY_RELATIVE_PATH)

    decision_plan, plan_status = _safe_json_with_status(plan_path)
    system_summary, _ = _safe_json_with_status(summary_path)

    if plan_status != "ok":
        payload = {
            "generated_at": datetime.now().isoformat(),
            "available": False,
            "observe_only": True,
            "summary_line": "Decision plan unavailable." if plan_status == "missing" else "Decision plan malformed.",
            "source_artifacts": {
                "decision_plan": "outputs/latest/decision_plan.json",
                "system_decision_summary": "outputs/latest/system_decision_summary.json",
            },
            "explanations": [],
        }
    else:
        payload = build_decision_explanations(decision_plan, system_summary)

    markdown = render_decision_explanations_md(payload)

    if write_files:
        json_path = root_path.joinpath(*EXPLANATIONS_JSON_RELATIVE_PATH)
        md_path = root_path.joinpath(*EXPLANATIONS_MD_RELATIVE_PATH)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        md_path.write_text(markdown, encoding="utf-8")

    return payload, markdown


if __name__ == "__main__":
    generate_decision_explanations()
