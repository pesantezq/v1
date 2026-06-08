import json
from pathlib import Path
from portfolio_automation.brokers import schwab_sync as sync
import portfolio_automation.brokers.schwab_oauth as oauth_mod


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
        assert leak not in blob


def test_sync_error_path_redacts_secret_in_status(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "cid")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "csec")
    monkeypatch.setenv("SCHWAB_REDIRECT_URI", "https://127.0.0.1/cb")
    monkeypatch.setattr(sync.oauth, "valid_access_token", lambda: "TOK")

    def boom():
        raise RuntimeError("network fail access_token=LEAKED_TOKEN_XYZ client_secret=SHH")

    import portfolio_automation.brokers.schwab_client as cl
    monkeypatch.setattr(cl.SchwabClient, "get_account_numbers", lambda self: boom())
    st = sync.run_sync(root=tmp_path)
    written = json.loads((tmp_path / "outputs/latest/broker_sync_status.json").read_text())
    assert written["overall_status"] == "error"
    for leak in ("LEAKED_TOKEN_XYZ", "SHH"):
        assert leak not in json.dumps(written)


def test_reconcile_does_not_raise_on_non_dict_artifact(tmp_path):
    (tmp_path / "outputs/latest").mkdir(parents=True)
    (tmp_path / "outputs/latest/schwab_portfolio_snapshot.json").write_text(json.dumps([1, 2, 3]))
    (tmp_path / "outputs/latest/schwab_positions.json").write_text(json.dumps("garbage"))
    out = sync.run_reconcile(root=tmp_path)  # must NOT raise
    assert "summary_status" in out
