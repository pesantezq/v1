"""Tests for portfolio_automation.discovery.event_classifier."""
import pytest

from portfolio_automation.discovery.event_classifier import (
    ClassificationResult,
    EventType,
    classify_event,
    classify_record,
)


# ---------------------------------------------------------------------------
# 1. Each event type fires correctly
# ---------------------------------------------------------------------------

class TestEventTypeClassification:
    def test_earnings(self):
        r = classify_event("Company beats earnings estimates with strong quarterly results")
        assert r.event_type == EventType.EARNINGS

    def test_guidance(self):
        r = classify_event("Company raises guidance and updates its full-year outlook forecast")
        assert r.event_type == EventType.GUIDANCE

    def test_analyst_action_upgrade(self):
        r = classify_event("Analyst upgrades stock with new price target and overweight rating")
        assert r.event_type == EventType.ANALYST_ACTION

    def test_analyst_action_downgrade(self):
        r = classify_event("Analyst downgrades to neutral with lowered target and hold rating")
        assert r.event_type == EventType.ANALYST_ACTION

    def test_product_launch(self):
        r = classify_event("Company launches new product and unveils new model to the market")
        assert r.event_type == EventType.PRODUCT_LAUNCH

    def test_partnership(self):
        r = classify_event("Companies announce strategic alliance and partnership agreement")
        assert r.event_type == EventType.PARTNERSHIP

    def test_regulatory(self):
        r = classify_event("FDA approval granted after regulatory review clearance process")
        assert r.event_type == EventType.REGULATORY

    def test_macro_theme(self):
        r = classify_event("Federal reserve interest rate hike affects inflation and monetary policy")
        assert r.event_type == EventType.MACRO_THEME

    def test_merger_acquisition(self):
        r = classify_event("Company agreed to acquire rival in merger takeover deal valued at billions")
        assert r.event_type == EventType.MERGER_ACQUISITION

    def test_legal_risk(self):
        r = classify_event("Company faces class action lawsuit and securities fraud litigation")
        assert r.event_type == EventType.LEGAL_RISK

    def test_financing(self):
        r = classify_event("Company raised funding with equity offering and capital raise")
        assert r.event_type == EventType.FINANCING

    def test_management_change(self):
        r = classify_event("New CEO appointed as previous chief executive steps down")
        assert r.event_type == EventType.MANAGEMENT_CHANGE


# ---------------------------------------------------------------------------
# 2. Unknown classification
# ---------------------------------------------------------------------------

class TestUnknownClassification:
    def test_no_keywords_returns_unknown(self):
        r = classify_event("Stock moved today in a notable way")
        assert r.event_type == EventType.UNKNOWN

    def test_empty_string_returns_unknown(self):
        r = classify_event("")
        assert r.event_type == EventType.UNKNOWN
        assert r.confidence == 0.0
        assert r.matched_keywords == []

    def test_whitespace_only_returns_unknown(self):
        r = classify_event("   ")
        assert r.event_type == EventType.UNKNOWN

    def test_unknown_confidence_zero(self):
        r = classify_event("Nothing relevant here")
        assert r.confidence == 0.0


# ---------------------------------------------------------------------------
# 3. Confidence scoring
# ---------------------------------------------------------------------------

class TestConfidenceScoring:
    def test_more_matches_higher_confidence(self):
        single = classify_event("earnings reported today")
        multi = classify_event("earnings revenue quarterly results profit net income")
        assert multi.confidence >= single.confidence

    def test_confidence_max_one(self):
        r = classify_event(
            "earnings revenue quarterly results profit net income fiscal year q1 q2 q3 q4"
        )
        assert r.confidence <= 1.0

    def test_confidence_above_zero_when_matched(self):
        r = classify_event("earnings beat this quarter")
        assert r.confidence > 0.0

    def test_confidence_formula(self):
        # min(match_count * 0.25, 1.0)
        r = classify_event("earnings")
        assert r.confidence == pytest.approx(0.25, abs=0.01)


