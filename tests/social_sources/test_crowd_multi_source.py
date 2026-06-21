"""
Tests for the no-extra-cost multi-source Crowd Radar lane.

Updated 2026-06-21: removed dead probe tests (FMP/Finnhub/Stocktwits/Quiver
were probe-only with no runtime value; their adapters have been deleted). Kept:
- ApeWisdom connector tests
- multi-source aggregator tests
- runner artifact + governance tests
- no-trade-verb invariant

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
        rows = [{"rank": 1, "ticker": "AMC", "name": "AMC", "mentions": 10,
                 "upvotes": 5, "rank_24h_ago": 1, "mentions_24h_ago": 8}]
        return _ape_page(rows, pages=2)
    c = _ape(max_pages=5, http_get=get)
    c.fetch()
    assert len(calls) == 2  # stops at pages=2 even though max_pages=5


def test_apewisdom_disabled_returns_inert():
    c = ApeWisdomConnector({"enabled": False}, crowd_radar_enabled=True)
    assert c.fetch().status == SourceStatus.DISABLED


def test_apewisdom_crowd_radar_disabled_returns_inert():
    c = ApeWisdomConnector({"enabled": True}, crowd_radar_enabled=False)
    assert c.fetch().status == SourceStatus.DISABLED


def test_apewisdom_error_returns_error_status():
    def bad(url):
        raise ConnectionError("timeout")
    c = _ape(http_get=bad)
    assert c.fetch().status in (SourceStatus.ERROR, SourceStatus.DEGRADED,
                                 SourceStatus.INSUFFICIENT_DATA)


def test_apewisdom_normalize_deduplicates_per_ticker():
    rows = [
        {"rank": 1, "ticker": "GME", "name": "GME", "mentions": 100,
         "upvotes": 500, "rank_24h_ago": 2, "mentions_24h_ago": 50, "source_filter": "wsb"},
        {"rank": 2, "ticker": "GME", "name": "GME", "mentions": 50,
         "upvotes": 200, "rank_24h_ago": 3, "mentions_24h_ago": 40, "source_filter": "stocks"},
    ]
    c = _ape(http_get=lambda url: _ape_page(rows, pages=1))
    raw = c.fetch()
    normalized = c.normalize(raw)
    gme_records = [r for r in normalized.records if r["ticker"] == "GME"]
    assert len(gme_records) == 1  # merged
    assert gme_records[0]["mentions"] == 150  # summed


def test_apewisdom_is_configured():
    c_ok = ApeWisdomConnector({"enabled": True}, crowd_radar_enabled=True)
    c_no = ApeWisdomConnector({"enabled": False}, crowd_radar_enabled=True)
    assert c_ok.is_configured()
    assert not c_no.is_configured()


# ----- Source health factory -----------------------------------------------

def test_build_sources_returns_apewisdom():
    from portfolio_automation.social_sources.source_health import build_sources
    cfg = {"enabled": True, "source_policy": {"apewisdom": {"enabled": True}}}
    sources = build_sources(cfg)
    assert "apewisdom" in sources


def test_build_sources_text_connectors_when_available():
    """Text connectors (bluesky/mastodon/lemmy) appear when import succeeds."""
    from portfolio_automation.social_sources.source_health import build_sources
    cfg = {
        "enabled": True,
        "source_policy": {
            "apewisdom": {"enabled": True},
            "bluesky": {"enabled": True},
            "mastodon": {"enabled": True},
            "lemmy": {"enabled": True},
        },
    }
    sources = build_sources(cfg)
    # At minimum ApeWisdom is always present
    assert "apewisdom" in sources
    # Text connectors should be present (they're installed)
    for name in ("bluesky", "mastodon", "lemmy"):
        assert name in sources, f"Expected {name} in sources"


def test_no_paid_probes_in_build_sources():
    """Phase 2: paid probes must not appear in build_sources output."""
    from portfolio_automation.social_sources.source_health import build_sources
    cfg = {"enabled": True, "source_policy": {}}
    sources = build_sources(cfg)
    for dead in ("fmp_social_sentiment", "finnhub_social", "stocktwits", "quiver_wsb"):
        assert dead not in sources, f"Deleted probe '{dead}' still in build_sources"


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
    good = _norm("apewisdom", [{"ticker": "AMC", "mention_velocity_ratio": 2.0,
                                 "mention_delta_24h": 10}])
    bad = SourceResult("some_failed_source", SourceStatus.NOT_ENTITLED, records=[])
    agg = aggregate_crowd_sources([good, bad])
    assert agg["contributing_sources"] == ["apewisdom"]
    assert agg["record_count"] == 1


# ----- Dev-doc audit -------------------------------------------------------

def test_dev_doc_audit_only_free_sources():
    from portfolio_automation.social_sources.dev_doc_audit import SOURCE_AUDIT
    for s in SOURCE_AUDIT:
        assert s["allowed_under_no_extra_cost"] is True, (
            f"Source {s['source_name']} in audit but allowed_under_no_extra_cost=False"
        )
        assert s["implementation_status"] == "active"


def test_dev_doc_audit_removed_sources_not_present():
    from portfolio_automation.social_sources.dev_doc_audit import SOURCE_AUDIT
    names = {s["source_name"] for s in SOURCE_AUDIT}
    for removed in ("fmp_social_sentiment", "finnhub_social", "stocktwits", "quiver_wsb"):
        assert removed not in names, f"Removed source {removed} still in dev_doc_audit"


def test_dev_doc_audit_new_free_sources_present():
    from portfolio_automation.social_sources.dev_doc_audit import SOURCE_AUDIT
    names = {s["source_name"] for s in SOURCE_AUDIT}
    for expected in ("apewisdom", "bluesky", "mastodon", "lemmy"):
        assert expected in names, f"Expected free source {expected} missing from dev_doc_audit"


# ----- Runner artifacts + governance ---------------------------------------

def _write_config(root: Path, enabled: bool):
    cfg = {"crowd_radar": {
        "enabled": enabled, "cost_policy": "no_extra_cost", "allow_paid_sources": False,
        "source_policy": {
            "apewisdom": {"enabled": True, "max_pages": 1, "filters": ["wallstreetbets"]},
            "bluesky": {"enabled": False},  # disabled to avoid real network in tests
            "mastodon": {"enabled": False},
            "lemmy": {"enabled": False},
        },
    }}
    (root / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


def test_runner_artifacts_include_required_metadata(tmp_path, monkeypatch):
    _write_config(tmp_path, enabled=False)
    from portfolio_automation.social_sources.run_multi_source_crowd import run_multi_source_crowd
    run_multi_source_crowd(root=tmp_path, run_mode="discovery")

    for rel in ("crowd_source_dev_doc_audit.json", "crowd_source_health.json",
                "crowd_multi_source_velocity.json"):
        p = tmp_path / "outputs" / "sandbox" / "discovery" / rel
        assert p.exists(), f"missing {rel}"
        data = json.loads(p.read_text())
        for field in ("run_id", "run_mode", "created_at", "schema_version", "source_status",
                      "warnings"):
            assert field in data, f"{rel} missing {field}"


def test_runner_outputs_only_in_sandbox_discovery(tmp_path, monkeypatch):
    _write_config(tmp_path, enabled=False)
    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    decision = tmp_path / "outputs" / "latest" / "decision_plan.json"
    decision.write_text('{"x": "UNTOUCHED"}', encoding="utf-8")
    before = decision.read_bytes()

    from portfolio_automation.social_sources.run_multi_source_crowd import run_multi_source_crowd
    run_multi_source_crowd(root=tmp_path, run_mode="discovery")

    assert decision.read_bytes() == before  # never touches production outputs
    sandbox = tmp_path / "outputs" / "sandbox" / "discovery"
    assert sandbox.exists()


def test_crowd_outputs_have_no_trade_verbs(tmp_path, monkeypatch):
    _write_config(tmp_path, enabled=False)
    from portfolio_automation.social_sources.run_multi_source_crowd import run_multi_source_crowd
    run_multi_source_crowd(root=tmp_path, run_mode="discovery")

    blob = ""
    for p in (tmp_path / "outputs" / "sandbox" / "discovery").glob("*.json"):
        blob += p.read_text().lower()
    for verb in ("buy", "sell", "hold", "rebalance", "promote", "trim", "scale"):
        assert verb in FORBIDDEN_TRADE_VERBS
        assert f'"recommended_next_step": "{verb}"' not in blob
