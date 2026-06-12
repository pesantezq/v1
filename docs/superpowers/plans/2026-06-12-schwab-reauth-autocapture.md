# Schwab Re-Auth Auto-Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the operator re-authorize Schwab with a single phone tap — a self-contained task brings up an on-demand cloudflared tunnel, captures the OAuth `?code=` server-side via an ephemeral listener, exchanges it, and tears the tunnel down.

**Architecture:** New `schwab_reauth.py` orchestrator (CLI `--begin`/`--check`) owns a state machine: generate CSRF nonce → start listener on `127.0.0.1:<ephemeral port>` → bring up named tunnel routing to it → surface authorize URL (email + print) → wait for one valid callback → `exchange_code()` → guaranteed teardown. State-nonce helpers are added to the existing `schwab_oauth.py`. Observe-only session artifact + daily health coverage. `gui_v2` is untouched; no standing public surface.

**Tech Stack:** Python stdlib (`http.server`, `subprocess`, `threading`, `queue`, `secrets`, `hmac`), existing `schwab_oauth`/`memo_email_sender`/`data_governance`, `cloudflared` (operator-provisioned named tunnel). `pytest`. Run Python via `.venv/bin/python3`.

**Conventions:** Additive + observe-only (`observe_only: true` hardcoded in artifacts); never touches the decision core; non-blocking. TDD: failing test → run-fail → minimal impl → run-pass → commit. Stage explicit paths (never `git commit -am`). Preserve `config/signal_registry.yaml` `default_weight: 0.4947` if the full suite mutates it.

---

## File Structure

- **Modify** `portfolio_automation/brokers/schwab_oauth.py` — add `STATE_PATH`, `STATE_TTL_SEC`, `generate_state()`, `verify_state()`. (`build_authorize_url(state=...)` already accepts the nonce.)
- **Create** `portfolio_automation/brokers/schwab_reauth.py` — `check_readiness()`, `_CallbackListener`, `TunnelManager`, `_surface_authorize_url()`, `run_begin()`, `_finish()`, CLI.
- **Modify** `tests/test_schwab_oauth.py` — nonce generate/verify tests.
- **Create** `tests/test_schwab_reauth.py` — readiness, listener, tunnel manager, orchestrator, artifact tests.
- **Modify** `.claude/commands/daily-tool-analysis.md` — read `schwab_reauth_session_status.json`.
- **Modify** `docs/schwab_integration.md` — cloudflared setup + auto-capture usage.
- **Modify** `docs/CHANGELOG_DECISIONS.md` — entry.
- **Operator (not committed):** add `SCHWAB_REAUTH_TUNNEL_NAME=stockbot-reauth` to `/opt/stockbot/.env`.

---

## Task 1: State nonce helpers in `schwab_oauth.py`

**Files:**
- Modify: `portfolio_automation/brokers/schwab_oauth.py`
- Test: `tests/test_schwab_oauth.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_schwab_oauth.py`:

```python
def test_generate_and_verify_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(oa, "STATE_PATH", tmp_path / "state.json")
    nonce = oa.generate_state()
    assert nonce and isinstance(nonce, str)
    assert oa.verify_state(nonce, consume=False) is True


def test_verify_state_rejects_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(oa, "STATE_PATH", tmp_path / "state.json")
    oa.generate_state()
    assert oa.verify_state("not-the-nonce") is False


def test_verify_state_rejects_expired(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(oa, "STATE_PATH", tmp_path / "state.json")
    nonce = oa.generate_state(now=int(time.time()) - oa.STATE_TTL_SEC - 5)
    assert oa.verify_state(nonce) is False


def test_verify_state_single_use_consumes(tmp_path, monkeypatch):
    monkeypatch.setattr(oa, "STATE_PATH", tmp_path / "state.json")
    nonce = oa.generate_state()
    assert oa.verify_state(nonce) is True          # consumes
    assert oa.verify_state(nonce) is False          # gone after one use


def test_verify_state_missing_file_false(tmp_path, monkeypatch):
    monkeypatch.setattr(oa, "STATE_PATH", tmp_path / "nope.json")
    assert oa.verify_state("anything") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python3 -m pytest -q tests/test_schwab_oauth.py -k state`