# ---------------------------------------------------------------------------
# 4. risk_flag for legal risk
# ---------------------------------------------------------------------------

class TestRiskFlag:
    def test_legal_risk_sets_risk_flag(self):
        r = classify_event("Company faces class action lawsuit for securities fraud")
        assert r.risk_flag is True

    def test_regulatory_with_investigation_sets_risk_flag(self):
        r = classify_event("clearance compliance regulator antitrust investigation penalty fine")
        assert r.event_type == EventType.REGULATORY
        assert r.risk_flag is True

    def test_regulatory_without_negative_keywords_no_risk_flag(self):
        r = classify_event("FDA approval granted for new drug clearance regulatory")
        # regulatory without negative keywords → risk_flag False
        assert r.risk_flag is False

    def test_earnings_no_risk_flag(self):
        r = classify_event("Company beats earnings estimates this quarter")
        assert r.risk_flag is False

    def test_analyst_action_no_risk_flag(self):
        r = classify_event("Analyst upgrades to overweight with new price target")
        assert r.risk_flag is False

    def test_macro_theme_no_risk_flag(self):
        r = classify_event("Federal reserve interest rate cut announced")
        assert r.risk_flag is False

    def test_risk_flag_false_for_unknown(self):
        r = classify_event("Nothing here")
        assert r.risk_flag is False


# ---------------------------------------------------------------------------
# 5. matched_keywords populated
# ---------------------------------------------------------------------------

class TestMatchedKeywords:
    def test_matched_keywords_not_empty_when_classified(self):
        r = classify_event("Company beats earnings this quarter")
        assert len(r.matched_keywords) > 0

    def test_matched_keywords_are_strings(self):
        r = classify_event("Company beats earnings this quarter")
        assert all(isinstance(kw, str) for kw in r.matched_keywords)

    def test_matched_keywords_empty_for_unknown(self):
        r = classify_event("Nothing relevant here")
        assert r.matched_keywords == []


# ---------------------------------------------------------------------------
# 6. classify_record
# ---------------------------------------------------------------------------

class TestClassifyRecord:
    def test_classify_record_uses_title(self):
        record = {"title": "Company beats earnings estimates quarterly results"}
        r = classify_record(record)
        assert r.event_type == EventType.EARNINGS

    def test_classify_record_uses_summary(self):
        record = {"summary": "Company agreed to acquire rival in merger deal"}
        r = classify_record(record)
        assert r.event_type == EventType.MERGER_ACQUISITION

    def test_classify_record_combines_title_and_summary(self):
        record = {
            "title": "Company news",
            "summary": "earnings revenue quarterly results beat profit",
        }
        r = classify_record(record)
        assert r.event_type == EventType.EARNINGS

    def test_classify_record_empty_dict_returns_unknown(self):
        r = classify_record({})
        assert r.event_type == EventType.UNKNOWN

    def test_classify_record_none_fields_handled(self):
        record = {"title": None, "summary": None}
        r = classify_record(record)
        assert r.event_type == EventType.UNKNOWN

    def test_classify_record_result_type(self):
        r = classify_record({"title": "earnings beat"})
        assert isinstance(r, ClassificationResult)


# ---------------------------------------------------------------------------
# 7. ClassificationResult shape
# ---------------------------------------------------------------------------

class TestClassificationResultShape:
    def test_has_event_type(self):
        r = classify_event("earnings results quarterly")
        assert isinstance(r.event_type, EventType)

    def test_has_confidence(self):
        r = classify_event("earnings results quarterly")
        assert isinstance(r.confidence, float)

    def test_has_matched_keywords(self):
        r = classify_event("earnings results quarterly")
        assert isinstance(r.matched_keywords, list)

    def test_has_risk_flag(self):
        r = classify_event("earnings results quarterly")
        assert isinstance(r.risk_flag, bool)
