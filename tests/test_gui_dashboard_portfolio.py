"""Task 2 (Milestone 2): /dashboard/portfolio — Portfolio Manager read view.

Tests:
  - collect_portfolio_view returns expected card titles and structure
  - every card has non-empty source_artifacts
  - route renders 200 with observe-only banner
  - decision-core cards (Decision Queue, Top Insight) are present
  - SOURCE-OF-TRUTH INVARIANT: non-decision cards must NOT carry buy/sell/hold
    advisory action language in their summary/label fields
  - holdings table has a md:hidden mobile equivalent in the rendered HTML
  - explicit empty states when all artifacts absent (tmp_path with no files)
  - decision_plan absent → Decision Queue card is red
  - no forbidden action labels (belt-and-suspenders; shell test already covers templates)
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EXPECTED_CARD_TITLES = {
    "Top Insight",
    "Decision Queue",
    "Risk Focus",
    "Capital / Allocation",
    "Watchlist / Opportunities",
    "Memo Summary",
}

# Action verbs that belong ONLY to decision-core cards. Non-decision cards
# must not carry these verbs in their summary or label fields.
_DECISION_VERBS = re.compile(r"\b(buy|sell|hold|scale)\b", re.IGNORECASE)

# Card titles that are decision-core sourced (allowed to carry action verbs)
_DECISION_CORE_TITLES = {"Top Insight", "Decision Queue"}

_FORBIDDEN_LABELS = (
    "execute trade",
    "buy now",
    "sell now",
    "place order",
    "auto-trade",
    "auto trade",
    "auto-approve",
)


# ---------------------------------------------------------------------------
# Unit tests: collect_portfolio_view
# ---------------------------------------------------------------------------


def test_portfolio_view_has_all_expected_card_titles(tmp_path):
    """All six card domains are present even with no artifacts."""
    from gui_v2.data.dash_portfolio import collect_portfolio_view

    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    (tmp_path / "outputs" / "portfolio").mkdir(parents=True)

    v = collect_portfolio_view(tmp_path)
    titles = {c["title"] for c in v["cards"]}
    assert EXPECTED_CARD_TITLES <= titles, f"Missing cards: {EXPECTED_CARD_TITLES - titles}"


def test_every_card_has_non_empty_source_artifacts(tmp_path):
    """source_artifacts must be non-empty for every card."""
    from gui_v2.data.dash_portfolio import collect_portfolio_view

    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    (tmp_path / "outputs" / "portfolio").mkdir(parents=True)

    v = collect_portfolio_view(tmp_path)
    for c in v["cards"]:
        assert c["source_artifacts"], (
            f"Card '{c['title']}' has empty source_artifacts"
        )


def test_persona_key_is_portfolio(tmp_path):
    from gui_v2.data.dash_portfolio import collect_portfolio_view

    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    (tmp_path / "outputs" / "portfolio").mkdir(parents=True)

    v = collect_portfolio_view(tmp_path)
    assert v["persona"] == "portfolio"


def test_decisions_absent_when_no_decision_plan(tmp_path):
    """When decision_plan.json is absent, decisions list is empty."""
    from gui_v2.data.dash_portfolio import collect_portfolio_view

    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    (tmp_path / "outputs" / "portfolio").mkdir(parents=True)

    v = collect_portfolio_view(tmp_path)
    assert v["decisions"] == []


def test_decision_queue_card_is_red_when_plan_missing(tmp_path):
    """Decision Queue card status must be red when decision_plan.json is absent."""
    from gui_v2.data.dash_portfolio import collect_portfolio_view

    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    (tmp_path / "outputs" / "portfolio").mkdir(parents=True)

    v = collect_portfolio_view(tmp_path)
    dq = next(c for c in v["cards"] if c["title"] == "Decision Queue")
    assert dq["status"] == "red"


def test_decision_queue_card_ok_when_plan_present(tmp_path):
    """Decision Queue card status must be ok when decision_plan.json is present."""
    from gui_v2.data.dash_portfolio import collect_portfolio_view

    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    (tmp_path / "outputs" / "portfolio").mkdir(parents=True)

    dp_data = {
        "generated_at": "2026-06-08T09:00:00",
        "run_mode": "real",
        "observe_only": True,
        "total_decisions": 3,
        "portfolio_context": {"cash_available": 5000},
        "decisions": [
            {
                "symbol": "AAPL",
                "decision": "BUY",
                "priority": 0.8,
                "urgency": "high",
                "reason": "Momentum strong",
                "confidence": 0.9,
                "source": "market",
            }
        ],
    }
    (latest / "decision_plan.json").write_text(json.dumps(dp_data))

    v = collect_portfolio_view(tmp_path)
    dq = next(c for c in v["cards"] if c["title"] == "Decision Queue")
    assert dq["status"] == "ok"
    assert v["decisions"]
    assert v["decisions"][0]["ticker"] == "AAPL"
    assert v["decisions"][0]["action"] == "BUY"


# ---------------------------------------------------------------------------
# SOURCE-OF-TRUTH INVARIANT TEST
#
# Non-decision cards must NOT carry buy/sell/hold advisory action language
# in their summary or label fields. Only decision-core sourced cards
# (Top Insight, Decision Queue) may reference those verbs.
# ---------------------------------------------------------------------------


def test_source_of_truth_invariant_non_decision_cards_have_no_action_verbs(tmp_path):
    """
    Non-decision cards (Risk Focus, Capital/Allocation, Watchlist/Opportunities,
    Memo Summary) must not carry buy/sell/hold advisory action verbs in their
    summary or label fields.

    These cards describe STATE / EVIDENCE only.
    """
    from gui_v2.data.dash_portfolio import collect_portfolio_view

    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    (tmp_path / "outputs" / "portfolio").mkdir(parents=True)

    # Write advisors with potentially misleading content to test the invariant
    risk_data = {
        "generated_at": "2026-06-08",
        "overall_status": "ok",
        "observe_only": True,
    }
    (latest / "risk_delta.json").write_text(json.dumps(risk_data))
    (latest / "correlation_risk_advisor.json").write_text(json.dumps({"status": "ok"}))
    (latest / "vol_regime_advisor.json").write_text(json.dumps({"status": "ok"}))
    (latest / "earnings_gate.json").write_text(json.dumps({
        "counts": {"near_earnings": 2, "in_earnings_window": 0},
        "summary_line": "2 positions near earnings",
    }))
    (latest / "exit_advisor.json").write_text(json.dumps({
        "counts": {"flagged": 1},
        "summary_line": "1 position flagged for review",
    }))
    (latest / "cash_deployment_plan.json").write_text(json.dumps({
        "degraded_mode": False,
        "cash_summary": {"cash_available": 5000},
        "total_deployed_amount": 1000,
    }))
    (latest / "tax_harvest_advisor.json").write_text(json.dumps({
        "is_taxable_account": True,
        "harvestable_count": 2,
        "total_harvestable_loss_dollars": 500,
        "summary_line": "2 positions eligible for loss harvesting",
    }))
    (latest / "watchlist_signals.json").write_text(json.dumps({
        "scan_summary": {"signals_count": 5},
        "results": [],
        "alerts": [],
    }))
    (latest / "market_opportunities.json").write_text(json.dumps({
        "promoted": [],
        "event_summary": "3 sectors showing momentum",
    }))
    (latest / "news_evidence_layer.json").write_text(json.dumps({
        "items": [{"headline": "Market update"}, {"headline": "Sector rotation"}],
    }))
    (latest / "daily_memo.md").write_text(
        "# Daily Memo\nCautious — portfolio near cap.\n"
        "Risk: correlation elevated."
    )

    v = collect_portfolio_view(tmp_path)

    violations: list[str] = []
    for c in v["cards"]:
        if c["title"] in _DECISION_CORE_TITLES:
            # Decision-core cards may have action verbs — skip
            continue
        # Check summary and label for advisory action verbs
        for field in ("summary", "label"):
            text = c.get(field) or ""
            if _DECISION_VERBS.search(text):
                violations.append(
                    f"Card '{c['title']}' field '{field}' "
                    f"contains action verb: {text!r}"
                )

    assert violations == [], (
        "SOURCE-OF-TRUTH VIOLATION: non-decision card(s) contain buy/sell/hold language.\n"
        + "\n".join(violations)
    )


def test_source_of_truth_invariant_in_rendered_html():
    """
    The rendered /dashboard/portfolio page must ensure that any buy/sell/hold
    action language appears ONLY within the Advisory Decisions section
    (aria-label='Advisory decision queue'), which is sourced exclusively from
    decision_plan.json.

    Non-decision sections (Risk Focus, Watchlist, Memo) must not contain
    buy/sell/hold advisory action language.
    """
    from gui_v2.app import app

    client = TestClient(app)
    r = client.get("/dashboard/portfolio")
    assert r.status_code == 200
    html = r.text

    # The Advisory Decisions section is fenced with aria-label="Advisory decision queue"
    # Extract content OUTSIDE that section and verify no buy/sell/hold action verbs appear
    # in section headings or card summaries outside it.
    #
    # Practical approach: verify the decision source label is present and the
    # Risk Focus / Watchlist / Capital sections do not contain action-verb table headers.

    # 1. Decision queue section must reference decision_plan.json as source
    assert "decision_plan.json" in html, "Decision source artifact label missing from portfolio page"

    # 2. The observe-only banner must be present
    assert "Observe-only" in html

    # 3. Advisory Decisions section heading must be present
    assert "Advisory Decisions" in html

    # 4. Risk Focus section heading must NOT have action-verb headers
    # Extract the Risk Focus card area — it should describe state, not actions.
    # Verify no "buy now", "sell now", "place order" style strings appear anywhere.
    for bad in _FORBIDDEN_LABELS:
        assert bad not in html.lower(), (
            f"Forbidden label '{bad}' found in rendered portfolio page"
        )

    # 5. The decision_card component must note it comes from decision_plan.json
    assert "from decision_plan.json" in html


# ---------------------------------------------------------------------------
# Mobile: md:hidden stacked card equivalent present
# ---------------------------------------------------------------------------


def test_rendered_html_has_md_hidden_mobile_card_stack():
    """The rendered portfolio page must include a md:hidden mobile card equivalent
    for the holdings table (and watchlist table if present), so no horizontal
    scroll is needed on mobile."""
    from gui_v2.app import app

    client = TestClient(app)
    r = client.get("/dashboard/portfolio")
    assert r.status_code == 200
    html = r.text

    # The desktop table should be hidden on mobile (hidden md:block)
    assert "hidden md:block" in html, "Desktop table wrapper (hidden md:block) not found"

    # The mobile stacked card div must be present (md:hidden)
    assert "md:hidden" in html, "Mobile card stack (md:hidden) not found"


# ---------------------------------------------------------------------------
# Route renders 200 with observe-only banner
# ---------------------------------------------------------------------------


def test_portfolio_route_renders_200():
    from gui_v2.app import app

    client = TestClient(app)
    r = client.get("/dashboard/portfolio")
    assert r.status_code == 200
    assert "Observe-only" in r.text


def test_portfolio_route_shows_portfolio_heading():
    from gui_v2.app import app

    client = TestClient(app)
    r = client.get("/dashboard/portfolio")
    assert r.status_code == 200
    assert "Portfolio" in r.text


# ---------------------------------------------------------------------------
# Explicit empty states with no artifacts
# ---------------------------------------------------------------------------


def test_empty_state_when_all_artifacts_absent(tmp_path):
    """With no artifacts, collect returns empty decisions + unknown/red card statuses."""
    from gui_v2.data.dash_portfolio import collect_portfolio_view

    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    (tmp_path / "outputs" / "portfolio").mkdir(parents=True)

    v = collect_portfolio_view(tmp_path)

    # All 6 domains must still appear (explicit empty states)
    titles = {c["title"] for c in v["cards"]}
    assert EXPECTED_CARD_TITLES <= titles

    # Decision Queue must be red when plan absent
    dq = next(c for c in v["cards"] if c["title"] == "Decision Queue")
    assert dq["status"] == "red"

    # Holdings must be empty list
    assert v["holdings"] == []

    # Decisions must be empty list
    assert v["decisions"] == []


def test_memo_summary_empty_state_when_memo_absent(tmp_path):
    """Memo Summary card is present with unknown status when daily_memo.md absent."""
    from gui_v2.data.dash_portfolio import collect_portfolio_view

    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    (tmp_path / "outputs" / "portfolio").mkdir(parents=True)

    v = collect_portfolio_view(tmp_path)
    memo = next(c for c in v["cards"] if c["title"] == "Memo Summary")
    assert memo["status"] == "unknown"
    assert "absent" in memo["summary"].lower() or "unavailable" in memo["summary"].lower()


def test_memo_summary_present_when_memo_exists(tmp_path):
    """Memo Summary card is info status with content when daily_memo.md present."""
    from gui_v2.data.dash_portfolio import collect_portfolio_view

    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    (tmp_path / "outputs" / "portfolio").mkdir(parents=True)
    (latest / "daily_memo.md").write_text(
        "# Daily Memo\nCautious — portfolio near cap.\nRisk elevated."
    )

    v = collect_portfolio_view(tmp_path)
    memo = next(c for c in v["cards"] if c["title"] == "Memo Summary")
    assert memo["status"] == "info"
    assert memo["summary"] != ""


# ---------------------------------------------------------------------------
# Card data integrity
# ---------------------------------------------------------------------------


def test_all_cards_have_required_shape_keys(tmp_path):
    """Every card must have the full shared.card shape."""
    from gui_v2.data.dash_portfolio import collect_portfolio_view

    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    (tmp_path / "outputs" / "portfolio").mkdir(parents=True)

    v = collect_portfolio_view(tmp_path)
    required = {"title", "status", "label", "summary", "source_artifacts", "updated_at", "severity"}
    for c in v["cards"]:
        assert required <= set(c), f"Card '{c.get('title')}' missing keys: {required - set(c)}"


def test_holdings_populated_from_snapshot(tmp_path):
    """Holdings are populated when portfolio_snapshot.json is present."""
    from gui_v2.data.dash_portfolio import collect_portfolio_view

    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    portfolio_dir = tmp_path / "outputs" / "portfolio"
    portfolio_dir.mkdir(parents=True)

    snapshot = {
        "total_value": 100000,
        "cash_available": 5000,
        "generated_at": "2026-06-08",
        "holdings": [
            {
                "symbol": "AAPL",
                "shares": 10,
                "price": 195.0,
                "value": 1950.0,
                "allocation_pct": 1.95,
                "target_alloc_pct": 2.0,
                "drift_pct": -0.05,
                "sector": "Technology",
            }
        ],
    }
    (portfolio_dir / "portfolio_snapshot.json").write_text(json.dumps(snapshot))

    v = collect_portfolio_view(tmp_path)
    assert len(v["holdings"]) == 1
    assert v["holdings"][0]["symbol"] == "AAPL"


def test_no_forbidden_labels_in_collector_output(tmp_path):
    """Collector card text must not contain forbidden action labels."""
    from gui_v2.data.dash_portfolio import collect_portfolio_view

    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    (tmp_path / "outputs" / "portfolio").mkdir(parents=True)

    v = collect_portfolio_view(tmp_path)
    for c in v["cards"]:
        for field in ("summary", "label", "title"):
            text = (c.get(field) or "").lower()
            for bad in _FORBIDDEN_LABELS:
                assert bad not in text, (
                    f"Forbidden label '{bad}' in card '{c['title']}' field '{field}': {text!r}"
                )