Expected: FAIL — `AttributeError: module ... has no attribute 'STATE_PATH'/'generate_state'`.

- [ ] **Step 3: Implement the helpers**

In `portfolio_automation/brokers/schwab_oauth.py`, add `import hmac` and `import secrets` to the imports, then add below the `TOKEN_PATH`/`REAUTH_WARN_SEC` constants:

```python
# Single-use CSRF state nonce for the auth-code flow (used by schwab_reauth
# auto-capture). Persisted 0600 with a short TTL; consumed on first match.
STATE_PATH = Path(__file__).resolve().parents[2] / "data" / "schwab_reauth_state.json"
STATE_TTL_SEC = 600  # 10 minutes


def generate_state(now: int | None = None) -> str:
    """Create + persist (0600) a single-use state nonce with a TTL; return it."""
    nonce = secrets.token_urlsafe(32)
    n = int(now if now is not None else time.time())
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({"state": nonce, "created_at": n,
                                      "expires_at": n + STATE_TTL_SEC}), encoding="utf-8")
    try:
        os.chmod(STATE_PATH, 0o600)
    except OSError:
        pass
    return nonce


def verify_state(candidate: str, *, now: int | None = None, consume: bool = True) -> bool:
    """Constant-time match against the persisted nonce. False if missing/expired/
    mismatched. Single-use: deletes the state file on a successful match."""
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return False
    stored = str(data.get("state", ""))
    n = int(now if now is not None else time.time())
    if not candidate or not stored or int(data.get("expires_at", 0)) < n:
        return False
    ok = hmac.compare_digest(str(candidate), stored)
    if ok and consume:
        try:
            STATE_PATH.unlink()
        except OSError:
            pass
    return ok
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python3 -m pytest -q tests/test_schwab_oauth.py -k state`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/brokers/schwab_oauth.py tests/test_schwab_oauth.py
git commit -m "feat(schwab): single-use CSRF state nonce for auth-code flow"
```

---

## Task 2: Readiness check (`check_readiness`)

**Files:**
- Create: `portfolio_automation/brokers/schwab_reauth.py`
- Test: `tests/test_schwab_reauth.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_schwab_reauth.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python3 -m pytest -q tests/test_schwab_reauth.py -k readiness`
Expected: FAIL — `ModuleNotFoundError: No module named '...schwab_reauth'`.

- [ ] **Step 3: Create the module skeleton + `check_readiness`**

Create `portfolio_automation/brokers/schwab_reauth.py`:

```python
# portfolio_automation/brokers/schwab_reauth.py
"""Schwab re-auth auto-capture. Observe-only; no-trade; non-blocking.

Operator-triggered self-contained task: brings up an on-demand named cloudflared
tunnel, runs an ephemeral 127.0.0.1 /schwab/callback listener, surfaces the
authorize URL (email + print), captures the OAuth code, exchanges it, and tears
the tunnel down (guaranteed). No standing public surface; gui_v2 untouched.
"""
from __future__ import annotations

import http.server
import json
import os
import queue
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from portfolio_automation.data_governance import OutputNamespace, safe_write_json
from portfolio_automation.brokers import broker_models as bm
from portfolio_automation.brokers import schwab_oauth as oauth
from portfolio_automation.brokers import schwab_reauth_notifier as notifier
from portfolio_automation import memo_email_sender as mes

TUNNEL_NAME_ENV = "SCHWAB_REAUTH_TUNNEL_NAME"
CALLBACK_PATH = "/schwab/callback"
DEFAULT_TIMEOUT_SEC = 300
_STATUS_FILENAME = "schwab_reauth_session_status.json"


