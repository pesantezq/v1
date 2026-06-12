"""
Robust ticker extraction for noisy public-discussion text.

Forum text (WallStreetBets-style) is far noisier than the curated news feeds
handled by ``discovery/news_ticker_discovery.py``: bare uppercase tokens like
``AI``, ``IT``, ``ARE``, ``CEO`` are everywhere, and a naive matcher produces
mostly false positives. This module therefore:

- reuses the shared ``NOISE_WORDS`` set from news_ticker_discovery (single source
  of truth for known false positives),
- assigns a **confidence** score and **match_type** per detection,
- estimates a **false_positive_risk**,
- and only treats a bare uppercase token as a ticker when a known-universe
  allowlist confirms it (context filtering for the highest-noise case).

No network, no AI, deterministic.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Reuse the curated false-positive set rather than maintaining a second copy.
from portfolio_automation.discovery.news_ticker_discovery import NOISE_WORDS

# Additional forum-specific noise (slang / common all-caps interjections).
_FORUM_NOISE: frozenset[str] = frozenset({
    "YOLO", "FOMO", "FUD", "DD", "TLDR", "IMO", "IMHO", "EOD", "EOW",
    "ATH", "ATL", "OTM", "ITM", "PT", "PUMP", "DUMP", "MOON", "BTFD",
    "WSB", "OP", "EDIT", "LOL", "LMAO", "WTF", "RIP", "GG", "HODL",
    "CALLS", "PUTS", "BAGS", "TENDIES", "STONK", "STONKS",
    "ARE", "CAN", "ON", "IT", "BE", "DO", "GO", "SO", "UP", "OR", "OK",
    "YES", "NO", "WAY", "GET", "GOT", "BIG", "RED", "GREEN", "BULL", "BEAR",
    # Single letters + common short English words that look like tickers
    "A", "I", "AS", "AT", "BY", "IF", "IN", "IS", "OF", "TO", "WE", "ME",
    "MY", "HE", "US", "AN", "ANY", "HAS", "HAD", "WAS", "ARE", "WHO", "WHY",
    "HOW", "NOW", "OWN", "SEE", "SAY", "TWO", "USE", "MAY", "DAY", "LET",
})

ALL_NOISE: frozenset[str] = NOISE_WORDS | _FORUM_NOISE

_CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,5})\b")
_UPPER_TOKEN_RE = re.compile(r"\b([A-Z]{1,5})\b")


@dataclass
class TickerDetection:
    """One detected ticker mention with confidence metadata."""

    ticker: str
    confidence: float          # 0.0 .. 1.0
    match_type: str            # "cashtag" | "company_name" | "uppercase_known" | "uppercase_unknown"
    evidence_type: str         # "explicit" | "contextual"
    false_positive_risk: str   # "low" | "medium" | "high"


def _risk_from_confidence(confidence: float) -> str:
    if confidence >= 0.8:
        return "low"
    if confidence >= 0.5:
        return "medium"
    return "high"


def _valid_shape(token: str) -> bool:
    return bool(token) and token.isalpha() and 1 <= len(token) <= 5


def extract_from_text(
    text: str,
    *,
    known_universe: set[str] | frozenset[str] | None = None,
    company_names: dict[str, str] | None = None,
    extra_noise: set[str] | None = None,
) -> list[TickerDetection]:
    """
    Extract ticker detections from a single piece of *text*.

    Parameters
    ----------
    text:
        Raw post text (title + body). Held transiently by the caller.
    known_universe:
        Allowlist of valid symbols. Required to promote bare-uppercase tokens to
        detections; without it, bare-uppercase tokens are returned only as
        low-confidence ``uppercase_unknown`` *if* they survive noise filtering.
    company_names:
        Optional ``{"NVIDIA": "NVDA", ...}`` map for company-name detection.
        Keys are matched case-insensitively as whole words.
    extra_noise:
        Extra tokens to treat as noise (merged with ALL_NOISE).

    Returns
    -------
    One :class:`TickerDetection` per distinct ticker (highest confidence wins on
    duplicate detections within the same text), sorted by confidence descending.
    """
    if not text:
        return []

    noise = ALL_NOISE | {w.upper() for w in extra_noise} if extra_noise else ALL_NOISE
    universe = {t.upper() for t in known_universe} if known_universe else None

    best: dict[str, TickerDetection] = {}

    def _record(det: TickerDetection) -> None:
        prev = best.get(det.ticker)
        if prev is None or det.confidence > prev.confidence:
            best[det.ticker] = det

    # 1. Cashtags — most explicit signal in forum text.
    for m in _CASHTAG_RE.finditer(text):
        tkr = m.group(1).upper()
        if not _valid_shape(tkr):
            continue
        in_universe = universe is None or tkr in universe
        # A cashtag is an explicit author intent even for an unknown symbol, but
        # noise words written as cashtags ($AI) are still suspect.
        if tkr in noise and not (universe and tkr in universe):
            confidence = 0.45
        else:
            confidence = 0.95 if in_universe else 0.7
        _record(TickerDetection(
            ticker=tkr,
            confidence=confidence,
            match_type="cashtag",
            evidence_type="explicit",
            false_positive_risk=_risk_from_confidence(confidence),
        ))

    # 2. Company names → ticker.
    if company_names:
        lowered = text.lower()
        for name, tkr in company_names.items():
            if not name:
                continue
            if re.search(rf"\b{re.escape(name.lower())}\b", lowered):
                tkr_u = str(tkr).upper()
                if not _valid_shape(tkr_u):
                    continue
                _record(TickerDetection(
                    ticker=tkr_u,
                    confidence=0.85,
                    match_type="company_name",
                    evidence_type="contextual",
                    false_positive_risk="low",
                ))

    # 3. Bare uppercase tokens — highest false-positive surface.
    for m in _UPPER_TOKEN_RE.finditer(text):
        tkr = m.group(1).upper()
        if not _valid_shape(tkr) or tkr in noise:
            continue
        if universe is not None and tkr in universe:
            confidence = 0.75
            _record(TickerDetection(
                ticker=tkr,
                confidence=confidence,
                match_type="uppercase_known",
                evidence_type="contextual",
                false_positive_risk=_risk_from_confidence(confidence),
            ))
        elif universe is None:
            # No allowlist to lean on: emit but flag as high-risk so downstream
            # aggregation can require corroboration before trusting it.
            confidence = 0.3
            _record(TickerDetection(
                ticker=tkr,
                confidence=confidence,
                match_type="uppercase_unknown",
                evidence_type="contextual",
                false_positive_risk="high",
            ))
        # If a universe IS provided and the token isn't in it, drop it entirely.

    return sorted(best.values(), key=lambda d: d.confidence, reverse=True)
