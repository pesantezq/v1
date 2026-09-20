# portfolio_automation/brokers/schwab_auth_manager.py
"""The single logical authority for Schwab token lifecycle decisions.

    load token -> determine state -> refresh once if needed -> persist under
    lock -> classify any failure -> surface re-auth-required state

Every other component (scheduler sync, CLI, GUI, agents) asks this manager
and never implements its own refresh. The result is STRUCTURED: a closed set
of :class:`AuthState` values instead of a lossy ``None``, so a transient
network failure is never mistaken for an expired authorization, and an
expired authorization is never retried into a rate limit.

Conservative lifecycle rules (G4):

* The interactive (~7-day) authorization expiry is a Schwab-controlled
  property. It is anchored ONLY by the browser flow (``exchange_code``).
* A refresh that returns a rotated refresh_token string is RECORDED as rotated
  in non-secret telemetry and is NEVER used to infer a renewed window.
* Telemetry carries no token bytes. Fingerprints are one-way sha256 prefixes.

Bounded: exactly ONE refresh attempt per ``acquire()``. No loops, no sleeps.

Read-only: this module talks only to the token endpoint. It has no account,
order or trade surface (AST-enforced across the package).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from portfolio_automation.brokers import schwab_oauth as oauth
from portfolio_automation.brokers import schwab_token_store as tstore
from portfolio_automation.brokers.broker_models import redact

ACCESS_TOKEN_SKEW_SEC = 30
DEFAULT_LOCK_TIMEOUT_SEC = 10.0


class AuthState(str, Enum):
    """Closed set of auth acquisition outcomes. Names are audit evidence."""
    UNCONFIGURED = "UNCONFIGURED"            # SCHWAB_* env absent — inert, not an error
    OK = "OK"                                # stored access token still valid
    ACCESS_REFRESHED = "ACCESS_REFRESHED"    # refreshed + persisted this call
    REAUTH_DUE_SOON = "REAUTH_DUE_SOON"      # usable now; interactive re-auth needed soon
    REAUTH_REQUIRED = "REAUTH_REQUIRED"      # no usable token; operator browser re-auth
    AUTH_REJECTED = "AUTH_REJECTED"          # Schwab refused the refresh (400/401/403)
    RATE_LIMITED = "RATE_LIMITED"            # 429 — transient; do NOT re-auth
    SCHWAB_UNAVAILABLE = "SCHWAB_UNAVAILABLE"  # 5xx / transport — transient; do NOT re-auth
    STORE_BUSY = "STORE_BUSY"                # another writer holds the token lock


#: States in which ``access_token`` is present and usable right now.
USABLE_STATES = frozenset({AuthState.OK, AuthState.ACCESS_REFRESHED, AuthState.REAUTH_DUE_SOON})
#: States that mean "an operator must run the interactive flow".
REAUTH_STATES = frozenset({AuthState.REAUTH_REQUIRED, AuthState.AUTH_REJECTED})
#: States that are transient and must not be escalated to re-auth.
TRANSIENT_STATES = frozenset({AuthState.RATE_LIMITED, AuthState.SCHWAB_UNAVAILABLE, AuthState.STORE_BUSY})


@dataclass(frozen=True)
class AuthResult:
    """One acquisition outcome. ``access_token`` is the ONLY secret and is
    excluded from repr/to_dict; everything else is safe to persist."""
    state: AuthState
    access_token: Optional[str] = field(default=None, repr=False, compare=False)
    detail: str = ""                                   # redacted, non-secret
    telemetry: dict[str, Any] = field(default_factory=dict)
    reauth: dict[str, Any] = field(default_factory=dict)  # refresh_token_status block

    @property
    def ok(self) -> bool:
        return self.state in USABLE_STATES and bool(self.access_token)

    @property
    def reauth_required(self) -> bool:
        return self.state in REAUTH_STATES

    @property
    def transient(self) -> bool:
        return self.state in TRANSIENT_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "auth_state": self.state.value,
            "usable": self.ok,
            "reauth_required": self.reauth_required,
            "transient": self.transient,
            "access_token_present": bool(self.access_token),
            "detail": redact(self.detail),
            "telemetry": dict(self.telemetry),
            "reauth": dict(self.reauth),
        }


def _classify_endpoint_status(status: int) -> AuthState:
    if status in (400, 401, 403):
        return AuthState.AUTH_REJECTED
    if status == 429:
        return AuthState.RATE_LIMITED
    return AuthState.SCHWAB_UNAVAILABLE


class SchwabAuthManager:
    """Single-writer token authority. Construct once per operation; cheap."""

    def __init__(self, store: Optional[tstore.TokenStore] = None, *,
                 post_token: Optional[Callable[[dict], dict]] = None,
                 now: Optional[Callable[[], int]] = None,
                 lock_timeout: float = DEFAULT_LOCK_TIMEOUT_SEC) -> None:
        self._store = store
        self._post_token = post_token
        self._now = now
        self._lock_timeout = lock_timeout

    # Resolved per call so monkeypatched module attributes (tests) and env
    # overrides (operators) are honoured without a restart.
    def _resolve_store(self) -> tstore.TokenStore:
        return self._store if self._store is not None else oauth.token_store()

    def _resolve_post(self) -> Callable[[dict], dict]:
        return self._post_token if self._post_token is not None else oauth._post_token

    def _clock(self) -> int:
        return int(self._now()) if self._now is not None else int(time.time())

    def acquire(self) -> AuthResult:
        """Return a usable access token or a classified reason there is none.
        Never raises; never loops; at most one token-endpoint call."""
        # Configuration gates the REFRESH step only (a refresh needs the client
        # credentials); a stored, still-valid access token is usable regardless,
        # which preserves the legacy valid_access_token() contract.
        store = self._resolve_store()
        base: dict[str, Any] = {
            "token_path_overridden": tstore.token_path_is_overridden(),
            "refresh_attempted_at": None,
            "refresh_succeeded": None,
            "refresh_token_present": None,
            "refresh_token_rotated": None,
            "access_token_expires_in": None,
            "authorization_expiry_basis": oauth.EXPIRY_BASIS_UNKNOWN,
        }
        try:
            with store.lock(timeout=self._lock_timeout):
                return self._acquire_locked(store, base)
        except tstore.TokenStoreLocked as exc:
            return AuthResult(state=AuthState.STORE_BUSY, detail=redact(str(exc)), telemetry=base)
        except Exception as exc:  # noqa: BLE001 — the authority never raises to callers
            return AuthResult(state=AuthState.SCHWAB_UNAVAILABLE,
                              detail=redact(f"unexpected {type(exc).__name__} in auth manager"),
                              telemetry=base)

    def _acquire_locked(self, store: tstore.TokenStore, base: dict[str, Any]) -> AuthResult:
        now = self._clock()
        tok, tstate = store.load_state()
        base["token_state"] = tstate
        if tok is None:
            return AuthResult(state=AuthState.REAUTH_REQUIRED, telemetry=base,
                              detail=f"no usable token in store ({tstate}); run the interactive re-auth")
        reauth = oauth.refresh_token_status(tok, now=now)
        base["authorization_expiry_basis"] = reauth.get("authorization_expiry_basis", oauth.EXPIRY_BASIS_UNKNOWN)
        base["refresh_token_present"] = bool(tok.get("refresh_token"))
        base["refresh_token_fingerprint"] = tstore.fingerprint(tok.get("refresh_token"))

        expires_at = int(tok.get("expires_at", 0) or 0)
        access_valid = expires_at > now + ACCESS_TOKEN_SKEW_SEC
        if access_valid and tok.get("access_token"):
            base["access_token_expires_in"] = expires_at - now
            state = AuthState.REAUTH_DUE_SOON if reauth.get("reauth_status") in ("due_soon", "expired") else AuthState.OK
            detail = ("stored access token valid" if state is AuthState.OK
                      else f"stored access token valid; interactive re-auth {reauth.get('reauth_status')}")
            return AuthResult(state=state, access_token=tok.get("access_token"),
                              detail=detail, telemetry=base, reauth=reauth)

        # --- exactly one refresh attempt --------------------------------
        if not tok.get("refresh_token"):
            return AuthResult(state=AuthState.REAUTH_REQUIRED, telemetry=base, reauth=reauth,
                              detail="access token expired and no refresh token is stored")
        from datetime import datetime, timezone
        base["refresh_attempted_at"] = datetime.fromtimestamp(now, timezone.utc).isoformat()
        try:
            resp = self._resolve_post()({"grant_type": "refresh_token",
                                         "refresh_token": tok.get("refresh_token", "")})
        except oauth.TokenEndpointError as exc:
            base["refresh_succeeded"] = False
            base["refresh_failure_status"] = exc.status_code
            state = _classify_endpoint_status(exc.status_code)
            return AuthResult(state=state, detail=redact(str(exc)), telemetry=base, reauth=reauth)
        except Exception as exc:  # noqa: BLE001 — transport/unknown: transient, not re-auth
            base["refresh_succeeded"] = False
            base["refresh_failure_class"] = type(exc).__name__
            return AuthResult(state=AuthState.SCHWAB_UNAVAILABLE,
                              detail=redact(f"refresh failed: {type(exc).__name__}: {exc}"),
                              telemetry=base, reauth=reauth)
        if not isinstance(resp, dict) or not resp.get("access_token"):
            base["refresh_succeeded"] = False
            return AuthResult(state=AuthState.SCHWAB_UNAVAILABLE, telemetry=base, reauth=reauth,
                              detail="token endpoint returned no access token (malformed response)")

        new_tok, rotated = oauth.apply_refresh_response(tok, resp, now=now)
        store.save(new_tok)
        reauth = oauth.refresh_token_status(new_tok, now=now)
        base.update({
            "refresh_succeeded": True,
            "refresh_token_present": bool(new_tok.get("refresh_token")),
            "refresh_token_rotated": rotated,
            "refresh_token_fingerprint": tstore.fingerprint(new_tok.get("refresh_token")),
            "access_token_expires_in": int(new_tok.get("expires_at", now)) - now,
            "authorization_expiry_basis": reauth.get("authorization_expiry_basis", oauth.EXPIRY_BASIS_UNKNOWN),
        })
        state = AuthState.REAUTH_DUE_SOON if reauth.get("reauth_status") in ("due_soon", "expired") else AuthState.ACCESS_REFRESHED
        return AuthResult(state=state, access_token=new_tok.get("access_token"),
                          detail="access token refreshed and persisted", telemetry=base, reauth=reauth)


def acquire(**kwargs: Any) -> AuthResult:
    """Module-level convenience: ``SchwabAuthManager(**kwargs).acquire()``."""
    return SchwabAuthManager(**kwargs).acquire()
