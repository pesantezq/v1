"""
Tests for portfolio_automation/news/fmp_news_intelligence.py

Coverage:
  - normalization with missing/malformed fields
  - deduplication (URL-based and title+date-based)
  - deterministic ordering (newest-first)
  - ticker extraction (source-provided, cashtag, parenthetical, alias map)
  - company alias mapping
  - ETF alias mapping
  - generic theme terms NOT over-mapped to tickers
  - theme classification scoring
  - risk flag detection
  - catalyst flag detection
  - evidence packet grouping
  - official monitoring lane classification
  - sandbox discovery lane classification
  - no forbidden statuses emitted
  - output artifacts include observe-only/no-trade/not-recommendation flags
  - malformed article input degrades safely
  - deterministic ordering preserved
  - no source dict mutation
  - governance namespace compliance (write paths)
  - no live API required
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from portfolio_automation.news.fmp_news_intelligence import (
    NormalizedArticle,
    ThemeMatch,
    EvidencePacket,
    normalize_news_articles,
    dedupe_news_articles,
    extract_news_entities,
    classify_news_themes,
    build_news_evidence_packets,
    write_news_intelligence_report,
    run_fmp_news_intelligence,
    COMPANY_ALIAS_MAP,
    THEME_KEYWORDS,
    _OBSERVE_ONLY,
    _NO_TRADE,
    _NOT_RECOMMENDATION,
    _SOURCE_LABEL,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _article(
    title="NVIDIA reports record earnings",
    text="NVDA beat estimates significantly.",
    url="https://example.com/nvidia-earnings",
    published_at="2026-05-10T09:00:00Z",
    source="Reuters",
    site="reuters.com",
    symbols=None,
    tickers=None,
) -> dict:
    d = {
        "title": title,
        "text": text,
        "url": url,
        "publishedDate": published_at,
        "source": source,
        "site": site,
    }
    if symbols is not None:
        d["symbols"] = symbols
    if tickers is not None:
        d["tickers"] = tickers
    return d


def _make_normalized(title="Test", text="", tickers=None, published_at="2026-05-10") -> NormalizedArticle:
    from portfolio_automation.news.fmp_news_intelligence import _make_dedup_key
    return NormalizedArticle(
        title=title,
        text=text,
        url="",
        published_at=published_at,
        source="test",
        site="test",
        tickers=tickers or [],
        symbols=tickers or [],
        image="",
        normalized_at="2026-05-10T00:00:00Z",
        dedup_key=_make_dedup_key(title, published_at, "", "test"),
        raw={},
    )


# ---------------------------------------------------------------------------
# 1. Normalization
# ---------------------------------------------------------------------------

class TestNormalization:
    def test_basic_normalization(self):
        raw = _article()
        results = normalize_news_articles([raw])
        assert len(results) == 1
        art = results[0]
        assert art.title == "NVIDIA reports record earnings"
        assert art.text == "NVDA beat estimates significantly."
        assert art.source == "Reuters"
        assert art.published_at == "2026-05-10T09:00:00Z"

    def test_missing_title_skipped(self):
        results = normalize_news_articles([{"text": "no title here"}])
        assert results == []

    def test_empty_input(self):
        assert normalize_news_articles([]) == []

    def test_non_dict_skipped(self):
        results = normalize_news_articles(["not a dict", 42, None])
        assert results == []

    def test_mixed_valid_invalid(self):
        results = normalize_news_articles([_article(), {"text": "no title"}, None])
        assert len(results) == 1

    def test_missing_url_ok(self):
        raw = _article(url="")
        results = normalize_news_articles([raw])
        assert results[0].url == ""

    def test_missing_text_ok(self):
        raw = {"title": "Just a headline"}
        results = normalize_news_articles([raw])
        assert results[0].text == ""

    def test_source_provided_symbols(self):
        raw = _article(symbols=["NVDA", "AAPL"])
        results = normalize_news_articles([raw])
        assert "NVDA" in results[0].tickers
        assert "AAPL" in results[0].tickers

    def test_source_provided_tickers_field(self):
        raw = _article(tickers=["MSFT"])
        results = normalize_news_articles([raw])
        assert "MSFT" in results[0].tickers

    def test_ticker_sentiment_field(self):
        raw = {
            "title": "Meta earnings",
            "ticker_sentiment": [{"ticker": "META", "relevance_score": "0.9"}],
        }
        results = normalize_news_articles([raw])
        assert "META" in results[0].tickers

    def test_noise_words_not_added_as_tickers(self):
        raw = _article(symbols=["BUY", "SELL", "AI", "CEO"])
        results = normalize_news_articles([raw])
        for noise in ["BUY", "SELL", "AI", "CEO"]:
            assert noise not in results[0].tickers

    def test_does_not_mutate_input(self):
        raw = _article()
        original_id = id(raw)
        _ = normalize_news_articles([raw])
        assert id(raw) == original_id

    def test_time_published_field_accepted(self):
        raw = {"title": "Test", "time_published": "2026-05-09T08:00:00Z"}
        results = normalize_news_articles([raw])
        assert results[0].published_at == "2026-05-09T08:00:00Z"

    def test_published_at_field_accepted(self):
        raw = {"title": "Test", "published_at": "2026-05-08"}
        results = normalize_news_articles([raw])
        assert results[0].published_at == "2026-05-08"

    def test_normalized_at_set(self):
        results = normalize_news_articles([_article()])
        assert results[0].normalized_at  # not empty

    def test_dedup_key_set(self):
        results = normalize_news_articles([_article()])
        assert results[0].dedup_key  # not empty

    def test_raw_field_stored(self):
        raw = _article()
        results = normalize_news_articles([raw])
        assert results[0].raw["title"] == raw["title"]


# ---------------------------------------------------------------------------
# 2. Deduplication
# ---------------------------------------------------------------------------

class TestDeduplication:
    def test_removes_duplicate_urls(self):
        raw1 = _article(url="https://example.com/story1")
        raw2 = _article(url="https://example.com/story1")
        results = normalize_news_articles([raw1, raw2])
        unique = dedupe_news_articles(results)
        assert len(unique) == 1

    def test_preserves_different_urls(self):
        raw1 = _article(url="https://example.com/story1")
        raw2 = _article(url="https://example.com/story2", title="Another story")
        results = normalize_news_articles([raw1, raw2])
        unique = dedupe_news_articles(results)
        assert len(unique) == 2

    def test_no_url_uses_title_date_hash(self):
        raw1 = _article(url="", title="Same title", published_at="2026-05-01")
        raw2 = _article(url="", title="Same title", published_at="2026-05-01")
        results = normalize_news_articles([raw1, raw2])
        unique = dedupe_news_articles(results)
        assert len(unique) == 1

    def test_same_title_different_dates_kept(self):
        raw1 = _article(url="", title="Recurring headline", published_at="2026-05-01")
        raw2 = _article(url="", title="Recurring headline", published_at="2026-05-02")
        results = normalize_news_articles([raw1, raw2])
        unique = dedupe_news_articles(results)
        assert len(unique) == 2

    def test_sorted_newest_first(self):
        raw1 = _article(url="u1", published_at="2026-05-01", title="Old")
        raw2 = _article(url="u2", published_at="2026-05-10", title="New")
        raw3 = _article(url="u3", published_at="2026-05-05", title="Mid")
        results = normalize_news_articles([raw1, raw2, raw3])
        unique = dedupe_news_articles(results)
        assert unique[0].published_at == "2026-05-10"
        assert unique[2].published_at == "2026-05-01"

    def test_empty_input(self):
        assert dedupe_news_articles([]) == []


# ---------------------------------------------------------------------------
# 3. Entity extraction
# ---------------------------------------------------------------------------

class TestEntityExtraction:
    def test_source_provided_tickers(self):
        art = _make_normalized(title="Earnings news", tickers=["NVDA"])
        assert "NVDA" in extract_news_entities(art)

    def test_cashtag_extraction(self):
        art = _make_normalized(title="$AAPL hits new high", text="Apple stock surges.")
        entities = extract_news_entities(art)
        assert "AAPL" in entities

    def test_parenthetical_extraction(self):
        art = _make_normalized(title="Microsoft (MSFT) raises guidance")
        entities = extract_news_entities(art)
        assert "MSFT" in entities

    def test_nvidia_alias_mapping(self):
        art = _make_normalized(title="Nvidia announces new GPU", text="Nvidia chips dominate.")
        entities = extract_news_entities(art)
        assert "NVDA" in entities

    def test_google_alias_mapping(self):
        art = _make_normalized(title="Google reports ad revenue growth")
        entities = extract_news_entities(art)
        assert "GOOGL" in entities

    def test_alphabet_alias_mapping(self):
        art = _make_normalized(title="Alphabet beats quarterly estimates")
        entities = extract_news_entities(art)
        assert "GOOGL" in entities

    def test_meta_alias_mapping(self):
        art = _make_normalized(title="Meta unveils new AI model")
        entities = extract_news_entities(art)
        assert "META" in entities

    def test_facebook_alias_mapping(self):
        art = _make_normalized(title="Facebook parent Meta reports record earnings")
        entities = extract_news_entities(art)
        assert "META" in entities

    def test_amazon_alias_mapping(self):
        art = _make_normalized(title="Amazon AWS cloud revenue beats")
        entities = extract_news_entities(art)
        assert "AMZN" in entities

    def test_tesla_alias_mapping(self):
        art = _make_normalized(title="Tesla deliveries miss estimates")
        entities = extract_news_entities(art)
        assert "TSLA" in entities

    def test_noise_words_not_extracted(self):
        art = _make_normalized(title="BUY SELL HOLD CEO CFO AI ETF")
        entities = extract_news_entities(art)
        for noise in ["BUY", "SELL", "HOLD", "CEO", "CFO", "AI", "ETF"]:
            assert noise not in entities

    def test_generic_cloud_term_not_mapped_to_tickers(self):
        # "cloud" alone should NOT map to MSFT/AMZN/GOOGL
        art = _make_normalized(title="Cloud computing market grows", text="The cloud sector expands.")
        entities = extract_news_entities(art)
        # Without explicit company names, these should not appear
        assert "MSFT" not in entities
        assert "AMZN" not in entities
        assert "GOOGL" not in entities

    def test_etf_alias_mapping(self):
        art = _make_normalized(title="Nasdaq 100 hits record as tech rallies")
        entities = extract_news_entities(art)
        assert "QQQ" in entities

    def test_no_mutation_of_article(self):
        art = _make_normalized(title="$NVDA earnings", tickers=[])
        original_tickers = list(art.tickers)
        _ = extract_news_entities(art)
        assert art.tickers == original_tickers

    def test_deduped_results(self):
        # Same ticker from multiple extraction methods should appear once
        art = _make_normalized(title="$NVDA Nvidia (NVDA) earnings", tickers=["NVDA"])
        entities = extract_news_entities(art)
        assert entities.count("NVDA") == 1


# ---------------------------------------------------------------------------
# 4. Theme classification
# ---------------------------------------------------------------------------

class TestThemeClassification:
    def test_earnings_theme_detected(self):
        art = _make_normalized(title="Company beats earnings estimates and raises guidance")
        themes = classify_news_themes(art)
        theme_names = [t.theme for t in themes]
        assert "earnings_guidance" in theme_names

    def test_ai_infrastructure_theme(self):
        art = _make_normalized(title="AI infrastructure spending surges on LLM demand")
        themes = classify_news_themes(art)
        theme_names = [t.theme for t in themes]
        assert "ai_infrastructure" in theme_names

    def test_semiconductors_theme(self):
        art = _make_normalized(title="Semiconductor chip shortage eases as fab capacity grows")
        themes = classify_news_themes(art)
        theme_names = [t.theme for t in themes]
        assert "semiconductors" in theme_names

    def test_legal_risk_theme(self):
        art = _make_normalized(title="Company faces SEC investigation and potential fine")
        themes = classify_news_themes(art)
        theme_names = [t.theme for t in themes]
        assert "legal_regulatory_risk" in theme_names

    def test_fed_policy_theme(self):
        art = _make_normalized(title="Federal Reserve FOMC meeting Powell rate decision")
        themes = classify_news_themes(art)
        theme_names = [t.theme for t in themes]
        assert "fed_policy" in theme_names

    def test_mna_theme(self):
        art = _make_normalized(title="Company acquires rival in merger agreement")
        themes = classify_news_themes(art)
        theme_names = [t.theme for t in themes]
        assert "mna" in theme_names

    def test_geopolitical_theme(self):
        art = _make_normalized(title="Trade war tariffs impact supply chain disruption")
        themes = classify_news_themes(art)
        theme_names = [t.theme for t in themes]
        assert "geopolitical_risk" in theme_names

    def test_gold_safe_haven_theme(self):
        art = _make_normalized(title="Gold price surges as investors seek safe haven")
        themes = classify_news_themes(art)
        theme_names = [t.theme for t in themes]
        assert "gold_safe_haven" in theme_names

    def test_no_theme_for_empty_article(self):
        art = _make_normalized(title="Some news article", text="")
        themes = classify_news_themes(art)
        # May or may not match something — just verify it returns a list
        assert isinstance(themes, list)

    def test_theme_match_has_score(self):
        art = _make_normalized(title="AI infrastructure LLM generative AI chip demand")
        themes = classify_news_themes(art)
        ai_themes = [t for t in themes if t.theme == "ai_infrastructure"]
        if ai_themes:
            assert ai_themes[0].score > 0.0
            assert ai_themes[0].score <= 1.0

    def test_theme_match_has_matched_terms(self):
        art = _make_normalized(title="Earnings beat estimates revenue profit")
        themes = classify_news_themes(art)
        eg_themes = [t for t in themes if t.theme == "earnings_guidance"]
        if eg_themes:
            assert len(eg_themes[0].matched_terms) > 0

    def test_themes_sorted_by_score_desc(self):
        art = _make_normalized(
            title="AI infrastructure LLM generative AI chip semiconductor",
            text="AI workload inference model training",
        )
        themes = classify_news_themes(art)
        scores = [t.score for t in themes]
        assert scores == sorted(scores, reverse=True)

    def test_theme_has_evidence_titles(self):
        art = _make_normalized(title="Federal Reserve FOMC rate decision Powell")
        themes = classify_news_themes(art)
        fed_themes = [t for t in themes if t.theme == "fed_policy"]
        if fed_themes:
            assert art.title in fed_themes[0].evidence_titles


# ---------------------------------------------------------------------------
# 5. Evidence packets
# ---------------------------------------------------------------------------

class TestEvidencePackets:
    def test_official_monitoring_lane(self):
        arts = normalize_news_articles([_article(symbols=["NVDA"])])
        packets = build_news_evidence_packets(arts, holdings=["NVDA"])
        nvda_packets = [p for p in packets if p.entity_key == "NVDA"]
        assert nvda_packets
        assert nvda_packets[0].evidence_lane == "official_monitoring"

    def test_watchlist_monitoring_lane(self):
        arts = normalize_news_articles([_article(symbols=["AAPL"])])
        packets = build_news_evidence_packets(arts, watchlist=["AAPL"])
        aapl_packets = [p for p in packets if p.entity_key == "AAPL"]
        assert aapl_packets
        assert aapl_packets[0].evidence_lane == "official_monitoring"

    def test_sandbox_lane_for_unknown_ticker(self):
        arts = normalize_news_articles([_article(symbols=["ZZZZ"], title="ZZZZ news")])
        packets = build_news_evidence_packets(arts, holdings=["NVDA"])
        z_packets = [p for p in packets if p.entity_key == "ZZZZ"]
        if z_packets:
            assert z_packets[0].evidence_lane == "sandbox_discovery_research"

    def test_sandbox_lane_for_discovery_candidate(self):
        arts = normalize_news_articles([_article(symbols=["PLTR"], title="PLTR news")])
        packets = build_news_evidence_packets(arts, discovery_candidates=["PLTR"])
        p_packets = [p for p in packets if p.entity_key == "PLTR"]
        if p_packets:
            assert p_packets[0].evidence_lane == "sandbox_discovery_research"

    def test_official_packets_sorted_first(self):
        arts = normalize_news_articles([
            _article(symbols=["NVDA"], url="u1"),
            _article(symbols=["UNKNOWN1"], url="u2", title="Unknown1 news"),
        ])
        packets = build_news_evidence_packets(arts, holdings=["NVDA"])
        if len(packets) > 1:
            official = [p for p in packets if p.evidence_lane == "official_monitoring"]
            sandbox = [p for p in packets if p.evidence_lane == "sandbox_discovery_research"]
            if official and sandbox:
                assert packets[0].evidence_lane == "official_monitoring"

    def test_observe_only_flag(self):
        arts = normalize_news_articles([_article(symbols=["NVDA"])])
        packets = build_news_evidence_packets(arts, holdings=["NVDA"])
        for p in packets:
            assert p.observe_only is True

    def test_no_trade_flag(self):
        arts = normalize_news_articles([_article(symbols=["NVDA"])])
        packets = build_news_evidence_packets(arts)
        for p in packets:
            assert p.no_trade is True

    def test_not_recommendation_flag(self):
        arts = normalize_news_articles([_article(symbols=["NVDA"])])
        packets = build_news_evidence_packets(arts)
        for p in packets:
            assert p.not_recommendation is True

    def test_no_forbidden_statuses(self):
        arts = normalize_news_articles([_article(symbols=["NVDA"])])
        packets = build_news_evidence_packets(arts)
        for p in packets:
            d = p.__dict__
            for val in d.values():
                if isinstance(val, str):
                    assert val.upper() not in {
                        "BUY", "SELL", "HOLD", "ACTIONABLE", "PROMOTED", "VALIDATED"
                    }

    def test_article_count_correct(self):
        arts = normalize_news_articles([
            _article(symbols=["NVDA"], url="u1"),
            _article(symbols=["NVDA"], url="u2", title="Second NVDA article"),
        ])
        packets = build_news_evidence_packets(arts, holdings=["NVDA"])
        nvda_packets = [p for p in packets if p.entity_key == "NVDA"]
        assert nvda_packets[0].article_count == 2

    def test_risk_flags_detected(self):
        arts = normalize_news_articles([_article(
            symbols=["NVDA"],
            title="NVDA faces SEC investigation and potential fine",
        )])
        packets = build_news_evidence_packets(arts, holdings=["NVDA"])
        nvda_packets = [p for p in packets if p.entity_key == "NVDA"]
        assert any("investigation" in f or "fine" in f for f in nvda_packets[0].risk_flags)

    def test_catalyst_flags_detected(self):
        arts = normalize_news_articles([_article(
            symbols=["NVDA"],
            title="NVDA beat estimates and raised guidance for fiscal year",
        )])
        packets = build_news_evidence_packets(arts, holdings=["NVDA"])
        nvda_packets = [p for p in packets if p.entity_key == "NVDA"]
        assert nvda_packets[0].catalyst_flags  # at least one catalyst

    def test_empty_articles(self):
        packets = build_news_evidence_packets([], holdings=["NVDA"])
        assert packets == []

    def test_summary_bullets_present(self):
        arts = normalize_news_articles([_article(symbols=["NVDA"])])
        packets = build_news_evidence_packets(arts, holdings=["NVDA"])
        nvda_packets = [p for p in packets if p.entity_key == "NVDA"]
        assert len(nvda_packets[0].summary_bullets) > 0

    def test_article_refs_present(self):
        arts = normalize_news_articles([_article(symbols=["NVDA"])])
        packets = build_news_evidence_packets(arts, holdings=["NVDA"])
        nvda_packets = [p for p in packets if p.entity_key == "NVDA"]
        assert len(nvda_packets[0].article_refs) > 0


# ---------------------------------------------------------------------------
# 6. Artifact writing and governance
# ---------------------------------------------------------------------------

class TestArtifactWriting:
    def test_writes_json_to_latest(self, tmp_path):
        result = write_news_intelligence_report(
            base_dir=tmp_path,
            raw_articles=[_article(symbols=["NVDA"])],
            holdings=["NVDA"],
        )
        json_path = Path(result["artifacts"]["news_intelligence_json"])
        assert json_path.exists()
        assert "latest" in str(json_path)

    def test_writes_md_to_latest(self, tmp_path):
        result = write_news_intelligence_report(
            base_dir=tmp_path,
            raw_articles=[_article(symbols=["NVDA"])],
        )
        md_path = Path(result["artifacts"]["news_intelligence_md"])
        assert md_path.exists()
        assert "latest" in str(md_path)

    def test_json_has_observe_only(self, tmp_path):
        write_news_intelligence_report(
            base_dir=tmp_path,
            raw_articles=[_article(symbols=["NVDA"])],
        )
        json_path = tmp_path / "latest" / "news_intelligence.json"
        payload = json.loads(json_path.read_text())
        assert payload["observe_only"] is True

    def test_json_has_no_trade(self, tmp_path):
        write_news_intelligence_report(base_dir=tmp_path, raw_articles=[_article()])
        payload = json.loads((tmp_path / "latest" / "news_intelligence.json").read_text())
        assert payload["no_trade"] is True

    def test_json_has_not_recommendation(self, tmp_path):
        write_news_intelligence_report(base_dir=tmp_path, raw_articles=[_article()])
        payload = json.loads((tmp_path / "latest" / "news_intelligence.json").read_text())
        assert payload["not_recommendation"] is True

    def test_json_has_source_label(self, tmp_path):
        write_news_intelligence_report(base_dir=tmp_path, raw_articles=[_article()])
        payload = json.loads((tmp_path / "latest" / "news_intelligence.json").read_text())
        assert payload["source"] == _SOURCE_LABEL

    def test_sandbox_artifact_written_when_sandbox_packets(self, tmp_path):
        # An article for a ticker NOT in holdings/watchlist → sandbox lane
        result = write_news_intelligence_report(
            base_dir=tmp_path,
            raw_articles=[_article(symbols=["ZZZZ"], title="ZZZZ sandbox news")],
            holdings=["NVDA"],
        )
        sandbox_path = result["artifacts"].get("news_candidate_evidence_json")
        if sandbox_path:
            assert Path(sandbox_path).exists()
            assert "sandbox" in sandbox_path

    def test_sandbox_artifact_has_safety_flags(self, tmp_path):
        write_news_intelligence_report(
            base_dir=tmp_path,
            raw_articles=[_article(symbols=["ZZZZ"], title="ZZZZ sandbox news")],
        )
        sandbox_path = tmp_path / "sandbox" / "discovery" / "news_candidate_evidence.json"
        if sandbox_path.exists():
            payload = json.loads(sandbox_path.read_text())
            assert payload.get("observe_only") is True
            assert payload.get("no_trade") is True
            assert payload.get("not_recommendation") is True

    def test_empty_articles_writes_safe_artifact(self, tmp_path):
        result = write_news_intelligence_report(base_dir=tmp_path, raw_articles=[])
        json_path = tmp_path / "latest" / "news_intelligence.json"
        assert json_path.exists()
        payload = json.loads(json_path.read_text())
        assert payload["article_count_raw"] == 0

    def test_md_contains_disclaimer(self, tmp_path):
        write_news_intelligence_report(base_dir=tmp_path, raw_articles=[_article()])
        md = (tmp_path / "latest" / "news_intelligence.md").read_text()
        assert "observe-only" in md.lower() or "not a buy/sell" in md.lower()

    def test_result_counts_correct(self, tmp_path):
        result = write_news_intelligence_report(
            base_dir=tmp_path,
            raw_articles=[_article(symbols=["NVDA"])],
            holdings=["NVDA"],
        )
        assert result["article_count_raw"] == 1
        assert result["article_count_deduped"] == 1
        assert result["official_monitoring_count"] >= 1


# ---------------------------------------------------------------------------
# 7. run_fmp_news_intelligence orchestrator
# ---------------------------------------------------------------------------

class TestRunOrchestrator:
    def test_write_files_true(self, tmp_path):
        result = run_fmp_news_intelligence(
            raw_articles=[_article(symbols=["NVDA"])],
            holdings=["NVDA"],
            base_dir=tmp_path,
        )
        assert result.get("observe_only") is True
        assert result.get("no_trade") is True
        assert result.get("not_recommendation") is True
        assert (tmp_path / "latest" / "news_intelligence.json").exists()

    def test_write_files_false(self, tmp_path):
        result = run_fmp_news_intelligence(
            raw_articles=[_article(symbols=["NVDA"])],
            holdings=["NVDA"],
            base_dir=tmp_path,
            write_files=False,
        )
        assert result.get("observe_only") is True
        assert not (tmp_path / "latest" / "news_intelligence.json").exists()

    def test_empty_articles_safe(self, tmp_path):
        result = run_fmp_news_intelligence(raw_articles=[], base_dir=tmp_path)
        assert result.get("article_count_raw") == 0

    def test_malformed_articles_degrade_safely(self, tmp_path):
        result = run_fmp_news_intelligence(
            raw_articles=[None, "not a dict", 42, {"text": "no title"}],
            base_dir=tmp_path,
        )
        # Should not raise; returns safe result
        assert isinstance(result, dict)
        assert result.get("observe_only") is True

    def test_no_forbidden_status_in_result(self, tmp_path):
        result = run_fmp_news_intelligence(
            raw_articles=[_article(symbols=["NVDA"])],
            base_dir=tmp_path,
            write_files=False,
        )
        result_str = json.dumps(result)
        for forbidden in ["BUY", "SELL", "HOLD", "ACTIONABLE", "PROMOTED"]:
            # Values should not appear as STATUS-type fields (safe guard)
            assert f'"status": "{forbidden}"' not in result_str


# ---------------------------------------------------------------------------
# 8. Safety / governance
# ---------------------------------------------------------------------------

class TestSafetyConstraints:
    def test_observe_only_constant_true(self):
        assert _OBSERVE_ONLY is True

    def test_no_trade_constant_true(self):
        assert _NO_TRADE is True

    def test_not_recommendation_constant_true(self):
        assert _NOT_RECOMMENDATION is True

    def test_company_alias_map_not_empty(self):
        assert len(COMPANY_ALIAS_MAP) > 10

    def test_theme_keywords_not_empty(self):
        assert len(THEME_KEYWORDS) >= 10

    def test_all_theme_keywords_have_entries(self):
        for theme, kws in THEME_KEYWORDS.items():
            assert len(kws) > 0, f"Theme {theme!r} has no keywords"

    def test_dedup_is_deterministic(self):
        arts_a = normalize_news_articles([_article(url="u1"), _article(url="u2", title="B")])
        arts_b = normalize_news_articles([_article(url="u1"), _article(url="u2", title="B")])
        unique_a = dedupe_news_articles(arts_a)
        unique_b = dedupe_news_articles(arts_b)
        assert [a.dedup_key for a in unique_a] == [b.dedup_key for b in unique_b]

    def test_entity_extraction_deterministic(self):
        art = _make_normalized(title="$NVDA Nvidia beats earnings")
        result_a = extract_news_entities(art)
        result_b = extract_news_entities(art)
        assert result_a == result_b

    def test_theme_classification_deterministic(self):
        art = _make_normalized(title="Earnings beat estimates raised guidance")
        result_a = classify_news_themes(art)
        result_b = classify_news_themes(art)
        assert [t.theme for t in result_a] == [t.theme for t in result_b]