def check_readiness(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Verify cloudflared + tunnel name + Schwab creds before starting a session."""
    env = env if env is not None else dict(os.environ)
    if shutil.which("cloudflared") is None:
        return {"ready": False, "reason": "cloudflared_not_installed",
                "hint": "install cloudflared; see docs/schwab_integration.md"}
    name = env.get(TUNNEL_NAME_ENV, "").strip()
    if not name:
        return {"ready": False, "reason": "tunnel_name_unset",
                "hint": f"set {TUNNEL_NAME_ENV} in .env"}
    if not oauth.is_configured():
        return {"ready": False, "reason": "schwab_unconfigured",
                "hint": "set SCHWAB_CLIENT_ID/SECRET/REDIRECT_URI"}
    return {"ready": True, "reason": None, "tunnel_name": name}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python3 -m pytest -q tests/test_schwab_reauth.py -k readiness`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/brokers/schwab_reauth.py tests/test_schwab_reauth.py
git commit -m "feat(schwab): re-auth module skeleton + readiness check"
```

---

## Task 3: Ephemeral callback listener (`_CallbackListener`)

**Files:**
- Modify: `portfolio_automation/brokers/schwab_reauth.py`
- Test: `tests/test_schwab_reauth.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_schwab_reauth.py`:

```python
import urllib.request


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
        assert lis.result_q.empty()  # no code captured
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
```

Add `import urllib.error` to the test file's imports.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python3 -m pytest -q tests/test_schwab_reauth.py -k listener`
Expected: FAIL — `AttributeError: ... has no attribute '_CallbackListener'`.

- [ ] **Step 3: Implement the listener**

Append to `portfolio_automation/brokers/schwab_reauth.py`:

```python
class _CallbackListener:
    """Ephemeral one-shot HTTP listener bound to 127.0.0.1:<random port>.
    Validates the state nonce, captures the code, and reports via result_q."""

    def __init__(self) -> None:
        self.result_q: "queue.Queue[dict]" = queue.Queue()
        self._httpd: http.server.HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int | None = None

    def start(self) -> int:
        result_q = self.result_q

        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a):  # no access log — avoid logging the code/query
                return

            def _html(self, msg: str) -> None:
                body = f"<html><body><p>{msg}</p></body></html>".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path != CALLBACK_PATH:
                    self.send_response(404)
                    self.end_headers()
                    return
                qs = parse_qs(parsed.query)
                err = (qs.get("error") or [None])[0]
                state = (qs.get("state") or [None])[0]
                code = (qs.get("code") or [None])[0]
                if err:
                    self._html("Authorization failed. You can close this tab.")
                    result_q.put({"error": bm.redact(err)})
                    return
                if not code or not oauth.verify_state(state or ""):
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"invalid request")
                    return
                self._html("Schwab re-auth received. You can close this tab.")
                result_q.put({"code": code})

        self._httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python3 -m pytest -q tests/test_schwab_reauth.py -k listener`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/brokers/schwab_reauth.py tests/test_schwab_reauth.py
git commit -m "feat(schwab): ephemeral one-shot callback listener"
```

---

## Task 4: Tunnel manager (`TunnelManager`)

**Files:**
- Modify: `portfolio_automation/brokers/schwab_reauth.py`
- Test: `tests/test_schwab_reauth.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_schwab_reauth.py`:

```python
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
    assert tm._proc.terminated is True  # teardown guaranteed on exit
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python3 -m pytest -q tests/test_schwab_reauth.py -k tunnel`
Expected: FAIL — `AttributeError: ... has no attribute 'TunnelManager'`.

- [ ] **Step 3: Implement the tunnel manager**

Append to `portfolio_automation/brokers/schwab_reauth.py`:

```python
class TunnelManager:
    """Context manager: runs `cloudflared tunnel run --url http://127.0.0.1:<port>
    <name>` and guarantees teardown on exit (terminate, then kill on timeout)."""

    def __init__(self, tunnel_name: str, local_port: int) -> None:
        self.tunnel_name = tunnel_name
        self.local_port = local_port
        self._proc: subprocess.Popen | None = None

    def __enter__(self) -> "TunnelManager":
        self._proc = subprocess.Popen(
            ["cloudflared", "tunnel", "run", "--url",
             f"http://127.0.0.1:{self.local_port}", self.tunnel_name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return self

    def __exit__(self, *_exc) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python3 -m pytest -q tests/test_schwab_reauth.py -k tunnel`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/brokers/schwab_reauth.py tests/test_schwab_reauth.py
git commit -m "feat(schwab): on-demand cloudflared tunnel manager with guaranteed teardown"
```

---

## Task 5: Authorize-URL surfacing (`_surface_authorize_url`)

**Files:**
- Modify: `portfolio_automation/brokers/schwab_reauth.py`
- Test: `tests/test_schwab_reauth.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_schwab_reauth.py`:

```python
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
    assert "https://auth.example/x?state=n" in out          # printed for terminal/SSH
    assert "https://auth.example/x?state=n" in sent["body"]  # emailed too
    assert sent["to"] == "me@gmail.com"


def test_surface_url_email_optional(capsys):
    sr._surface_authorize_url("https://auth.example/x", env={}, notify=False, sender=None)
    assert "https://auth.example/x" in capsys.readouterr().out  # print always; no email
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python3 -m pytest -q tests/test_schwab_reauth.py -k surface`
Expected: FAIL — `AttributeError: ... has no attribute '_surface_authorize_url'`.

- [ ] **Step 3: Implement URL surfacing**

Append to `portfolio_automation/brokers/schwab_reauth.py`:

```python
def _surface_authorize_url(url: str, *, env: dict[str, str], notify: bool,
                           sender: Callable[[Any, EmailMessage], dict] | None = None) -> None:
    """Print the authorize URL for the terminal/SSH operator and (when notify)
    email it via the re-auth notifier's transport so it can be tapped on a phone."""
    print("\nSchwab authorize URL (open on your phone, log in, approve):\n")
    print(f"  {url}\n")
    if not notify:
        return
    try:
        cfg = notifier._load_transport(env)
        if not cfg.has_valid_recipients() or not cfg.has_smtp_config():
            print("(email skipped — SMTP not fully configured)")
            return
        msg = EmailMessage()
        msg["Subject"] = "Schwab re-auth — tap to authorize"
        msg["From"] = cfg.from_addr
        msg["To"] = ", ".join(cfg.to_addrs)
        msg.set_content(
            "Tap to re-authorize Schwab (log in + approve in the app):\n\n"
            f"{url}\n\nThis link is valid for ~10 minutes. Advisory only — no trades.\n"
        )
        (sender or mes.send_daily_memo_email)(cfg, msg)
    except Exception as exc:  # email is best-effort; the printed URL always works
        print(f"(email failed — {type(exc).__name__}; use the printed URL)")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python3 -m pytest -q tests/test_schwab_reauth.py -k surface`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/brokers/schwab_reauth.py tests/test_schwab_reauth.py
git commit -m "feat(schwab): surface authorize URL via print + email"
```

---

## Task 6: Orchestrator state machine (`run_begin` + `_finish`)

**Files:**
- Modify: `portfolio_automation/brokers/schwab_reauth.py`
- Test: `tests/test_schwab_reauth.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_schwab_reauth.py`:

```python
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
    assert listener.stopped is True  # listener always stopped
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python3 -m pytest -q tests/test_schwab_reauth.py -k run_begin`
Expected: FAIL — `AttributeError: ... has no attribute 'run_begin'`.

- [ ] **Step 3: Implement the orchestrator**

Append to `portfolio_automation/brokers/schwab_reauth.py`:

```python
def _finish(base_dir, started: str, *, outcome: str, detail: str | None = None,
            new_expires_at: str | None = None) -> dict[str, Any]:
    status = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "observe_only": True, "no_trade": True,
        "started_at": started, "outcome": outcome, "detail": detail,
        "new_expires_at": new_expires_at,
    }
    try:
        safe_write_json(OutputNamespace.LATEST, _STATUS_FILENAME, status, base_dir=base_dir)
    except Exception:
        pass
    return status


def run_begin(*, base_dir: str | Path = "outputs", env: dict[str, str] | None = None,
              timeout: float = DEFAULT_TIMEOUT_SEC, notify: bool = True,
              tunnel_cls: Callable[..., Any] = TunnelManager,
              listener_cls: Callable[[], Any] = _CallbackListener) -> dict[str, Any]:
    """Run one auto-capture re-auth session. Never raises; always writes a status
    artifact and guarantees listener + tunnel teardown."""
    env = dict(env if env is not None else os.environ)
    started = datetime.now(timezone.utc).isoformat()
    ready = check_readiness(env)
    if not ready["ready"]:
        return _finish(base_dir, started, outcome="cloudflared_missing", detail=ready["reason"])

    listener = listener_cls()
    port = listener.start()
    try:
        nonce = oauth.generate_state()
        url = oauth.build_authorize_url(state=nonce)
        _surface_authorize_url(url, env=env, notify=notify)
        with tunnel_cls(ready["tunnel_name"], port):
            try:
                res = listener.result_q.get(timeout=timeout)
            except queue.Empty:
                return _finish(base_dir, started, outcome="timeout")
            if "error" in res:
                return _finish(base_dir, started, outcome="error", detail=res["error"])
            try:
                tok = oauth.exchange_code(res["code"])
            except Exception as exc:
                return _finish(base_dir, started, outcome="error", detail=bm.redact(str(exc)))
            new_exp = oauth.refresh_token_status(tok).get("expires_at")
            return _finish(base_dir, started, outcome="success", new_expires_at=new_exp)
    finally:
        listener.stop()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python3 -m pytest -q tests/test_schwab_reauth.py -k run_begin`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/brokers/schwab_reauth.py tests/test_schwab_reauth.py
git commit -m "feat(schwab): re-auth orchestrator state machine (success/timeout/error/teardown)"
```

---

## Task 7: No-secret artifact guard

**Files:**
- Test: `tests/test_schwab_reauth.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_schwab_reauth.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it passes (artifact already excludes secrets)**

Run: `.venv/bin/python3 -m pytest -q tests/test_schwab_reauth.py -k no_secret`
Expected: PASS — `_finish` writes only outcome/timestamps, never the token or code. (If it FAILS, the leak is a bug: fix `_finish` to exclude any secret-bearing field before moving on.)

- [ ] **Step 3: Commit**

```bash
git add tests/test_schwab_reauth.py
git commit -m "test(schwab): assert re-auth session artifact carries no secrets"
```

---

## Task 8: CLI (`--begin` / `--check`)

**Files:**
- Modify: `portfolio_automation/brokers/schwab_reauth.py`
- Test: `tests/test_schwab_reauth.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_schwab_reauth.py`:

```python
def test_cli_check_reports_readiness(monkeypatch, capsys):
    monkeypatch.setattr(sr.shutil, "which", lambda _n: None)
    rc = sr._cli_main(["--check"])
    out = capsys.readouterr().out.lower()
    assert rc == 1 and "cloudflared_not_installed" in out


def test_cli_begin_invokes_run_begin(monkeypatch):
    called = {}
    monkeypatch.setattr(sr, "run_begin", lambda **k: called.setdefault("k", k) or {"outcome": "timeout"})
    rc = sr._cli_main(["--begin", "--timeout", "1"])
    assert rc == 0 and called["k"]["timeout"] == 1.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python3 -m pytest -q tests/test_schwab_reauth.py -k cli`
Expected: FAIL — `AttributeError: ... has no attribute '_cli_main'`.

- [ ] **Step 3: Implement the CLI**

Append to `portfolio_automation/brokers/schwab_reauth.py`:

```python
def _cli_main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m portfolio_automation.brokers.schwab_reauth",
                                 description="Schwab re-auth auto-capture (observe-only, read-only).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--begin", action="store_true", help="Run one auto-capture re-auth session")
    g.add_argument("--check", action="store_true", help="Report cloudflared/tunnel readiness")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SEC)
    ap.add_argument("--no-email", action="store_true", help="Print URL only, do not email")
    args = ap.parse_args(argv)
    print("READ-ONLY MODE — no trading endpoints are called.")
    if args.check:
        r = check_readiness()
        print(f"ready={r['ready']} reason={r.get('reason')} hint={r.get('hint', '')}")
        return 0 if r["ready"] else 1
    st = run_begin(timeout=args.timeout, notify=not args.no_email)
    print(f"outcome={st['outcome']} detail={st.get('detail')} new_expires_at={st.get('new_expires_at')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python3 -m pytest -q tests/test_schwab_reauth.py -k cli`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/brokers/schwab_reauth.py tests/test_schwab_reauth.py
git commit -m "feat(schwab): re-auth CLI (--begin / --check)"
```

---

## Task 9: Daily health coverage

**Files:**
- Modify: `.claude/commands/daily-tool-analysis.md`

- [ ] **Step 1: Add the artifacts-read entry**

In `.claude/commands/daily-tool-analysis.md`, find the line added for `schwab_reauth_notification_status.json` (search `schwab_reauth_notification_status`) and add immediately after it:

```markdown
- `outputs/latest/schwab_reauth_session_status.json` → `outcome` (`success|timeout|error|cloudflared_missing`), `started_at`, `new_expires_at` (auto-capture re-auth session result; absent until the first `schwab_reauth --begin` run — absence is inert, report don't alert). `broker_reauth_capture_failed` = the most recent session `outcome ∈ {timeout, error, cloudflared_missing}` AND `broker_reauth_status ∈ {due_soon, expired}` (an attempted auto-capture re-auth did not complete while re-auth is actually due — advisory; fall back to the manual `exchange_code` flow).
```

- [ ] **Step 2: Add the AMBER dispatch line**

Find the AMBER line `broker_reauth_notify_failed` (search it) and add immediately after:

```markdown
- `broker_reauth_capture_failed` (an auto-capture re-auth session failed/timed out while re-auth is due — the manual `exchange_code` flow in `docs/schwab_integration.md` still works. Advisory; never RED.)
```

- [ ] **Step 3: Extend the body-grammar line 6f**

In line `6f. Broker-sync (always)`, append to the end of the description (before "Never RED"):

```markdown
If a `schwab_reauth_session_status.json` exists, append `"· last capture {outcome}"`; if `broker_reauth_capture_failed`, append `"(auto-capture failed — use manual exchange_code)"`.
```

- [ ] **Step 4: Verify the skill file still parses (no test; grep the new tokens)**

Run: `grep -c "schwab_reauth_session_status\|broker_reauth_capture_failed" .claude/commands/daily-tool-analysis.md`
Expected: `>= 3`.

> Note: the producer's healthy/degraded states are already test-covered by Task 6 (`outcome` ∈ success/timeout/error/cloudflared_missing). No new pytest needed for the prose skill.

- [ ] **Step 5: Commit**

```bash
git add .claude/commands/daily-tool-analysis.md
git commit -m "docs(daily-analysis): health coverage for re-auth auto-capture session"
```

---

## Task 10: Documentation (cloudflared setup + usage + CHANGELOG)

**Files:**
- Modify: `docs/schwab_integration.md`
- Modify: `docs/CHANGELOG_DECISIONS.md`

- [ ] **Step 1: Add the auto-capture section to `docs/schwab_integration.md`**

Find the heading `#### Optional: email heads-up (out-of-band)` and add a new subsection immediately before it:

````markdown
#### Optional: one-tap re-auth (auto-capture)

Instead of copy-pasting the `?code=`, the `schwab_reauth` task captures it
server-side through an on-demand cloudflared tunnel. One-time setup (your
Cloudflare account):

```bash
curl -L --output /tmp/cloudflared.deb \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i /tmp/cloudflared.deb
cloudflared tunnel login                         # pick the portfolio-ops-center.com zone
cloudflared tunnel create stockbot-reauth
cloudflared tunnel route dns stockbot-reauth stockbot.portfolio-ops-center.com
```

Then add `SCHWAB_REAUTH_TUNNEL_NAME=stockbot-reauth` to `.env`. Leave the tunnel
**created but not running** — the task starts it on demand and tears it down after.
Verify readiness: `python3 -m portfolio_automation.brokers.schwab_reauth --check`.

To re-auth: `python3 -m portfolio_automation.brokers.schwab_reauth --begin`. It
brings up the tunnel, emails + prints the authorize URL, waits up to 5 minutes,
then on a successful tap captures the code, exchanges it, and tears the tunnel
down. Outcome is written to `outputs/latest/schwab_reauth_session_status.json`.
````

- [ ] **Step 2: Add the CHANGELOG entry**

In `docs/CHANGELOG_DECISIONS.md`, add immediately after the `## How To Use This File` ... `---` preamble (before the first `## ` change entry):

```markdown
## Schwab Re-Auth Auto-Capture

### Date

`2026-06-12`

### Area

`architecture`

### Files / Functions

- `portfolio_automation/brokers/schwab_oauth.py` — `generate_state()` / `verify_state()` (single-use CSRF nonce).
- `portfolio_automation/brokers/schwab_reauth.py` (new) — `check_readiness`, `_CallbackListener`, `TunnelManager`, `_surface_authorize_url`, `run_begin`, CLI.
- `.claude/commands/daily-tool-analysis.md` — `broker_reauth_capture_failed` coverage.

### Decision

Operator-triggered one-tap re-auth: an ephemeral on-demand named cloudflared tunnel routes `stockbot.portfolio-ops-center.com/schwab/callback` to an in-process `127.0.0.1` listener that validates a nonce and exchanges the code. Tunnel is up only during the ~2-min window.

### Why

The Schwab 7-day refresh token requires a weekly browser re-auth; copy-pasting the code is friction. Auto-capture reduces it to one phone tap while keeping the operator-approval (Schwab MFA) property.

### Invariants Preserved

Observe-only session artifact (`observe_only:true`, `no_trade:true`); read-only/no-trade unchanged; no scoring/decision-core change; `gui_v2` untouched; no standing public endpoint (tunnel created-but-stopped between uses); state nonce single-use + TTL; token/code never logged.

### Downstream Impact

New artifact `schwab_reauth_session_status.json`. New tests `test_schwab_reauth.py` + nonce tests in `test_schwab_oauth.py`. Operator one-time cloudflared setup + `SCHWAB_REAUTH_TUNNEL_NAME` env.

### Artifact Health Severity

No severity change; artifact optional; producer is the operator-run `schwab_reauth --begin` (not a cron stage).

---
```

- [ ] **Step 3: Compile + commit**

Run: `.venv/bin/python3 -m py_compile portfolio_automation/brokers/schwab_reauth.py`
Expected: no output (success).

```bash
git add docs/schwab_integration.md docs/CHANGELOG_DECISIONS.md
git commit -m "docs(schwab): re-auth auto-capture setup, usage, and CHANGELOG"
```

---

## Task 11: Full-suite regression + push

**Files:** none (verification)

- [ ] **Step 1: Run the full broker + oauth suite**

Run: `.venv/bin/python3 -m pytest -q tests/test_schwab_reauth.py tests/test_schwab_oauth.py tests/test_schwab_reauth_notifier.py tests/test_schwab_status.py tests/test_schwab_sync.py`
Expected: all PASS.

- [ ] **Step 2: Run the full suite (collection + run)**

Run: `.venv/bin/python3 -m pytest -q`
Expected: PASS except the 3 known pre-existing failures (`test_run_loop_summary_includes_oos_window`, 2× `test_tuning_proposals`). Confirm no NEW failures.

- [ ] **Step 3: Restore signal_registry if the full suite mutated it**

Run: `grep -n "default_weight: 0.4947" config/signal_registry.yaml || git checkout -- config/signal_registry.yaml`
Expected: the line is present (or restored). Also `git status --short config/` should be clean before pushing.

- [ ] **Step 4: Push**

```bash
git push origin main
```

- [ ] **Step 5: Provide VPS validation block to the operator**

After cloudflared setup, the operator runs:
```bash
cd /opt/stockbot && set -a; . ./.env; set +a
.venv/bin/python3 -m portfolio_automation.brokers.schwab_reauth --check       # expect ready=True
.venv/bin/python3 -m portfolio_automation.brokers.schwab_reauth --begin       # tap the link, approve
.venv/bin/python3 -c "import json;print(json.load(open('outputs/latest/broker_sync_status.json'))['reauth_status'])"  # expect ok
```

---

## Notes for the implementer

- Run all Python via `.venv/bin/python3`.
- The `schwab_reauth_notifier._load_transport` reuse (Task 5) already falls back to the legacy Gmail env vars (`SMTP_SERVER`/`EMAIL_USER`/`EMAIL_PASS`/`EMAIL_TO`) — no new SMTP config needed.
- Do NOT touch `decision_engine.py`, scoring, or `gui_v2`. This feature is operator-run and observe-only.
- The optional terminal QR enhancement from the spec is intentionally omitted (YAGNI; the printed + emailed URL covers the phone-tap path). Add later only if a no-heavy-dependency renderer is desired.
