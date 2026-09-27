"""gui_v2.app._static_version: the cache-busting token can never be account-shaped.

Background (PR #48 CI flake): the token was ``md5(mtime)[:8]`` in hex, with an
``"a"`` appended when the eight characters happened to be all digits. That
guard is ineffective against the account-number detector used by the GUI
tests, ``(?<!\d)\d{8,}(?!\d)``: the lookaround only requires a NON-digit on
either side of the run, so ``65512658a`` still contains the eight-digit run
``65512658``. Roughly 2.3% of mtimes ((10/16)**8) produce an all-digit prefix,
and both static links share one checkout mtime, so a fresh CI checkout could
render two false "account numbers" on every page.

These tests pin the offending mtime deterministically (``os.utime`` on a temp
asset) rather than hashing random mtimes and hoping.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import gui_v2.app as app_module

#: The masking detector the portfolio-sync tests use (kept identical here so a
#: producer regression is caught by the same rule that caught the flake).
_FULL_ACCOUNT_RE = re.compile(r"(?<!\d)\d{8,}(?!\d)")

#: md5("1790271044")[:8] == "65512658" -- the mtime of the CI checkout that
#: failed run 36034801536. A deterministic reproduction, not a probability.
_ALL_DIGIT_MTIME = 1790271044
#: A neighbouring mtime whose digest prefix is NOT all digits.
_OTHER_MTIME = 1790271045


@pytest.fixture
def static_dir(tmp_path, monkeypatch):
    """A private STATIC_DIR so tests control asset mtimes exactly."""
    d = tmp_path / "static"
    d.mkdir()
    for name in ("app.css", "htmx.min.js"):
        (d / name).write_text("/* asset */", encoding="utf-8")
    monkeypatch.setattr(app_module, "STATIC_DIR", d)
    return d


def _set_mtime(path: Path, mtime: int) -> None:
    os.utime(path, (mtime, mtime))


def test_all_digit_digest_prefix_mtime_yields_a_non_account_shaped_token(static_dir):
    """The exact CI case. Fails on the previous implementation ("65512658a")."""
    _set_mtime(static_dir / "app.css", _ALL_DIGIT_MTIME)
    token = app_module._static_version("app.css")
    assert _FULL_ACCOUNT_RE.findall(f"?v={token}") == [], token
    assert not token.isdigit()
    assert re.search(r"\d{8,}", token) is None


def test_token_is_never_digit_shaped_for_any_mtime(static_dir):
    """Structural invariant, not luck: the token is letters only, so no digit
    run of any length can appear, whatever the mtime hashes to."""
    for mtime in (_ALL_DIGIT_MTIME, _OTHER_MTIME, 1790000022, 0, 1, 2_000_000_000):
        _set_mtime(static_dir / "app.css", mtime)
        token = app_module._static_version("app.css")
        assert token.isalpha() and token.isascii(), (mtime, token)
        assert len(token) == 8, (mtime, token)
        assert _FULL_ACCOUNT_RE.findall(f'href="/static/app.css?v={token}"') == []


def test_token_is_deterministic_and_changes_with_the_mtime(static_dir):
    _set_mtime(static_dir / "app.css", _ALL_DIGIT_MTIME)
    a1 = app_module._static_version("app.css")
    a2 = app_module._static_version("app.css")
    assert a1 == a2
    _set_mtime(static_dir / "app.css", _OTHER_MTIME)
    b = app_module._static_version("app.css")
    assert b != a1
    # same mtime on a different file -> same token (the token hashes the mtime)
    _set_mtime(static_dir / "htmx.min.js", _OTHER_MTIME)
    assert app_module._static_version("htmx.min.js") == b


def test_missing_asset_keeps_the_safe_fallback(static_dir):
    assert app_module._static_version("does-not-exist.css") == "0"


def test_token_is_url_safe():
    for mtime in (_ALL_DIGIT_MTIME, _OTHER_MTIME):
        token = app_module._static_token_for_mtime(mtime)
        assert re.fullmatch(r"[A-Za-z0-9_.~-]+", token), token


def test_rendered_page_carries_cache_busted_static_links_without_account_shaped_tokens(
        static_dir, monkeypatch, tmp_path):
    """End to end through the shell: both static links render with the token,
    and the page as a whole contains no 8+ digit run from the token."""
    for name in ("app.css", "htmx.min.js"):
        _set_mtime(static_dir / name, _ALL_DIGIT_MTIME)
    repo = tmp_path / "repo"
    (repo / "outputs" / "latest").mkdir(parents=True)
    monkeypatch.setattr(app_module, "REPO_ROOT", repo)
    html = TestClient(app_module.app).get("/dashboard/system").text
    token = app_module._static_version("app.css")
    assert f'/static/app.css?v={token}' in html
    assert f'/static/htmx.min.js?v={token}' in html
    assert _FULL_ACCOUNT_RE.findall(html) == []
