# portfolio_automation/brokers/schwab_oauth.py
"""Schwab OAuth2 (auth-code + refresh) + conservative gitignored token storage.
Secrets via env only; tokens never logged. READ-ONLY scopes; no trade auth."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import urlencode

from portfolio_automation.brokers.broker_models import redact

_AUTH_BASE = "https://api.schwabapi.com/v1/oauth/authorize"
_TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
# data/ is gitignored at repo root -> token file is auto-protected.
TOKEN_PATH = Path(__file__).resolve().parents[2] / "data" / "schwab_token.json"


def _env(key: str) -> str:
    return os.environ.get(key, "").strip()


def is_configured() -> bool:
    return bool(_env("SCHWAB_CLIENT_ID") and _env("SCHWAB_CLIENT_SECRET") and _env("SCHWAB_REDIRECT_URI"))


def read_only_mode() -> bool:
    # default true; trading is never implemented regardless of this flag.
    return _env("SCHWAB_READ_ONLY_MODE").lower() not in ("0", "false", "no")


def build_authorize_url(state: str = "stockbot") -> str:
    """Step-1 of auth-code flow. Contains client_id + redirect_uri only — NOT the secret."""
    params = {"response_type": "code", "client_id": _env("SCHWAB_CLIENT_ID"),
              "redirect_uri": _env("SCHWAB_REDIRECT_URI"), "state": state}
    return f"{_AUTH_BASE}?{urlencode(params)}"


def save_token(token: dict) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(json.dumps(token), encoding="utf-8")
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass


def load_token() -> dict | None:
    try:
        return json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return None


def _post_token(data: dict) -> dict:
    """Single POST to the token endpoint. Network isolated here; raises on failure
    with a REDACTED message. Tests monkeypatch this."""
    import requests  # local import so the module loads without requests in pure paths
    resp = requests.post(_TOKEN_URL, data=data, auth=(_env("SCHWAB_CLIENT_ID"), _env("SCHWAB_CLIENT_SECRET")),
                         headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(redact(f"token endpoint {resp.status_code}: {resp.text}"))
    tok = resp.json()
    tok["expires_at"] = int(time.time()) + int(tok.get("expires_in", 1800))
    return tok


def exchange_code(code: str) -> dict:
    tok = _post_token({"grant_type": "authorization_code", "code": code,
                       "redirect_uri": _env("SCHWAB_REDIRECT_URI")})
    save_token(tok)
    return tok


def refresh(token: dict) -> dict:
    tok = _post_token({"grant_type": "refresh_token", "refresh_token": token.get("refresh_token", "")})
    if "refresh_token" not in tok:
        tok["refresh_token"] = token.get("refresh_token", "")
    save_token(tok)
    return tok


def valid_access_token() -> str | None:
    """Return a fresh access token, refreshing if expired. None if unauthenticated."""
    tok = load_token()
    if not tok:
        return None
    if int(tok.get("expires_at", 0)) <= int(time.time()) + 30:
        try:
            tok = refresh(tok)
        except Exception:
            return None
    return tok.get("access_token")
