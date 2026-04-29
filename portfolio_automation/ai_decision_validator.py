from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("stockbot.portfolio_automation.ai_decision_validator")

DECISION_PLAN_RELATIVE_PATH = ("outputs", "latest", "decision_plan.json")
VALIDATION_JSON_RELATIVE_PATH = ("outputs", "latest", "ai_decision_validation.json")
VALIDATION_MD_RELATIVE_PATH = ("outputs", "latest", "ai_decision_validation.md")

STATUS_ALIGNED = "aligned"
STATUS_CAUTION = "caution"
STATUS_CONTRADICTION = "contradiction"
STATUS_INSUFFICIENT = "insufficient_context"

_MAX_DECISIONS = 5
_MAX_WATCH_ITEMS = 3

_DEPLOY_KEYWORDS = frozenset({"deploy", "buy", "invest", "purchase", "scale", "add", "open"})
_DEGRADED_FLAGS = frozenset({"degraded_data", "degraded_mode", "cache_only", "fallback"})
_GUARDRAIL_FLAGS = frozenset({"leverage_breach", "concentration_breach"})

# Phrases that negate a deploy keyword — "do not deploy" is not a contradiction
# even though it contains "deploy".  Checked before _DEPLOY_KEYWORDS.
_NO_DEPLOY_PATTERNS = (
    "do not deploy",
    "do not buy",
    "do not invest",
    "do not purchase",
    "do not scale",
    "do not add",
    "do not open",
    "don't deploy",
    "don't buy",
    "don't invest",
    "don't purchase",
    "don't scale",
    "stand by",
    "hold off",
    "until conditions",
    "no action",
    "no new position",
)

_RULE_LABELS: dict[str, str] = {
    "structural_sell_guardrail_violation": "Structural SELL — guardrail violation confirmed",
    "structural_sell_violation_type": "Structural SELL — violation type confirmed",
    "action_conflicts_with_capital_action": "WAIT/HOLD decision conflicts with capital action wording",
    "no_reason_available": "No decision reason available",
    "missing_structured_reason": "decision_reason_structured is absent",
    "degraded_data_actionable": "Degraded data mode with actionable decision",
    "low_confidence_buy_scale": "Low-confidence BUY/SCALE action",
    "default_conservative": "No specific rule matched; conservative caution applied",
}


def _safe_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _safe_list(v: Any) -> list[Any]:
    return v if isinstance(v, list) else []


def _safe_str(v: Any) -> str:
    return str(v or "").strip()


def _capital_action_deploys(capital_action: str) -> bool:
    lowered = capital_action.lower()
    # Negation/hold language takes precedence: "do not deploy" is not a contradiction
    # even though it contains the keyword "deploy".
    if any(pat in lowered for pat in _NO_DEPLOY_PATTERNS):
        return False
    return any(kw in lowered for kw in _DEPLOY_KEYWORDS)


def _has_degraded_signal(row: dict[str, Any]) -> bool:
    risk_flags = {str(f).lower() for f in _safe_list(row.get("risk_flags"))}
    if risk_flags & _DEGRADED_FLAGS:
        return True
    inputs_used = _safe_dict(row.get("inputs_used"))
    if inputs_used.get("degraded_mode") or _safe_str(inputs_used.get("data_mode")).lower() == "fallback":
        return True
    return False


