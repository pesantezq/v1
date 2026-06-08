import json
from pathlib import Path
from portfolio_automation.brokers import schwab_oauth as oa


def test_is_configured_reads_env(monkeypatch):
    monkeypatch.delenv("SCHWAB_CLIENT_ID", raising=False)
    assert oa.is_configured() is False
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "cid")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "csec")
    monkeypatch.setenv("SCHWAB_REDIRECT_URI", "https://127.0.0.1/cb")
    assert oa.is_configured() is True


def test_build_authorize_url_has_no_secret(monkeypatch):
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "cid")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "csec-SEKRET")
    monkeypatch.setenv("SCHWAB_REDIRECT_URI", "https://127.0.0.1/cb")
    url = oa.build_authorize_url()
    assert "cid" in url and "csec-SEKRET" not in url  # secret never in authorize URL
    assert url.startswith("https://api.schwabapi.com/v1/oauth/authorize")


def test_token_save_load_roundtrip_and_perms(tmp_path, monkeypatch):
    p = tmp_path / "schwab_token.json"
    monkeypatch.setattr(oa, "TOKEN_PATH", p)
    oa.save_token({"access_token": "a", "refresh_token": "r", "expires_at": 999})
    assert oa.load_token()["access_token"] == "a"
    import os, stat
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600  # 0600


def test_load_token_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(oa, "TOKEN_PATH", tmp_path / "nope.json")
    assert oa.load_token() is None
