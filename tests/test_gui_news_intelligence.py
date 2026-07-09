"""GUI backlog — surface news_intelligence.json on the Portfolio tab.

news_intelligence.json holds per-entity news evidence packets (themes, risk /
catalyst flags, sentiment, summary bullets) split into official_monitoring vs
sandbox lanes. It was consumed by nothing in the GUI (distinct from the rendered
news_evidence_layer). Surface a compact, observe-only research section: counts
header + top packets sorted by flag-relevance then article count, capped with an
honest "showing N of M".
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path


def _view(payload: dict | None) -> dict:
    from gui_v2.data.dash_portfolio import collect_portfolio_view

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        latest = root / "outputs" / "latest"
        latest.mkdir(parents=True)
        if payload is not None:
            (latest / "news_intelligence.json").write_text(json.dumps(payload), encoding="utf-8")
        return collect_portfolio_view(root)


def _packet(entity, articles, sentiment, risk=None, catalyst=None):
    return {
        "entity_key": entity, "entity_type": "ticker", "related_tickers": [entity],
        "article_count": articles, "source_count": 3,
        "latest_published_at": "2026-07-09 04:22:54",
        "themes": ["earnings_guidance"], "risk_flags": risk or [],
        "catalyst_flags": catalyst or [], "sentiment_hint": sentiment,
        "summary_bullets": [f"{entity} headline"], "evidence_lane": "official_monitoring",
    }


_PAYLOAD = {
    "observe_only": True, "evidence_packet_count": 3,
    "official_monitoring_count": 2, "sandbox_count": 1,
    "article_count_deduped": 12,
    "evidence_packets": [
        _packet("MSFT", 7, "negative", risk=["class action"]),
        _packet("AAPL", 9, "positive"),
        _packet("NVDA", 3, "neutral", catalyst=["product launch"]),
    ],
}


def test_news_intelligence_view_shaped_and_sorted():
    ni = _view(_PAYLOAD).get("news_intelligence")
    assert ni and ni["available"] is True
    assert ni["entity_count"] == 3
    assert ni["official_count"] == 2 and ni["sandbox_count"] == 1
    assert ni["article_count"] == 12
    # flagged packets (MSFT risk, NVDA catalyst) sort ahead of unflagged AAPL,
    # then by article_count. MSFT(7,risk) & NVDA(3,catalyst) before AAPL(9,none).
    order = [p["entity"] for p in ni["packets"]]
    assert order.index("MSFT") < order.index("AAPL")
    assert order.index("NVDA") < order.index("AAPL")


def test_sentiment_severity_mapping():
    ni = _view(_PAYLOAD)["news_intelligence"]
    by = {p["entity"]: p for p in ni["packets"]}
    assert by["MSFT"]["sentiment_sev"] == "red"      # negative
    assert by["AAPL"]["sentiment_sev"] == "green"    # positive
    assert by["NVDA"]["sentiment_sev"] == "gray"     # neutral


def test_absent_artifact_yields_unavailable():
    ni = _view(None).get("news_intelligence")
    assert ni is None or ni.get("available") is False


def test_news_section_renders():
    from gui_v2.app import templates
    v = _view(_PAYLOAD)
    html = templates.env.get_template("dashboard/portfolio.html").render(**v)
    assert "News Intelligence" in html
    assert "MSFT" in html
    assert "class action" in html
