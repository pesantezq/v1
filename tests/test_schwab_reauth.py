import json
import os
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


def test_tunnel_manager_writes_isolated_config_and_tears_down(monkeypatch):
    captured = {}
    def fake_popen(cmd, **k):
        captured["cmd"] = cmd
        i = cmd.index("--config")
        captured["cfg_path"] = cmd[i + 1]
        captured["cfg_body"] = open(cmd[i + 1]).read()
        return _FakeProc()
    monkeypatch.setattr(sr.subprocess, "Popen", fake_popen)
    with sr.TunnelManager("stockbot-reauth", 12345, "stockbot.example.com") as tm:
        cmd = captured["cmd"]
        assert "cloudflared" in cmd[0]
        # uses an isolated --config (NOT --url; cloudflared refuses --url when the
        # ambient config has an ingress block, which is the dashboard-reuse bug).
        assert "--config" in cmd and "--url" not in cmd
        assert cmd[-2:] == ["run", "stockbot-reauth"]
        # the isolated config routes the Schwab callback host to the listener,
        # with a 404 fallback — independent of /root/.cloudflared/config.yml.
        body = captured["cfg_body"]
        assert "stockbot.example.com" in body
        assert "http://127.0.0.1:12345" in body
        assert "http_status:404" in body
        assert os.path.exists(captured["cfg_path"])
    assert tm._proc.terminated is True
    # temp config is removed on teardown
    assert not os.path.exists(captured["cfg_path"])


def test_surface_url_prints_and_emails(capsys):
    sent = {}
    def fake_sender(cfg, msg):
        sent["to"] = msg["To"]; sent["body"] = msg.get_content()
        return {"attempted": True, "sent": True}
    env = {"SMTP_SERVER": "smtp.gmail.com", "EMAIL_USER": "me@gmail.com",
           "EMAIL_PASS": "pw", "EMAIL_TO": "me@gmail.com"}
    sr._surface_authorize_url("https://auth.example/x?state=n", env=env,
                              notify=True, sender=fake_sender)
    out = capsys.readouterr().out
    assert "https://auth.example/x?state=n" in out
    assert "https://auth.example/x?state=n" in sent["body"]
    assert sent["to"] == "me@gmail.com"


def test_surface_url_email_optional(capsys):
    sr._surface_authorize_url("https://auth.example/x", env={}, notify=False, sender=None)
    assert "https://auth.example/x" in capsys.readouterr().out


class _FakeListener:
    """Listener double that immediately yields a preset result."""
    def __init__(self, result):
        self._result = result
        self.result_q = __import__("queue").Queue()
        self.stopped = False
        self.port = 9999
    def start(self):
        if self._result is not None:
            self.result_q.put(self._result)
        return self.port
    def stop(self):
        self.stopped = True


class _NoopTunnel:
    def __init__(self, *a, **k): self.entered = False; self.exited = False
    def __enter__(self): self.entered = True; return self
    def __exit__(self, *a): self.exited = True


def _ready(monkeypatch):
    monkeypatch.setattr(sr.shutil, "which", lambda _n: "/usr/bin/cloudflared")
    monkeypatch.setattr(sr.oauth, "is_configured", lambda: True)
    monkeypatch.setattr(sr.oauth, "generate_state", lambda: "nonce")
    monkeypatch.setattr(sr.oauth, "build_authorize_url", lambda state="x": f"https://auth/x?state={state}")
    monkeypatch.setattr(sr, "_surface_authorize_url", lambda *a, **k: None)


def test_run_begin_success(tmp_path, monkeypatch):
    _ready(monkeypatch)
    monkeypatch.setattr(sr.oauth, "exchange_code", lambda code: {"refresh_token_expires_at": 9999999999})
    monkeypatch.setattr(sr.oauth, "refresh_token_status",
                        lambda tok: {"expires_at": "2026-06-26T00:00:00+00:00"})
    tunnel = _NoopTunnel
    st = sr.run_begin(base_dir=tmp_path, env={"SCHWAB_REAUTH_TUNNEL_NAME": "t"},
                      tunnel_cls=tunnel, listener_cls=lambda: _FakeListener({"code": "ABC"}))
    assert st["outcome"] == "success" and st["new_expires_at"] == "2026-06-26T00:00:00+00:00"
    p = get_output_path(OutputNamespace.LATEST, "schwab_reauth_session_status.json", base_dir=tmp_path)
    assert json.loads(p.read_text())["observe_only"] is True


def test_run_begin_passes_callback_host_from_redirect_uri(tmp_path, monkeypatch):
    _ready(monkeypatch)
    monkeypatch.setattr(sr.oauth, "exchange_code", lambda code: {})
    monkeypatch.setattr(sr.oauth, "refresh_token_status", lambda tok: {"expires_at": "x"})
    seen = {}

    class _CapTunnel:
        def __init__(self, name, port, callback_host, *a, **k):
            seen["name"] = name; seen["port"] = port; seen["host"] = callback_host
        def __enter__(self): return self
        def __exit__(self, *a): pass

    sr.run_begin(
        base_dir=tmp_path,
        env={"SCHWAB_REAUTH_TUNNEL_NAME": "t",
             "SCHWAB_REDIRECT_URI": "https://stockbot.example.com/schwab/callback"},
        tunnel_cls=_CapTunnel, listener_cls=lambda: _FakeListener({"code": "ABC"}),
    )
    assert seen["host"] == "stockbot.example.com"


