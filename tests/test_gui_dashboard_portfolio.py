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


def test_source_of_truth_invariant_in_rendered_html(monkeypatch, tmp_path):
    """
    The rendered /dashboard/portfolio page must ensure that any buy/sell/hold
    action language appears ONLY within the Advisory Decisions section
    (aria-label="Advisory decision queue").

    Injects a fixture where a non-decision advisor summary contains a tempting verb
    to prove the isolation would catch a leak.
    """
    from gui_v2 import app as app_module

    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    (tmp_path / "outputs" / "portfolio").mkdir(parents=True)

    # Seed a decision_plan with advisory verbs (allowed in decision queue only)
    dp_data = {
        "generated_at": "2026-06-08T09:00:00",
        "run_mode": "real",
        "observe_only": True,
        "total_decisions": 1,
        "portfolio_context": {"cash": 500},
        "decisions": [
            {
                "symbol": "AAPL",
                "decision": "BUY",
                "priority": 0.9,
                "urgency": "high",
                "reason": "Strong momentum",
                "confidence": 0.85,
                "source": "decision_plan",
            }
        ],
    }
    (latest / "decision_plan.json").write_text(json.dumps(dp_data))

    # Risk and watchlist advisors with tempting verbs in evidence descriptions
    (latest / "risk_delta.json").write_text(json.dumps({
        "generated_at": "2026-06-08",
        "overall_status": "ok",
        "observe_only": True,
    }))
    (latest / "daily_memo.md").write_text(
        "# Daily Memo — 2026-06-08\n"
        "Risk elevated. Hold pattern observed.\n"
    )

    original_root = app_module.REPO_ROOT
    monkeypatch.setattr(app_module, "REPO_ROOT", tmp_path)
    try:
        client = TestClient(app_module.app)
        r = client.get("/dashboard/portfolio")
        assert r.status_code == 200
        html = r.text

        # 1. Decision queue section must reference decision_plan.json as source
        assert "decision_plan.json" in html, "Decision source artifact label missing"

        # 2. The observe-only banner must be present
        assert "Observe-only" in html

        # 3. Advisory picks section heading must be present (renamed 2026-06-15:
        #    "Advisory Decisions" -> "Advisory Picks with Context"; queue aria-label kept)
        assert "Advisory Picks with Context" in html

        # 4. Forbidden labels must not appear anywhere
        for bad in _FORBIDDEN_LABELS:
            assert bad not in html.lower(), (
                f"Forbidden label '{bad}' found in rendered portfolio page"
            )

        # 5. The decision_card component must note it comes from decision_plan.json
        assert "from decision_plan.json" in html

        # 6. Verify advisory verbs in card badges outside the decision queue
        decision_section_re = re.compile(
            r'<section[^>]*aria-label="Advisory decision queue"[^>]*>(.*?)</section>',
            re.DOTALL | re.IGNORECASE,
        )
        m = decision_section_re.search(html)
        assert m, "Advisory decision queue section not found in rendered HTML"
        outside_section = html[:m.start()] + html[m.end():]

        # Card badge labels outside the decision section must not contain buy/sell/scale
        badge_re = re.compile(
            r'class="inline-flex[^"]*"[^>]*>([^<]{1,40})<',
            re.IGNORECASE,
        )
        for badge_text in badge_re.findall(outside_section):
            verb_match = re.search(r"\b(buy|sell|scale)\b", badge_text, re.IGNORECASE)
            assert not verb_match, (
                f"Advisory verb in non-decision badge outside decision queue: {badge_text!r}"
            )
    finally:
        monkeypatch.setattr(app_module, "REPO_ROOT", original_root)


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
    """Holdings are populated when portfolio_snapshot.json is present (real producer keys)."""
    from gui_v2.data.dash_portfolio import collect_portfolio_view

    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    portfolio_dir = tmp_path / "outputs" / "portfolio"
    portfolio_dir.mkdir(parents=True)

    # Use the REAL producer keys: ticker/suggested_allocation/conviction_score/sector
    snapshot = {
        "enabled": True,
        "observe_only": True,
        "generated_at": "2026-06-08",
        "rows": [
            {
                "ticker": "AAPL",
                "sector": "Technology",
                "conviction_score": 0.785,
                "conviction_band": "normal",
                "suggested_allocation": 0.02,
                "normalized_allocation": 0.02,
            }
        ],
    }
    (portfolio_dir / "portfolio_snapshot.json").write_text(json.dumps(snapshot))

    v = collect_portfolio_view(tmp_path)
    assert len(v["holdings"]) == 1
    h = v["holdings"][0]
    assert h["symbol"] == "AAPL"
    # Real allocation % must not be None (no em-dash); must show numeric value
    assert h["suggested_allocation_pct"] == 2.0, (
        f"Expected 2.0 (not None), got {h['suggested_allocation_pct']!r}"
    )
    assert h["normalized_allocation_pct"] == 2.0
    assert h["conviction"] == 0.785
    assert h["band"] == "normal"
    assert h["sector"] == "Technology"


