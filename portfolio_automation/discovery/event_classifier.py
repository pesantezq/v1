"""
Deterministic rule-based event classification for discovery candidates.

No network calls. No AI calls. Keyword-matching only.

Risk flags are set for legal_risk events and regulatory events that contain
negative-signal keywords (lawsuit, fraud, penalty, fine, investigation).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------

class EventType(str, Enum):
    EARNINGS          = "earnings"
    GUIDANCE          = "guidance"
    ANALYST_ACTION    = "analyst_action"
    PRODUCT_LAUNCH    = "product_launch"
    PARTNERSHIP       = "partnership"
    REGULATORY        = "regulatory"
    MACRO_THEME       = "macro_theme"
    MERGER_ACQUISITION = "merger_acquisition"
    LEGAL_RISK        = "legal_risk"
    FINANCING         = "financing"
    MANAGEMENT_CHANGE = "management_change"
    UNKNOWN           = "unknown"


# ---------------------------------------------------------------------------
# Keyword tables (lowercase)
# ---------------------------------------------------------------------------

_EVENT_KEYWORDS: dict[EventType, list[str]] = {
    EventType.EARNINGS: [
        "earnings", "revenue", "quarterly results", "beat estimates",
        "missed estimates", "profit", "net income", "operating income",
        "eps beat", "eps miss", "q1", "q2", "q3", "q4", "fiscal year",
        "top line", "bottom line",
    ],
    EventType.GUIDANCE: [
        "guidance", "outlook", "forecast", "raised guidance", "lowered guidance",
        "raised its forecast", "full-year", "updated guidance", "expects",
        "projects revenue", "revised",
    ],
    EventType.ANALYST_ACTION: [
        "upgrade", "downgrade", "price target", "buy rating", "sell rating",
        "hold rating", "analyst", "initiated coverage", "reiterated",
        "raised target", "lowered target", "overweight", "underweight",
        "outperform", "underperform", "neutral",
    ],
    EventType.PRODUCT_LAUNCH: [
        "launches", "launch", "new product", "unveils", "introduces",
        "debut", "released", "announced product", "ships", "rolls out",
        "new model", "new version",
    ],
    EventType.PARTNERSHIP: [
        "partnership", "strategic alliance", "joint venture", "collaboration",
        "agreement with", "deal with", "signed agreement", "memorandum of understanding",
        "mou", "contract with", "team up",
    ],
    EventType.REGULATORY: [
        "fda approval", "fda approved", "fda rejected", "sec filing",
        "regulatory approval", "approved by", "clearance", "compliance",
        "regulator", "antitrust", "review by",
    ],
    EventType.MACRO_THEME: [
        "interest rate", "inflation", "federal reserve", "recession",
        "gdp growth", "cpi", "consumer prices", "economy", "macro",
        "rate hike", "rate cut", "monetary policy", "treasury yield",
        "yield curve",
    ],
    EventType.MERGER_ACQUISITION: [
        "acquisition", "acquires", "acquired by", "merger", "takeover",
        "buyout", "purchase of", "buy out", "merges with", "deal valued",
        "agreed to acquire", "bid for",
    ],
    EventType.LEGAL_RISK: [
        "lawsuit", "litigation", "class action", "settlement", "subpoena",
        "fraud allegations", "securities fraud", "whistleblower", "indicted",
        "charged with", "probe into", "criminal investigation",
    ],
    EventType.FINANCING: [
        "ipo", "initial public offering", "secondary offering", "debt offering",
        "equity offering", "raised funding", "series a", "series b",
        "convertible notes", "bond offering", "equity issuance", "shelf offering",
        "capital raise",
    ],
    EventType.MANAGEMENT_CHANGE: [
        "new ceo", "ceo resigns", "ceo departs", "appoints ceo",
        "new chief executive", "new cfo", "appoints president", "names new",
        "steps down", "succession", "management shakeup", "leadership change",
    ],
}

# Keywords that trigger risk_flag=True even for REGULATORY events
_REGULATORY_RISK_KEYWORDS: frozenset[str] = frozenset({
    "investigation", "probe", "penalty", "fine", "violation", "enforcement",
    "seized", "suspended", "banned", "revoked",
})


# ---------------------------------------------------------------------------
# Classification result
# ---------------------------------------------------------------------------

@dataclass
class ClassificationResult:
    """Result of event classification for a single text input."""
    event_type: EventType
    confidence: float           # 0.0 to 1.0
    matched_keywords: list[str]
    risk_flag: bool             # True for legal_risk or risky regulatory


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    return text.lower()


def _count_keyword_matches(text: str, keywords: list[str]) -> list[str]:
    """Return list of keywords found in *text* (lowercased)."""
    return [kw for kw in keywords if kw in text]


def _is_risk_flag(event_type: EventType, text: str) -> bool:
    if event_type == EventType.LEGAL_RISK:
        return True
    if event_type == EventType.REGULATORY:
        return any(kw in text for kw in _REGULATORY_RISK_KEYWORDS)
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_event(text: str) -> ClassificationResult:
    """
    Classify a raw text string into an :class:`EventType`.

    Uses deterministic keyword matching. No AI, no network calls.

    Confidence is computed as ``min(match_count * 0.25, 1.0)`` — four keyword
    matches yields full confidence. When no keywords match, returns UNKNOWN
    with confidence 0.0.
    """
    if not text or not text.strip():
        return ClassificationResult(
            event_type=EventType.UNKNOWN,
            confidence=0.0,
            matched_keywords=[],
            risk_flag=False,
        )

    normalized = _normalize(text)
    best_type = EventType.UNKNOWN
    best_matches: list[str] = []
    best_count = 0

    for event_type, keywords in _EVENT_KEYWORDS.items():
        matches = _count_keyword_matches(normalized, keywords)
        if len(matches) > best_count:
            best_count = len(matches)
            best_type = event_type
            best_matches = matches

    confidence = min(best_count * 0.25, 1.0) if best_count > 0 else 0.0
    risk_flag = _is_risk_flag(best_type, normalized)

    return ClassificationResult(
        event_type=best_type,
        confidence=confidence,
        matched_keywords=best_matches,
        risk_flag=risk_flag,
    )


def classify_record(record: dict) -> ClassificationResult:
    """
    Classify a news record dict by combining ``title`` and ``summary`` fields.

    Delegates to :func:`classify_event`.
    """
    parts: list[str] = []
    for key in ("title", "summary"):
        val = record.get(key) or ""
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
    combined = " ".join(parts)
    return classify_event(combined)
