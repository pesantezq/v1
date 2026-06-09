"""Task 6 (Milestone 6): /dashboard/portfolio-sync — Schwab read view.

Tests:
  - collect_portfolio_sync_view returns expected card titles and structure
  - every card has non-empty source_artifacts
  - route GET renders 200 with artifacts absent (empty "Schwab not configured" state)
  - route GET renders 200 with full fixtures (connected, mismatches present)
  - mismatch rows extracted correctly; table/card markup present
  - "Generate Config Update Proposal" button rendered DISABLED when schwab_sync not importable
  - note text present when module not installed
  - "updates local config only — no trades" safety banner present
  - account masking: rendered HTML must NOT contain any 8-9 digit account number
  - config.json byte-unchanged after POST /reconcile (brokers absent path)
  - config.json byte-unchanged after POST /reconcile (brokers stubbed path)
  - no forbidden action labels in rendered HTML
  - no forbidden action labels in template file
  - mobile stacked cards present (md:hidden)
  - observe_only=True in view dict
  - persona field is "portfolio_sync"
  - schwab_available reflects import availability
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EXPECTED_CARD_TITLES = {
    "Connection",
    "Last Sync",
    "Holdings Matched",
    "Cash Difference",
    "Config Update Proposal",
}

_FORBIDDEN_LABELS = (
    "execute trade",
    "buy now",
    "sell now",
    "place order",
    "auto-trade",
    "auto trade",
    "auto-approve",
)

# Full account number pattern: 8+ consecutive digits (masked forms like …1234 are safe)
_FULL_ACCOUNT_RE = re.compile(r"(?<!\d)\d{8,}(?!\d)")


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_latest(tmp_path: Path) -> Path:
    d = tmp_path / "outputs" / "latest"
    d.mkdir(parents=True)
    return d


def _write(directory: Path, filename: str, data: dict) -> None:
    (directory / filename).write_text(json.dumps(data), encoding="utf-8")


def _make_broker_sync_status(
    latest: Path,
    overall_status: str = "ok",
    configured: bool = True,
    authenticated: bool = True,
    last_success_at: str | None = "2026-06-08T10:00:00Z",
    last_error: str | None = None,
) -> None:
    _write(
        latest,
        "broker_sync_status.json",
        {
            "generated_at": "2026-06-08T12:00:00Z",
            "observe_only": True,
            "source": "schwab",
            "enabled": True,
            "configured": configured,
            "authenticated": authenticated,
            "read_only_mode": True,
            "trading_enabled": False,
            "last_success_at": last_success_at,
            "last_error": last_error,
            # Masked account ID — must NOT contain 8-9 digit string
            "account_id_masked": "…1234",
            "overall_status": overall_status,
        },
    )


def _make_portfolio_reconciliation(
    latest: Path,
    matched_count: int = 8,
    quantity_mismatches: list | None = None,
    missing_in_local: list | None = None,
    missing_in_schwab: list | None = None,
    cash_delta: float = 0.50,
    cash_local: float = 5000.00,
    cash_schwab: float = 5000.50,
) -> None:
    _write(
        latest,
        "portfolio_reconciliation.json",
        {
            "generated_at": "2026-06-08T12:00:00Z",
            "observe_only": True,
            "schema_version": "1.0",
            "matched_count": matched_count,
            "total_count": matched_count
            + len(quantity_mismatches or [])
            + len(missing_in_local or [])
            + len(missing_in_schwab or []),
            "quantity_mismatches": quantity_mismatches or [],
            "missing_in_local": missing_in_local or [],
            "missing_in_schwab": missing_in_schwab or [],
            "cash": {
                "local": cash_local,
                "schwab": cash_schwab,
                "delta": cash_delta,
            },
        },
    )


def _make_proposal(
    latest: Path,
    operator_approval_required: bool = True,
    auto_applied: bool = False,
    n_changes: int = 2,
) -> None:
    _write(
        latest,
        "portfolio_config_update_proposal.json",
        {
            "generated_at": "2026-06-08T12:00:00Z",
            "observe_only": True,
            "schema_version": "1.0",
            "operator_approval_required": operator_approval_required,
            "auto_applied": auto_applied,
            "status": "pending",
            "changes": [{"symbol": "AAPL", "field": "weight"} for _ in range(n_changes)],
        },
    )


def _make_all_artifacts(tmp_path: Path, latest: Path) -> None:
    _make_broker_sync_status(latest)
    _make_portfolio_reconciliation(
        latest,
        quantity_mismatches=[
            {
                "symbol": "AAPL",
                "local_quantity": 10,
                "schwab_quantity": 11,
                "delta": 1,
            }
        ],
        missing_in_local=["MSFT"],
        missing_in_schwab=[],
    )
    _make_proposal(latest)


# ---------------------------------------------------------------------------
# Unit tests: collect_portfolio_sync_view — structure
# ---------------------------------------------------------------------------


def test_sync_view_has_all_expected_card_titles(tmp_path):
    """All expected card domains are present even with no artifacts."""
    from gui_v2.data.dash_portfolio_sync import collect_portfolio_sync_view

    _make_latest(tmp_path)
    v = collect_portfolio_sync_view(tmp_path)
    titles = {c["title"] for c in v["cards"]}
    assert EXPECTED_CARD_TITLES <= titles, f"Missing cards: {EXPECTED_CARD_TITLES - titles}"


def test_every_card_has_non_empty_source_artifacts(tmp_path):
    """source_artifacts must be non-empty for every card — artifacts absent."""
    from gui_v2.data.dash_portfolio_sync import collect_portfolio_sync_view

    _make_latest(tmp_path)
    v = collect_portfolio_sync_view(tmp_path)
    bad = [c["title"] for c in v["cards"] if not c.get("source_artifacts")]
    assert bad == [], f"Cards missing source_artifacts: {bad}"


def test_every_card_has_non_empty_source_artifacts_with_artifacts(tmp_path):
    """source_artifacts non-empty when all artifacts are present."""
    from gui_v2.data.dash_portfolio_sync import collect_portfolio_sync_view

    latest = _make_latest(tmp_path)
    _make_all_artifacts(tmp_path, latest)
    v = collect_portfolio_sync_view(tmp_path)
    bad = [c["title"] for c in v["cards"] if not c.get("source_artifacts")]
    assert bad == [], f"Cards missing source_artifacts: {bad}"


def test_sync_view_persona_field(tmp_path):
    from gui_v2.data.dash_portfolio_sync import collect_portfolio_sync_view

    _make_latest(tmp_path)
    v = collect_portfolio_sync_view(tmp_path)
    assert v["persona"] == "portfolio_sync"


def test_sync_view_observe_only_flag(tmp_path):
    from gui_v2.data.dash_portfolio_sync import collect_portfolio_sync_view

    _make_latest(tmp_path)
    v = collect_portfolio_sync_view(tmp_path)
    assert v.get("observe_only") is True


def test_sync_view_mismatch_rows_key_present(tmp_path):
    from gui_v2.data.dash_portfolio_sync import collect_portfolio_sync_view

    _make_latest(tmp_path)
    v = collect_portfolio_sync_view(tmp_path)
    assert "mismatch_rows" in v


def test_sync_view_schwab_available_key_present(tmp_path):
    from gui_v2.data.dash_portfolio_sync import collect_portfolio_sync_view

    _make_latest(tmp_path)
    v = collect_portfolio_sync_view(tmp_path)
    assert "schwab_available" in v
    assert isinstance(v["schwab_available"], bool)


# ---------------------------------------------------------------------------
# Connection card
# ---------------------------------------------------------------------------


def test_connection_card_absent_is_info_not_red(tmp_path):
    """broker_sync_status.json absent → Connection card status='info', NOT 'red'."""
    from gui_v2.data.dash_portfolio_sync import collect_portfolio_sync_view

    _make_latest(tmp_path)
    v = collect_portfolio_sync_view(tmp_path)
    conn = next(c for c in v["cards"] if c["title"] == "Connection")
    assert conn["status"] == "info", f"Expected status='info' when absent, got {conn['status']!r}"
    assert conn["severity"] == "blue"


def test_connection_card_absent_label_mentions_not_configured(tmp_path):
    """broker_sync_status absent → label contains 'not configured'."""
    from gui_v2.data.dash_portfolio_sync import collect_portfolio_sync_view

    _make_latest(tmp_path)
    v = collect_portfolio_sync_view(tmp_path)
    conn = next(c for c in v["cards"] if c["title"] == "Connection")
    assert "not configured" in conn["label"].lower()


def test_connection_card_ok_status(tmp_path):
    """broker_sync_status with overall_status=ok → Connection card status='ok'."""
    from gui_v2.data.dash_portfolio_sync import collect_portfolio_sync_view

    latest = _make_latest(tmp_path)
    _make_broker_sync_status(latest, overall_status="ok", configured=True, authenticated=True)
    v = collect_portfolio_sync_view(tmp_path)
    conn = next(c for c in v["cards"] if c["title"] == "Connection")
    assert conn["status"] == "ok", f"Expected status='ok', got {conn['status']!r}"


def test_connection_card_error_status(tmp_path):
    """broker_sync_status with overall_status=error → Connection card status='red'."""
    from gui_v2.data.dash_portfolio_sync import collect_portfolio_sync_view

    latest = _make_latest(tmp_path)
    _make_broker_sync_status(latest, overall_status="error", configured=True, authenticated=False)
    v = collect_portfolio_sync_view(tmp_path)
    conn = next(c for c in v["cards"] if c["title"] == "Connection")
    assert conn["status"] == "red", f"Expected status='red', got {conn['status']!r}"


def test_connection_card_no_trade_language(tmp_path):
    """Connection card summary/label must not contain trade/execute language."""
    from gui_v2.data.dash_portfolio_sync import collect_portfolio_sync_view

    latest = _make_latest(tmp_path)
    _make_broker_sync_status(latest, overall_status="ok", configured=True, authenticated=True)
    v = collect_portfolio_sync_view(tmp_path)
    conn = next(c for c in v["cards"] if c["title"] == "Connection")
    for field in ("summary", "label"):
        text = (conn.get(field) or "").lower()
        for bad in _FORBIDDEN_LABELS:
            assert bad not in text, (
                f"Forbidden term '{bad}' in Connection card {field}: {text!r}"
            )


# ---------------------------------------------------------------------------
# Last Sync card
# ---------------------------------------------------------------------------


def test_last_sync_card_absent_is_info(tmp_path):
    from gui_v2.data.dash_portfolio_sync import collect_portfolio_sync_view

    _make_latest(tmp_path)
    v = collect_portfolio_sync_view(tmp_path)
    ls = next(c for c in v["cards"] if c["title"] == "Last Sync")
    assert ls["status"] == "info"


def test_last_sync_card_ok_when_success_at_set(tmp_path):
    from gui_v2.data.dash_portfolio_sync import collect_portfolio_sync_view

    latest = _make_latest(tmp_path)
    _make_broker_sync_status(latest, last_success_at="2026-06-08T10:00:00Z")
    v = collect_portfolio_sync_view(tmp_path)
    ls = next(c for c in v["cards"] if c["title"] == "Last Sync")
    assert ls["status"] == "ok"


def test_last_sync_card_info_when_no_success_at(tmp_path):
    from gui_v2.data.dash_portfolio_sync import collect_portfolio_sync_view

    latest = _make_latest(tmp_path)
    _make_broker_sync_status(latest, last_success_at=None)
    v = collect_portfolio_sync_view(tmp_path)
    ls = next(c for c in v["cards"] if c["title"] == "Last Sync")
    assert ls["status"] == "info"
    assert ls["label"] == "never"


# ---------------------------------------------------------------------------
# Holdings Matched card
# ---------------------------------------------------------------------------


def test_holdings_matched_absent_is_info(tmp_path):
    from gui_v2.data.dash_portfolio_sync import collect_portfolio_sync_view

    _make_latest(tmp_path)
    v = collect_portfolio_sync_view(tmp_path)
    hm = next(c for c in v["cards"] if c["title"] == "Holdings Matched")
    assert hm["status"] == "info"


def test_holdings_matched_ok_when_no_mismatches(tmp_path):
    from gui_v2.data.dash_portfolio_sync import collect_portfolio_sync_view

    latest = _make_latest(tmp_path)
    _make_portfolio_reconciliation(latest, matched_count=10, quantity_mismatches=[], missing_in_local=[], missing_in_schwab=[])
    v = collect_portfolio_sync_view(tmp_path)
    hm = next(c for c in v["cards"] if c["title"] == "Holdings Matched")
    assert hm["status"] == "ok"


def test_holdings_matched_warning_when_mismatches(tmp_path):
    from gui_v2.data.dash_portfolio_sync import collect_portfolio_sync_view

    latest = _make_latest(tmp_path)
    _make_portfolio_reconciliation(
        latest,
        matched_count=8,
        quantity_mismatches=[{"symbol": "AAPL", "local_quantity": 10, "schwab_quantity": 11, "delta": 1}],
    )
    v = collect_portfolio_sync_view(tmp_path)
    hm = next(c for c in v["cards"] if c["title"] == "Holdings Matched")
    assert hm["status"] == "warning"
    assert "mismatch" in hm["label"].lower()


# ---------------------------------------------------------------------------
# Cash Difference card
# ---------------------------------------------------------------------------


def test_cash_difference_absent_is_info(tmp_path):
    from gui_v2.data.dash_portfolio_sync import collect_portfolio_sync_view

    _make_latest(tmp_path)
    v = collect_portfolio_sync_view(tmp_path)
    cd = next(c for c in v["cards"] if c["title"] == "Cash Difference")
    assert cd["status"] == "info"


def test_cash_difference_ok_when_tiny_delta(tmp_path):
    from gui_v2.data.dash_portfolio_sync import collect_portfolio_sync_view

    latest = _make_latest(tmp_path)
    _make_portfolio_reconciliation(latest, cash_delta=0.001)
    v = collect_portfolio_sync_view(tmp_path)
    cd = next(c for c in v["cards"] if c["title"] == "Cash Difference")
    assert cd["status"] == "ok"


def test_cash_difference_warning_when_moderate_delta(tmp_path):
    from gui_v2.data.dash_portfolio_sync import collect_portfolio_sync_view

    latest = _make_latest(tmp_path)
    _make_portfolio_reconciliation(latest, cash_delta=50.0)
    v = collect_portfolio_sync_view(tmp_path)
    cd = next(c for c in v["cards"] if c["title"] == "Cash Difference")
    assert cd["status"] == "warning"


def test_cash_difference_red_when_large_delta(tmp_path):
    from gui_v2.data.dash_portfolio_sync import collect_portfolio_sync_view

    latest = _make_latest(tmp_path)
    _make_portfolio_reconciliation(latest, cash_delta=500.0)
    v = collect_portfolio_sync_view(tmp_path)
    cd = next(c for c in v["cards"] if c["title"] == "Cash Difference")
    assert cd["status"] == "red"


# ---------------------------------------------------------------------------
# Proposal status card
# ---------------------------------------------------------------------------


def test_proposal_card_absent_is_info(tmp_path):
    from gui_v2.data.dash_portfolio_sync import collect_portfolio_sync_view

    _make_latest(tmp_path)
    v = collect_portfolio_sync_view(tmp_path)
    pc = next(c for c in v["cards"] if c["title"] == "Config Update Proposal")
    assert pc["status"] == "info"


def test_proposal_card_warning_when_operator_approval_required(tmp_path):
    from gui_v2.data.dash_portfolio_sync import collect_portfolio_sync_view

    latest = _make_latest(tmp_path)
    _make_proposal(latest, operator_approval_required=True)
    v = collect_portfolio_sync_view(tmp_path)
    pc = next(c for c in v["cards"] if c["title"] == "Config Update Proposal")
    assert pc["status"] == "warning"
    assert "review" in pc["label"].lower() or "pending" in pc["label"].lower()


# ---------------------------------------------------------------------------
# Mismatch rows
# ---------------------------------------------------------------------------


def test_mismatch_rows_empty_when_no_recon(tmp_path):
    from gui_v2.data.dash_portfolio_sync import collect_portfolio_sync_view

    _make_latest(tmp_path)
    v = collect_portfolio_sync_view(tmp_path)
    assert v["mismatch_rows"] == []


def test_mismatch_rows_extracted_correctly(tmp_path):
    from gui_v2.data.dash_portfolio_sync import collect_portfolio_sync_view

    latest = _make_latest(tmp_path)
    _make_portfolio_reconciliation(
        latest,
        quantity_mismatches=[
            {"symbol": "AAPL", "local_quantity": 10, "schwab_quantity": 11, "delta": 1}
        ],
        missing_in_local=["MSFT"],
        missing_in_schwab=["TSLA"],
    )
    v = collect_portfolio_sync_view(tmp_path)
    rows = v["mismatch_rows"]
    symbols = {r["symbol"] for r in rows}
    types = {r["mismatch_type"] for r in rows}
    assert "AAPL" in symbols
    assert "MSFT" in symbols
    assert "TSLA" in symbols
    assert "quantity" in types
    assert "missing in local" in types
    assert "missing in Schwab" in types


# ---------------------------------------------------------------------------
# schwab_available flag
# ---------------------------------------------------------------------------


def test_schwab_available_true_when_brokers_present(tmp_path):
    """
    The Schwab brokers package is now merged into main, so
    portfolio_automation.brokers.schwab_sync IS importable and schwab_available
    must be True. (It was False on the pre-merge cockpit branch.)
    """
    from gui_v2.data.dash_portfolio_sync import schwab_available

    assert schwab_available is True, (
        "Expected schwab_available=True now that the brokers package is present"
    )


# ---------------------------------------------------------------------------
# Route / integration tests
# ---------------------------------------------------------------------------


def test_portfolio_sync_route_renders_200():
    """GET /dashboard/portfolio-sync returns 200."""
    from gui_v2.app import app

    client = TestClient(app)
    r = client.get("/dashboard/portfolio-sync")
    assert r.status_code == 200


def test_portfolio_sync_route_200_with_no_artifacts(monkeypatch, tmp_path):
    """Route renders 200 when all artifacts are absent (empty states)."""
    from gui_v2 import app as app_module

    empty_latest = tmp_path / "outputs" / "latest"
    empty_latest.mkdir(parents=True)

    original_root = app_module.REPO_ROOT
    monkeypatch.setattr(app_module, "REPO_ROOT", tmp_path)
    try:
        client = TestClient(app_module.app)
        r = client.get("/dashboard/portfolio-sync")
        assert r.status_code == 200
    finally:
        monkeypatch.setattr(app_module, "REPO_ROOT", original_root)


def test_portfolio_sync_route_200_with_all_artifacts(monkeypatch, tmp_path):
    """Route renders 200 when all Schwab artifacts are present (full fixture)."""
    from gui_v2 import app as app_module

    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    _make_all_artifacts(tmp_path, latest)

    original_root = app_module.REPO_ROOT
    monkeypatch.setattr(app_module, "REPO_ROOT", tmp_path)
    try:
        client = TestClient(app_module.app)
        r = client.get("/dashboard/portfolio-sync")
        assert r.status_code == 200
        # Mismatch table should appear (AAPL quantity mismatch + MSFT missing in local)
        assert "AAPL" in r.text or "mismatch" in r.text.lower()
    finally:
        monkeypatch.setattr(app_module, "REPO_ROOT", original_root)


def test_portfolio_sync_route_has_safety_banner():
    """Page contains the safety banner mentioning 'no trades'."""
    from gui_v2.app import app

    client = TestClient(app)
    r = client.get("/dashboard/portfolio-sync")
    assert r.status_code == 200
    text = r.text.lower()
    assert "no trade" in text or "does not execute trades" in text, (
        "Safety banner missing from portfolio-sync page"
    )


def test_portfolio_sync_safety_banner_text():
    """Safety banner must contain the required advisory text."""
    from gui_v2.app import app

    client = TestClient(app)
    r = client.get("/dashboard/portfolio-sync")
    assert r.status_code == 200
    # Must mention config-only and no trade activity (without forbidden label 'execute trade')
    text = r.text.lower()
    assert "configuration only" in text or "config" in text, (
        "Required safety banner text about config-only not found in page"
    )
    assert "no trade" in text or "trade activity" in text, (
        "Required safety banner text about no trade activity not found in page"
    )


def test_portfolio_sync_generate_button_disabled_when_schwab_unavailable(monkeypatch):
    """
    When schwab_available is False (brokers unavailable / degraded path), the
    Generate Config Update Proposal button must render with the 'disabled'
    attribute and the not-installed note. Brokers are normally present now, so
    unavailability is forced via the module flag to keep covering this path.
    """
    from gui_v2.app import app
    from gui_v2.data import dash_portfolio_sync as dps_module

    monkeypatch.setattr(dps_module, "schwab_available", False)

    client = TestClient(app)
    r = client.get("/dashboard/portfolio-sync")
    assert r.status_code == 200
    text = r.text
    # Button must be disabled
    assert "disabled" in text, "Generate button not rendered as disabled when schwab_unavailable"
    # Note about the missing package must be visible
    assert "feat/schwab-readonly-sync" in text, (
        "Not-installed note ('merge feat/schwab-readonly-sync') missing from page"
    )


def test_portfolio_sync_no_forbidden_labels_in_rendered_html():
    """Rendered HTML must not contain forbidden action labels."""
    from gui_v2.app import app

    client = TestClient(app)
    r = client.get("/dashboard/portfolio-sync")
    assert r.status_code == 200
    text = r.text.lower()
    offenders = [label for label in _FORBIDDEN_LABELS if label in text]
    assert offenders == [], f"Forbidden labels in /dashboard/portfolio-sync: {offenders}"


def test_no_forbidden_action_labels_in_portfolio_sync_template():
    """portfolio_sync.html must not contain forbidden action label strings."""
    template_path = Path("gui_v2/templates/dashboard/portfolio_sync.html")
    text = template_path.read_text(encoding="utf-8").lower()
    offenders = [label for label in _FORBIDDEN_LABELS if label in text]
    assert offenders == [], f"Forbidden labels in portfolio_sync.html: {offenders}"


def test_portfolio_sync_account_masking_no_full_account_number(monkeypatch, tmp_path):
    """
    Rendered HTML must NOT contain any 8-9 consecutive digit account number.
    The broker_sync_status fixture uses the masked form (…1234), which is safe.
    """
    from gui_v2 import app as app_module

    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    _make_all_artifacts(tmp_path, latest)

    original_root = app_module.REPO_ROOT
    monkeypatch.setattr(app_module, "REPO_ROOT", tmp_path)
    try:
        client = TestClient(app_module.app)
        r = client.get("/dashboard/portfolio-sync")
        assert r.status_code == 200
        # No 8-9 digit account numbers should appear
        matches = _FULL_ACCOUNT_RE.findall(r.text)
        assert matches == [], (
            f"Full account number(s) found in rendered HTML: {matches}"
        )
    finally:
        monkeypatch.setattr(app_module, "REPO_ROOT", original_root)


def test_portfolio_sync_mobile_card_stack_present():
    """Template has md:hidden mobile card stack alongside hidden md:block desktop table."""
    template_path = Path("gui_v2/templates/dashboard/portfolio_sync.html")
    tpl_text = template_path.read_text(encoding="utf-8")
    assert "hidden md:block" in tpl_text, (
        "Missing 'hidden md:block' desktop table class in portfolio_sync.html"
    )
    assert "md:hidden" in tpl_text, (
        "Missing 'md:hidden' mobile stack class in portfolio_sync.html"
    )


# ---------------------------------------------------------------------------
# Safety test: config.json byte-unchanged after POST /reconcile
# ---------------------------------------------------------------------------


def test_reconcile_post_does_not_mutate_config_json(monkeypatch, tmp_path):
    """
    POST /dashboard/portfolio-sync/reconcile must NOT modify config.json,
    regardless of whether the brokers module is importable.

    This test covers the ImportError path (brokers not installed on this branch).
    """
    from gui_v2 import app as app_module

    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    _make_all_artifacts(tmp_path, latest)

    # Seed a config.json in the root
    config_path = tmp_path / "config.json"
    config_data = {"sentinel": "unchanged", "version": 42}
    config_path.write_text(json.dumps(config_data), encoding="utf-8")
    original_bytes = config_path.read_bytes()

    original_root = app_module.REPO_ROOT
    monkeypatch.setattr(app_module, "REPO_ROOT", tmp_path)
    try:
        client = TestClient(app_module.app)
        r = client.post("/dashboard/portfolio-sync/reconcile")
        # Route should return 200 (or redirect) — not crash
        assert r.status_code in (200, 302, 303)
        # config.json must be byte-for-byte identical
        after_bytes = config_path.read_bytes()
        assert after_bytes == original_bytes, (
            "config.json was mutated by the reconcile POST — this must NEVER happen"
        )
    finally:
        monkeypatch.setattr(app_module, "REPO_ROOT", original_root)


def test_reconcile_post_with_stubbed_run_reconcile_does_not_mutate_config(
    monkeypatch, tmp_path
):
    """
    POST /dashboard/portfolio-sync/reconcile with a stubbed run_reconcile that
    writes a proposal artifact must still NOT modify config.json.
    """
    from gui_v2 import app as app_module

    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    _make_all_artifacts(tmp_path, latest)

    # Seed config.json
    config_path = tmp_path / "config.json"
    config_data = {"sentinel": "must_not_change", "holdings": []}
    config_path.write_text(json.dumps(config_data), encoding="utf-8")
    original_bytes = config_path.read_bytes()

    # Build a fake brokers module
    import types

    fake_module_pkg = types.ModuleType("portfolio_automation.brokers")
    fake_module_sync = types.ModuleType("portfolio_automation.brokers.schwab_sync")

    reconcile_called = []

    def _fake_run_reconcile(root):
        reconcile_called.append(root)
        # Only writes proposal artifact — never touches config.json
        proposal_path = Path(root) / "outputs" / "latest" / "portfolio_config_update_proposal.json"
        proposal_path.write_text(
            json.dumps({"observe_only": True, "status": "pending", "changes": []}),
            encoding="utf-8",
        )

    fake_module_sync.run_reconcile = _fake_run_reconcile

    original_root = app_module.REPO_ROOT
    monkeypatch.setattr(app_module, "REPO_ROOT", tmp_path)
    monkeypatch.setitem(sys.modules, "portfolio_automation.brokers", fake_module_pkg)
    monkeypatch.setitem(sys.modules, "portfolio_automation.brokers.schwab_sync", fake_module_sync)

    try:
        client = TestClient(app_module.app)
        r = client.post("/dashboard/portfolio-sync/reconcile")
        assert r.status_code in (200, 302, 303)
        # config.json byte-unchanged
        after_bytes = config_path.read_bytes()
        assert after_bytes == original_bytes, (
            "config.json was mutated by the stubbed reconcile POST"
        )
    finally:
        monkeypatch.setattr(app_module, "REPO_ROOT", original_root)
        # Remove fake modules
        sys.modules.pop("portfolio_automation.brokers", None)
        sys.modules.pop("portfolio_automation.brokers.schwab_sync", None)


def test_reconcile_post_returns_not_installed_message_when_brokers_absent(monkeypatch):
    """
    POST /reconcile when the brokers module is unavailable → response contains the
    not-installed note. Brokers are normally present now, so absence is forced by
    sabotaging the import (sys.modules entry → None raises ImportError) to keep
    covering the degraded path.
    """
    import sys
    from gui_v2.app import app

    monkeypatch.setitem(sys.modules, "portfolio_automation.brokers.schwab_sync", None)

    client = TestClient(app)
    r = client.post("/dashboard/portfolio-sync/reconcile")
    assert r.status_code == 200
    assert "feat/schwab-readonly-sync" in r.text or "not installed" in r.text.lower()


# ---------------------------------------------------------------------------
# Mismatch row rendering in the route
# ---------------------------------------------------------------------------


def test_portfolio_sync_mismatch_rows_shown_in_page(monkeypatch, tmp_path):
    """When mismatches exist, their symbol names appear in the rendered page."""
    from gui_v2 import app as app_module

    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    _make_broker_sync_status(latest)
    _make_portfolio_reconciliation(
        latest,
        quantity_mismatches=[
            {"symbol": "NVDA", "local_quantity": 5, "schwab_quantity": 6, "delta": 1}
        ],
        missing_in_local=["META"],
    )

    original_root = app_module.REPO_ROOT
    monkeypatch.setattr(app_module, "REPO_ROOT", tmp_path)
    try:
        client = TestClient(app_module.app)
        r = client.get("/dashboard/portfolio-sync")
        assert r.status_code == 200
        assert "NVDA" in r.text, "Mismatch symbol NVDA not found in rendered page"
        assert "META" in r.text, "Mismatch symbol META not found in rendered page"
    finally:
        monkeypatch.setattr(app_module, "REPO_ROOT", original_root)


# ---------------------------------------------------------------------------
# T-HIGH: 10-digit account number masking
# ---------------------------------------------------------------------------


def test_account_masking_10_digit_suppressed_in_rendered_html(monkeypatch, tmp_path):
    """
    A 10-digit account number in an artifact MUST NOT appear in the rendered HTML.
    The defensive _mask_account_fields call in collect_portfolio_sync_view must
    catch IDs wider than 9 digits (the old r'\\d{8,9}' regex missed them).
    """
    from gui_v2 import app as app_module

    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)

    # Write a broker_sync_status with an unmasked 10-digit account ID in a string field
    unmasked_10_digit = "1234567890"
    _write(
        latest,
        "broker_sync_status.json",
        {
            "generated_at": "2026-06-08T12:00:00Z",
            "observe_only": True,
            "source": "schwab",
            "enabled": True,
            "configured": True,
            "authenticated": True,
            "read_only_mode": True,
            "trading_enabled": False,
            "last_success_at": "2026-06-08T10:00:00Z",
            "last_error": None,
            # Simulates an upstream artifact that leaked a 10-digit account number
            "account_id_masked": unmasked_10_digit,
            "overall_status": "ok",
        },
    )

    original_root = app_module.REPO_ROOT
    monkeypatch.setattr(app_module, "REPO_ROOT", tmp_path)
    try:
        client = TestClient(app_module.app)
        r = client.get("/dashboard/portfolio-sync")
        assert r.status_code == 200
        # The 10-digit raw string must NOT appear in the rendered page
        assert unmasked_10_digit not in r.text, (
            f"Unmasked 10-digit account number '{unmasked_10_digit}' leaked into rendered HTML"
        )
    finally:
        monkeypatch.setattr(app_module, "REPO_ROOT", original_root)