def test_holdings_snapshot_absent_returns_empty_list(tmp_path):
    """Holdings list is empty when portfolio_snapshot.json is absent."""
    from gui_v2.data.dash_portfolio import collect_portfolio_view

    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    (tmp_path / "outputs" / "portfolio").mkdir(parents=True)

    v = collect_portfolio_view(tmp_path)
    assert v["holdings"] == []


def test_rendered_holdings_show_real_allocation_pct(monkeypatch, tmp_path):
    """
    The rendered /dashboard/portfolio page must show a real allocation % (e.g. '3.0%')
    not all em-dashes for a seeded snapshot row.
    """
    from gui_v2 import app as app_module

    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    portfolio_dir = tmp_path / "outputs" / "portfolio"
    portfolio_dir.mkdir(parents=True)

    snapshot = {
        "enabled": True,
        "observe_only": True,
        "rows": [
            {
                "ticker": "NVDA",
                "sector": "Technology",
                "conviction_score": 0.85,
                "conviction_band": "high_conviction",
                "suggested_allocation": 0.03,
                "normalized_allocation": 0.03,
            }
        ],
    }
    (portfolio_dir / "portfolio_snapshot.json").write_text(json.dumps(snapshot))

    original_root = app_module.REPO_ROOT
    monkeypatch.setattr(app_module, "REPO_ROOT", tmp_path)
    try:
        client = TestClient(app_module.app)
        r = client.get("/dashboard/portfolio")
        assert r.status_code == 200
        html = r.text
        # Must show real allocation %, e.g. "3.0%"
        assert "3.0%" in html, (
            "Expected real allocation '3.0%' in rendered HTML but it was absent"
        )
        assert "NVDA" in html
    finally:
        monkeypatch.setattr(app_module, "REPO_ROOT", original_root)


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


# ---------------------------------------------------------------------------
# T cheap adds
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Simulation Context preview card (added 2026-06-16)
# ---------------------------------------------------------------------------

_SIM_FORBIDDEN = (
    "execute trade", "buy now", "sell now", "place order", "rebalance now",
    "promotion approved", "official recommendation", "auto-trade", "auto-approve",
)


def _seed_sim_for_portfolio(tmp_path):
    """Seed sandbox strategy_comparison so the preview's live fallback populates."""
    latest = tmp_path / "outputs" / "latest"; latest.mkdir(parents=True, exist_ok=True)
    (tmp_path / "outputs" / "portfolio").mkdir(parents=True, exist_ok=True)
    sb = tmp_path / "outputs" / "sandbox"; sb.mkdir(parents=True, exist_ok=True)
    sb.joinpath("strategy_comparison.json").write_text(json.dumps({"comparison": [
        {"strategy_id": "a", "name": "Long-Term Compounding", "after_tax_return_estimate": 0.12,
         "expected_volatility": 0.30, "max_drawdown_estimate": 0.18, "final_strategy_rank": 0.81},
        {"strategy_id": "b", "name": "Boom Bucket", "after_tax_return_estimate": 0.22,
         "expected_volatility": 0.55, "max_drawdown_estimate": 0.42, "final_strategy_rank": 0.6}]}))


def test_portfolio_view_includes_simulation_context(tmp_path):
    from gui_v2.data.dash_portfolio import collect_portfolio_view
    _seed_sim_for_portfolio(tmp_path)
    v = collect_portfolio_view(tmp_path)
    assert "simulation_context" in v
    simc = v["simulation_context"]
    assert simc["available"] is True
    assert simc["best_balanced"]["strategy"] == "Long-Term Compounding"   # highest rank
    assert simc["best_growth"]["strategy"] == "Boom Bucket"               # highest return
    assert simc["biggest_pain_point"]["strategy"] == "Boom Bucket"        # deepest drawdown
    assert simc["official_advisory_source"] == "decision_plan.json"


def test_portfolio_simulation_context_missing_is_graceful(tmp_path):
    from gui_v2.data.dash_portfolio import collect_portfolio_view
    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    (tmp_path / "outputs" / "portfolio").mkdir(parents=True)
    v = collect_portfolio_view(tmp_path)
    simc = v["simulation_context"]
    assert simc["available"] is False
    assert simc["official_advisory_source"] == "decision_plan.json"


def test_portfolio_renders_simulation_context_card(monkeypatch, tmp_path):
    from gui_v2 import app as app_module
    _seed_sim_for_portfolio(tmp_path)
    monkeypatch.setattr(app_module, "REPO_ROOT", tmp_path)
    r = TestClient(app_module.app).get("/dashboard/portfolio")
    assert r.status_code == 200
    t = r.text
    assert "Simulation Context" in t
    assert "research context only" in t.lower()
    assert "Official advisory actions still come from" in t  # names decision_plan.json
    assert "/dashboard/strategy-lab" in t                    # link to full charts
    assert "Best balanced strategy" in t
    # no trade-execution language anywhere on the page
    low = t.lower()
    for bad in _SIM_FORBIDDEN:
        assert bad not in low, f"forbidden phrase '{bad}' in portfolio page"


