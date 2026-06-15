"""Advisory context enrichment — context-ONLY labels + explanation lines.

Pure. Turns a Phase-2A crowd signal into context-oriented labels and plain-English
lines that explain advisory picks WITHOUT ever implying a trade. A forbidden-phrase
guard makes it structurally impossible to emit buy/sell/confirm language.
"""
from __future__ import annotations

from typing import Any

LABELS = ("Supportive", "Neutral", "Caution", "High Attention", "Insufficient Data")

# Lowercased substrings that must NEVER appear in any enrichment text or label.
FORBIDDEN = (
    "buy because", "sell because", "confirms trade", "crowd signal confirms",
    "social sentiment is positive", "buy signal", "sell signal",
    "strong buy", "strong sell", "bullish", "bearish",
    "privileged", "insider knowledge", "guaranteed",
)

_SEVERITY = {
    "Supportive": "green", "Caution": "yellow", "High Attention": "blue",
    "Neutral": "gray", "Insufficient Data": "gray",
}


class ForbiddenPhraseError(AssertionError):
    pass


def assert_safe(text: str) -> str:
    low = (text or "").lower()
    for bad in FORBIDDEN:
        if bad in low:
            raise ForbiddenPhraseError(f"forbidden phrase in context text: {bad!r}")
    return text


def context_label(signal: dict[str, Any] | None) -> str:
    if not signal:
        return "Insufficient Data"
    if not signal.get("present", True):
        return "Insufficient Data"
    if (signal.get("source_records_count") or 0) <= 0:
        return "Insufficient Data"
    conf = float(signal.get("confidence") or 0.0)
    if conf < 0.2:
        return "Insufficient Data"
    cats = signal.get("category_scores") or {}
    if abs(float(cats.get("attention") or 0.0)) >= 0.5:
        return "High Attention"
    composite = float(signal.get("composite_crowd_score") or 0.0)
    if composite >= 0.15:
        return "Supportive"
    if composite <= -0.15:
        return "Caution"
    return "Neutral"


def label_severity(label: str) -> str:
    return _SEVERITY.get(label, "gray")


def enrich(signal: dict[str, Any] | None, label: str, *, social_disabled: bool = True) -> list[str]:
    """Context-only explanation lines. Always safe (forbidden-guarded)."""
    lines: list[str] = []
    if label == "Insufficient Data":
        lines.append("Insufficient crowd data; advisory is driven entirely by "
                     "portfolio drift / risk rules.")
        return [assert_safe(x) for x in lines]

    lines.append(f"Crowd context is {label.lower()}; advisory remains driven by "
                 f"portfolio drift / risk rules.")

    cats = (signal or {}).get("category_scores") or {}
    analyst = float(cats.get("analyst") or 0.0)
    if analyst >= 0.2:
        lines.append("Analyst context leans supportive (ratings/grades distribution).")
    elif analyst <= -0.2:
        lines.append("Analyst context leans cautious (ratings/grades distribution).")

    if label == "High Attention":
        lines.append("Market attention is elevated; treat as context, not a trade signal.")

    if social_disabled:
        lines.append("Direct FMP social sentiment is unavailable on the current Starter plan.")

    return [assert_safe(x) for x in lines]
