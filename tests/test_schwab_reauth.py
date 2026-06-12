import json
from portfolio_automation.brokers import schwab_reauth as sr
from portfolio_automation.data_governance import OutputNamespace, get_output_path


def test_readiness_cloudflared_missing(monkeypatch):
    monkeypatch.setattr(sr.shutil, "which", lambda _n: None)
    r = sr.check_readiness(env={"SCHWAB_REAUTH_TUNNEL_NAME": "t"})
    assert r["ready"] is False and r["reason"] == "cloudflared_not_installed"


def test_readiness_tunnel_name_unset(monkeypatch):
    monkeypatch.setattr(sr.shutil, "which", lambda _n: "/usr/bin/cloudflared")
    r = sr.check_readiness(env={})
    assert r["ready"] is False and r["reason"] == "tunnel_name_unset"


def test_readiness_schwab_unconfigured(monkeypatch):
    monkeypatch.setattr(sr.shutil, "which", lambda _n: "/usr/bin/cloudflared")
    monkeypatch.setattr(sr.oauth, "is_configured", lambda: False)
    r = sr.check_readiness(env={"SCHWAB_REAUTH_TUNNEL_NAME": "t"})
    assert r["ready"] is False and r["reason"] == "schwab_unconfigured"


def test_readiness_ok(monkeypatch):
    monkeypatch.setattr(sr.shutil, "which", lambda _n: "/usr/bin/cloudflared")
    monkeypatch.setattr(sr.oauth, "is_configured", lambda: True)
    r = sr.check_readiness(env={"SCHWAB_REAUTH_TUNNEL_NAME": "stockbot-reauth"})
    assert r["ready"] is True and r["tunnel_name"] == "stockbot-reauth"
