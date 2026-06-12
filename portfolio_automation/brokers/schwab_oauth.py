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

# Schwab refresh tokens live 7 days; Schwab issues NO rolling replacement, so a
# full browser re-auth is mandatory when this lapses. We anchor the 7-day clock at
# exchange_code() (the browser flow) and surface time-remaining so the operator is
# warned BEFORE it expires rather than discovering a silently stale snapshot.
REFRESH_TOKEN_TTL_SEC = 7 * 24 * 3600
# Warn this far ahead of refresh-token expiry so re-auth stays a planned ~30s task.
REAUTH_WARN_SEC = 2 * 24 * 3600


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


def _stamp_refresh_expiry(tok: dict, *, prev: dict | None, now: int | None = None) -> None:
    """Anchor/carry the 7-day refresh-token clock. A genuinely new refresh token
    (prev is None) starts a fresh window; otherwise carry the prior anchor forward
    unchanged — an access-token refresh does NOT extend the refresh-token life."""
    if prev is not None and prev.get("refresh_token_expires_at"):
        tok["refresh_token_obtained_at"] = prev.get("refresh_token_obtained_at")
        tok["refresh_token_expires_at"] = prev.get("refresh_token_expires_at")
        return
    n = int(now if now is not None else time.time())
    tok["refresh_token_obtained_at"] = n
    tok["refresh_token_expires_at"] = n + REFRESH_TOKEN_TTL_SEC


def exchange_code(code: str) -> dict:
    tok = _post_token({"grant_type": "authorization_code", "code": code,
                       "redirect_uri": _env("SCHWAB_REDIRECT_URI")})
    _stamp_refresh_expiry(tok, prev=None)  # browser auth -> fresh 7-day window
    save_token(tok)
    return tok


def refresh(token: dict) -> dict:
    tok = _post_token({"grant_type": "refresh_token", "refresh_token": token.get("refresh_token", "")})
    rotated = bool(tok.get("refresh_token")) and tok.get("refresh_token") != token.get("refresh_token")
    if "refresh_token" not in tok:
        tok["refresh_token"] = token.get("refresh_token", "")
    # rotated -> Schwab issued a new refresh token (fresh window); else carry prior anchor.
    _stamp_refresh_expiry(tok, prev=(None if rotated else token))
    save_token(tok)
    return tok


def refresh_token_status(tok: dict | None = None, *, now: int | None = None) -> dict:
    """Observe-only view of the 7-day refresh-token clock. Never raises.
    reauth_status: ok | due_soon | expired | unknown (legacy/untracked token)."""
    if tok is None:
        tok = load_token() or {}
    exp = tok.get("refresh_token_expires_at")
    if not exp:
        return {"tracked": False, "expires_at": None, "seconds_remaining": None,
                "days_remaining": None, "expired": False, "reauth_status": "unknown"}
    n = int(now if now is not None else time.time())
    rem = int(exp) - n
    expired = rem <= 0
    due_soon = 0 < rem <= REAUTH_WARN_SEC
    from datetime import datetime, timezone
    return {
        "tracked": True,
        "expires_at": datetime.fromtimestamp(int(exp), timezone.utc).isoformat(),
        "seconds_remaining": rem,
        "days_remaining": round(rem / 86400, 2),
        "expired": expired,
        "reauth_status": "expired" if expired else ("due_soon" if due_soon else "ok"),
    }


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
