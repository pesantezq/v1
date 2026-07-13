"""Regression coverage for the bounded GUI clarity + condensation pass.

Presentation-only refactor: accurate global safety language, operator-intent
navigation (primary tabs + More disclosure), condensed Today / Portfolio /
Strategy Lab / Governance / System / Operator surfaces. These tests pin the
behaviours the pass must preserve — every route reachable, the human-approval
gate intact, no bulk approval, verb isolation, and mobile-safety — plus the
intended changes (banner wording, nav, de-duplication).

Numbered to the task's verification checklist (1..15).
"""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gui_v2.app import app

TPL = Path("gui_v2/templates")

ALL_ROUTES = [
    "/dashboard/today", "/dashboard/portfolio", "/dashboard/quant",
    "/dashboard/strategy-lab", "/dashboard/crowd-radar", "/dashboard/strategy-tax",
    "/dashboard/governance", "/dashboard/system", "/dashboard/memo",
    "/dashboard/operator",
]


@pytest.fixture()
def client():
    return TestClient(app)


# 1. All existing dashboard routes remain reachable.
@pytest.mark.parametrize("route", ALL_ROUTES)
def test_all_dashboard_routes_reachable(client, route):
    assert client.get(route).status_code == 200


# 2. Every previous nav route remains accessible from primary nav or More.
def test_every_route_present_in_shell_nav(client):
    body = client.get("/dashboard/today").text
    for route in ALL_ROUTES:
        assert f'href="{route}"' in body, f"{route} not reachable from the shell nav/More"


# 3. Mobile navigation exposes Review and Research (+ a More control for the rest).
def test_mobile_nav_exposes_review_and_research():
    bottom = (TPL / "components/bottom_nav.html").read_text()
    assert "/dashboard/governance" in bottom and "Review" in bottom
    assert "/dashboard/strategy-lab" in bottom and "Research" in bottom
    status_bar = (TPL / "components/mobile_status_bar.html").read_text()
    # secondary destinations reachable via the mobile More menu
    assert "More" in status_bar
    for href in ("/dashboard/quant", "/dashboard/crowd-radar", "/dashboard/strategy-tax",
                 "/dashboard/memo", "/dashboard/operator"):
        assert href in status_bar


# 4. The global banner no longer calls the whole application observe-only.
def test_global_banner_is_accurate_not_observe_only(client):
    body = client.get("/dashboard/today").text
    assert "No brokerage trade execution" in body
    assert "human-gated" in body
    # the old inaccurate global claim is gone
    assert "Observe-only &middot; No trade execution" not in body
    assert "Observe-only · No trade execution" not in body


# 5. Governance still clearly requires human approval.
def test_governance_requires_human_approval(client):
    body = client.get("/dashboard/governance").text
    assert "human" in body.lower()
    assert "never approves production" in body.lower() or "human-gated" in body.lower()


# 6. Governance approve/reject endpoints + forms unchanged (pinned via template + a
#    pending fixture so the decision form actually renders).
def test_governance_decision_form_contract_unchanged():
    src = (TPL / "dashboard/governance.html").read_text()
    assert 'action="/dashboard/governance/decide"' in src
    assert 'name="proposal_id"' in src
    assert 'name="decision" value="approve"' in src
    assert 'name="decision" value="reject"' in src


def test_governance_pending_proposal_renders_decision_card(monkeypatch):
    import gui_v2.app as app_module
    tmp = Path(tempfile.mkdtemp())
    (tmp / "outputs/promotion_review").mkdir(parents=True)
    (tmp / "outputs/promotion_approvals").mkdir(parents=True)
    (tmp / "outputs/promotion_review/pending_proposals.json").write_text(json.dumps({
        "proposals": [{
            "proposal_id": "prop_test1", "proposal_type": "flock_advisory_context_logic",
            "workflow": "advisory", "proposed_production_change": {"op": "set", "symbol": "CHAT"},
            "risk_summary": "Bounded reversible overlay.", "rollback_plan": "Delete overlay.",
            "approval_status": "pending",
        }]}))
    monkeypatch.setattr(app_module, "REPO_ROOT", tmp)
    body = TestClient(app_module.app).get("/dashboard/governance").text
    assert "Needs decision" in body
    assert "CHAT" in body
    # confirmation must carry symbol + humanized type, not just the raw id
    m = re.search(r"confirm\('Approve ([^']*)'\)", body)
    assert m and "CHAT" in m.group(1) and "prop_test1" in m.group(1)


