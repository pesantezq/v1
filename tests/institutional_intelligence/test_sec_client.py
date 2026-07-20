"""Phase 2 tests — governed SEC client (fully offline; never touches network).

Covers: CIK padding, pure retry/backoff logic, fixture resolution, the
disabled-path guarantee (network never called when live is off), cache
round-trip, live success/terminal/transient-retry/exhaustion via an injected
urlopen, readiness, and the invariant that the User-Agent is never persisted to
the ledger.
"""

from __future__ import annotations

import io
import urllib.error

import pytest

from portfolio_automation.institutional_intelligence import sec_client as sc


# --- fakes ---------------------------------------------------------------

class _FakeHTTP:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _queue_urlopen(*outcomes):
    """Return a urlopen callable yielding queued outcomes.

    Each outcome is either bytes (200 OK), an int status (→ HTTPError), or a
    tuple (status, retry_after) for a retryable error with a Retry-After.
    """
    seq = list(outcomes)

    def _urlopen(req, timeout=None):
        outcome = seq.pop(0)
        if isinstance(outcome, bytes):
            return _FakeHTTP(outcome, 200)
        if isinstance(outcome, tuple):
            status, retry_after = outcome
            hdrs = {"Retry-After": str(retry_after)} if retry_after is not None else {}
            raise urllib.error.HTTPError(req.full_url, status, "err", hdrs, io.BytesIO(b""))
        raise urllib.error.HTTPError(req.full_url, int(outcome), "err", {}, io.BytesIO(b""))

    return _urlopen


def _cfg(tmp_path, **over):
    base = dict(
        live_enabled=False,
        cache_dir=tmp_path / "cache",
        db_path=tmp_path / "inst.db",
        fixtures_dir=tmp_path / "fixtures",
    )
    base.update(over)
    return sc.SECClientConfig(**base)


def _no_network(*a, **k):
    raise AssertionError("network must NOT be called")


# --- pure helpers --------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("320193", "0000320193"),
    (320193, "0000320193"),
    ("0002045724", "0002045724"),
    ("CIK0000320193", "0000320193"),
])
def test_cik_padding(raw, expected):
    assert sc.cik_to_padded(raw) == expected


def test_cik_padding_invalid():
    with pytest.raises(sc.SECClientError):
        sc.cik_to_padded("abc")


def test_should_retry():
    assert sc.should_retry(429) is True
    assert sc.should_retry(503) is True
    assert sc.should_retry(404) is False
    assert sc.should_retry(200) is False
    assert sc.should_retry(None) is False


def test_backoff_bounded_and_honors_retry_after():
    assert sc.backoff_delay(0) == 1.0
    assert sc.backoff_delay(1) == 2.0
    assert sc.backoff_delay(100) == sc._BACKOFF_MAX_S     # bounded
    assert sc.backoff_delay(0, retry_after=7) == 7.0       # honored
    assert sc.backoff_delay(0, retry_after=999) == sc._BACKOFF_MAX_S  # bounded


# --- fixture resolution --------------------------------------------------

def test_fetch_resolves_fixture_offline(tmp_path):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    url = "https://data.sec.gov/submissions/CIK0000320193.json"
    key = sc._sanitize_url_to_key(url)
    (fixtures / f"{key}.json").write_text('{"cik": 320193}', encoding="utf-8")
    client = sc.GovernedSECClient(_cfg(tmp_path), user_agent="ua",
                                  urlopen=_no_network)
    resp = client.fetch(url)
    assert resp.from_fixture and resp.source == "fixture"
    assert resp.body == '{"cik": 320193}'
    assert resp.content_hash is not None


def test_fetch_resolves_fixture_via_manifest(tmp_path):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    url = "https://data.sec.gov/submissions/CIK0000320193.json"
    (fixtures / "aapl.json").write_text('{"ok": true}', encoding="utf-8")
    import json
    (fixtures / "manifest.json").write_text(json.dumps({url: "aapl.json"}), encoding="utf-8")
    client = sc.GovernedSECClient(_cfg(tmp_path), user_agent="ua", urlopen=_no_network)
    assert client.fetch(url).body == '{"ok": true}'


# --- disabled path: network is NEVER called -----------------------------