def test_run_begin_cloudflared_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(sr.shutil, "which", lambda _n: None)
    st = sr.run_begin(base_dir=tmp_path, env={"SCHWAB_REAUTH_TUNNEL_NAME": "t"})
    assert st["outcome"] == "cloudflared_missing"


def test_run_begin_timeout(tmp_path, monkeypatch):
    _ready(monkeypatch)
    st = sr.run_begin(base_dir=tmp_path, env={"SCHWAB_REAUTH_TUNNEL_NAME": "t"}, timeout=0.2,
                      tunnel_cls=_NoopTunnel, listener_cls=lambda: _FakeListener(None))
    assert st["outcome"] == "timeout"


def test_run_begin_oauth_error(tmp_path, monkeypatch):
    _ready(monkeypatch)
    st = sr.run_begin(base_dir=tmp_path, env={"SCHWAB_REAUTH_TUNNEL_NAME": "t"},
                      tunnel_cls=_NoopTunnel, listener_cls=lambda: _FakeListener({"error": "access_denied"}))
    assert st["outcome"] == "error" and "access_denied" in (st["detail"] or "")


def test_run_begin_exchange_failure(tmp_path, monkeypatch):
    _ready(monkeypatch)
    def boom(code): raise RuntimeError("bad code")
    monkeypatch.setattr(sr.oauth, "exchange_code", boom)
    st = sr.run_begin(base_dir=tmp_path, env={"SCHWAB_REAUTH_TUNNEL_NAME": "t"},
                      tunnel_cls=_NoopTunnel, listener_cls=lambda: _FakeListener({"code": "ABC"}))
    assert st["outcome"] == "error"


def test_run_begin_teardown_always_runs(tmp_path, monkeypatch):
    _ready(monkeypatch)
    monkeypatch.setattr(sr.oauth, "exchange_code", lambda code: {})
    monkeypatch.setattr(sr.oauth, "refresh_token_status", lambda tok: {"expires_at": None})
    listener = _FakeListener({"code": "ABC"})
    sr.run_begin(base_dir=tmp_path, env={"SCHWAB_REAUTH_TUNNEL_NAME": "t"},
                 tunnel_cls=_NoopTunnel, listener_cls=lambda: listener)
    assert listener.stopped is True


def test_session_status_has_no_secret(tmp_path, monkeypatch):
    _ready(monkeypatch)
    monkeypatch.setattr(sr.oauth, "exchange_code",
                        lambda code: {"access_token": "SECRET-AT", "refresh_token": "SECRET-RT"})
    monkeypatch.setattr(sr.oauth, "refresh_token_status", lambda tok: {"expires_at": "2026-06-26T00:00:00+00:00"})
    sr.run_begin(base_dir=tmp_path, env={"SCHWAB_REAUTH_TUNNEL_NAME": "t"},
                 tunnel_cls=_NoopTunnel, listener_cls=lambda: _FakeListener({"code": "SECRET-CODE"}))
    blob = get_output_path(OutputNamespace.LATEST, "schwab_reauth_session_status.json",
                           base_dir=tmp_path).read_text()
    for leak in ("SECRET-AT", "SECRET-RT", "SECRET-CODE"):
        assert leak not in blob


def test_cli_check_reports_readiness(monkeypatch, capsys):
    monkeypatch.setattr(sr.shutil, "which", lambda _n: None)
    rc = sr._cli_main(["--check"])
    out = capsys.readouterr().out.lower()
    assert rc == 1 and "cloudflared_not_installed" in out


def test_cli_begin_invokes_run_begin(monkeypatch):
    called = {}
    def fake(**k):
        called["k"] = k
        return {"outcome": "success"}
    monkeypatch.setattr(sr, "run_begin", fake)
    rc = sr._cli_main(["--begin", "--timeout", "1"])
    assert rc == 0 and called["k"]["timeout"] == 1.0


def test_cli_begin_nonsuccess_returns_1(monkeypatch):
    monkeypatch.setattr(sr, "run_begin", lambda **k: {"outcome": "timeout"})
    assert sr._cli_main(["--begin"]) == 1


def test_run_begin_listener_start_fails(tmp_path, monkeypatch):
    _ready(monkeypatch)
    class _BoomListener:
        result_q = __import__("queue").Queue()
        def start(self): raise OSError("port busy")
        def stop(self): pass
    st = sr.run_begin(base_dir=tmp_path, env={"SCHWAB_REAUTH_TUNNEL_NAME": "t"},
                      tunnel_cls=_NoopTunnel, listener_cls=lambda: _BoomListener())
    assert st["outcome"] == "error"  # never raises; writes an error status instead