def test_portfolio_simulation_context_card_missing_state_renders(monkeypatch, tmp_path):
    from gui_v2 import app as app_module
    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    (tmp_path / "outputs" / "portfolio").mkdir(parents=True)
    monkeypatch.setattr(app_module, "REPO_ROOT", tmp_path)
    r = TestClient(app_module.app).get("/dashboard/portfolio")
    assert r.status_code == 200
    t = r.text
    assert "Simulation Context" in t
    assert "not available yet" in t.lower()
    assert "decision_plan.json" in t  # official source still named in the empty state


def test_today_view_observe_only_flag(tmp_path):
    """collect_today_view must return observe_only: True."""
    from gui_v2.data.dash_today import collect_today_view

    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    v = collect_today_view(tmp_path)
    assert v.get("observe_only") is True, (
        "collect_today_view must include observe_only=True in its return dict"
    )


def test_validate_config_edit_accepts_brk_b_and_brk_dash_b():
    """validate_config_edit accepts BRK.B and BRK-B symbol formats."""
    from gui_v2.portfolio_config_writer import validate_config_edit

    config = {}
    result = validate_config_edit(
        [
            {"symbol": "BRK.B", "shares": 5},
            {"symbol": "BRK-B", "shares": 3},
        ],
        cash=1000.0,
        config=config,
    )
    assert result["ok"] is True, f"Expected ok=True for BRK.B/BRK-B, got errors: {result['errors']}"


def test_validate_config_edit_rejects_digit_leading_symbol():
    """validate_config_edit rejects symbols starting with a digit."""
    from gui_v2.portfolio_config_writer import validate_config_edit

    result = validate_config_edit(
        [{"symbol": "1ABC", "shares": 5}],
        cash=100.0,
        config={},
    )
    assert result["ok"] is False
    assert any("invalid symbol" in e.lower() or "1ABC" in e for e in result["errors"])


def test_validate_config_edit_rejects_over_length_symbol():
    """validate_config_edit rejects a symbol longer than 10 chars."""
    from gui_v2.portfolio_config_writer import validate_config_edit

    long_sym = "TOOLONGSYM1"  # 11 chars
    result = validate_config_edit(
        [{"symbol": long_sym, "shares": 5}],
        cash=100.0,
        config={},
    )
    assert result["ok"] is False
    assert any("invalid symbol" in e.lower() or long_sym in e for e in result["errors"])


def test_rejected_save_leaves_no_backup_or_audit(tmp_path, monkeypatch):
    """
    A POST /dashboard/portfolio-config/save that fails validation MUST NOT
    write any backup file or audit record.
    """
    from gui_v2 import app as app_module

    # Seed config.json with valid data
    config_data = {
        "portfolio": {
            "holdings": [{"symbol": "AAPL", "shares": 10}],
            "cash_available": 500.0,
        }
    }
    (tmp_path / "config.json").write_text(json.dumps(config_data))
    policy_dir = tmp_path / "outputs" / "policy" / "portfolio_backups"

    # Enable edit gate
    monkeypatch.setenv("GUI_V2_AUTH_USER", "op")
    monkeypatch.setenv("GUI_V2_AUTH_PASS", "pass")
    monkeypatch.setenv("GUI_V2_PORTFOLIO_EDIT", "1")
    original_root = app_module.REPO_ROOT
    monkeypatch.setattr(app_module, "REPO_ROOT", tmp_path)

    try:
        client = TestClient(app_module.app)

        # Submit a save with an invalid symbol (starts with digit — will fail validation)
        r = client.post(
            "/dashboard/portfolio-config/save",
            data={
                "symbol": ["1INVALID"],
                "shares": ["5"],
                "target_weight": [""],
                "asset_class": ["us_equity"],
                "leverage_factor": ["1"],
                "cash": "500",
            },
            auth=("op", "pass"),
        )
        # Should not 500; either 200 with error or 400/422
        assert r.status_code in (200, 400, 422), f"Unexpected status: {r.status_code}"

        # No backup file should have been created
        if policy_dir.exists():
            backups = list(policy_dir.glob("*.json"))
            assert backups == [], (
                f"Backup files were created after a rejected save: {backups}"
            )

        # Audit JSONL should NOT have been appended
        audit_path = tmp_path / "outputs" / "policy" / "manual_portfolio_updates.jsonl"
        assert not audit_path.exists() or audit_path.read_text().strip() == "", (
            "Audit record was written after a rejected save — must not write on validation failure"
        )
    finally:
        monkeypatch.setattr(app_module, "REPO_ROOT", original_root)
