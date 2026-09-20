"""Schwab token store: atomic, owner-only, single-writer, secret-free."""
from __future__ import annotations

import json
import os
import stat
import threading
import time

import pytest

from portfolio_automation.brokers import schwab_token_store as ts


def test_resolve_token_path_prefers_env_override(monkeypatch, tmp_path):
    monkeypatch.delenv(ts.TOKEN_PATH_ENV, raising=False)
    assert ts.resolve_token_path(tmp_path / "legacy.json") == tmp_path / "legacy.json"
    assert ts.token_path_is_overridden() is False
    monkeypatch.setenv(ts.TOKEN_PATH_ENV, str(tmp_path / "override" / "token.json"))
    assert ts.resolve_token_path(tmp_path / "legacy.json") == tmp_path / "override" / "token.json"
    assert ts.token_path_is_overridden() is True


def test_recommended_production_path_is_outside_any_checkout():
    assert ts.RECOMMENDED_PRODUCTION_PATH.startswith("/var/lib/stockbot/")
    assert "/opt/stockbot" not in ts.RECOMMENDED_PRODUCTION_PATH


def test_save_is_owner_only_and_creates_private_parent(tmp_path):
    p = tmp_path / "deep" / "er" / "token.json"
    store = ts.TokenStore(p)
    store.save({"access_token": "A", "refresh_token": "R"})
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(p.parent).st_mode) == 0o700
    assert store.load() == {"access_token": "A", "refresh_token": "R"}


def test_save_is_atomic_no_temp_debris_and_replaces_old(tmp_path):
    p = tmp_path / "token.json"
    store = ts.TokenStore(p)
    store.save({"access_token": "OLD"})
    store.save({"access_token": "NEW"})
    assert json.loads(p.read_text())["access_token"] == "NEW"
    assert [x.name for x in tmp_path.iterdir() if x.name.endswith(".tmp")] == []


def test_interrupted_write_never_clobbers_prior_good_token(tmp_path, monkeypatch):
    p = tmp_path / "token.json"
    store = ts.TokenStore(p)
    store.save({"access_token": "GOOD"})

    def exploding_replace(src, dst):
        raise OSError("disk yanked mid-write")

    monkeypatch.setattr(ts.os, "replace", exploding_replace)
    with pytest.raises(OSError):
        store.save({"access_token": "PARTIAL"})
    assert json.loads(p.read_text())["access_token"] == "GOOD"     # prior token intact
    assert [x.name for x in tmp_path.iterdir() if x.name.endswith(".tmp")] == []  # temp cleaned


def test_load_state_distinguishes_absent_corrupt_present(tmp_path):
    p = tmp_path / "token.json"
    store = ts.TokenStore(p)
    assert store.load_state() == (None, ts.TOKEN_ABSENT)
    p.write_text("{not json")
    assert store.load_state() == (None, ts.TOKEN_CORRUPT)
    p.write_text(json.dumps([1, 2]))
    assert store.load_state() == (None, ts.TOKEN_CORRUPT)   # not a mapping
    store.save({"access_token": "A"})
    tok, state = store.load_state()
    assert state == ts.TOKEN_PRESENT and tok["access_token"] == "A"
    assert store.load() == tok


def test_save_rejects_non_mapping(tmp_path):
    with pytest.raises(ts.TokenStoreError):
        ts.TokenStore(tmp_path / "t.json").save(["not", "a", "dict"])  # type: ignore[arg-type]


def test_lock_is_exclusive_across_holders_and_bounded(tmp_path):
    store = ts.TokenStore(tmp_path / "token.json")
    with store.lock():
        # a second holder (same process, separate descriptor) must NOT get in
        t0 = time.monotonic()
        with pytest.raises(ts.TokenStoreLocked):
            with ts.TokenStore(tmp_path / "token.json").lock(timeout=0.2):
                pass
        assert time.monotonic() - t0 < 2.0   # bounded wait, never a spin forever
    # released: acquirable again
    with store.lock(timeout=0.2):
        pass
    assert stat.S_IMODE(os.stat(store.lock_path).st_mode) == 0o600


def test_two_writers_serialize_refresh_and_persist(tmp_path):
    """Two concurrent refresh+persist sequences must not interleave: the final
    file must be exactly one writer's complete token, never a mix."""
    store = ts.TokenStore(tmp_path / "token.json")
    store.save({"access_token": "SEED", "n": 0})
    order: list[str] = []

    def writer(name: str):
        s = ts.TokenStore(tmp_path / "token.json")
        with s.lock(timeout=5):
            order.append(f"{name}:in")
            tok = s.load()
            time.sleep(0.05)               # widen the race window
            s.save({"access_token": name, "n": tok["n"] + 1})
            order.append(f"{name}:out")

    a = threading.Thread(target=writer, args=("A",))
    b = threading.Thread(target=writer, args=("B",))
    a.start(); b.start(); a.join(); b.join()
    # strictly alternating in/out: no writer entered while the other was inside
    assert order[0].endswith(":in") and order[1].endswith(":out")
    assert order[2].endswith(":in") and order[3].endswith(":out")
    assert store.load()["n"] == 2          # both increments landed, none lost


def test_repr_never_shows_token_contents(tmp_path):
    store = ts.TokenStore(tmp_path / "token.json")
    store.save({"access_token": "SUPERSECRET", "refresh_token": "ALSOSECRET"})
    assert "SUPERSECRET" not in repr(store) and "ALSOSECRET" not in repr(store)


def test_fingerprint_is_one_way_stable_and_none_for_empty():
    assert ts.fingerprint(None) is None and ts.fingerprint("") is None
    fp = ts.fingerprint("refresh-token-value")
    assert fp == ts.fingerprint("refresh-token-value") and len(fp) == 16
    assert "refresh-token-value" not in fp
    assert fp != ts.fingerprint("refresh-token-valu3")
