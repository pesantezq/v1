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
