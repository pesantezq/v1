import json
from pathlib import Path
from portfolio_automation.brokers import schwab_oauth as oa


def test_is_configured_reads_env(monkeypatch):
    monkeypatch.delenv("SCHWAB_CLIENT_ID", raising=False)
    assert oa.is_configured() is False
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "cid")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "csec")
    monkeypatch.setenv("SCHWAB_REDIRECT_URI", "https://127.0.0.1/cb")
    assert oa.is_configured() is True


def test_build_authorize_url_has_no_secret(monkeypatch):
    monkeypatch.setenv("SCHWAB_CLIENT_ID", "cid")
    monkeypatch.setenv("SCHWAB_CLIENT_SECRET", "csec-SEKRET")
    monkeypatch.setenv("SCHWAB_REDIRECT_URI", "https://127.0.0.1/cb")
    url = oa.build_authorize_url()
    assert "cid" in url and "csec-SEKRET" not in url  # secret never in authorize URL
    assert url.startswith("https://api.schwabapi.com/v1/oauth/authorize")


def test_token_save_load_roundtrip_and_perms(tmp_path, monkeypatch):
    p = tmp_path / "schwab_token.json"
    monkeypatch.setattr(oa, "TOKEN_PATH", p)
    oa.save_token({"access_token": "a", "refresh_token": "r", "expires_at": 999})
    assert oa.load_token()["access_token"] == "a"
    import os, stat
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600  # 0600


def test_load_token_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(oa, "TOKEN_PATH", tmp_path / "nope.json")
    assert oa.load_token() is None


def test_valid_access_token_none_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(oa, "TOKEN_PATH", tmp_path / "nope.json")
    assert oa.valid_access_token() is None


def test_valid_access_token_returns_fresh_token(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(oa, "TOKEN_PATH", tmp_path / "t.json")
    oa.save_token({"access_token": "FRESH", "refresh_token": "r", "expires_at": int(time.time()) + 3600})
    assert oa.valid_access_token() == "FRESH"  # not near expiry -> no refresh


def test_valid_access_token_refreshes_when_near_expiry(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(oa, "TOKEN_PATH", tmp_path / "t.json")
    oa.save_token({"access_token": "OLD", "refresh_token": "r", "expires_at": int(time.time()) + 10})
    monkeypatch.setattr(oa, "_post_token", lambda data: {"access_token": "NEW", "expires_at": int(time.time()) + 3600})
    assert oa.valid_access_token() == "NEW"  # near expiry -> refreshed


def test_valid_access_token_none_when_refresh_fails(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(oa, "TOKEN_PATH", tmp_path / "t.json")
    oa.save_token({"access_token": "OLD", "refresh_token": "r", "expires_at": int(time.time()) - 5})
    def boom(data): raise RuntimeError("refresh failed")
    monkeypatch.setattr(oa, "_post_token", boom)
    assert oa.valid_access_token() is None  # expired + refresh fails -> None


# --- 7-day refresh-token expiry tracking (re-auth heads-up) ----------------

def test_exchange_code_anchors_seven_day_refresh_clock(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(oa, "TOKEN_PATH", tmp_path / "t.json")
    monkeypatch.setenv("SCHWAB_REDIRECT_URI", "https://127.0.0.1/cb")
    monkeypatch.setattr(oa, "_post_token", lambda data: {"access_token": "A", "refresh_token": "R",
                                                         "expires_at": int(time.time()) + 1800})
    tok = oa.exchange_code("CODE")
    # browser auth issues a genuinely new refresh token -> fresh 7-day window
    assert tok["refresh_token_expires_at"] - tok["refresh_token_obtained_at"] == oa.REFRESH_TOKEN_TTL_SEC
    assert abs(tok["refresh_token_obtained_at"] - int(time.time())) < 5


def test_refresh_carries_forward_refresh_clock_not_reset(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(oa, "TOKEN_PATH", tmp_path / "t.json")
    anchor = int(time.time()) - 3 * 86400          # token obtained 3 days ago
    exp = anchor + oa.REFRESH_TOKEN_TTL_SEC          # ~4 days left
    prev = {"access_token": "OLD", "refresh_token": "r", "expires_at": int(time.time()) - 5,
            "refresh_token_obtained_at": anchor, "refresh_token_expires_at": exp}
    # Schwab refresh reuses the SAME refresh token (no new one in the response)
    monkeypatch.setattr(oa, "_post_token", lambda data: {"access_token": "NEW",
                                                         "expires_at": int(time.time()) + 1800})
    tok = oa.refresh(prev)
    assert tok["refresh_token_expires_at"] == exp        # clock NOT reset by an access-token refresh
    assert tok["refresh_token"] == "r"                    # old refresh token carried forward


def test_refresh_token_status_ok_due_soon_expired_unknown(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(oa, "TOKEN_PATH", tmp_path / "t.json")
    now = int(time.time())
    # ok: 5 days remaining
    assert oa.refresh_token_status({"refresh_token_expires_at": now + 5 * 86400}, now=now)["reauth_status"] == "ok"
    # due_soon: within the warning window (< 2 days)
    s = oa.refresh_token_status({"refresh_token_expires_at": now + 86400}, now=now)
    assert s["reauth_status"] == "due_soon" and s["expired"] is False and s["days_remaining"] == 1.0
    # expired
    s = oa.refresh_token_status({"refresh_token_expires_at": now - 10}, now=now)
    assert s["reauth_status"] == "expired" and s["expired"] is True
    # untracked (legacy token with no anchor) -> unknown, never a false alarm
    s = oa.refresh_token_status({"access_token": "x"}, now=now)
    assert s["reauth_status"] == "unknown" and s["tracked"] is False


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
    assert oa.verify_state(nonce) is True
    assert oa.verify_state(nonce) is False


def test_verify_state_missing_file_false(tmp_path, monkeypatch):
    monkeypatch.setattr(oa, "STATE_PATH", tmp_path / "nope.json")
    assert oa.verify_state("anything") is False
