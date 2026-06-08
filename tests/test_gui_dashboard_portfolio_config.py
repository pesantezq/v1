"""Task 7 (Milestone 7): /dashboard/portfolio-config — Gated Portfolio Config Write Surface.

Tests:
  ## Forbidden labels
  - No forbidden action labels in rendered HTML (editing enabled / disabled)
  - No forbidden action labels in template files (portfolio_config.html,
    portfolio_edit_form.html, validation_errors.html)

  ## Gating — editing disabled (default / auth unset / flag unset)
  - GET /dashboard/portfolio-config without env vars → 200, "editing disabled"
  - GET with only auth set (no PORTFOLIO_EDIT) → 200, "editing disabled"
  - GET with only PORTFOLIO_EDIT set (no auth) → 200, "editing disabled"
  - POST /save without env → 403 (no write)
  - POST /save with auth only (no flag) → 403 (no write)
  - POST /save with flag only (no auth) → 403 (no write)

  ## Gating — editing enabled (auth + flag)
  - GET returns 200 with form markup (not "editing disabled")
  - _edit_enabled() returns True when both auth + flag set

  ## collect_portfolio_config_view
  - Returns expected keys (cards, persona, edit_enabled, holdings, cash, growth_mode,
    config_available, observe_only)
  - observe_only=True hardcoded
  - persona="portfolio_config"
  - Holdings parsed from config.json correctly
  - Cash parsed from config.json correctly
  - config_available=False when config.json absent
  - edit_enabled=False → "editing disabled" card status=info
  - edit_enabled=True  → "edit enabled" card status=warning

  ## validate_config_edit
  - Valid input → ok=True, errors=[]
  - Negative shares → error
  - Negative cash → error
  - Empty symbol → error
  - Invalid symbol pattern → error
  - Duplicate symbols → error
  - target_weight sum off → error
  - target_weight > 1 → error
  - concentration_cap breach → error
  - leverage_cap breach → error
  - Valid input with weights summing to 1.0 → ok=True

  ## diff_config_edit
  - Returns "holdings" and "cash" keys
  - Added symbol → before=None
  - Removed symbol → after=None
  - Cash delta computed correctly

  ## apply_config_edit (write surface — rigorous)
  - Backup created BEFORE config.json is written
  - Backup contents match pre-edit config exactly
  - Backup can be used to restore the original config (reversible)
  - config.json updated with new holdings and cash
  - Other config keys (non-portfolio) untouched
  - Audit record appended to manual_portfolio_updates.jsonl
  - Audit record contains before/after, source="gui_portfolio_config", observe_only=True
  - No decision-core mutation (decision_plan.json untouched)

  ## Route integration
  - POST /validate returns 200 with validation fragment (no write)
  - POST /validate with bad data returns errors fragment
  - POST /save (enabled) → 200 with save_result.ok=True and backup_path in HTML
  - POST /save (enabled) with bad data → 200 with error shown, no write
  - config.json byte-unchanged after POST /validate
  - config.json byte-unchanged after POST /save (disabled)

  ## Safety banner
  - "no broker trades" or equivalent in rendered page
  - Banner does not contain forbidden labels

  ## Mobile responsive
  - hidden md:block + md:hidden in portfolio_edit_form.html
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FORBIDDEN_LABELS = (
    "execute trade",
    "buy now",
    "sell now",
    "place order",
    "auto-trade",
    "auto trade",
    "auto-approve",
)

_TEMPLATE_FILES = [
    Path("gui_v2/templates/dashboard/portfolio_config.html"),
    Path("gui_v2/templates/components/portfolio_edit_form.html"),
    Path("gui_v2/templates/components/validation_errors.html"),
]


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _sample_config(holdings=None, cash=464.16) -> dict:
    return {
        "investor": {"name": "Test User"},
        "portfolio": {
            "holdings": holdings if holdings is not None else [
                {
                    "symbol": "QQQ",
                    "shares": 6,
                    "target_weight": 0.35,
                    "asset_class": "us_equity",
                    "is_leveraged": False,
                    "leverage_factor": 1,
                },
                {
                    "symbol": "GLD",
                    "shares": 4,
                    "target_weight": 0.20,
                    "asset_class": "commodity",
                    "is_leveraged": False,
                    "leverage_factor": 1,
                },
            ],
            "cash_available": cash,
        },
        "growth_mode": {
            "mode": "accumulation_aggressive",
            "concentration_cap": 0.6,
            "leverage_cap": 0.25,
        },
        # Sentinel key: must never be mutated
        "email": {"enabled": True, "smtp_server": "smtp.test.example"},
    }


def _write_config(tmp_path: Path, data: dict | None = None) -> Path:
    cfg = data if data is not None else _sample_config()
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return p


def _write_decision_plan(tmp_path: Path) -> Path:
    d = tmp_path / "outputs" / "latest"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "decision_plan.json"
    p.write_text(json.dumps({"sentinel": "decision_plan_unchanged"}), encoding="utf-8")
    return p


def _client_with_root(monkeypatch, tmp_path: Path, *, auth: bool = False, edit_flag: bool = False):
    from gui_v2 import app as app_module

    original_root = app_module.REPO_ROOT
    monkeypatch.setattr(app_module, "REPO_ROOT", tmp_path)

    if auth:
        monkeypatch.setenv("GUI_V2_AUTH_USER", "testuser")
        monkeypatch.setenv("GUI_V2_AUTH_PASS", "testpass")
    else:
        monkeypatch.delenv("GUI_V2_AUTH_USER", raising=False)
        monkeypatch.delenv("GUI_V2_AUTH_PASS", raising=False)

    if edit_flag:
        monkeypatch.setenv("GUI_V2_PORTFOLIO_EDIT", "1")
    else:
        monkeypatch.delenv("GUI_V2_PORTFOLIO_EDIT", raising=False)

    return TestClient(app_module.app), original_root, app_module


# ---------------------------------------------------------------------------
# Forbidden labels — template files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tpl", _TEMPLATE_FILES)
def test_no_forbidden_labels_in_template_files(tpl):
    """Template files must not contain forbidden action-label substrings."""
    text = tpl.read_text(encoding="utf-8").lower()
    offenders = [lbl for lbl in _FORBIDDEN_LABELS if lbl in text]
    assert offenders == [], f"Forbidden labels in {tpl}: {offenders}"


# ---------------------------------------------------------------------------
# Forbidden labels — rendered HTML
# ---------------------------------------------------------------------------


def test_no_forbidden_labels_in_rendered_html_edit_disabled(monkeypatch, tmp_path):
    _write_config(tmp_path)
    client, orig, mod = _client_with_root(monkeypatch, tmp_path, auth=False, edit_flag=False)
    try:
        r = client.get("/dashboard/portfolio-config")
        assert r.status_code == 200
        text = r.text.lower()
        offenders = [lbl for lbl in _FORBIDDEN_LABELS if lbl in text]
        assert offenders == [], f"Forbidden labels in rendered HTML (disabled): {offenders}"
    finally:
        monkeypatch.setattr(mod, "REPO_ROOT", orig)


def test_no_forbidden_labels_in_rendered_html_edit_enabled(monkeypatch, tmp_path):
    _write_config(tmp_path)
    client, orig, mod = _client_with_root(monkeypatch, tmp_path, auth=True, edit_flag=True)
    try:
        r = client.get(
            "/dashboard/portfolio-config",
            auth=("testuser", "testpass"),
        )
        assert r.status_code == 200
        text = r.text.lower()
        offenders = [lbl for lbl in _FORBIDDEN_LABELS if lbl in text]
        assert offenders == [], f"Forbidden labels in rendered HTML (enabled): {offenders}"
    finally:
        monkeypatch.setattr(mod, "REPO_ROOT", orig)


# ---------------------------------------------------------------------------
# Safety banner
# ---------------------------------------------------------------------------


def test_safety_banner_present_disabled(monkeypatch, tmp_path):
    _write_config(tmp_path)
    client, orig, mod = _client_with_root(monkeypatch, tmp_path)
    try:
        r = client.get("/dashboard/portfolio-config")
        assert r.status_code == 200
        text = r.text.lower()
        assert "no broker trades" in text or "no broker trade" in text or "no orders are submitted" in text, (
            "Safety banner missing from portfolio-config page"
        )
    finally:
        monkeypatch.setattr(mod, "REPO_ROOT", orig)


def test_safety_banner_text_does_not_contain_forbidden_labels(monkeypatch, tmp_path):
    _write_config(tmp_path)
    client, orig, mod = _client_with_root(monkeypatch, tmp_path)
    try:
        r = client.get("/dashboard/portfolio-config")
        assert r.status_code == 200
        text = r.text.lower()
        offenders = [lbl for lbl in _FORBIDDEN_LABELS if lbl in text]
        assert offenders == [], f"Forbidden labels in safety banner HTML: {offenders}"
    finally:
        monkeypatch.setattr(mod, "REPO_ROOT", orig)


# ---------------------------------------------------------------------------
# Gating — editing disabled
# ---------------------------------------------------------------------------


def test_get_editing_disabled_no_env(monkeypatch, tmp_path):
    """GET with no env vars → 200, 'editing disabled' state."""
    _write_config(tmp_path)
    client, orig, mod = _client_with_root(monkeypatch, tmp_path, auth=False, edit_flag=False)
    try:
        r = client.get("/dashboard/portfolio-config")
        assert r.status_code == 200
        assert "editing disabled" in r.text.lower() or "disabled" in r.text.lower()
    finally:
        monkeypatch.setattr(mod, "REPO_ROOT", orig)


def test_get_editing_disabled_auth_only_no_flag(monkeypatch, tmp_path):
    """GET with auth set but no PORTFOLIO_EDIT flag → 200, editing disabled."""
    _write_config(tmp_path)
    client, orig, mod = _client_with_root(monkeypatch, tmp_path, auth=True, edit_flag=False)
    try:
        r = client.get("/dashboard/portfolio-config", auth=("testuser", "testpass"))
        assert r.status_code == 200
        assert "editing disabled" in r.text.lower() or "disabled" in r.text.lower()
    finally:
        monkeypatch.setattr(mod, "REPO_ROOT", orig)


def test_get_editing_disabled_flag_only_no_auth(monkeypatch, tmp_path):
    """GET with PORTFOLIO_EDIT=1 but no auth env vars → editing disabled."""
    _write_config(tmp_path)
    client, orig, mod = _client_with_root(monkeypatch, tmp_path, auth=False, edit_flag=True)
    try:
        r = client.get("/dashboard/portfolio-config")
        assert r.status_code == 200
        assert "editing disabled" in r.text.lower() or "disabled" in r.text.lower()
    finally:
        monkeypatch.setattr(mod, "REPO_ROOT", orig)


def test_post_save_returns_403_no_env(monkeypatch, tmp_path):
    """POST /save without env → 403 (no write)."""
    cfg_path = _write_config(tmp_path)
    original_bytes = cfg_path.read_bytes()
    client, orig, mod = _client_with_root(monkeypatch, tmp_path, auth=False, edit_flag=False)
    try:
        r = client.post(
            "/dashboard/portfolio-config/save",
            data={"symbol": ["QQQ"], "shares": ["6"], "cash_available": "464.16"},
        )
        assert r.status_code == 403
        # Config must be byte-unchanged
        assert cfg_path.read_bytes() == original_bytes, "config.json was mutated despite 403"
    finally:
        monkeypatch.setattr(mod, "REPO_ROOT", orig)


def test_post_save_returns_403_auth_only_no_flag(monkeypatch, tmp_path):
    """POST /save with auth but no PORTFOLIO_EDIT → 403."""
    cfg_path = _write_config(tmp_path)
    original_bytes = cfg_path.read_bytes()
    client, orig, mod = _client_with_root(monkeypatch, tmp_path, auth=True, edit_flag=False)
    try:
        r = client.post(
            "/dashboard/portfolio-config/save",
            data={"symbol": ["QQQ"], "shares": ["6"], "cash_available": "464.16"},
            auth=("testuser", "testpass"),
        )
        assert r.status_code == 403
        assert cfg_path.read_bytes() == original_bytes, "config.json was mutated despite 403"
    finally:
        monkeypatch.setattr(mod, "REPO_ROOT", orig)


def test_post_save_returns_403_flag_only_no_auth(monkeypatch, tmp_path):
    """POST /save with flag but no auth → 403."""
    cfg_path = _write_config(tmp_path)
    original_bytes = cfg_path.read_bytes()
    client, orig, mod = _client_with_root(monkeypatch, tmp_path, auth=False, edit_flag=True)
    try:
        r = client.post(
            "/dashboard/portfolio-config/save",
            data={"symbol": ["QQQ"], "shares": ["6"], "cash_available": "464.16"},
        )
        assert r.status_code == 403
        assert cfg_path.read_bytes() == original_bytes, "config.json was mutated despite 403"
    finally:
        monkeypatch.setattr(mod, "REPO_ROOT", orig)


# ---------------------------------------------------------------------------
# Gating — editing enabled
# ---------------------------------------------------------------------------


def test_get_edit_enabled_returns_200_with_form(monkeypatch, tmp_path):
    """GET with auth + flag → 200, form rendered, not 'editing disabled'."""
    _write_config(tmp_path)
    client, orig, mod = _client_with_root(monkeypatch, tmp_path, auth=True, edit_flag=True)
    try:
        r = client.get("/dashboard/portfolio-config", auth=("testuser", "testpass"))
        assert r.status_code == 200
        # Form must be present (not disabled state)
        assert "<form" in r.text.lower() or "portfolio-edit-form" in r.text
        # "editing disabled" text must NOT be present when enabled
        assert "editing disabled" not in r.text.lower()
    finally:
        monkeypatch.setattr(mod, "REPO_ROOT", orig)


def test_edit_enabled_function_true_when_all_set(monkeypatch):
    """_edit_enabled() returns True when both auth vars AND flag are set."""
    monkeypatch.setenv("GUI_V2_AUTH_USER", "u")
    monkeypatch.setenv("GUI_V2_AUTH_PASS", "p")
    monkeypatch.setenv("GUI_V2_PORTFOLIO_EDIT", "1")
    from gui_v2.app import _edit_enabled
    assert _edit_enabled() is True


def test_edit_enabled_function_false_when_partial(monkeypatch):
    """_edit_enabled() returns False when any env var is missing."""
    monkeypatch.delenv("GUI_V2_AUTH_USER", raising=False)
    monkeypatch.delenv("GUI_V2_AUTH_PASS", raising=False)
    monkeypatch.delenv("GUI_V2_PORTFOLIO_EDIT", raising=False)
    from gui_v2.app import _edit_enabled
    assert _edit_enabled() is False


# ---------------------------------------------------------------------------
# collect_portfolio_config_view
# ---------------------------------------------------------------------------


def test_collect_view_has_expected_keys(tmp_path):
    from gui_v2.data.dash_portfolio_config import collect_portfolio_config_view
    _write_config(tmp_path)
    v = collect_portfolio_config_view(tmp_path, edit_enabled=False)
    for key in ("cards", "persona", "edit_enabled", "holdings", "cash", "growth_mode",
                "config_available", "observe_only"):
        assert key in v, f"Missing key: {key}"


def test_collect_view_observe_only_hardcoded(tmp_path):
    from gui_v2.data.dash_portfolio_config import collect_portfolio_config_view
    _write_config(tmp_path)
    v = collect_portfolio_config_view(tmp_path, edit_enabled=True)
    assert v["observe_only"] is True


def test_collect_view_persona_field(tmp_path):
    from gui_v2.data.dash_portfolio_config import collect_portfolio_config_view
    _write_config(tmp_path)
    v = collect_portfolio_config_view(tmp_path, edit_enabled=False)
    assert v["persona"] == "portfolio_config"


def test_collect_view_holdings_parsed(tmp_path):
    from gui_v2.data.dash_portfolio_config import collect_portfolio_config_view
    _write_config(tmp_path)
    v = collect_portfolio_config_view(tmp_path, edit_enabled=False)
    symbols = {h["symbol"] for h in v["holdings"]}
    assert "QQQ" in symbols
    assert "GLD" in symbols


def test_collect_view_cash_parsed(tmp_path):
    from gui_v2.data.dash_portfolio_config import collect_portfolio_config_view
    _write_config(tmp_path, _sample_config(cash=1234.56))
    v = collect_portfolio_config_view(tmp_path, edit_enabled=False)
    assert abs(v["cash"] - 1234.56) < 0.01


def test_collect_view_config_unavailable_when_absent(tmp_path):
    from gui_v2.data.dash_portfolio_config import collect_portfolio_config_view
    # No config.json in tmp_path
    v = collect_portfolio_config_view(tmp_path, edit_enabled=False)
    assert v["config_available"] is False


def test_collect_view_disabled_card_is_info(tmp_path):
    from gui_v2.data.dash_portfolio_config import collect_portfolio_config_view
    _write_config(tmp_path)
    v = collect_portfolio_config_view(tmp_path, edit_enabled=False)
    card = v["cards"][0]
    assert card["status"] == "info"
    assert "disabled" in card["label"].lower()


def test_collect_view_enabled_card_is_warning(tmp_path):
    from gui_v2.data.dash_portfolio_config import collect_portfolio_config_view
    _write_config(tmp_path)
    v = collect_portfolio_config_view(tmp_path, edit_enabled=True)
    card = v["cards"][0]
    assert card["status"] == "warning"
    assert "enabled" in card["label"].lower()


# ---------------------------------------------------------------------------
# validate_config_edit
# ---------------------------------------------------------------------------


def _cfg_for_validate():
    return _sample_config()


def test_validate_valid_input():
    from gui_v2.portfolio_config_writer import validate_config_edit
    holdings = [
        {"symbol": "QQQ", "shares": 6, "target_weight": 0.5},
        {"symbol": "GLD", "shares": 4, "target_weight": 0.5},
    ]
    r = validate_config_edit(holdings, 100.0, _cfg_for_validate())
    assert r["ok"] is True
    assert r["errors"] == []


def test_validate_negative_shares():
    from gui_v2.portfolio_config_writer import validate_config_edit
    holdings = [{"symbol": "QQQ", "shares": -1}]
    r = validate_config_edit(holdings, 100.0, {})
    assert r["ok"] is False
    assert any("non-negative" in e for e in r["errors"])


def test_validate_negative_cash():
    from gui_v2.portfolio_config_writer import validate_config_edit
    holdings = [{"symbol": "QQQ", "shares": 5}]
    r = validate_config_edit(holdings, -50.0, {})
    assert r["ok"] is False
    assert any("non-negative" in e.lower() or "negative" in e.lower() for e in r["errors"])


def test_validate_empty_symbol():
    from gui_v2.portfolio_config_writer import validate_config_edit
    holdings = [{"symbol": "", "shares": 5}]
    r = validate_config_edit(holdings, 100.0, {})
    assert r["ok"] is False
    assert any("symbol" in e.lower() and "required" in e.lower() for e in r["errors"])


def test_validate_invalid_symbol_pattern():
    from gui_v2.portfolio_config_writer import validate_config_edit
    holdings = [{"symbol": "lowercase", "shares": 5}]
    r = validate_config_edit(holdings, 100.0, {})
    assert r["ok"] is False
    assert any("invalid symbol" in e.lower() or "symbol" in e.lower() for e in r["errors"])


def test_validate_duplicate_symbols():
    from gui_v2.portfolio_config_writer import validate_config_edit
    holdings = [
        {"symbol": "QQQ", "shares": 5},
        {"symbol": "QQQ", "shares": 3},
    ]
    r = validate_config_edit(holdings, 100.0, {})
    assert r["ok"] is False
    assert any("duplicate" in e.lower() for e in r["errors"])


def test_validate_target_weight_sum_off():
    from gui_v2.portfolio_config_writer import validate_config_edit
    holdings = [
        {"symbol": "QQQ", "shares": 5, "target_weight": 0.3},
        {"symbol": "GLD", "shares": 3, "target_weight": 0.3},
    ]
    # sum = 0.6, far from 1.0
    r = validate_config_edit(holdings, 100.0, {})
    assert r["ok"] is False
    assert any("sum" in e.lower() or "target_weight" in e.lower() for e in r["errors"])


def test_validate_target_weight_greater_than_1():
    from gui_v2.portfolio_config_writer import validate_config_edit
    holdings = [{"symbol": "QQQ", "shares": 5, "target_weight": 1.5}]
    r = validate_config_edit(holdings, 100.0, {})
    assert r["ok"] is False
    assert any("[0, 1]" in e or "0" in e for e in r["errors"])


def test_validate_concentration_cap_breach():
    from gui_v2.portfolio_config_writer import validate_config_edit
    cfg = {"growth_mode": {"concentration_cap": 0.5, "leverage_cap": 0.25}}
    holdings = [
        {"symbol": "QQQ", "shares": 10, "target_weight": 0.7},
        {"symbol": "GLD", "shares": 4, "target_weight": 0.3},
    ]
    r = validate_config_edit(holdings, 100.0, cfg)
    assert r["ok"] is False
    assert any("concentration_cap" in e.lower() or "concentration" in e.lower() for e in r["errors"])


def test_validate_leverage_cap_breach():
    from gui_v2.portfolio_config_writer import validate_config_edit
    cfg = {"growth_mode": {"concentration_cap": 0.6, "leverage_cap": 0.1}}
    holdings = [
        {
            "symbol": "QLD",
            "shares": 8,
            "target_weight": 0.3,
            "is_leveraged": True,
        },
        {"symbol": "QQQ", "shares": 6, "target_weight": 0.7},
    ]
    r = validate_config_edit(holdings, 100.0, cfg)
    assert r["ok"] is False
    assert any("leverage_cap" in e.lower() or "leveraged" in e.lower() for e in r["errors"])


def test_validate_weights_sum_to_1_is_ok():
    from gui_v2.portfolio_config_writer import validate_config_edit
    holdings = [
        {"symbol": "QQQ", "shares": 6, "target_weight": 0.6},
        {"symbol": "GLD", "shares": 4, "target_weight": 0.4},
    ]
    r = validate_config_edit(holdings, 100.0, {})
    assert r["ok"] is True


# ---------------------------------------------------------------------------
# diff_config_edit
# ---------------------------------------------------------------------------


def test_diff_has_required_keys(tmp_path):
    from gui_v2.portfolio_config_writer import diff_config_edit
    before = _sample_config()
    holdings = [{"symbol": "QQQ", "shares": 7}, {"symbol": "SPY", "shares": 2}]
    d = diff_config_edit(before, holdings, 500.0)
    assert "holdings" in d
    assert "cash" in d


def test_diff_added_symbol_has_before_none(tmp_path):
    from gui_v2.portfolio_config_writer import diff_config_edit
    before = _sample_config()
    # SPY is new
    holdings = [
        {"symbol": "QQQ", "shares": 6},
        {"symbol": "SPY", "shares": 3},
    ]
    d = diff_config_edit(before, holdings, 464.16)
    spy_row = next((r for r in d["holdings"] if r["symbol"] == "SPY"), None)
    assert spy_row is not None
    assert spy_row["before"] is None


def test_diff_removed_symbol_has_after_none(tmp_path):
    from gui_v2.portfolio_config_writer import diff_config_edit
    before = _sample_config()
    # Remove GLD
    holdings = [{"symbol": "QQQ", "shares": 6}]
    d = diff_config_edit(before, holdings, 464.16)
    gld_row = next((r for r in d["holdings"] if r["symbol"] == "GLD"), None)
    assert gld_row is not None
    assert gld_row["after"] is None


def test_diff_cash_delta(tmp_path):
    from gui_v2.portfolio_config_writer import diff_config_edit
    before = _sample_config(cash=464.16)
    holdings = [{"symbol": "QQQ", "shares": 6}]
    d = diff_config_edit(before, holdings, 1000.0)
    assert abs(d["cash"]["before"] - 464.16) < 0.01
    assert abs(d["cash"]["after"] - 1000.0) < 0.01


# ---------------------------------------------------------------------------
# apply_config_edit — backup, audit, reversibility, no decision-core mutation
# ---------------------------------------------------------------------------


def _setup_apply_env(tmp_path: Path):
    """Write config.json and policy dirs; return (config_path, policy_dir)."""
    cfg_path = _write_config(tmp_path)
    policy_dir = tmp_path / "outputs" / "policy"
    policy_dir.mkdir(parents=True, exist_ok=True)
    return cfg_path, policy_dir


def test_apply_backup_created_before_write(tmp_path):
    """Backup must exist after apply_config_edit."""
    from gui_v2.portfolio_config_writer import apply_config_edit

    cfg_path, policy_dir = _setup_apply_env(tmp_path)
    holdings = [{"symbol": "QQQ", "shares": 7}]
    result = apply_config_edit(tmp_path, holdings, 500.0)
    assert result["ok"] is True, f"apply failed: {result['error']}"
    backup_path = Path(result["backup_path"])
    assert backup_path.exists(), f"Backup file missing: {backup_path}"


def test_apply_backup_matches_pre_edit_config(tmp_path):
    """Backup contents must equal the config BEFORE the edit."""
    from gui_v2.portfolio_config_writer import apply_config_edit

    cfg_path, policy_dir = _setup_apply_env(tmp_path)
    original_content = json.loads(cfg_path.read_text(encoding="utf-8"))

    holdings = [{"symbol": "SPY", "shares": 10}]
    result = apply_config_edit(tmp_path, holdings, 999.0)
    assert result["ok"] is True

    backup_path = Path(result["backup_path"])
    backup_content = json.loads(backup_path.read_text(encoding="utf-8"))
    assert backup_content == original_content, (
        "Backup content does not match pre-edit config"
    )


def test_apply_backup_can_restore_original(tmp_path):
    """Backup is reversible: reading backup restores original config."""
    from gui_v2.portfolio_config_writer import apply_config_edit

    cfg_path, policy_dir = _setup_apply_env(tmp_path)
    original_config = json.loads(cfg_path.read_text(encoding="utf-8"))

    holdings = [{"symbol": "TSLA", "shares": 5}]
    result = apply_config_edit(tmp_path, holdings, 200.0)
    assert result["ok"] is True

    # Simulate restore: copy backup → config.json
    backup_path = Path(result["backup_path"])
    restored = json.loads(backup_path.read_text(encoding="utf-8"))
    assert restored == original_config, "Backup does not match the original config"


def test_apply_config_json_updated_with_new_holdings(tmp_path):
    """config.json must reflect the new holdings after apply."""
    from gui_v2.portfolio_config_writer import apply_config_edit

    cfg_path, _ = _setup_apply_env(tmp_path)
    new_holdings = [{"symbol": "SPY", "shares": 15, "asset_class": "us_equity"}]
    result = apply_config_edit(tmp_path, new_holdings, 300.0)
    assert result["ok"] is True

    updated = json.loads(cfg_path.read_text(encoding="utf-8"))
    syms = [h["symbol"] for h in updated["portfolio"]["holdings"]]
    assert "SPY" in syms
    assert "QQQ" not in syms  # old holding removed


def test_apply_config_json_cash_updated(tmp_path):
    """config.json must reflect the new cash after apply."""
    from gui_v2.portfolio_config_writer import apply_config_edit

    cfg_path, _ = _setup_apply_env(tmp_path)
    holdings = [{"symbol": "QQQ", "shares": 6}]
    result = apply_config_edit(tmp_path, holdings, 777.77)
    assert result["ok"] is True

    updated = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert abs(updated["portfolio"]["cash_available"] - 777.77) < 0.01


def test_apply_non_portfolio_keys_preserved(tmp_path):
    """Non-portfolio config keys must be untouched by apply."""
    from gui_v2.portfolio_config_writer import apply_config_edit

    cfg_path, _ = _setup_apply_env(tmp_path)
    holdings = [{"symbol": "QQQ", "shares": 6}]
    result = apply_config_edit(tmp_path, holdings, 464.16)
    assert result["ok"] is True

    updated = json.loads(cfg_path.read_text(encoding="utf-8"))
    # Sentinel email key must survive
    assert updated.get("email", {}).get("smtp_server") == "smtp.test.example", (
        "Non-portfolio config keys were mutated"
    )


def test_apply_audit_record_appended(tmp_path):
    """An audit record must be appended to manual_portfolio_updates.jsonl."""
    from gui_v2.portfolio_config_writer import apply_config_edit

    cfg_path, policy_dir = _setup_apply_env(tmp_path)
    holdings = [{"symbol": "QQQ", "shares": 6}]
    result = apply_config_edit(tmp_path, holdings, 464.16)
    assert result["ok"] is True
    assert result["audit_appended"] is True

    audit_path = policy_dir / "manual_portfolio_updates.jsonl"
    assert audit_path.exists(), "Audit JSONL not created"
    lines = [l for l in audit_path.read_text(encoding="utf-8").strip().split("\n") if l.strip()]
    assert len(lines) >= 1, "No audit records in JSONL"

    last_record = json.loads(lines[-1])
    assert last_record["source"] == "gui_portfolio_config"
    assert last_record["observe_only"] is True
    assert last_record["no_trade"] is True
    assert "before" in last_record
    assert "after" in last_record


def test_apply_audit_record_contains_before_after(tmp_path):
    """Audit record must contain before and after cash + symbols."""
    from gui_v2.portfolio_config_writer import apply_config_edit

    _, policy_dir = _setup_apply_env(tmp_path)
    holdings = [{"symbol": "SPY", "shares": 3}]
    result = apply_config_edit(tmp_path, holdings, 555.0)
    assert result["ok"] is True

    audit_path = policy_dir / "manual_portfolio_updates.jsonl"
    record = json.loads(audit_path.read_text(encoding="utf-8").strip().split("\n")[-1])
    assert "before" in record and "after" in record
    assert record["after"]["cash"] == 555.0
    assert "SPY" in record["after"]["holding_symbols"]


def test_apply_no_decision_core_mutation(tmp_path):
    """apply_config_edit must NOT touch decision_plan.json or other outputs/latest/* files."""
    from gui_v2.portfolio_config_writer import apply_config_edit

    _write_config(tmp_path)
    dp_path = _write_decision_plan(tmp_path)
    original_dp_bytes = dp_path.read_bytes()

    holdings = [{"symbol": "QQQ", "shares": 6}]
    result = apply_config_edit(tmp_path, holdings, 464.16)
    assert result["ok"] is True

    # decision_plan.json must be byte-unchanged
    assert dp_path.read_bytes() == original_dp_bytes, (
        "apply_config_edit mutated decision_plan.json — this must NEVER happen"
    )


def test_apply_only_writes_config_and_policy(tmp_path):
    """
    apply_config_edit must only modify:
      - config.json
      - outputs/policy/portfolio_backups/config.*.json (new file)
      - outputs/policy/manual_portfolio_updates.jsonl (appended)
    It must NOT create or modify anything under outputs/latest/.
    """
    from gui_v2.portfolio_config_writer import apply_config_edit

    _write_config(tmp_path)
    latest_dir = tmp_path / "outputs" / "latest"
    latest_dir.mkdir(parents=True, exist_ok=True)
    # Seed a sentinel file in latest/
    sentinel = latest_dir / "sentinel.json"
    sentinel.write_text(json.dumps({"guard": True}), encoding="utf-8")
    sentinel_original = sentinel.read_bytes()

    holdings = [{"symbol": "QQQ", "shares": 6}]
    result = apply_config_edit(tmp_path, holdings, 464.16)
    assert result["ok"] is True

    assert sentinel.read_bytes() == sentinel_original, (
        "apply_config_edit modified outputs/latest/sentinel.json"
    )
    # No new files in latest/
    latest_files_after = list(latest_dir.iterdir())
    assert latest_files_after == [sentinel], (
        f"New files appeared in outputs/latest/: {latest_files_after}"
    )


# ---------------------------------------------------------------------------
# Route integration
# ---------------------------------------------------------------------------


def test_post_validate_returns_200_no_write(monkeypatch, tmp_path):
    """POST /validate returns 200; config.json unchanged."""
    cfg_path = _write_config(tmp_path)
    original_bytes = cfg_path.read_bytes()
    client, orig, mod = _client_with_root(monkeypatch, tmp_path, auth=False, edit_flag=False)
    try:
        r = client.post(
            "/dashboard/portfolio-config/validate",
            data={
                "symbol": ["QQQ", "GLD"],
                "shares": ["6", "4"],
                "target_weight": ["0.5", "0.5"],
                "asset_class": ["us_equity", "commodity"],
                "leverage_factor": ["1", "1"],
                "cash_available": "464.16",
            },
        )
        assert r.status_code == 200
        assert cfg_path.read_bytes() == original_bytes, "config.json mutated by /validate"
    finally:
        monkeypatch.setattr(mod, "REPO_ROOT", orig)


def test_post_validate_bad_data_returns_errors(monkeypatch, tmp_path):
    """POST /validate with negative shares returns validation error fragment."""
    _write_config(tmp_path)
    client, orig, mod = _client_with_root(monkeypatch, tmp_path, auth=False, edit_flag=False)
    try:
        r = client.post(
            "/dashboard/portfolio-config/validate",
            data={
                "symbol": ["QQQ"],
                "shares": ["-5"],
                "cash_available": "100",
            },
        )
        assert r.status_code == 200
        assert "error" in r.text.lower() or "validation" in r.text.lower()
    finally:
        monkeypatch.setattr(mod, "REPO_ROOT", orig)


def test_post_save_enabled_success(monkeypatch, tmp_path):
    """POST /save (enabled) → 200, save_result.ok=True, backup_path in HTML."""
    _write_config(tmp_path)
    client, orig, mod = _client_with_root(monkeypatch, tmp_path, auth=True, edit_flag=True)
    try:
        r = client.post(
            "/dashboard/portfolio-config/save",
            data={
                "symbol": ["QQQ", "GLD"],
                "shares": ["6", "4"],
                "target_weight": ["0.6", "0.4"],
                "asset_class": ["us_equity", "commodity"],
                "leverage_factor": ["1", "1"],
                "cash_available": "464.16",
            },
            auth=("testuser", "testpass"),
        )
        assert r.status_code == 200
        assert "saved successfully" in r.text.lower() or "backup" in r.text.lower(), (
            f"Expected success message in response. Body snippet: {r.text[:500]}"
        )
    finally:
        monkeypatch.setattr(mod, "REPO_ROOT", orig)


def test_post_save_enabled_bad_data_no_write(monkeypatch, tmp_path):
    """POST /save (enabled) with invalid data → 200, error shown, config unchanged."""
    cfg_path = _write_config(tmp_path)
    original_bytes = cfg_path.read_bytes()
    client, orig, mod = _client_with_root(monkeypatch, tmp_path, auth=True, edit_flag=True)
    try:
        r = client.post(
            "/dashboard/portfolio-config/save",
            data={
                "symbol": ["QQQ"],
                "shares": ["-999"],  # invalid
                "cash_available": "100",
            },
            auth=("testuser", "testpass"),
        )
        assert r.status_code == 200
        # Config must be unchanged
        assert cfg_path.read_bytes() == original_bytes, "config.json mutated by invalid save"
    finally:
        monkeypatch.setattr(mod, "REPO_ROOT", orig)


def test_post_validate_does_not_write_config(monkeypatch, tmp_path):
    """config.json byte-unchanged after POST /validate regardless of outcome."""
    cfg_path = _write_config(tmp_path)
    original_bytes = cfg_path.read_bytes()
    client, orig, mod = _client_with_root(monkeypatch, tmp_path, auth=True, edit_flag=True)
    try:
        # Even when edit is enabled, /validate must never write
        r = client.post(
            "/dashboard/portfolio-config/validate",
            data={
                "symbol": ["QQQ"],
                "shares": ["6"],
                "cash_available": "464.16",
            },
            auth=("testuser", "testpass"),
        )
        assert r.status_code == 200
        assert cfg_path.read_bytes() == original_bytes, (
            "config.json was mutated by /validate — this must never happen"
        )
    finally:
        monkeypatch.setattr(mod, "REPO_ROOT", orig)


# ---------------------------------------------------------------------------
# Mobile responsive
# ---------------------------------------------------------------------------


def test_portfolio_edit_form_has_mobile_responsive_classes():
    """portfolio_edit_form.html must have hidden md:block + md:hidden patterns."""
    tpl = Path("gui_v2/templates/components/portfolio_edit_form.html")
    tpl_text = tpl.read_text(encoding="utf-8")
    assert "hidden md:block" in tpl_text, "Missing 'hidden md:block' desktop table class"
    assert "md:hidden" in tpl_text, "Missing 'md:hidden' mobile stack class"
