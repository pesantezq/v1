"""
Keyword-based theme classifier for financial news headlines.

No LLM required — uses a curated keyword dictionary with simple
frequency-weighted scoring. Each theme gets a score in [0, 1].

Themes: AI, Semiconductors, Energy, Crypto, Defense, EV,
        Biotech, China, Inflation, Interest Rates
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Theme keyword dictionary
# Each entry: theme_name → list of lowercase keyword phrases
# Longer / more specific phrases score slightly higher (len > 1 word → 1.5×).
# ---------------------------------------------------------------------------

THEME_KEYWORDS: dict[str, list[str]] = {
    "AI": [
        "artificial intelligence", "machine learning", "deep learning",
        "large language model", "llm", "generative ai", "chatgpt", "copilot",
        "neural network", "ai chip", "ai infrastructure", "ai data center",
        "openai", "anthropic", "ai model",
        # single-word anchors
        "ai", "gpt",
    ],
    "Semiconductors": [
        "semiconductor", "chip maker", "chip manufacturer", "integrated circuit",
        "advanced packaging", "hbm memory", "gpu cluster", "wafer fab",
        "chip shortage", "chip export", "arm holdings",
        "tsmc", "nvidia", "intel", "amd", "qualcomm", "broadcom",
        "chip", "fab", "foundry", "wafer",
    ],
    "Energy": [
        "crude oil", "natural gas", "lng export", "oil production", "energy crisis",
        "opec cut", "opec+", "strategic petroleum", "refinery capacity",
        "renewable energy", "solar panel", "wind farm", "oil price",
        "oil", "gas", "energy", "petroleum", "opec", "refinery",
    ],
    "Crypto": [
        "bitcoin etf", "crypto regulation", "digital asset", "blockchain network",
        "defi protocol", "stablecoin", "crypto exchange", "spot bitcoin",
        "ethereum upgrade", "crypto market",
        "bitcoin", "crypto", "blockchain", "ethereum", "btc", "eth",
        "defi", "altcoin", "mining",
    ],
    "Defense": [
        "defense contract", "military spending", "nato ally", "pentagon budget",
        "missile defense", "drone warfare", "space force", "defense department",
        "lockheed martin", "raytheon", "northrop grumman", "general dynamics",
        "defense", "military", "weapon", "missile", "nato", "aerospace",
    ],
    "EV": [
        "electric vehicle", "ev sales", "battery range", "ev charging",
        "autonomous driving", "self-driving", "lithium battery", "ev adoption",
        "ev manufacturer", "gigafactory", "solid state battery",
        "tesla", "rivian", "lucid", "ev", "lithium",
    ],
    "Biotech": [
        "fda approval", "clinical trial", "drug approval", "phase 3",
        "cancer therapy", "mrna vaccine", "gene therapy", "rare disease",
        "drug pipeline", "biotech acquisition", "pharma merger",
        "fda", "drug", "biotech", "pharma", "vaccine", "therapy", "cancer",
    ],
    "China": [
        "us china trade", "china tariff", "decoupling", "supply chain china",
        "taiwan strait", "china tech ban", "chip export ban", "china slowdown",
        "chinese economy", "beijing policy", "hong kong",
        "china", "chinese", "beijing", "tariff", "taiwan",
    ],
    "Inflation": [
        "consumer price index", "cpi data", "core inflation", "ppi reading",
        "wage growth", "price pressure", "stagflation risk", "inflation expectation",
        "cost of living", "purchasing power",
        "inflation", "cpi", "ppi", "stagflation", "deflation",
    ],
    "Interest Rates": [
        "federal reserve", "fed decision", "rate hike", "rate cut",
        "fomc meeting", "monetary policy", "treasury yield", "yield curve",
        "quantitative tightening", "quantitative easing", "fed funds rate",
        "fed", "fomc", "interest rate", "yield", "rate hike", "rate cut",
    ],
}

# Pre-compiled patterns: phrase → (pattern, weight)
_compiled: dict[str, list[tuple[re.Pattern, float]]] = {}

for _theme, _kws in THEME_KEYWORDS.items():
    _compiled[_theme] = []
    for _kw in _kws:
        _weight = 1.5 if " " in _kw else 1.0   # multi-word = higher signal
        _compiled[_theme].append(
            (re.compile(rf"\b{re.escape(_kw)}\b", re.IGNORECASE), _weight)
        )


def _score_text(text: str, theme: str) -> float:
    """Return raw keyword-hit score for one theme against one text blob."""
    total = 0.0
    for pattern, weight in _compiled[theme]:
        hits = len(pattern.findall(text))
        total += hits * weight
    return total


def classify_headlines(
    headlines: list[str],
    threshold: float = 0.0,
) -> dict[str, float]:
    """
    Score a list of news headlines against all themes.

    Args:
        headlines: List of title + summary strings.
        threshold: Minimum raw score to include a theme (default 0 = include all).

    Returns:
        Dict mapping theme name → normalised score in [0, 1].
        Scores are normalised by dividing by (number of headlines * max_possible_weight)
        so that a single strong hit on 1 headline ≠ dominate.
    """
    if not headlines:
        return {t: 0.0 for t in THEME_KEYWORDS}

    combined = " ".join(headlines).lower()
    raw: dict[str, float] = {}
    for theme in THEME_KEYWORDS:
        raw[theme] = _score_text(combined, theme)

    # Normalise: cap at 1 using the 95th-percentile value so outliers don't compress everything
    max_raw = max(raw.values()) if raw else 1.0
    if max_raw == 0:
        return {t: 0.0 for t in THEME_KEYWORDS}

    normalised = {t: min(1.0, v / max_raw) for t, v in raw.items()}

    # Apply threshold filter
    return {t: s for t, s in normalised.items() if s >= threshold}


def top_themes(
    scores: dict[str, float],
    min_score: float = 0.0,
    max_themes: int = 5,
) -> list[str]:
    """Return theme names sorted by score, filtered by min_score, capped at max_themes."""
    ranked = sorted(
        ((t, s) for t, s in scores.items() if s >= min_score),
        key=lambda x: x[1],
        reverse=True,
    )
    return [t for t, _ in ranked[:max_themes]]


def extract_headline_examples(
    articles: list[dict[str, Any]],
    themes: list[str],
    max_per_theme: int = 2,
) -> list[str]:
    """
    Select representative headlines for the given themes.

    Args:
        articles:     Raw AV news feed dicts (with 'title' and 'summary' keys).
        themes:       Active themes to match against.
        max_per_theme: Max headlines per theme (de-duplicated).

    Returns:
        De-duplicated list of headline strings.
    """
    seen: set[str] = set()
    examples: list[str] = []

    for theme in themes:
        count = 0
        for art in articles:
            title = art.get("title", "")
            if not title or title in seen:
                continue
            text = (title + " " + art.get("summary", "")).lower()
            if any(pat.search(text) for pat, _ in _compiled.get(theme, [])):
                seen.add(title)
                examples.append(title)
                count += 1
                if count >= max_per_theme:
                    break

    return examples
