import json
from pathlib import Path
from portfolio_automation.brokers import schwab_sync as sync


def test_status_when_unconfigured_writes_artifact(tmp_path, monkeypatch):
    monkeypatch.delenv("SCHWAB_CLIENT_ID", raising=False)
    st = sync.run_status(root=tmp_path)
    assert st["overall_status"] == "unconfigured"
    assert st["read_only_mode"] is True and st["trading_enabled"] is False
    p = tmp_path / "outputs/latest/broker_sync_status.json"
    assert p.exists() and json.loads(p.read_text())["source"] == "schwab"


def test_sync_unconfigured_is_fail_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("SCHWAB_CLIENT_ID", raising=False)
    st = sync.run_sync(root=tmp_path)  # must not raise, must not network
    assert st["overall_status"] in ("unconfigured", "disabled")


def test_reconcile_from_fixture_writes_artifacts(tmp_path, monkeypatch):
    # seed a snapshot+positions as if a sync had run, plus a config
    (tmp_path / "outputs/latest").mkdir(parents=True)
    (tmp_path / "outputs/latest/schwab_portfolio_snapshot.json").write_text(
        json.dumps({"generated_at": "t", "totals": {"market_value": 5400, "cash": 464.16}}))
    (tmp_path / "outputs/latest/schwab_positions.json").write_text(
        json.dumps({"positions": [{"symbol": "QQQ", "quantity": 6}]}))
    (tmp_path / "config.json").write_text(json.dumps(
        {"portfolio": {"cash_available": 464.16, "holdings": [{"symbol": "QQQ", "shares": 6}]}}))
    out = sync.run_reconcile(root=tmp_path)
    assert out["summary_status"] in ("ok", "mismatch")
    assert (tmp_path / "outputs/latest/portfolio_reconciliation.json").exists()
    assert (tmp_path / "outputs/latest/portfolio_config_update_proposal.json").exists()


def test_no_secrets_in_any_written_artifact(tmp_path, monkeypatch):
    monkeypatch.delenv("SCHWAB_CLIENT_ID", raising=False)
    sync.run_status(root=tmp_path)
    blob = ""
    for p in (tmp_path / "outputs/latest").glob("*.json"):
        blob += p.read_text()
    for leak in ("access_token", "client_secret", "refresh_token"):
        assert leak not in blob or "<redacted>" in blob
