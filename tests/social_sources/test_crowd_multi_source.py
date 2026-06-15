"""
Tests for the no-extra-cost multi-source Crowd Radar lane:
connectors (ApeWisdom active, FMP/Finnhub probes, Stocktwits/Quiver blocked),
the aggregator, the runner artifacts, and governance invariants.

All network is stubbed via DI seams — no test hits a live API.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from portfolio_automation.social_intelligence.base import FORBIDDEN_TRADE_VERBS, SourceStatus
from portfolio_automation.social_intelligence.multi_source_crowd_aggregator import (
    aggregate_crowd_sources,
)
from portfolio_automation.social_sources.apewisdom_connector import ApeWisdomConnector
from portfolio_automation.social_sources.base import SourceResult
from portfolio_automation.social_sources.finnhub_social_probe import FinnhubSocialProbe
from portfolio_automation.social_sources.fmp_social_sentiment_connector import (
    FMPSocialSentimentConnector,
)
from portfolio_automation.social_sources.quiver_probe import QuiverProbe
from portfolio_automation.social_sources.stocktwits_probe import StocktwitsProbe


def _ape_page(results, pages=1):
    return {"count": len(results), "pages": pages, "current_page": 1, "results": results}


def _ape(enabled=True, **kw):
    cfg = {"enabled": enabled, "max_pages": kw.pop("max_pages", 1),
           "filters": kw.pop("filters", ["wallstreetbets"])}
    return ApeWisdomConnector(cfg, crowd_radar_enabled=True, sleep=lambda s: None, **kw)


# ----- ApeWisdom -----------------------------------------------------------

def test_apewisdom_response_parsing():
    rows = [{"rank": 1, "ticker": "GME", "name": "GameStop", "mentions": 300,
             "upvotes": 1200, "rank_24h_ago": 3, "mentions_24h_ago": 100}]
    c = _ape(http_get=lambda url: _ape_page(rows))
    raw = c.fetch()
    assert raw.status == SourceStatus.OK
    assert raw.records[0]["ticker"] == "GME"


def test_apewisdom_pagination_handling():
    calls = []
    def get(url):
        calls.append(url)
        # page count 2 → connector should request page1 then page2 then stop
        rows = [{"rank": 1, "ticker": "AMC", "name": "AMC", "mentions": 10,
                 "upvotes": 5, "rank_24h_ago": 1, "mentions_24h_ago": 8}]
        return _ape_page(rows, pages=2)
    c = _ape(max_pages=5, http_get=get)
    c.fetch()
    # stops at total pages=2 even though max_pages=5
    assert any("/page/2" in u for u in calls)
    assert not any("/page/3" in u for u in calls)


def test_apewisdom_velocity_metrics():
    rows = [{"rank": 2, "ticker": "TSLA", "name": "Tesla", "mentions": 200,
             "upvotes": 400, "rank_24h_ago": 6, "mentions_24h_ago": 50}]
    c = _ape(http_get=lambda url: _ape_page(rows))
    rec = c.normalize(c.fetch()).records[0]
    assert rec["mention_delta_24h"] == 150
    assert rec["mention_velocity_ratio"] == 4.0
    assert rec["rank_change_24h"] == 4
    assert rec["upvote_per_mention"] == 2.0


def test_apewisdom_degraded_response_does_not_crash():
    def boom(url):
        raise ConnectionError("network down")
    c = _ape(http_get=boom)
    raw = c.fetch()  # must not raise
    assert raw.status in (SourceStatus.DEGRADED, SourceStatus.INSUFFICIENT_DATA)
    assert raw.records == []


# ----- FMP social sentiment ------------------------------------------------

def _fmp(status, body=None, msg="", budget=False):
    return FMPSocialSentimentConnector(
        {"enabled": True, "entitlement_probe_only_until_confirmed": True},
        crowd_radar_enabled=True, api_key="KEY",
        http_get_status=lambda url: (status, body, msg),
        budget_exhausted=lambda: budget,
    )


def test_fmp_social_entitlement_success():
    rows = [{"date": "2026-06-14", "symbol": "AAPL", "sentiment": 0.4}]
    res = _fmp(200, rows).probe()
    assert res.status == SourceStatus.OK
    assert res.meta["entitled"] is True


def test_fmp_social_not_entitled_handling():
    res = _fmp(403, None, "Access denied").probe()
    assert res.status == SourceStatus.NOT_ENTITLED
    assert res.meta["entitled"] is False


def test_fmp_budget_exhaustion_surfaced():
    res = _fmp(200, [{"x": 1}], budget=True).probe()
    assert res.status == SourceStatus.BUDGET_EXHAUSTED  # not empty/not_entitled


def test_fmp_empty_body_is_not_entitled():
    res = _fmp(200, []).probe()
    assert res.status == SourceStatus.NOT_ENTITLED


# ----- Stocktwits / Finnhub / Quiver probes --------------------------------

def test_stocktwits_absent_config_not_configured(monkeypatch):
    monkeypatch.delenv("STOCKTWITS_TOKEN", raising=False)
    monkeypatch.delenv("STOCKTWITS_API_KEY", raising=False)
    res = StocktwitsProbe({"enabled": False, "probe_only": True}, crowd_radar_enabled=True).probe()
    assert res.status in (SourceStatus.NOT_CONFIGURED, SourceStatus.REQUIRES_MANUAL_REVIEW)
    assert res.meta["network_called"] is False


def test_finnhub_absent_key_no_credentials(monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    res = FinnhubSocialProbe({"enabled": False, "probe_only": True},
                             crowd_radar_enabled=True, api_key="").probe()
    assert res.status == SourceStatus.NO_CREDENTIALS


def test_finnhub_premium_rejection_not_entitled():
    res = FinnhubSocialProbe(
        {"probe_only": True}, crowd_radar_enabled=True, api_key="KEY",
        http_get_status=lambda url: (403, None, "You don't have access to this resource."),
    ).probe()
    assert res.status == SourceStatus.NOT_ENTITLED


def test_quiver_blocked_when_paid_disallowed(monkeypatch):
    monkeypatch.delenv("QUIVER_API_KEY", raising=False)
    res = QuiverProbe({"blocked_no_extra_cost": True}, crowd_radar_enabled=True,
                      allow_paid_sources=False).probe()
    assert res.status == SourceStatus.BLOCKED_NO_EXTRA_COST
    assert res.meta["network_called"] is False


# ----- Aggregator ----------------------------------------------------------

def _norm(source, records, status=SourceStatus.OK):
    return SourceResult(source, status, records=records)


def test_aggregator_zero_active_sources():
    agg = aggregate_crowd_sources([])
    assert agg["record_count"] == 0
    assert "no_active_sources" in agg["labels"]


def test_aggregator_one_active_source_labels_low_breadth():
    rec = {"ticker": "GME", "mention_velocity_ratio": 3.0, "mention_delta_24h": 200,
           "upvote_per_mention": 4.0}
    agg = aggregate_crowd_sources([_norm("apewisdom", [rec])])
    assert agg["record_count"] == 1
    out = agg["records"][0]
    assert "low_source_breadth" in out["labels"]
    assert "mention_velocity_only" in out["labels"]
    assert out["sentiment_score_if_available"] is None


def test_aggregator_partial_failure_ignores_failed_source():
    good = _norm("apewisdom", [{"ticker": "AMC", "mention_velocity_ratio": 2.0, "mention_delta_24h": 10}])
    bad = SourceResult("fmp_social_sentiment", SourceStatus.NOT_ENTITLED, records=[])
    agg = aggregate_crowd_sources([good, bad])
    assert agg["contributing_sources"] == ["apewisdom"]
    assert agg["record_count"] == 1


# ----- Runner artifacts + governance ---------------------------------------

def _write_config(root: Path, enabled: bool):
    cfg = {"crowd_radar": {
        "enabled": enabled, "cost_policy": "no_extra_cost", "allow_paid_sources": False,
        "source_policy": {
            "apewisdom": {"enabled": True, "max_pages": 1, "filters": ["wallstreetbets"]},
            "fmp_social_sentiment": {"enabled": True, "entitlement_probe_only_until_confirmed": True},
            "stocktwits": {"enabled": False, "probe_only": True},
            "finnhub_social": {"enabled": False, "probe_only": True},
            "quiver_wsb": {"enabled": False, "blocked_no_extra_cost": True},
        },
    }}
    (root / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


def test_runner_artifacts_include_required_metadata(tmp_path, monkeypatch):
    for k in ("FMP_API_KEY", "FINNHUB_API_KEY", "QUIVER_API_KEY", "STOCKTWITS_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    _write_config(tmp_path, enabled=False)
    from portfolio_automation.social_sources.run_multi_source_crowd import run_multi_source_crowd
    run_multi_source_crowd(root=tmp_path, run_mode="discovery")

    for rel in ("crowd_source_dev_doc_audit.json", "crowd_source_health.json",
                "crowd_multi_source_velocity.json"):
        p = tmp_path / "outputs" / "sandbox" / "discovery" / rel
        assert p.exists(), f"missing {rel}"
        data = json.loads(p.read_text())
        for field in ("run_id", "run_mode", "created_at", "schema_version", "source_status",
                      "warnings", "records"):
            assert field in data, f"{rel} missing {field}"


def test_runner_outputs_only_in_sandbox_discovery(tmp_path, monkeypatch):
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    _write_config(tmp_path, enabled=False)
    # seed official artifacts
    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    decision = tmp_path / "outputs" / "latest" / "decision_plan.json"
    decision.write_text('{"x": "UNTOUCHED"}', encoding="utf-8")
    before = decision.read_bytes()

    from portfolio_automation.social_sources.run_multi_source_crowd import run_multi_source_crowd
    run_multi_source_crowd(root=tmp_path, run_mode="discovery")

    assert decision.read_bytes() == before  # never touches official outputs
    # writes land only under outputs/sandbox/discovery/
    sandbox = tmp_path / "outputs" / "sandbox" / "discovery"
    assert sandbox.exists()


def test_crowd_outputs_have_no_trade_verbs(tmp_path, monkeypatch):
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    _write_config(tmp_path, enabled=False)
    from portfolio_automation.social_sources.run_multi_source_crowd import run_multi_source_crowd
    run_multi_source_crowd(root=tmp_path, run_mode="discovery")

    blob = ""
    for p in (tmp_path / "outputs" / "sandbox" / "discovery").glob("*.json"):
        blob += p.read_text().lower()
    # No forbidden trade verb may appear as a recommended action token.
    for verb in ("buy", "sell", "hold", "rebalance", "promote", "trim", "scale"):
        assert verb in FORBIDDEN_TRADE_VERBS  # guard the guard
        assert f'"recommended_next_step": "{verb}"' not in blob
