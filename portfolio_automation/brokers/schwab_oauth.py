# portfolio_automation/brokers/schwab_oauth.py
"""Schwab OAuth2 (auth-code + refresh) + conservative token storage.
Secrets via env only; tokens never logged. READ-ONLY scopes; no trade auth.

Token PERSISTENCE is delegated to :mod:`schwab_token_store` (atomic, 0600,
single-writer lock, configurable ``SCHWAB_TOKEN_PATH``). Token LIFECYCLE
decisions (is it usable, refresh it, classify a failure) are owned by
:mod:`schwab_auth_manager`; :func:`valid_access_token` remains as the legacy
facade over that authority so no caller grows its own refresh logic.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

from portfolio_automation.brokers.broker_models import redact
from portfolio_automation.brokers import schwab_token_store as tstore

_AUTH_BASE = "https://api.schwabapi.com/v1/oauth/authorize"
_TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
# Legacy default. data/ is gitignored at repo root. On a deployed release this
# resolves through the release's `data` runtime attachment to shared host
# state; SCHWAB_TOKEN_PATH overrides it (recommended: a path outside any
# checkout — see schwab_token_store.RECOMMENDED_PRODUCTION_PATH).
TOKEN_PATH = Path(__file__).resolve().parents[2] / "data" / "schwab_token.json"

# Schwab's interactive authorization is ~7 days. The anchor is set ONLY by the
# browser flow (exchange_code). An access-token refresh — even one that returns
# a different refresh_token string — never extends it: whether rotation renews
# the interactive window is a Schwab-controlled property this code does not
# assume (see schwab_auth_manager for the non-secret telemetry that studies it).
REFRESH_TOKEN_TTL_SEC = 7 * 24 * 3600
# Warn this far ahead of refresh-token expiry so re-auth stays a planned ~30s task.
REAUTH_WARN_SEC = 2 * 24 * 3600

#: How the authorization-expiry anchor in the token file came to be.
EXPIRY_BASIS_INTERACTIVE = "interactive_auth_anchor"   # set by exchange_code()
EXPIRY_BASIS_UNKNOWN = "unknown"                       # legacy/untracked token

# Single-use CSRF state nonce for the auth-code flow (used by schwab_reauth
# auto-capture). Persisted 0600 with a short TTL; consumed on first match.
STATE_PATH = Path(__file__).resolve().parents[2] / "data" / "schwab_reauth_state.json"
STATE_TTL_SEC = 600  # 10 minutes


class TokenEndpointError(RuntimeError):
    """The token endpoint answered with a non-200 status. Message is redacted;
    ``status_code`` lets the auth manager classify (rejected vs rate-limited vs
    unavailable) without parsing text."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = int(status_code)


class TokenTransportError(RuntimeError):
    """The token endpoint could not be reached (network/TLS/timeout)."""


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


# ---------------------------------------------------------------------------
# Token persistence — delegated to the single-writer store
# ---------------------------------------------------------------------------

def token_store() -> tstore.TokenStore:
    """The store for the EFFECTIVE token path (env override, else TOKEN_PATH).
    Resolved per call so monkeypatched/overridden paths are honoured."""
    return tstore.TokenStore(tstore.resolve_token_path(TOKEN_PATH))


def save_token(token: dict) -> None:
    """Atomic 0600 write under the single-writer lock."""
    store = token_store()
    with store.lock():
        store.save(token)


def load_token() -> dict | None:
    return token_store().load()


# ---------------------------------------------------------------------------
# Token endpoint
# ---------------------------------------------------------------------------

