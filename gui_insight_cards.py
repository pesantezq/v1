from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

import streamlit as st


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt_pct(value: Any) -> str:
    num = _coerce_float(value, 0.0)
    return f"{num * 100:.2f}%"


def _fmt_usd(value: Any) -> str:
    num = _coerce_float(value, 0.0)
    return f"${num:,.2f}"


def _status_tone(decision: str) -> str:
    dec = str(decision or "").upper()
    if dec == "SELL":
        return "danger"
    if dec in {"BUY", "SCALE"}:
        return "positive"
    if dec in {"WAIT", "HOLD"}:
        return "neutral"
    return "muted"


def _status_badge(decision: str) -> str:
    dec = str(decision or "UNKNOWN").upper()
    tone = _status_tone(dec)
    if tone == "danger":
        return f":red[{dec}]"
    if tone == "positive":
        return f":green[{dec}]"
    if tone == "neutral":
        return f":blue[{dec}]"
    return f":gray[{dec}]"


def _dedup_strings(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _split_reason_lines(*parts: Any, limit: int = 3) -> list[str]:
    out: list[str] = []
    for part in parts:
        text = str(part or "").strip()
        if not text:
            continue
        text = text.replace(" | ", "|")
        segments = [seg.strip() for seg in text.split("|") if seg.strip()]
        if not segments:
            segments = [text]
        for segment in segments:
            normalized = re.sub(r"\s+", " ", segment).strip()
            if not normalized:
                continue
            subparts = [
                item.strip()
                for item in re.split(r"(?<=[.!?;])\s+", normalized)
                if item.strip()
            ]
            if not subparts:
                subparts = [normalized]
            for item in subparts:
                if item not in out:
                    out.append(item)
                if len(out) >= limit:
                    return out
    return out[:limit]


def _fallback_why(row: dict[str, Any]) -> list[str]:
    for candidate in (
        row.get("decision_reason"),
        row.get("reason"),
        "No structured insight available.",
    ):
        lines = _split_reason_lines(candidate, limit=3)
        if lines:
            return lines
    return ["No structured insight available."]


def build_insight_card_models(
    decision_rows: list[dict[str, Any]],
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """
    Build stable card models from already-loaded decision-plan rows.

    Pure helper: no Streamlit calls and no mutation of the input rows.
    """
    rows = deepcopy(list(decision_rows or []))
    cards: list[dict[str, Any]] = []

    for row in rows[:limit]:
        structured = row.get("decision_reason_structured")
        structured = structured if isinstance(structured, dict) else {}
        drivers = structured.get("drivers") if isinstance(structured.get("drivers"), dict) else {}
        allocation = (
            structured.get("allocation")
            if isinstance(structured.get("allocation"), dict)
            else {}
        )

        decision = str(row.get("decision") or structured.get("decision") or "UNKNOWN").upper()
        symbol = str(row.get("symbol") or "UNKNOWN")
        priority_score = _coerce_float(
            drivers.get("priority_score", row.get("priority_score", row.get("priority", 0.0))),
            0.0,
        )
        strategy = str(structured.get("strategy") or "unknown")
        band = str(structured.get("band") or "unknown")
        why = structured.get("why") if isinstance(structured.get("why"), list) else []
        what_would_change = (
            structured.get("what_would_change")
            if isinstance(structured.get("what_would_change"), list)
            else []
        )
        watch_next = (
            structured.get("watch_next")
            if isinstance(structured.get("watch_next"), list)
            else []
        )
        risk_flags = structured.get("risk_flags") if isinstance(structured.get("risk_flags"), list) else row.get("risk_flags") or []
        override_flags = (
            structured.get("override_flags")
            if isinstance(structured.get("override_flags"), list)
            else row.get("override_flags") or []
        )
        tags = _dedup_strings([*list(risk_flags), *list(override_flags)])

        why = _split_reason_lines(*why, limit=3) if why else []
        if not why:
            why = _fallback_why(row)

        cards.append(
            {
                "symbol": symbol,
                "decision": decision,
                "status_badge": _status_badge(decision),
                "status_tone": _status_tone(decision),
                "strategy": strategy,
                "band": band,
                "priority_score": round(priority_score, 3),
                "why": _dedup_strings(why),
                "risk_tags": tags,
                "allocation_pct": _coerce_float(
                    allocation.get("recommended_allocation_pct", row.get("recommended_allocation_pct")),
                    0.0,
                ),
                "allocation_amount": _coerce_float(
                    allocation.get("recommended_amount", row.get("recommended_amount")),
                    0.0,
                ),
                "what_would_change": _dedup_strings(what_would_change)[:3],
                "watch_next": _dedup_strings(watch_next)[:3],
                "fallback_reason": str(
                    row.get("decision_reason") or row.get("reason") or ""
                ).strip(),
            }
        )

    return cards


def render_insight_cards(decision_rows: list[dict[str, Any]]) -> None:
    """Render additive structured insight cards from already-loaded decision-plan rows."""
    cards = build_insight_card_models(decision_rows, limit=5)
    if not cards:
        st.caption("No insight cards available.")
        return

    for card in cards:
        with st.container():
            st.markdown(
                f"**{card['symbol']} {card['decision']}** | {card['status_badge']}"
            )
            st.caption(
                f"Strategy: {card['strategy']} | Band: {card['band']} | Priority: {card['priority_score']:.3f}"
            )

            st.markdown("**Why**")
            for item in card["why"]:
                st.markdown(f"- {item}")

            if card["risk_tags"]:
                st.markdown("**Risks**")
                st.markdown(" ".join(f"`{tag}`" for tag in card["risk_tags"]))

            st.markdown("**Allocation**")
            st.write(
                f"{_fmt_pct(card['allocation_pct'])} | {_fmt_usd(card['allocation_amount'])}"
            )

            if card["what_would_change"]:
                st.markdown("**What Would Change**")
                for item in card["what_would_change"]:
                    st.markdown(f"- {item}")

            if card["watch_next"]:
                st.markdown("**Watch Next**")
                for item in card["watch_next"]:
                    st.markdown(f"- {item}")

            st.divider()
