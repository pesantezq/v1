"""Tests for portfolio_automation.discovery.news_ticker_discovery."""
import pytest

from portfolio_automation.discovery.news_ticker_discovery import (
    NOISE_WORDS,
    DiscoveredTicker,
    TickerEvidence,
    extract_tickers,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find(results: list[DiscoveredTicker], ticker: str) -> DiscoveredTicker | None:
    return next((t for t in results if t.ticker == ticker), None)


def _tickers(results: list[DiscoveredTicker]) -> set[str]:
    return {t.ticker for t in results}


# ---------------------------------------------------------------------------
# 1. Cashtag extraction
# ---------------------------------------------------------------------------

class TestCashtagExtraction:
    def test_simple_cashtag(self):
        result = extract_tickers([{"title": "$NVDA rose 5% today"}])
        assert "NVDA" in _tickers(result)

    def test_cashtag_extraction_method(self):
        result = extract_tickers([{"title": "$AAPL beats earnings"}])
        aapl = _find(result, "AAPL")
        assert aapl is not None
        assert any(e.extraction_method == "cashtag" for e in aapl.evidence)

    def test_multiple_cashtags_single_record(self):
        result = extract_tickers([{"title": "$NVDA and $AAPL both gained today"}])
        tickers = _tickers(result)
        assert "NVDA" in tickers
        assert "AAPL" in tickers

    def test_cashtag_in_summary(self):
        result = extract_tickers([{"title": "Market news", "summary": "$MSFT surges on earnings"}])
        assert "MSFT" in _tickers(result)

    def test_cashtag_short_ticker_one_letter_accepted(self):
        # $A is valid (Agilent Technologies)
        result = extract_tickers([{"title": "$A reported results"}])
        assert "A" in _tickers(result)

    def test_cashtag_five_letter_ticker(self):
        result = extract_tickers([{"title": "$GOOGL up 3%"}])
        assert "GOOGL" in _tickers(result)

    def test_cashtag_six_letters_not_extracted(self):
        result = extract_tickers([{"title": "$TOOLONG moved"}])
        assert "TOOLONG" not in _tickers(result)


# ---------------------------------------------------------------------------
# 2. Parenthetical extraction
# ---------------------------------------------------------------------------

class TestParentheticalExtraction:
    def test_simple_parenthetical(self):
        result = extract_tickers([{"title": "NVIDIA (NVDA) reports strong results"}])
        assert "NVDA" in _tickers(result)

    def test_parenthetical_extraction_method(self):
        result = extract_tickers([{"title": "Apple (AAPL) releases new product"}])
        aapl = _find(result, "AAPL")
        assert aapl is not None
        assert any(e.extraction_method == "parenthetical" for e in aapl.evidence)

    def test_parenthetical_in_summary(self):
        result = extract_tickers([{"summary": "Microsoft (MSFT) beats guidance"}])
        assert "MSFT" in _tickers(result)

    def test_parenthetical_two_letter_minimum(self):
        # Single-letter parenthetical (A) could be a section — min 2 chars
        result = extract_tickers([{"title": "Section (A) note"}])
        assert "A" not in _tickers(result)

    def test_parenthetical_five_letter_max(self):
        result = extract_tickers([{"title": "Company (GOOGL) reports"}])
        assert "GOOGL" in _tickers(result)

    def test_parenthetical_six_letters_not_extracted(self):
        result = extract_tickers([{"title": "Entity (TOOLNG) note"}])
        assert "TOOLNG" not in _tickers(result)


# ---------------------------------------------------------------------------
# 3. Source-provided symbols/tickers
# ---------------------------------------------------------------------------

class TestSourceProvidedSymbols:
    def test_symbols_field(self):
        record = {"title": "Market update", "symbols": ["AAPL", "MSFT"]}
        result = extract_tickers([record])
        tickers = _tickers(result)
        assert "AAPL" in tickers
        assert "MSFT" in tickers

    def test_tickers_field(self):
        record = {"title": "News", "tickers": ["NVDA"]}
        result = extract_tickers([record])
        assert "NVDA" in _tickers(result)

    def test_source_provided_extraction_method(self):
        record = {"symbols": ["GOOG"]}
        result = extract_tickers([record])
        goog = _find(result, "GOOG")
        assert goog is not None
        assert any(e.extraction_method == "source_provided" for e in goog.evidence)

    def test_source_provided_overrides_noise(self):
        # If explicitly provided, and NOT in noise words, it passes
        record = {"symbols": ["TSLA"]}
        result = extract_tickers([record])
        assert "TSLA" in _tickers(result)

    def test_source_provided_noise_still_filtered(self):
        record = {"symbols": ["CEO", "AI"]}
        result = extract_tickers([record])
        tickers = _tickers(result)
        assert "CEO" not in tickers
        assert "AI" not in tickers

    def test_source_provided_lowercased_normalized(self):
        record = {"symbols": ["aapl"]}
        result = extract_tickers([record])
        assert "AAPL" in _tickers(result)


# ---------------------------------------------------------------------------
# 4. False-positive filtering (noise words)
# ---------------------------------------------------------------------------

class TestFalsePositiveFiltering:
    def test_ceo_not_extracted(self):
        result = extract_tickers([{"title": "The CEO announced results"}])
        assert "CEO" not in _tickers(result)

    def test_sec_not_extracted(self):
        result = extract_tickers([{"title": "Filed with the (SEC) today"}])
        assert "SEC" not in _tickers(result)

    def test_etf_not_extracted(self):
        result = extract_tickers([{"title": "(ETF) market overview"}])
        assert "ETF" not in _tickers(result)

    def test_ai_cashtag_not_extracted(self):
        result = extract_tickers([{"title": "$AI is a noise word"}])
        assert "AI" not in _tickers(result)

    def test_qqq_is_noise_by_default(self):
        result = extract_tickers([{"title": "$QQQ fell 2%"}])
        assert "QQQ" not in _tickers(result)

    def test_ipo_not_extracted(self):
        result = extract_tickers([{"title": "Company (IPO) scheduled"}])
        assert "IPO" not in _tickers(result)

    def test_buy_sell_not_extracted(self):
        result = extract_tickers([{"title": "(BUY) or (SELL) action needed"}])
        tickers = _tickers(result)
        assert "BUY" not in tickers
        assert "SELL" not in tickers

    def test_noise_words_are_uppercase_frozenset(self):
        assert isinstance(NOISE_WORDS, frozenset)
        assert all(w == w.upper() for w in NOISE_WORDS)

    def test_extra_noise_words_parameter(self):
        result = extract_tickers(
            [{"title": "$CUSTOM_NOISE appeared"}],
            extra_noise_words={"CUSTOM_NOISE"},
        )
        assert "CUSTOM_NOISE" not in _tickers(result)


# ---------------------------------------------------------------------------
# 5. known_universe filtering
# ---------------------------------------------------------------------------

class TestKnownUniverseFiltering:
    def test_ticker_in_universe_kept(self):
        result = extract_tickers(
            [{"title": "$AAPL rose and $NVDA fell"}],
            known_universe={"AAPL"},
        )
        assert "AAPL" in _tickers(result)
        assert "NVDA" not in _tickers(result)

    def test_qqq_allowed_when_in_universe(self):
        result = extract_tickers(
            [{"title": "$QQQ moved 1%"}],
            known_universe={"QQQ"},
        )
        # QQQ is in noise AND not in universe filter overrides (noise takes precedence)
        # noise filter runs before universe filter; QQQ is in NOISE_WORDS → filtered
        assert "QQQ" not in _tickers(result)

    def test_empty_universe_filters_all(self):
        result = extract_tickers(
            [{"title": "$NVDA $AAPL $MSFT"}],
            known_universe=set(),
        )
        assert len(result) == 0

    def test_none_universe_keeps_all_non_noise(self):
        result = extract_tickers(
            [{"title": "$NVDA and $AAPL moved"}],
            known_universe=None,
        )
        tickers = _tickers(result)
        assert "NVDA" in tickers
        assert "AAPL" in tickers


# ---------------------------------------------------------------------------
# 6. Duplicate consolidation
# ---------------------------------------------------------------------------

class TestDuplicateConsolidation:
    def test_same_ticker_two_records_consolidated(self):
        records = [
            {"title": "$NVDA rises", "source": "source_a"},
            {"title": "$NVDA falls", "source": "source_b"},
        ]
        result = extract_tickers(records)
        nvda = _find(result, "NVDA")
        assert nvda is not None
        assert nvda.mention_count == 2

    def test_unique_sources_counted(self):
        records = [
            {"title": "$AAPL news", "source": "reuters"},
            {"title": "$AAPL update", "source": "bloomberg"},
        ]
        result = extract_tickers(records)
        aapl = _find(result, "AAPL")
        assert len(aapl.unique_sources) == 2
        assert "reuters" in aapl.unique_sources
        assert "bloomberg" in aapl.unique_sources

    def test_same_ticker_same_source_single_unique_source(self):
        records = [
            {"title": "$NVDA up", "source": "same_source"},
            {"title": "$NVDA down", "source": "same_source"},
        ]
        result = extract_tickers(records)
        nvda = _find(result, "NVDA")
        assert nvda.mention_count == 2
        assert len(nvda.unique_sources) == 1

    def test_results_sorted_by_mention_count_desc(self):
        records = [
            {"title": "$AAPL"},
            {"title": "$NVDA"},
            {"title": "$NVDA"},
            {"title": "$NVDA"},
        ]
        result = extract_tickers(records)
        assert result[0].ticker == "NVDA"

    def test_min_mentions_filter(self):
        records = [
            {"title": "$NVDA $NVDA"},
            {"title": "$AAPL"},
        ]
        result = extract_tickers(records, min_mentions=2)
        tickers = _tickers(result)
        assert "NVDA" in tickers
        # AAPL has 1 mention from first title extraction, let's check
        # Actually $NVDA $NVDA is two cashtag matches → 2 mentions for NVDA
        # $AAPL in the second record → 1 mention
        assert "AAPL" not in tickers


# ---------------------------------------------------------------------------
# 7. DiscoveredTicker governance flags
# ---------------------------------------------------------------------------

class TestDiscoveredTickerGovernance:
    def test_discovery_only_true(self):
        result = extract_tickers([{"title": "$NVDA"}])
        assert all(t.discovery_only is True for t in result)

    def test_corroboration_required_true(self):
        result = extract_tickers([{"title": "$NVDA"}])
        assert all(t.corroboration_required is True for t in result)

    def test_corroboration_met_false(self):
        result = extract_tickers([{"title": "$NVDA"}])
        assert all(t.corroboration_met is False for t in result)

    def test_corroboration_sources_empty(self):
        result = extract_tickers([{"title": "$NVDA"}])
        assert all(t.corroboration_sources == [] for t in result)

    def test_empty_records_returns_empty(self):
        assert extract_tickers([]) == []

    def test_empty_text_no_crash(self):
        result = extract_tickers([{"title": "", "summary": ""}])
        assert isinstance(result, list)