# 7. No bulk / automatic approval control exists anywhere in the templates.
def test_no_bulk_approval_control():
    for p in TPL.rglob("*.html"):
        low = p.read_text().lower()
        assert "approve all" not in low, f"bulk approval in {p}"
        assert "approve_all" not in low, f"bulk approval in {p}"
        assert "auto-approve" not in low, f"auto-approve in {p}"


# 8. Today does not render the old duplicate primary-card grid; it triages instead.
def test_today_no_duplicate_primary_cards(client):
    body = client.get("/dashboard/today").text
    assert 'aria-label="Primary status cards"' not in body  # old duplicate grid removed
    assert "Run details" in body                            # remaining cards demoted here
    assert 'aria-label="Today status strip"' in body        # hero retained


# 9. Portfolio action verbs stay confined to the advisory decision queue.
def test_portfolio_verbs_confined_to_queue(client):
    body = client.get("/dashboard/portfolio").text
    m = re.search(r'aria-label="Advisory decision queue".*?</section>', body, re.DOTALL)
    assert m, "advisory decision queue section missing"
    outside = body[: body.find('aria-label="Advisory decision queue"')] + body[m.end():]
    # no directional action chip badge outside the queue section
    for verb in (">BUY<", ">SELL<", ">SCALE<"):
        assert verb not in outside, f"{verb} rendered outside the advisory decision queue"


# 10. System no longer claims there are no controls while rendering controls.
def test_system_notice_is_accurate(client):
    src = (TPL / "dashboard/system.html").read_text()
    assert "no repair or rerun controls" not in src  # the old false claim is gone
    body = client.get("/dashboard/system").text
    assert "read-only" in body.lower()  # still labels the telemetry honestly
    # controls the notice must acknowledge are in fact present
    assert 'action="/dashboard/operator/dispatch"' in body or "Operator control work orders" in body


# 11. Operator tables/cards are mobile-safe (no unguarded wide tables).
def test_operator_tables_mobile_safe():
    src = (TPL / "operator.html").read_text()
    # every <table> must sit inside a responsive_table() wrapper
    assert src.count("<table") <= src.count("responsive_table"), "unguarded table in operator.html"
    assert "responsive_table" in src


# 12. Raw source artifacts + proposal / work-order IDs remain available in details.
def test_ids_and_sources_available_in_details():
    gov = (TPL / "dashboard/governance.html").read_text()
    assert "Proposal ID" in gov and "p.proposal_id" in gov
    op = (TPL / "operator.html").read_text()
    assert "o.work_order_id" in op
    ui = (TPL / "components/_ui.html").read_text()
    assert "Source" in ui and "{{ tag }}" in ui  # section_header keeps the artifact name


# 13. HTMX refresh targets still work on the condensed pages.
@pytest.mark.parametrize("route,target", [
    ("/dashboard/today", "dashboard-today-content"),
    ("/dashboard/governance", "dashboard-governance-content"),
    ("/dashboard/system", "dashboard-system-content"),
])
def test_htmx_refresh_targets_present(client, route, target):
    body = client.get(route).text
    assert f'hx-select="#{target}"' in body or f'id="{target}"' in body


# 14. Dark / light theme model remains intact.
def test_theme_model_intact(client):
    body = client.get("/dashboard/today").text
    assert 'id="theme-toggle"' in body
    assert "data-theme" in body


# 15. Forbidden trade-execution verbs appear nowhere in the templates.
def test_no_forbidden_execution_verbs():
    forbidden = ["execute trade", "buy now", "sell now", "place order",
                 "submit order", "auto-trade", "auto trade", "rebalance now"]
    for p in TPL.rglob("*.html"):
        low = p.read_text().lower()
        for verb in forbidden:
            assert verb not in low, f"forbidden verb '{verb}' in {p}"
