"""Single-writer Schwab auth authority: structured states, one bounded refresh,
conservative seven-day anchor, secret-free telemetry."""
from __future__ import annotations

import json
import time

import pytest

from portfolio_automation.brokers import schwab_auth_manager as am
from portfolio_automation.brokers import schwab_oauth as oa
from portfolio_automation.brokers import schwab_token_store as ts

NOW = 1_800_000_000


@pytest.fixture
def store(tmp_path):
    return ts.TokenStore(tmp_path / "token.json")


def _mgr(store, post=None, now=NOW):
    return am.SchwabAuthManager(store, post_token=post, now=lambda: now)


def _anchored(now=NOW, days_left=5):
    obtained = now - (7 - days_left) * 86400
    return {"refresh_token_obtained_at": obtained,
            "refresh_token_expires_at": obtained + oa.REFRESH_TOKEN_TTL_SEC,
            "authorization_expiry_basis": oa.EXPIRY_BASIS_INTERACTIVE}


# ---------------------------------------------------------------- states ---

def test_no_token_is_reauth_required_not_none(store):
    r = _mgr(store).acquire()
    assert r.state is am.AuthState.REAUTH_REQUIRED and r.reauth_required
    assert r.access_token is None and r.ok is False
    assert r.telemetry["token_state"] == ts.TOKEN_ABSENT


def test_corrupt_token_store_is_reauth_required_and_named(store):
    store.path.write_text("{corrupt")
    r = _mgr(store).acquire()
    assert r.state is am.AuthState.REAUTH_REQUIRED
    assert r.telemetry["token_state"] == ts.TOKEN_CORRUPT


def test_valid_access_token_is_ok_without_network(store):
    store.save({"access_token": "A", "refresh_token": "R", "expires_at": NOW + 1200, **_anchored()})
    called = []
    r = _mgr(store, post=lambda d: called.append(d)).acquire()
    assert r.state is am.AuthState.OK and r.ok and r.access_token == "A"
    assert called == []                              # no refresh when not needed
    assert r.telemetry["refresh_attempted_at"] is None
    assert r.telemetry["access_token_expires_in"] == 1200


def test_refresh_success_persists_and_reports_access_refreshed(store):
    store.save({"access_token": "OLD", "refresh_token": "R", "expires_at": NOW + 5, **_anchored()})
    post = lambda d: {"access_token": "NEW", "expires_in": 1800, "expires_at": NOW + 1800}
    r = _mgr(store, post=post).acquire()
    assert r.state is am.AuthState.ACCESS_REFRESHED and r.access_token == "NEW"
    on_disk = store.load()
    assert on_disk["access_token"] == "NEW" and on_disk["refresh_token"] == "R"   # persisted
    assert r.telemetry["refresh_succeeded"] is True
    assert r.telemetry["refresh_token_rotated"] is False
    assert r.telemetry["access_token_expires_in"] == 1800


def test_refresh_rejected_401_is_auth_rejected_and_reauth_required(store):
    store.save({"access_token": "OLD", "refresh_token": "R", "expires_at": NOW - 5, **_anchored()})
    def post(d):
        raise oa.TokenEndpointError(401, "token endpoint 401: invalid_grant refresh_token=LEAK")
    r = _mgr(store, post=post).acquire()
    assert r.state is am.AuthState.AUTH_REJECTED and r.reauth_required
    assert r.access_token is None
    assert "LEAK" not in r.detail and "LEAK" not in json.dumps(r.to_dict())
    assert r.telemetry["refresh_succeeded"] is False and r.telemetry["refresh_failure_status"] == 401


def test_rate_limit_is_transient_not_reauth(store):
    store.save({"access_token": "OLD", "refresh_token": "R", "expires_at": NOW - 5, **_anchored()})
    def post(d):
        raise oa.TokenEndpointError(429, "token endpoint 429: slow down")
    r = _mgr(store, post=post).acquire()
    assert r.state is am.AuthState.RATE_LIMITED and r.transient and not r.reauth_required


