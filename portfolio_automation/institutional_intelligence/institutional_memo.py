"""
Compact institutional-context memo section.

Renders a short, honest block ONLY when there is material institutional context.
It NEVER implies a hedge fund "just bought" — every line states the filing age
and ends with a context-only disclaimer, because a 13F is a delayed, incomplete,
options-opaque, shorts-invisible disclosure.
"""

from __future__ import annotations

from typing import Any

_DISCLAIMER = ("Institutional context is delayed (13F filed weeks after "
               "quarter-end), incomplete (long US positions only; no shorts), and "
               "options cannot be fully reconstructed — evidence, not a live trade "
               "instruction. Context only — no funded-action override.")

_MATERIAL_STATES = {"strong_accumulation", "moderate_accumulation",
                    "crowded_accumulation", "strong_distribution",
                    "moderate_distribution", "crowded_distribution"}


def _material_records(artifact: dict[str, Any], min_confidence: float) -> list[dict]:
    recs = [r for r in (artifact.get("records") or [])
            if r.get("consensus_state") in _MATERIAL_STATES
            and (r.get("consensus_confidence") or 0.0) >= min_confidence]
    # Most-confident first, then by effective independent managers.
    recs.sort(key=lambda r: (r.get("consensus_confidence") or 0.0,
                             r.get("effective_independent_managers") or 0.0),
              reverse=True)
    return recs


def _state_phrase(state: str) -> str:
    return {
        "strong_accumulation": "strong accumulation",
        "moderate_accumulation": "moderate accumulation",
        "crowded_accumulation": "crowded accumulation (caution)",
        "strong_distribution": "strong distribution",
        "moderate_distribution": "moderate distribution",
        "crowded_distribution": "crowded distribution (caution)",
    }.get(state, state)


def render_institutional_memo_lines(
    artifact: dict[str, Any] | None,
    *,
    markdown: bool = True,
    max_symbols: int = 3,
    min_confidence: float = 0.55,
) -> list[str]:
    """Return memo lines, or [] when there is no material institutional context."""
    if not artifact or not artifact.get("records"):
        return []
    recs = _material_records(artifact, min_confidence)
    if not recs:
        return []

    out: list[str] = []
    out.append("## Institutional context" if markdown else "INSTITUTIONAL CONTEXT")
    for r in recs[:max_symbols]:
        sym = r.get("symbol", "?")
        eff = r.get("effective_independent_managers")
        age = r.get("filing_age_days")
        state = _state_phrase(r.get("consensus_state", ""))
        crowd = r.get("crowding_score")
        crowd_txt = ("moderate" if (crowd or 0) >= 0.3 else "low") if crowd is not None else "n/a"
        eff_txt = f"{eff:.1f} effective independent managers" if isinstance(eff, (int, float)) else "n/a managers"
        age_txt = f"filings {age} days old" if age is not None else "filing age n/a"
        line = (f"{sym} — {state}, {eff_txt}, {age_txt}; crowding {crowd_txt}.")
        out.append(f"- {line}" if markdown else f"  {line}")
    out.append(f"- _{_DISCLAIMER}_" if markdown else f"  {_DISCLAIMER}")
    out.append("")
    return out
