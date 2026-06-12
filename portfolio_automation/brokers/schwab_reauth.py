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