def test_transport_failure_is_schwab_unavailable_not_expired_authorization(store):
    store.save({"access_token": "OLD", "refresh_token": "R", "expires_at": NOW - 5, **_anchored()})
    def post(d):
        raise oa.TokenTransportError("token endpoint unreachable: ConnectionError")
    r = _mgr(store, post=post).acquire()
    assert r.state is am.AuthState.SCHWAB_UNAVAILABLE and r.transient
    assert not r.reauth_required
    assert store.load()["access_token"] == "OLD"     # a failed refresh never mutates the store


def test_server_error_is_schwab_unavailable(store):
    store.save({"access_token": "OLD", "refresh_token": "R", "expires_at": NOW - 5, **_anchored()})
    def post(d):
        raise oa.TokenEndpointError(503, "token endpoint 503")
    assert _mgr(store, post=post).acquire().state is am.AuthState.SCHWAB_UNAVAILABLE


def test_malformed_refresh_response_is_unavailable_not_ok(store):
    store.save({"access_token": "OLD", "refresh_token": "R", "expires_at": NOW - 5, **_anchored()})
    r = _mgr(store, post=lambda d: {"unexpected": True}).acquire()
    assert r.state is am.AuthState.SCHWAB_UNAVAILABLE and r.access_token is None


def test_expired_with_no_refresh_token_is_reauth_required(store):
    store.save({"access_token": "OLD", "expires_at": NOW - 5})
    r = _mgr(store, post=lambda d: pytest.fail("must not call endpoint")).acquire()
    assert r.state is am.AuthState.REAUTH_REQUIRED


def test_exactly_one_refresh_attempt_per_acquire(store):
    store.save({"access_token": "OLD", "refresh_token": "R", "expires_at": NOW - 5, **_anchored()})
    calls = []
    def post(d):
        calls.append(d)
        raise oa.TokenEndpointError(500, "flaky")
    _mgr(store, post=post).acquire()
    assert len(calls) == 1                               # bounded: no retry loop


def test_store_busy_when_another_writer_holds_the_lock(store):
    store.save({"access_token": "A", "refresh_token": "R", "expires_at": NOW + 1200})
    with store.lock():
        r = am.SchwabAuthManager(ts.TokenStore(store.path), now=lambda: NOW, lock_timeout=0.1).acquire()
    assert r.state is am.AuthState.STORE_BUSY and r.transient and r.access_token is None


# ------------------------------------------------- conservative anchor (G4) --

def test_rotated_refresh_token_does_not_extend_interactive_window(store):
    prev = {"access_token": "OLD", "refresh_token": "R1", "expires_at": NOW - 5, **_anchored(days_left=2)}
    store.save(prev)
    post = lambda d: {"access_token": "NEW", "refresh_token": "R2-ROTATED", "expires_at": NOW + 1800}
    r = _mgr(store, post=post).acquire()
    on_disk = store.load()
    assert on_disk["refresh_token"] == "R2-ROTATED"                       # new secret kept
    assert on_disk["refresh_token_expires_at"] == prev["refresh_token_expires_at"]  # anchor UNCHANGED
    assert on_disk["authorization_expiry_basis"] == oa.EXPIRY_BASIS_INTERACTIVE
    assert r.telemetry["refresh_token_rotated"] is True                   # rotation is REPORTED...
    assert r.telemetry["authorization_expiry_basis"] == oa.EXPIRY_BASIS_INTERACTIVE  # ...not inferred from
    assert r.state is am.AuthState.REAUTH_DUE_SOON                        # 2 days left -> due soon