def _detect_status(row: dict[str, Any]) -> tuple[str, str, list[str]]:
    """Return (status, rule_key, contradictions)."""
    decision = _safe_str(row.get("decision")).upper()
    source = _safe_str(row.get("source")).lower()
    risk_flags = {str(f).strip().lower() for f in _safe_list(row.get("risk_flags")) if str(f).strip()}
    structured = _safe_dict(row.get("decision_reason_structured"))
    capital_action = _safe_str(row.get("capital_action"))
    decision_reason = _safe_str(row.get("decision_reason") or row.get("reason"))

    # 1. Insufficient context: no reason at all
    if not decision_reason and not structured:
        return STATUS_INSUFFICIENT, "no_reason_available", []

    # 2. Insufficient context: structured reason missing
    if not structured:
        return STATUS_INSUFFICIENT, "missing_structured_reason", []

    # 3. Contradiction: WAIT/HOLD/AVOID + capital_action says deploy/buy
    if decision in {"WAIT", "HOLD", "AVOID"} and capital_action and _capital_action_deploys(capital_action):
        contradiction_msg = f"Decision is {decision} but capital_action says '{capital_action}'"
        return STATUS_CONTRADICTION, "action_conflicts_with_capital_action", [contradiction_msg]

    # 4. Aligned: structural SELL with guardrail violation flag
    if decision == "SELL" and source == "structural" and (risk_flags & _GUARDRAIL_FLAGS):
        return STATUS_ALIGNED, "structural_sell_guardrail_violation", []

    # 5. Aligned: structural SELL with violation_type from inputs_used
    if decision == "SELL" and source == "structural":
        inputs_used = _safe_dict(row.get("inputs_used"))
        vtype = _safe_str(inputs_used.get("violation_type")).lower()
        if vtype in {"leverage", "concentration"}:
            return STATUS_ALIGNED, "structural_sell_violation_type", []

    # 6. Caution: degraded data + actionable decision
    if _has_degraded_signal(row) and decision in {"BUY", "SCALE", "WAIT", "HOLD"}:
        return STATUS_CAUTION, "degraded_data_actionable", []

    # 7. Caution: low-confidence BUY/SCALE
    if decision in {"BUY", "SCALE"}:
        try:
            confidence = float(row.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < 0.7:
            return STATUS_CAUTION, "low_confidence_buy_scale", []

    return STATUS_CAUTION, "default_conservative", []


def _build_plain_english_summary(
    row: dict[str, Any], status: str, contradictions: list[str]
) -> str:
    decision = _safe_str(row.get("decision")).upper()
    symbol = _safe_str(row.get("symbol") or "UNKNOWN")
    source = _safe_str(row.get("source") or "unknown")
    urgency = _safe_str(row.get("urgency") or "unknown")

    if status == STATUS_ALIGNED:
        return (
            f"{decision} {symbol} ({source}, {urgency}) is structurally consistent: "
            f"the decision type matches the confirmed guardrail violation and risk context."
        )
    if status == STATUS_CONTRADICTION:
        detail = contradictions[0] if contradictions else "action conflicts with stated reason"
        return f"{decision} {symbol}: potential conflict detected — {detail}."
    if status == STATUS_INSUFFICIENT:
        return (
            f"{decision} {symbol}: insufficient structured context to fully validate "
            f"this decision. Check decision_reason_structured."
        )
    reason = _safe_str(row.get("decision_reason") or row.get("reason") or "no reason provided")
    short = reason[:80].rstrip(" ,;:.") + ("." if len(reason) <= 80 else "...")
    return (
        f"{decision} {symbol} ({source}, {urgency}): advisory watch — {short} "
        f"Review data quality and context before acting."
    )


def _build_narrative_context(row: dict[str, Any]) -> str:
    parts: list[str] = []
    source = _safe_str(row.get("source") or "unknown")
    urgency = _safe_str(row.get("urgency") or "unknown")
    risk_flags = [str(f) for f in _safe_list(row.get("risk_flags")) if str(f).strip()]

    try:
        priority = float(row.get("priority") or 0)
        parts.append(f"priority={priority:.3f}")
    except (TypeError, ValueError):
        pass

    parts.append(f"source={source}")
    parts.append(f"urgency={urgency}")

    if risk_flags:
        parts.append(f"risk_flags=[{', '.join(risk_flags)}]")

    structured = _safe_dict(row.get("decision_reason_structured"))
    band = _safe_str(structured.get("band"))
    strategy = _safe_str(structured.get("strategy"))
    if band:
        parts.append(f"band={band}")
    if strategy:
        parts.append(f"strategy={strategy}")

    return "; ".join(parts) if parts else "No additional context."


def _build_watch_next(row: dict[str, Any]) -> list[str]:
    structured = _safe_dict(row.get("decision_reason_structured"))
    seen: set[str] = set()
    items: list[str] = []
    for key in ("watch_next", "what_would_change"):
        for item in _safe_list(structured.get(key)):
            s = _safe_str(item)
            if s and s.lower() not in seen:
                seen.add(s.lower())
                items.append(s)
    return items[:_MAX_WATCH_ITEMS]


def validate_single_decision(row: dict[str, Any]) -> dict[str, Any]:
    """Deterministic validation of a single decision row. Never fails."""
    status, rule_key, contradictions = _detect_status(row)
    symbol = _safe_str(row.get("symbol") or "UNKNOWN")
    decision = _safe_str(row.get("decision") or "UNKNOWN").upper()

    return {
        "symbol": symbol,
        "decision": decision,
        "validation_status": status,
        "plain_english_summary": _build_plain_english_summary(row, status, contradictions),
        "rule_alignment": _RULE_LABELS.get(rule_key, rule_key),
        "narrative_context": _build_narrative_context(row),
        "contradictions": contradictions,
        "watch_next": _build_watch_next(row),
        "ai_used": False,
        "model": None,
        "generated_at": datetime.now().isoformat(),
    }


def _try_llm_enhance(
    record: dict[str, Any],
    row: dict[str, Any],
    *,
    provider: str,
    model: str,
    timeout: int = 20,
) -> dict[str, Any]:
    """Attempt LLM text enhancement. Returns the record unchanged if LLM fails."""
    try:
        from agent.llm_adapters import call_provider
    except ImportError:
        return record

    prompt = (
        "You are a validation assistant for an advisory-only investment system. "
        "Do NOT make decisions. Do NOT change allocations. Observe and explain only.\n\n"
        f"Decision: {record['decision']} {record['symbol']}\n"
        f"Source: {_safe_str(row.get('source'))}\n"
        f"Urgency: {_safe_str(row.get('urgency'))}\n"
        f"Risk flags: {', '.join(str(f) for f in _safe_list(row.get('risk_flags'))) or 'none'}\n"
        f"Decision reason: {_safe_str(row.get('decision_reason') or row.get('reason'))}\n"
        f"Validation status: {record['validation_status']}\n\n"
        "Write 1-2 plain English sentences explaining why this validation status applies. "
        "Be factual, concise, and advisory. Do not invent data or make trading recommendations."
    )

    try:
        text = call_provider(
            provider=provider,
            model=model,
            prompt=prompt,
            max_tokens=200,
            timeout=timeout,
        )
        if text and len(text.strip()) > 10:
            enhanced = dict(record)
            enhanced["plain_english_summary"] = text.strip()[:500]
            enhanced["ai_used"] = True
            enhanced["model"] = f"{provider}/{model}" if model else provider
            enhanced["generated_at"] = datetime.now().isoformat()
            return enhanced
    except Exception as exc:
        logger.debug(
            "LLM enhancement skipped for %s %s: %s",
            record["decision"],
            record["symbol"],
            exc,
        )
    return record


def _resolve_llm_model(provider: str, llm_model: str | None) -> str:
    if llm_model:
        return llm_model
    if provider == "anthropic":
        return (os.environ.get("ANTHROPIC_MODEL") or "claude-haiku-4-5-20251001").strip()
    if provider == "openai":
        return (os.environ.get("OPENAI_MODEL") or "").strip()
    return (os.environ.get("OLLAMA_MODEL") or "gemma3:4b").strip()


def build_ai_validation(
    decision_plan: dict[str, Any],
    *,
    use_llm: bool = False,
    llm_provider: str | None = None,
    llm_model: str | None = None,
) -> dict[str, Any]:
    decisions = _safe_list(decision_plan.get("decisions"))
    top_decisions = decisions[:_MAX_DECISIONS]

    provider = (
        llm_provider
        or os.environ.get("STOCKBOT_LLM_PROVIDER")
        or "ollama"
    ).strip().lower()
    model = _resolve_llm_model(provider, llm_model)

    records: list[dict[str, Any]] = []
    ai_used_any = False

    for row in top_decisions:
        record = validate_single_decision(row)
        if use_llm:
            try:
                record = _try_llm_enhance(record, row, provider=provider, model=model)
            except Exception as _llm_err:
                logger.debug("LLM enhancement skipped for %s: %s", row.get("symbol"), _llm_err)
            if record.get("ai_used"):
                ai_used_any = True
        records.append(record)

    counts: dict[str, int] = {
        STATUS_ALIGNED: 0,
        STATUS_CAUTION: 0,
        STATUS_CONTRADICTION: 0,
        STATUS_INSUFFICIENT: 0,
    }
    for r in records:
        s = r.get("validation_status", STATUS_CAUTION)
        if s in counts:
            counts[s] += 1

    return {
        "generated_at": datetime.now().isoformat(),
        "observe_only": True,
        "total_validated": len(records),
        "aligned_count": counts[STATUS_ALIGNED],
        "caution_count": counts[STATUS_CAUTION],
        "contradiction_count": counts[STATUS_CONTRADICTION],
        "insufficient_context_count": counts[STATUS_INSUFFICIENT],
        "ai_used": ai_used_any,
        "validations": records,
    }


def render_ai_validation_md(payload: dict[str, Any]) -> str:
    lines = [
        "# AI Decision Validation",
        "",
        "Observe-only. No trades are executed.",
        "",
        f"Generated: {payload.get('generated_at', '-')}",
        f"AI used: {payload.get('ai_used', False)}",
        "",
        (
            f"Validated: {payload.get('total_validated', 0)} | "
            f"Aligned: {payload.get('aligned_count', 0)} | "
            f"Caution: {payload.get('caution_count', 0)} | "
            f"Contradiction: {payload.get('contradiction_count', 0)} | "
            f"Insufficient context: {payload.get('insufficient_context_count', 0)}"
        ),
        "",
    ]

    for i, record in enumerate(_safe_list(payload.get("validations")), 1):
        status = record.get("validation_status", "-")
        lines.append(
            f"## {i}. {record.get('decision', '-')} {record.get('symbol', '-')} | {status}"
        )
        lines.append("")
        lines.append(f"- Summary: {record.get('plain_english_summary', '-')}")
        lines.append(f"- Rule: {record.get('rule_alignment', '-')}")
        lines.append(f"- Context: {record.get('narrative_context', '-')}")

        contradictions = record.get("contradictions") or []
        if contradictions:
            lines.append(f"- Contradictions: {'; '.join(contradictions)}")

        watch = record.get("watch_next") or []
        if watch:
            lines.append(f"- Watch next: {'; '.join(watch)}")

        model_label = record.get("model") or "-"
        lines.append(f"- AI used: {record.get('ai_used', False)} | Model: {model_label}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def _safe_json_load(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        return {}, "missing"
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}, "malformed"
    if not isinstance(payload, dict):
        return {}, "malformed"
    return payload, "ok"


def run_ai_validation(
    root: Path | str | None = None,
    *,
    write_files: bool = True,
    use_llm: bool = False,
    llm_provider: str | None = None,
    llm_model: str | None = None,
) -> tuple[dict[str, Any], str]:
    root_path = Path(root) if root is not None else Path(".")
    plan_path = root_path.joinpath(*DECISION_PLAN_RELATIVE_PATH)

    decision_plan, plan_status = _safe_json_load(plan_path)

    if plan_status != "ok":
        payload: dict[str, Any] = {
            "generated_at": datetime.now().isoformat(),
            "observe_only": True,
            "available": False,
            "total_validated": 0,
            "aligned_count": 0,
            "caution_count": 0,
            "contradiction_count": 0,
            "insufficient_context_count": 0,
            "ai_used": False,
            "summary_line": (
                "Decision plan unavailable."
                if plan_status == "missing"
                else "Decision plan malformed."
            ),
            "validations": [],
        }
    else:
        payload = build_ai_validation(
            decision_plan,
            use_llm=use_llm,
            llm_provider=llm_provider,
            llm_model=llm_model,
        )
        payload["available"] = True
        payload["summary_line"] = f"{payload['total_validated']} decisions validated."

    markdown = render_ai_validation_md(payload)

    if write_files:
        json_path = root_path.joinpath(*VALIDATION_JSON_RELATIVE_PATH)
        md_path = root_path.joinpath(*VALIDATION_MD_RELATIVE_PATH)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        md_path.write_text(markdown, encoding="utf-8")

    return payload, markdown


if __name__ == "__main__":
    run_ai_validation()
