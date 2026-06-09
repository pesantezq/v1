"""Tests for the opt-in HTTP basic auth on gui_v2."""
from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient


def _auth_header(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def client():
    from gui_v2.app import app
    return TestClient(app)


class TestAuthDisabled:
    def test_open_access_when_env_unset(
        self, client, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.delenv("GUI_V2_AUTH_USER", raising=False)
        monkeypatch.delenv("GUI_V2_AUTH_PASS", raising=False)
        # No Authorization header sent.
        # Old routes (/portfolio etc.) now redirect; the canonical dashboard path is used.
        # We verify with follow_redirects=False that the route itself is accessible
        # (not 401) and redirects as expected.
        from gui_v2.app import app
        c = TestClient(app, follow_redirects=False)
        for path in ("/", "/portfolio", "/research", "/health", "/operations"):
            r = c.get(path)
            assert r.status_code in (200, 302), f"{path}: open-mode should allow access (got {r.status_code})"
        # The new canonical route must return 200
        r = client.get("/dashboard/today")
        assert r.status_code == 200, "/dashboard/today: open-mode should allow access"

    def test_open_access_when_only_user_set(
        self, client, monkeypatch: pytest.MonkeyPatch,
    ):
        # Half-configured auth (only user) should NOT activate the gate;
        # the operator must set BOTH to opt in.
        monkeypatch.setenv("GUI_V2_AUTH_USER", "ops")
        monkeypatch.delenv("GUI_V2_AUTH_PASS", raising=False)
        r = client.get("/")
        assert r.status_code == 200

    def test_open_access_when_only_pass_set(
        self, client, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.delenv("GUI_V2_AUTH_USER", raising=False)
        monkeypatch.setenv("GUI_V2_AUTH_PASS", "secret")
        r = client.get("/")
        assert r.status_code == 200

    def test_blank_values_count_as_unset(
        self, client, monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("GUI_V2_AUTH_USER", "   ")
        monkeypatch.setenv("GUI_V2_AUTH_PASS", "   ")
        r = client.get("/")
        assert r.status_code == 200


class TestAuthEnabled:
    @pytest.fixture(autouse=True)
    def enable_auth(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("GUI_V2_AUTH_USER", "ops")
        monkeypatch.setenv("GUI_V2_AUTH_PASS", "hunter2")
        yield

    def test_no_credentials_returns_401(self, client):
        r = client.get("/")
        assert r.status_code == 401
        assert r.headers.get("www-authenticate", "").startswith("Basic")

    def test_wrong_credentials_returns_401(self, client):
        r = client.get("/", headers=_auth_header("ops", "wrong"))
        assert r.status_code == 401

    def test_correct_credentials_pass_through(self, client):
        r = client.get("/", headers=_auth_header("ops", "hunter2"))
        assert r.status_code == 200
        assert "<html" in r.text.lower()

    def test_every_page_requires_auth(self, client):
        # Old routes (/portfolio etc.) now redirect (302) when authenticated.
        # Unauthenticated → 401; authenticated → 302 (redirect) or 200 for canonical routes.
        from gui_v2.app import app
        c = TestClient(app, follow_redirects=False)
        for path in ("/", "/portfolio", "/research", "/health", "/operations"):
            r = c.get(path)
            assert r.status_code == 401, f"{path}: should be gated when no creds"
            r2 = c.get(path, headers=_auth_header("ops", "hunter2"))
            assert r2.status_code in (200, 302), f"{path}: should pass with correct creds (got {r2.status_code})"
        # The canonical dashboard route requires auth and serves 200 with correct creds
        r3 = c.get("/dashboard/today")
        assert r3.status_code == 401, "/dashboard/today: should be gated when no creds"
        r4 = c.get("/dashboard/today", headers=_auth_header("ops", "hunter2"))
        assert r4.status_code == 200, "/dashboard/today: should pass with correct creds"

    def test_constant_time_comparison_does_not_short_circuit_username(
        self, client,
    ):
        # We can't measure timing reliably in tests; confirm the code path
        # at least rejects a wrong username with the same 401 (not a
        # different status that would leak username validity).
        r = client.get("/", headers=_auth_header("wrong-user", "hunter2"))
        assert r.status_code == 401


class TestAuthIsRuntimeNotImportTime:
    """The auth gate must check env at request time so toggling auth
    on/off does not require a restart in tests (and only requires
    a systemd restart in production, which is fine)."""

    def test_toggle_on_then_off_within_one_client(
        self, client, monkeypatch: pytest.MonkeyPatch,
    ):
        # First request: auth on
        monkeypatch.setenv("GUI_V2_AUTH_USER", "ops")
        monkeypatch.setenv("GUI_V2_AUTH_PASS", "secret")
        assert client.get("/").status_code == 401

        # Second request: auth off
        monkeypatch.delenv("GUI_V2_AUTH_USER", raising=False)
        monkeypatch.delenv("GUI_V2_AUTH_PASS", raising=False)
        assert client.get("/").status_code == 200