def test_legacy_token_without_anchor_stays_untracked_on_refresh(store):
    store.save({"access_token": "OLD", "refresh_token": "R", "expires_at": NOW - 5})
    post = lambda d: {"access_token": "NEW", "refresh_token": "R-NEW", "expires_at": NOW + 1800}
    r = _mgr(store, post=post).acquire()
    on_disk = store.load()
    assert "refresh_token_expires_at" not in on_disk                      # no fabricated window
    assert on_disk["authorization_expiry_basis"] == oa.EXPIRY_BASIS_UNKNOWN
    assert r.reauth["reauth_status"] == "unknown"
    assert r.state is am.AuthState.ACCESS_REFRESHED


def test_due_soon_is_surfaced_while_still_usable(store):
    store.save({"access_token": "A", "refresh_token": "R", "expires_at": NOW + 1200, **_anchored(days_left=1)})
    r = _mgr(store).acquire()
    assert r.state is am.AuthState.REAUTH_DUE_SOON and r.ok and r.access_token == "A"


def test_expired_anchor_with_valid_access_token_is_due_soon_not_silent_ok(store):
    store.save({"access_token": "A", "refresh_token": "R", "expires_at": NOW + 1200, **_anchored(days_left=-1)})
    r = _mgr(store).acquire()
    assert r.state is am.AuthState.REAUTH_DUE_SOON and r.reauth["reauth_status"] == "expired"


# ------------------------------------------------------ secret-free output --

def test_result_dict_repr_and_telemetry_carry_no_secrets(store):
    store.save({"access_token": "ACCESS-SECRET", "refresh_token": "REFRESH-SECRET",
                "expires_at": NOW + 1200, **_anchored()})
    r = _mgr(store).acquire()
    blob = json.dumps(r.to_dict()) + repr(r) + json.dumps(r.telemetry) + json.dumps(r.reauth)
    assert "ACCESS-SECRET" not in blob and "REFRESH-SECRET" not in blob
    assert r.to_dict()["access_token_present"] is True
    assert "access_token" not in r.to_dict()
    fp = r.telemetry["refresh_token_fingerprint"]
    assert fp == ts.fingerprint("REFRESH-SECRET") and "REFRESH-SECRET" not in fp


def test_telemetry_has_the_documented_lifecycle_fields(store):
    store.save({"access_token": "OLD", "refresh_token": "R", "expires_at": NOW - 5, **_anchored()})
    r = _mgr(store, post=lambda d: {"access_token": "NEW", "expires_at": NOW + 1800}).acquire()
    for key in ("refresh_attempted_at", "refresh_succeeded", "refresh_token_present",
                "refresh_token_rotated", "access_token_expires_in", "authorization_expiry_basis"):
        assert key in r.telemetry, key
    assert r.telemetry["refresh_attempted_at"].startswith("2027-")


# ------------------------------------------------------- legacy facade -----

def test_valid_access_token_facade_delegates_to_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(oa, "TOKEN_PATH", tmp_path / "t.json")
    monkeypatch.delenv(ts.TOKEN_PATH_ENV, raising=False)
    oa.save_token({"access_token": "FRESH", "refresh_token": "r", "expires_at": int(time.time()) + 3600})
    assert oa.valid_access_token() == "FRESH"
    oa.save_token({"access_token": "OLD", "refresh_token": "r", "expires_at": int(time.time()) - 5})
    monkeypatch.setattr(oa, "_post_token", lambda d: (_ for _ in ()).throw(oa.TokenEndpointError(401, "no")))
    assert oa.valid_access_token() is None


def test_env_token_path_override_is_honoured_by_facade(tmp_path, monkeypatch):
    monkeypatch.setattr(oa, "TOKEN_PATH", tmp_path / "legacy.json")
    monkeypatch.setenv(ts.TOKEN_PATH_ENV, str(tmp_path / "override" / "token.json"))
    oa.save_token({"access_token": "OVR", "refresh_token": "r", "expires_at": int(time.time()) + 3600})
    assert not (tmp_path / "legacy.json").exists()
    assert (tmp_path / "override" / "token.json").exists()
    assert oa.valid_access_token() == "OVR"
