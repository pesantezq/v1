"""Env-name reconciliation for the two legacy email senders (2026-07-29).

Defect: the operator reported never receiving watchlist emails. `.env` carries the
generic mail config (`EMAIL_USER` / `EMAIL_PASS` / `EMAIL_TO` — the same vars
`portfolio_automation/memo_email_sender.py`, the Schwab re-auth notifier and
`tools/notify_status.py` all read), and the SMTP credential is valid. But
`email_reporter.EmailReporter` and `email_digest.FinanceEmailDigest` read a
DIFFERENT triple — `EMAIL_SENDER` / `EMAIL_RECIPIENT` / `EMAIL_PASSWORD` — none of
which is set. `config.json:email` supplies `smtp_server`/`smtp_port` but leaves
`sender_email`/`recipient_email` as empty strings, and those are falsy, so the
constructors fell through to the unset env names.

Net effect every cron run: `config.email.enabled` is true, so `main.py` entered the
send branch, `is_configured()` returned False, and it logged
"Email not configured (missing credentials)" — a real failure that read like
configuration intent.

`memo_email_sender._env_str_fallback` already bridges exactly this gap and its
docstring names these generic vars; the legacy pair never got the bridge. These
tests pin it, with the dedicated name still winning so existing configs are
unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_LEGACY_TO_GENERIC = [
    ("EMAIL_SENDER", "EMAIL_USER", "sender_email"),
    ("EMAIL_RECIPIENT", "EMAIL_TO", "recipient_email"),
    ("EMAIL_PASSWORD", "EMAIL_PASS", "password"),
]

_ALL_NAMES = [n for pair in _LEGACY_TO_GENERIC for n in pair[:2]]


@pytest.fixture(autouse=True)
def _clean_email_env(monkeypatch):
    """Every test starts from no mail env at all, so a value can only come from
    the name the test sets — never from the operator's real .env."""
    for name in _ALL_NAMES:
        monkeypatch.delenv(name, raising=False)


def _senders():
    from email_digest import FinanceEmailDigest
    from email_reporter import EmailReporter
    return [FinanceEmailDigest, EmailReporter]


@pytest.mark.parametrize("cls", _senders(), ids=lambda c: c.__name__)
def test_generic_env_names_configure_the_sender(cls, monkeypatch):
    """The .env shape the operator actually has must produce is_configured()."""
    monkeypatch.setenv("EMAIL_USER", "ops@example.com")
    monkeypatch.setenv("EMAIL_PASS", "app-password-1234")
    monkeypatch.setenv("EMAIL_TO", "ops@example.com")
    s = cls()
    assert s.sender_email == "ops@example.com"
    assert s.recipient_email == "ops@example.com"
    assert s.password == "app-password-1234"
    assert s.is_configured(), (
        "generic EMAIL_USER/EMAIL_PASS/EMAIL_TO must configure the legacy senders — "
        "this is the .env shape on the production VPS"
    )


@pytest.mark.parametrize("cls", _senders(), ids=lambda c: c.__name__)
@pytest.mark.parametrize("legacy,generic,attr", _LEGACY_TO_GENERIC,
                         ids=[p[0] for p in _LEGACY_TO_GENERIC])
def test_dedicated_name_wins_over_generic(cls, legacy, generic, attr, monkeypatch):
    """Backward compatibility: an existing config using the legacy names must be
    unaffected by the fallback."""
    # Minimum viable config via generic names, then override one with its legacy name.
    monkeypatch.setenv("EMAIL_USER", "generic@example.com")
    monkeypatch.setenv("EMAIL_PASS", "generic-pass")
    monkeypatch.setenv("EMAIL_TO", "generic@example.com")
    monkeypatch.setenv(legacy, "legacy-value")
    s = cls()
    assert getattr(s, attr) == "legacy-value", (
        f"{legacy} must take precedence over {generic}"
    )


@pytest.mark.parametrize("cls", _senders(), ids=lambda c: c.__name__)
def test_legacy_only_env_still_works(cls, monkeypatch):
    monkeypatch.setenv("EMAIL_SENDER", "legacy@example.com")
    monkeypatch.setenv("EMAIL_RECIPIENT", "legacy@example.com")
    monkeypatch.setenv("EMAIL_PASSWORD", "legacy-pass")
    s = cls()
    assert s.is_configured()
    assert s.sender_email == "legacy@example.com"


@pytest.mark.parametrize("cls", _senders(), ids=lambda c: c.__name__)
def test_no_env_at_all_is_not_configured(cls):
    """Absence must stay absence — the fallback must never manufacture a sender."""
    s = cls()
    assert not s.is_configured()


@pytest.mark.parametrize("cls", _senders(), ids=lambda c: c.__name__)
def test_explicit_constructor_args_win_over_every_env_name(cls, monkeypatch):
    monkeypatch.setenv("EMAIL_SENDER", "legacy@example.com")
    monkeypatch.setenv("EMAIL_USER", "generic@example.com")
    monkeypatch.setenv("EMAIL_PASS", "generic-pass")
    monkeypatch.setenv("EMAIL_TO", "generic@example.com")
    s = cls(sender_email="explicit@example.com")
    assert s.sender_email == "explicit@example.com"


@pytest.mark.parametrize("cls", _senders(), ids=lambda c: c.__name__)
def test_empty_string_config_value_falls_through_to_env(cls, monkeypatch):
    """main.py passes config.json's email.sender_email / recipient_email straight
    into the constructor. Those are "" on this deployment, which is falsy and must
    fall through to the env rather than pinning the sender to an empty string."""
    monkeypatch.setenv("EMAIL_USER", "ops@example.com")
    monkeypatch.setenv("EMAIL_PASS", "app-password-1234")
    monkeypatch.setenv("EMAIL_TO", "ops@example.com")
    s = cls(sender_email="", recipient_email="")
    assert s.is_configured(), (
        "empty-string config values must not defeat the env fallback — this is "
        "exactly how main.py calls it"
    )
    assert s.sender_email == "ops@example.com"


def test_get_env_first_returns_first_non_empty(monkeypatch):
    from utils import get_env_first
    monkeypatch.setenv("A_NAME", "")
    monkeypatch.setenv("B_NAME", "   ")
    monkeypatch.setenv("C_NAME", "value-c")
    assert get_env_first(["A_NAME", "B_NAME", "C_NAME"]) == "value-c", (
        "blank and whitespace-only values must be skipped, not returned"
    )
    assert get_env_first(["MISSING_1", "MISSING_2"], default="fb") == "fb"
    assert get_env_first(["MISSING_1"]) is None
