import json
import urllib.error
import urllib.request
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


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return r.status, r.read().decode()


def test_listener_captures_valid_code(monkeypatch):
    monkeypatch.setattr(sr.oauth, "verify_state", lambda s, **k: s == "good")
    lis = sr._CallbackListener()
    port = lis.start()
    try:
        status, body = _get(port, f"{sr.CALLBACK_PATH}?code=ABC&state=good")
        assert status == 200 and "close this tab" in body.lower()
        assert lis.result_q.get(timeout=2) == {"code": "ABC"}
    finally:
        lis.stop()


def test_listener_rejects_bad_state(monkeypatch):
    monkeypatch.setattr(sr.oauth, "verify_state", lambda s, **k: False)
    lis = sr._CallbackListener()
    port = lis.start()
    try:
        try:
            _get(port, f"{sr.CALLBACK_PATH}?code=ABC&state=bad")
            assert False, "expected HTTPError 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
        assert lis.result_q.empty()
    finally:
        lis.stop()


def test_listener_handles_oauth_error(monkeypatch):
    lis = sr._CallbackListener()
    port = lis.start()
    try:
        status, _ = _get(port, f"{sr.CALLBACK_PATH}?error=access_denied")
        assert status == 200
        assert lis.result_q.get(timeout=2)["error"] == "access_denied"
    finally:
        lis.stop()


class _FakeProc:
    def __init__(self, *a, **k):
        self.terminated = False
        self.killed = False
        self._alive = True
    def poll(self):
        return None if self._alive else 0
    def terminate(self):
        self.terminated = True
        self._alive = False
    def wait(self, timeout=None):
        return 0
    def kill(self):
        self.killed = True
        self._alive = False


def test_tunnel_manager_starts_and_tears_down(monkeypatch):
    captured = {}
    def fake_popen(cmd, **k):
        captured["cmd"] = cmd
        return _FakeProc()
    monkeypatch.setattr(sr.subprocess, "Popen", fake_popen)
    with sr.TunnelManager("stockbot-reauth", 12345) as tm:
        assert "cloudflared" in captured["cmd"][0]
        assert "http://127.0.0.1:12345" in captured["cmd"]
        assert "stockbot-reauth" in captured["cmd"]
    assert tm._proc.terminated is True
