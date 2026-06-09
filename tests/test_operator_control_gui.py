"""GUI tests for the operator-control plane integration.

Covers: per-tab rendering of operator actions, create-only POST flow, no
arbitrary-command input, no trade/broker controls, proposal-only labeling on
the quant tab, advisory/read-only portfolio tab, and clean empty-state.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client_root(tmp_path, monkeypatch):
    """A TestClient whose REPO_ROOT is an isolated tmp dir (no real artifacts)."""
    from gui_v2 import app as app_module

    (tmp_path / "outputs" / "latest").mkdir(parents=True)
    monkeypatch.setattr(app_module, "REPO_ROOT", tmp_path)
    return TestClient(app_module.app), tmp_path


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_system_tab_renders_operator_actions(client_root):
    client, _ = client_root
    body = client.get("/dashboard/system").text
    assert "Operator control work orders" in body
    # A system probe + its diagnose action button.
    assert "Daily run" in body
    assert "Diagnose" in body
    # The action button submits the registry-derived skill id.
    assert "diagnose_daily_run_failure" in body


def test_quant_tab_labels_proposal_only(client_root):
    client, _ = client_root
    body = client.get("/dashboard/quant").text
    assert "Operator control work orders" in body
    # The operator panel's proposal-only banner must be present on quant.
    assert "Proposal-only" in body
    assert "decision_plan.json" in body


def test_portfolio_tab_actions_are_review_only(client_root):
    client, _ = client_root
    body = client.get("/dashboard/portfolio").text
    assert "Operator control work orders" in body
    # No buy/sell/trade/execute affordance introduced by the operator panel.
    # Restrict to forms (the operator panel uses <form> per action button).
    for action in re.findall(r'<form[^>]*action="([^"]*)"', body, re.IGNORECASE):
        assert not re.search(r"(buy|sell|order|trade|execute)", action, re.IGNORECASE)


def test_memo_tab_renders_operator_actions(client_root):
    client, _ = client_root
    body = client.get("/dashboard/memo").text
    assert "Operator control work orders" in body


def test_today_tab_shows_work_order_summary(client_root):
    client, _ = client_root
    body = client.get("/dashboard/today").text
    assert "Operator work orders" in body


# ---------------------------------------------------------------------------
# Create-only POST flow
# ---------------------------------------------------------------------------


def test_create_work_order_via_post_then_visible(client_root):
    client, root = client_root
    resp = client.post(
        "/dashboard/operator/create",
        data={
            "probe_id": "data_quality.warnings",
            "skill_id": "diagnose_data_quality_warnings",
            "mode": "diagnose",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/system"

    body = client.get("/dashboard/system").text
    assert "data_quality.warnings" in body
    # Stored on disk (append-only artifact), not just in memory.
    from operator_control import work_orders_path
    assert work_orders_path(root).exists()


def test_post_rejects_non_allowlisted_combo(client_root):
    client, _ = client_root
    resp = client.post(
        "/dashboard/operator/create",
        data={
            "probe_id": "data_quality.warnings",
            "skill_id": "inspect_artifact_registry",
            "mode": "diagnose",
        },
    )
    assert resp.status_code == 400


def test_post_rejects_unknown_probe(client_root):
    client, _ = client_root
    resp = client.post(
        "/dashboard/operator/create",
        data={"probe_id": "totally.bogus", "skill_id": "x", "mode": "diagnose"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Safety guarantees
# ---------------------------------------------------------------------------


_DASH_ROUTES = [
    "/dashboard/today",
    "/dashboard/portfolio",
    "/dashboard/quant",
    "/dashboard/system",
    "/dashboard/memo",
]


@pytest.mark.parametrize("route", _DASH_ROUTES)
def test_no_arbitrary_command_input(client_root, route):
    """The operator panel must not expose any free-text/command input field."""
    client, _ = client_root
    body = client.get(route).text
    # No text/command inputs in the operator panel forms — only hidden registry
    # ids + submit buttons.
    operator_forms = re.findall(
        r'<form[^>]*action="/dashboard/operator/create"[^>]*>(.*?)</form>',
        body, re.DOTALL | re.IGNORECASE,
    )
    for f in operator_forms:
        # Only hidden inputs allowed (probe_id, skill_id, mode).
        for inp in re.findall(r'<input[^>]*>', f, re.IGNORECASE):
            assert 'type="hidden"' in inp, f"non-hidden input in operator form: {inp}"
        assert "<textarea" not in f.lower()


@pytest.mark.parametrize("route", _DASH_ROUTES)
def test_operator_forms_only_post_to_safe_endpoint(client_root, route):
    client, _ = client_root
    body = client.get(route).text
    for action in re.findall(r'<form[^>]*action="([^"]*)"', body, re.IGNORECASE):
        if "operator" in action:
            assert action == "/dashboard/operator/create"


@pytest.mark.parametrize("route", _DASH_ROUTES)
def test_no_execution_language_with_operator_panel(client_root, route):
    client, _ = client_root
    body = client.get(route).text.lower()
    for phrase in ("execute trade", "place order", "submit order", "auto-trade"):
        assert phrase not in body


# ---------------------------------------------------------------------------
# Empty-state
# ---------------------------------------------------------------------------


def test_empty_operator_state_renders_cleanly(client_root):
    client, _ = client_root
    # No work orders created yet → the queue shows its empty state, page is 200.
    body = client.get("/dashboard/system").text
    assert "No work orders yet" in body


def test_system_tab_shows_runner_card(client_root):
    client, _ = client_root
    body = client.get("/dashboard/system").text
    assert "Worker Runner" in body
