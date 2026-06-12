"""Tests for the social ticker extractor: cashtags, false positives, confidence."""
from __future__ import annotations

from portfolio_automation.social_intelligence.ticker_extractor import (
    ALL_NOISE,
    extract_from_text,
)

UNIVERSE = {"NVDA", "GME", "AMC", "TSLA", "AAPL", "PLTR"}


def _tickers(dets):
    return {d.ticker for d in dets}


def test_cashtag_detection():
    dets = extract_from_text("I'm watching $NVDA and $GME closely", known_universe=UNIVERSE)
    assert "NVDA" in _tickers(dets)
    assert "GME" in _tickers(dets)
    nvda = next(d for d in dets if d.ticker == "NVDA")
    assert nvda.match_type == "cashtag"
    assert nvda.evidence_type == "explicit"
    assert nvda.confidence >= 0.9
    assert nvda.false_positive_risk == "low"


def test_avoids_common_false_positives():
    text = "AI is great and IT and ARE and CAN and ON and the CEO said A LOT"
    dets = extract_from_text(text, known_universe=UNIVERSE)
    # None of these noise words should be returned as tickers.
    assert _tickers(dets) == set()


def test_noise_set_covers_required_false_positives():
    for w in ("AI", "ON", "IT", "ARE", "A", "CAN", "CEO", "USA"):
        assert w in ALL_NOISE


def test_uppercase_known_requires_universe():
    text = "GME and NVDA are interesting"
    # With universe → promoted as uppercase_known.
    dets = extract_from_text(text, known_universe=UNIVERSE)
    assert {"GME", "NVDA"} <= _tickers(dets)
    for d in dets:
        assert d.match_type in ("uppercase_known", "cashtag")


def test_uppercase_unknown_flagged_high_risk_without_universe():
    # No universe: a non-noise uppercase token surfaces but as high-risk.
    dets = extract_from_text("ZZZZ looks interesting", known_universe=None)
    zz = [d for d in dets if d.ticker == "ZZZZ"]
    assert zz and zz[0].false_positive_risk == "high"
    assert zz[0].match_type == "uppercase_unknown"
    assert zz[0].confidence < 0.5


def test_unknown_uppercase_dropped_when_universe_present():
    dets = extract_from_text("ZZZZ is not in the universe", known_universe=UNIVERSE)
    assert "ZZZZ" not in _tickers(dets)


def test_company_name_detection():
    dets = extract_from_text(
        "NVIDIA had a great quarter",
        known_universe=UNIVERSE,
        company_names={"NVIDIA": "NVDA"},
    )
    nvda = next(d for d in dets if d.ticker == "NVDA")
    assert nvda.match_type == "company_name"
    assert nvda.confidence >= 0.8


def test_cashtag_noise_word_is_low_confidence():
    # $AI is a cashtag but AI is a noise word and not in universe → suspect.
    dets = extract_from_text("$AI everywhere", known_universe=UNIVERSE)
    ai = [d for d in dets if d.ticker == "AI"]
    if ai:  # emitted but flagged risky
        assert ai[0].confidence <= 0.5


def test_empty_text_returns_nothing():
    assert extract_from_text("", known_universe=UNIVERSE) == []
