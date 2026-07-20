import copy

from portfolio_automation import memo_datasets as md


def _sources():
    return {
        "daily_capital_plan": {"available": True, "capital_summary": {
            "funded_capital": {"amount": 104.0, "state": "confirmed"},
            "funded_count": 2, "deferred_count": 20},
            "bottom_line": "You have $104 to deploy today."},
        "system_decision_summary": {"top_theme": {"label": "Energy Transition"},
            "top_opportunity": {"ticker": "MSFT"}},
        "decision_plan": {"decisions": [{"decision": "BUY"}, {"decision": "SELL"}]},
        "risk_delta": {"overall_status": "ok", "concentration": {"top_position":
            {"symbol": "QQQ", "weight": 0.42, "cap": 0.6}}, "leverage": {"total_exposure": 0.145}},
        "correlation_risk_advisor": {"effective_independent_bets": 1.23},
        "unified_crowd_status": {"overall_status": "ok", "state_counts":
            {"market_context_only": 27}, "top_confirmed_attention": [{"ticker": "AAPL"}]},
        "watch_candidates": {"candidates": [{"symbol": "XOM"}]},
        "institutional_intelligence": {"records": [{"symbol": "BE",
            "consensus_state": "moderate_accumulation", "filing_age_days": 24}]},
        "daily_run_status": {"overall_status": "ok", "content_warn_count": 0},
    }


def test_build_produces_all_five_domains():
    d = md.build_memo_datasets(_sources())
    assert set(d["domains"]) == set(md.DOMAINS)
    assert d["feeds_decision_engine"] is False and d["observe_only"] is True
    port = d["domains"]["portfolio"]
    assert port["status"] == "ok" and port["sections"]
    assert any("104" in ln for s in port["sections"] for ln in s["lines"])


def test_missing_source_degrades_only_that_domain():
    s = _sources(); del s["risk_delta"]; del s["correlation_risk_advisor"]
    d = md.build_memo_datasets(s)
    assert d["domains"]["risk"]["status"] == "unavailable"
    assert d["domains"]["portfolio"]["status"] == "ok"        # others intact


def test_institutional_inert_is_unavailable_not_error():
    s = _sources(); s["institutional_intelligence"] = {"records": []}
    inst = md.build_memo_datasets(s)["domains"]["institutional"]
    assert inst["status"] == "unavailable" and inst["warnings"]


def test_no_mutation_of_inputs():
    s = _sources(); before = copy.deepcopy(s)
    md.build_memo_datasets(s)
    assert s == before


def test_deterministic():
    s = _sources()
    a = md.build_memo_datasets(s, generated_at="t"); b = md.build_memo_datasets(s, generated_at="t")
    assert a == b


def test_domains_filter():
    d = md.build_memo_datasets(_sources(), domains=["risk"])
    assert list(d["domains"]) == ["risk"]
