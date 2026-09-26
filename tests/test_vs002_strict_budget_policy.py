"""VS-002 strict evidence client: the LOCAL FMP daily-budget policy is inherited
from config.json (api_limits.fmp_daily_calls_budget), matching the rest of the
app, while the 22-attempt mission cap remains an independent hard boundary.

Offline: no network, no real FMP calls.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from portfolio_automation.data_budget import factory as F
from portfolio_automation.vs002_evidence import production_runner as PR


@pytest.fixture(autouse=True)
def _fmp_key(monkeypatch):
    # The factory constructs a real FMPClient, which requires a credential to be
    # present. Presence only — no network is made in these tests.
    monkeypatch.setenv("FMP_API_KEY", "TEST_KEY_NOT_USED")


def _cfg(tmp_path: Path, value, name="config.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps({"api_limits": {"fmp_daily_calls_budget": value}}),
                 encoding="utf-8")
    return p


def _client(tmp_path, *, config_path=None, daily_budget=None, cache="c"):
    return F.vs002_strict_evidence_client(
        daily_budget=daily_budget,
        config_path=config_path if config_path is not None else "config.json",
        cache_dir=tmp_path / cache)


# CASE A — configured zero => no local daily cap; counter may exceed 230
def test_case_a_configured_zero_is_uncapped(tmp_path):
    cl = _client(tmp_path, config_path=_cfg(tmp_path, 0))
    assert cl._budget == 0
    cl._counter.increment(493)                      # far past the old 230
    assert cl.can_admit(22) is True                 # zero == no local cap


# CASE B — configured positive cap honored at the boundary
def test_case_b_configured_positive_cap(tmp_path):
    a = _client(tmp_path, config_path=_cfg(tmp_path, 500), cache="a")
    assert a._budget == 500
    a._counter.increment(470)
    assert a.can_admit(22) is True                  # 470 + 22 = 492 <= 500
    b = _client(tmp_path, config_path=_cfg(tmp_path, 500), cache="b")
    b._counter.increment(490)
    assert b.can_admit(22) is False                 # 490 + 22 = 512 > 500


# CASE C — legacy 230 behavior unchanged
def test_case_c_legacy_230(tmp_path):
    a = _client(tmp_path, config_path=_cfg(tmp_path, 230), cache="a")
    assert a._budget == 230
    a._counter.increment(208)
    assert a.can_admit(22) is True                  # 208 + 22 = 230 <= 230
    b = _client(tmp_path, config_path=_cfg(tmp_path, 230), cache="b")
    b._counter.increment(210)
    assert b.can_admit(22) is False                 # 210 + 22 = 232 > 230


# CASE D — key genuinely absent => safe fallback 230
def test_case_d_missing_key_falls_back_230(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"api_limits": {}}), encoding="utf-8")
    assert _client(tmp_path, config_path=p)._budget == 230
    # entirely absent file too
    assert _client(tmp_path, config_path=tmp_path / "nope.json", cache="c2")._budget == 230


# CASE E — malformed value must NOT become uncapped (falls back to 230)
@pytest.mark.parametrize("bad", [None, "x", {"a": 1}, [1, 2], True, False, 1.5])
def test_case_e_malformed_is_not_uncapped(tmp_path, bad):
    cl = _client(tmp_path, config_path=_cfg(tmp_path, bad))
    assert cl._budget == 230                         # capped, never silently 0


# CASE F — explicit override wins over config (deterministic seam)
def test_case_f_explicit_override(tmp_path):
    cl = _client(tmp_path, config_path=_cfg(tmp_path, 0), daily_budget=123)
    assert cl._budget == 123


# CASE G — the 22-attempt mission cap is independent of the (zero) daily budget
def test_case_g_mission_bound_independent_of_zero_budget(tmp_path):
    from tests.test_vs002_production_runner import FakeStrictClient
    # a config-0 client does not block on budget, even with a huge counter
    real = _client(tmp_path, config_path=_cfg(tmp_path, 0))
    real._counter.increment(999)
    assert real.can_admit(22) is True
    # yet the StrictLiveAcquirer still hard-bounds the mission to 22, SPY-first,
    # each once, no 23rd — regardless of budget
    plan = PR.acquisition_plan()
    acq = PR.StrictLiveAcquirer(FakeStrictClient(), plan)
    for sym in plan:
        acq.get_historical_prices_dividend_adjusted(sym)
    assert acq.attempted_http_requests == 22
    with pytest.raises(PR.StrictLiveAcquisitionError):
        acq.get_historical_prices_dividend_adjusted(plan[1])      # 23rd refused
    acq2 = PR.StrictLiveAcquirer(FakeStrictClient(), plan)
    with pytest.raises(PR.StrictLiveAcquisitionError):
        acq2.get_historical_prices_dividend_adjusted("AAPL")      # SPY must be first


# The production config's actual policy is 0 (uncapped local) — guard against a
# silent revert of config.json that would re-impose a local cap.
def test_production_config_is_uncapped_locally():
    cfg = json.loads((Path(__file__).resolve().parents[1] / "config.json")
                     .read_text(encoding="utf-8"))
    assert cfg["api_limits"]["fmp_daily_calls_budget"] == 0