def test_disabled_never_calls_network(tmp_path):
    # live disabled, no fixture for this URL, injected urlopen would explode.
    (tmp_path / "fixtures").mkdir()
    client = sc.GovernedSECClient(_cfg(tmp_path, live_enabled=False),
                                  user_agent="ua", urlopen=_no_network)
    resp = client.fetch("https://data.sec.gov/submissions/CIK0000000001.json")
    assert resp.source == "disabled" and resp.body is None and not resp.ok


def test_live_ready_requires_ua_and_flag(tmp_path):
    (tmp_path / "fixtures").mkdir()
    assert sc.GovernedSECClient(_cfg(tmp_path, live_enabled=True),
                                user_agent=None, urlopen=_no_network).live_ready is False
    assert sc.GovernedSECClient(_cfg(tmp_path, live_enabled=False),
                                user_agent="ua", urlopen=_no_network).live_ready is False
    assert sc.GovernedSECClient(_cfg(tmp_path, live_enabled=True),
                                user_agent="ua", urlopen=_no_network).live_ready is True


# --- live path (injected transport) -------------------------------------

def _live_client(tmp_path, urlopen):
    (tmp_path / "fixtures").mkdir()
    return sc.GovernedSECClient(
        _cfg(tmp_path, live_enabled=True), user_agent="TestBot contact@example.com",
        urlopen=urlopen, sleep_fn=lambda s: None, clock_fn=lambda: 0.0,
    )


def test_live_success_and_caches(tmp_path):
    url = "https://data.sec.gov/submissions/CIK0000320193.json"
    client = _live_client(tmp_path, _queue_urlopen(b'{"live": 1}'))
    resp = client.fetch(url)
    assert resp.source == "live" and resp.status == 200 and resp.body == '{"live": 1}'
    # Second fetch resolves from cache without touching the (exhausted) queue.
    resp2 = client.fetch(url)
    assert resp2.from_cache and resp2.body == '{"live": 1}'


def test_live_terminal_404_not_retried(tmp_path):
    client = _live_client(tmp_path, _queue_urlopen(404))
    with pytest.raises(sc.SECClientError, match="terminal"):
        client.fetch("https://data.sec.gov/submissions/CIK0000000404.json")


def test_live_transient_then_success(tmp_path):
    client = _live_client(tmp_path, _queue_urlopen((503, 1), b'{"ok": 1}'))
    resp = client.fetch("https://data.sec.gov/submissions/CIK0000000503.json")
    assert resp.source == "live" and resp.body == '{"ok": 1}'


def test_live_retries_exhausted(tmp_path):
    client = _live_client(tmp_path, _queue_urlopen((503, None), (503, None),
                                                   (503, None), (503, None)))
    with pytest.raises(sc.SECClientError, match="exhausted"):
        client.fetch("https://data.sec.gov/submissions/CIK0000000999.json")


# --- User-Agent is NEVER persisted --------------------------------------

def test_user_agent_never_in_ledger_or_readiness(tmp_path):
    ua = "SecretBot secret-contact@example.com"
    url = "https://data.sec.gov/submissions/CIK0000320193.json"
    client = _live_client(tmp_path, _queue_urlopen(b'{"x": 1}'))
    object.__setattr__  # noqa: B018 - readability
    # override UA to the sensitive value
    client._user_agent = ua
    client.fetch(url)
    rows = client.ledger_rows()
    assert rows, "ledger should have recorded the request"
    blob = repr(rows) + repr(client.readiness())
    assert "secret-contact@example.com" not in blob
    assert ua not in blob
    # readiness reports presence, never the value
    assert client.readiness()["user_agent_present"] is True


def test_fetch_submissions_builds_padded_url(tmp_path):
    captured = {}

    def _cap(req, timeout=None):
        captured["url"] = req.full_url
        return _FakeHTTP(b"{}", 200)

    client = _live_client(tmp_path, _cap)
    client.fetch_submissions("320193")
    assert captured["url"] == "https://data.sec.gov/submissions/CIK0000320193.json"


def test_ledger_records_provenance(tmp_path):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    url = "https://data.sec.gov/submissions/CIK0000320193.json"
    (fixtures / f"{sc._sanitize_url_to_key(url)}.json").write_text("{}", encoding="utf-8")
    client = sc.GovernedSECClient(_cfg(tmp_path), user_agent="ua", urlopen=_no_network)
    client.fetch(url)
    rows = client.ledger_rows()
    assert rows[0]["source"] == "fixture"
    assert rows[0]["content_hash"] is not None