def _post_token(data: dict) -> dict:
    """Single POST to the token endpoint. Network isolated here; raises on failure
    with a REDACTED message. Tests monkeypatch this. ONE attempt, no retry — the
    caller (auth manager) bounds retries, so a flaky endpoint can never turn into
    an unbounded loop that also burns Schwab's rate limit."""
    import requests  # local import so the module loads without requests in pure paths
    try:
        resp = requests.post(_TOKEN_URL, data=data, auth=(_env("SCHWAB_CLIENT_ID"), _env("SCHWAB_CLIENT_SECRET")),
                             headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    except Exception as exc:  # noqa: BLE001 — transport failures are classified, not hidden
        raise TokenTransportError(redact(f"token endpoint unreachable: {type(exc).__name__}")) from None
    if resp.status_code != 200:
        raise TokenEndpointError(resp.status_code, redact(f"token endpoint {resp.status_code}: {resp.text}"))
    tok = resp.json()
    tok["expires_at"] = int(time.time()) + int(tok.get("expires_in", 1800))
    return tok


def _stamp_refresh_expiry(tok: dict, *, prev: dict | None, now: int | None = None) -> None:
    """Anchor or carry the interactive-authorization clock. CONSERVATIVE:

    * ``prev is None`` — the browser flow just completed: a fresh window starts
      and its basis is recorded as ``interactive_auth_anchor``.
    * ``prev`` carries an anchor — carry it forward UNCHANGED. An access-token
      refresh never extends the interactive window, whether or not Schwab
      returned a different refresh_token string.
    * ``prev`` has no anchor (legacy token) — stay UNTRACKED (basis ``unknown``).
      Inventing a 7-day window from a refresh is exactly the inference this
      module refuses to make; the anchor populates on the next real re-auth.
    """
    if prev is not None:
        if prev.get("refresh_token_expires_at"):
            tok["refresh_token_obtained_at"] = prev.get("refresh_token_obtained_at")
            tok["refresh_token_expires_at"] = prev.get("refresh_token_expires_at")
            tok["authorization_expiry_basis"] = prev.get(
                "authorization_expiry_basis", EXPIRY_BASIS_INTERACTIVE)
        else:
            tok.pop("refresh_token_obtained_at", None)
            tok.pop("refresh_token_expires_at", None)
            tok["authorization_expiry_basis"] = EXPIRY_BASIS_UNKNOWN
        return
    n = int(now if now is not None else time.time())
    tok["refresh_token_obtained_at"] = n
    tok["refresh_token_expires_at"] = n + REFRESH_TOKEN_TTL_SEC
    tok["authorization_expiry_basis"] = EXPIRY_BASIS_INTERACTIVE


def apply_refresh_response(prev: dict, resp: dict, *, now: int | None = None) -> tuple[dict, bool]:
    """Merge a token-endpoint refresh response onto the prior token.

    Returns ``(new_token, refresh_token_rotated)``. Rotation is REPORTED (for
    non-secret telemetry) and never used to extend the authorization anchor.
    Pure: no I/O."""
    tok = dict(resp)
    rotated = bool(tok.get("refresh_token")) and tok.get("refresh_token") != prev.get("refresh_token")
    if "refresh_token" not in tok:
        tok["refresh_token"] = prev.get("refresh_token", "")
    _stamp_refresh_expiry(tok, prev=prev, now=now)
    return tok, rotated


def exchange_code(code: str) -> dict:
    """Browser auth-code exchange. The ONLY path that starts a fresh anchor."""
    tok = _post_token({"grant_type": "authorization_code", "code": code,
                       "redirect_uri": _env("SCHWAB_REDIRECT_URI")})
    _stamp_refresh_expiry(tok, prev=None)  # browser auth -> fresh 7-day window
    save_token(tok)
    return tok


def refresh(token: dict) -> dict:
    """Legacy one-shot refresh + persist. Prefer SchwabAuthManager.acquire()."""
    resp = _post_token({"grant_type": "refresh_token", "refresh_token": token.get("refresh_token", "")})
    tok, _rotated = apply_refresh_response(token, resp)
    save_token(tok)
    return tok


def refresh_token_status(tok: dict | None = None, *, now: int | None = None) -> dict:
    """Observe-only view of the interactive-authorization clock. Never raises.
    reauth_status: ok | due_soon | expired | unknown (legacy/untracked token)."""
    if tok is None:
        tok = load_token() or {}
    exp = tok.get("refresh_token_expires_at")
    if not exp:
        return {"tracked": False, "expires_at": None, "seconds_remaining": None,
                "days_remaining": None, "expired": False, "reauth_status": "unknown",
                "authorization_expiry_basis": EXPIRY_BASIS_UNKNOWN}
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
        "authorization_expiry_basis": tok.get("authorization_expiry_basis", EXPIRY_BASIS_INTERACTIVE),
    }


def valid_access_token() -> str | None:
    """Legacy facade: a usable access token, or None. The decision (load,
    classify, refresh once, persist under lock) is made by the single auth
    authority; this function only flattens its structured result."""
    from portfolio_automation.brokers.schwab_auth_manager import SchwabAuthManager
    result = SchwabAuthManager().acquire()
    return result.access_token if result.ok else None
