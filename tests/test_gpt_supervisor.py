"""Hermetic tests for the trusted GPT supervisor adapter (EW-0A).

No network: the OpenAI transport is dependency-injected. Proves the supervisor
(a) parses PASS/REPAIR/ESCALATE/ABSTAIN verdicts, (b) fails CLOSED to
SUPERVISOR_UNAVAILABLE on transport/parse/non-decision errors (never PASS),
(c) refuses to transmit a packet containing secret-like material, and (d) never
places the API key into the request body / packet.
"""
from __future__ import annotations

import json

import pytest

from portfolio_automation.engineer_worker import gpt_supervisor as GS
from portfolio_automation.engineer_worker.gpt_supervisor import (
    review, SupervisorConfig, SupervisorVerdict as V, SupervisorError)


def _clock():
    n = {"i": 0}
    def now():
        n["i"] += 1
        return f"2026-08-11T00:00:{n['i']:02d}Z"
    return now


def _cfg(**over):
    d = dict(key_file="/nonexistent/key", model="gpt-4o")
    d.update(over)
    return SupervisorConfig(**d)


def _resp(verdict, **extra):
    payload = {"verdict": verdict}
    payload.update(extra)
    return lambda body: {"model": "gpt-4o-x",
                         "choices": [{"message": {"content": json.dumps(payload)}}]}


def raising(body):
    raise GS.urllib.error.URLError("boom")


PACKET = {"task": {"title": "t"}, "requirements": ["r"], "acceptance_criteria": ["a"],
          "tests_run": ["tests/test_x.py"], "test_results": {"tests/test_x.py": "PASS"}}


@pytest.mark.parametrize("raw,exp", [
    ("PASS", V.PASS), ("repair", V.REPAIR), ("Escalate", V.ESCALATE), ("ABSTAIN", V.ABSTAIN),
])
def test_parses_all_decision_verdicts(raw, exp):
    d = review(PACKET, _cfg(), _clock(), transport=_resp(raw, reasons=["x"], unresolved_requirements=[]))
    assert d.verdict is exp and d.error is None
    assert d.verdict in GS._DECISION_VERDICTS


def test_transport_failure_is_unavailable_not_pass():
    d = review(PACKET, _cfg(), _clock(), transport=raising)
    assert d.verdict is V.SUPERVISOR_UNAVAILABLE and not d.is_pass and d.error


def test_non_decision_verdict_is_unavailable():
    d = review(PACKET, _cfg(), _clock(), transport=_resp("SUPERVISOR_UNAVAILABLE"))
    assert d.verdict is V.SUPERVISOR_UNAVAILABLE
    d2 = review(PACKET, _cfg(), _clock(), transport=_resp("LGTM_SHIP_IT"))
    assert d2.verdict is V.SUPERVISOR_UNAVAILABLE and not d2.is_pass


def test_malformed_response_is_unavailable():
    bad = lambda body: {"choices": [{"message": {"content": "not json at all"}}]}
    d = review(PACKET, _cfg(), _clock(), transport=bad)
    assert d.verdict is V.SUPERVISOR_UNAVAILABLE and not d.is_pass


def test_secret_bearing_packet_is_refused_without_transport(tmp_path):
    called = {"n": 0}
    def spy(body):
        called["n"] += 1
        return _resp("PASS")(body)
    leak = dict(PACKET, note="here is a key sk-svcacct-ABCDEFGH0123456789abcdef to use")
    d = review(leak, _cfg(), _clock(), transport=spy)
    assert d.verdict is V.SUPERVISOR_UNAVAILABLE and called["n"] == 0
    assert "secret" in (d.error or "")


def test_key_never_enters_request_body():
    captured = {}
    def spy(body):
        captured["body"] = body
        return _resp("PASS", reasons=[])(body)
    review(PACKET, _cfg(), _clock(), transport=spy)
    blob = json.dumps(captured["body"])
    for leak in ("Authorization", "Bearer", "sk-", "api_key", "/nonexistent/key"):
        assert leak not in blob


def test_oversized_packet_unavailable():
    big = dict(PACKET, blob="x" * (GS._MAX_PACKET_BYTES + 10))
    d = review(big, _cfg(), _clock(), transport=_resp("PASS"))
    assert d.verdict is V.SUPERVISOR_UNAVAILABLE and "size" in (d.error or "")


def test_default_transport_missing_keyfile_fails_closed():
    # No key_file -> the real transport raises SupervisorError -> UNAVAILABLE.
    d = review(PACKET, SupervisorConfig(key_file=None), _clock())
    assert d.verdict is V.SUPERVISOR_UNAVAILABLE and not d.is_pass
