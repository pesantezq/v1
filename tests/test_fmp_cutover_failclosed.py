"""FMP-4 — fail-closed cutover safety (issue #78).

No real network: urllib.request.urlopen is replaced by a recorder that FAILS the
test if reached when it must not be. No .env / secrets / real cache artifacts
are read; every client uses a tmp cache dir and a synthetic api key.
"""
from __future__ import annotations

import ast
import io
import json
import os
import shutil
import subprocess
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import fmp_client
from fmp_client import CallBudgetExceeded, FMPClient, FMPCutoverRefused, FMPError
from portfolio_automation.data_budget import fmp_cutover as fc

REPO = Path(__file__).resolve().parents[1]
KEY = "SYNTHETIC_KEY_DO_NOT_LEAK_9f3a"
UTC = timezone.utc

PRE = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
JUST_BEFORE = fc.FMP_CUTOVER_HOLD_UTC - timedelta(seconds=1)
POST = fc.FMP_CUTOVER_HOLD_UTC
LATER = datetime(2027, 1, 5, tzinfo=UTC)


def clock_at(ts):
    return lambda: ts


class _Resp:
    def __init__(self, body: bytes):
        self._b = body

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def net(monkeypatch):
    """Recorder for outbound HTTP. ``net.body`` / ``net.error`` shape replies."""
    class Net:
        calls: list = []
        body = json.dumps([{"symbol": "AAA", "price": 1.0}]).encode()
        error = None

    n = Net()
    n.calls = []

    def fake_urlopen(req, timeout=None):
        n.calls.append(req.full_url)
        if n.error is not None:
            raise n.error
        return _Resp(n.body)

    monkeypatch.setattr(fmp_client.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(fmp_client.time, "sleep", lambda s: None)
    return n


def mk(tmp_path, clock, **kw):
    kw.setdefault("retry_base_delay", 0)
    return FMPClient(api_key=KEY, cache_dir=tmp_path / "cache", clock=clock, **kw)


# ---------------------------------------------------------------------------
# Constants / clock semantics
# ---------------------------------------------------------------------------

def test_hold_is_effective_before_nov27_in_every_timezone():
    assert fc.FMP_CUTOVER_HOLD_UTC < fc.FMP_STARTER_END_UTC
    # Earliest civil timezone (UTC+14): Nov 27 begins at 2026-11-26T10:00Z.
    earliest_nov27 = datetime(2026, 11, 27, tzinfo=timezone(timedelta(hours=14)))
    assert fc.FMP_CUTOVER_HOLD_UTC <= earliest_nov27.astimezone(UTC)


def test_bash_and_python_epochs_match():
    text = (REPO / "scripts" / "fmp_cutover_guard.sh").read_text(encoding="utf-8")
    # Bash must agree with the Python cutoff but never read inherited globals.
    assert f'if [ "$now" -ge {fc.FMP_CUTOVER_HOLD_EPOCH} ]; then' in text
    assert "FMP_CUTOVER_HOLD_EPOCH=" not in "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert fc.FMP_CUTOVER_HOLD_EPOCH == 1795651200


@pytest.mark.parametrize("ts,held", [
    (PRE, False), (JUST_BEFORE, False), (POST, True), (LATER, True),
    # Same instant expressed in other offsets (DST / account-tz independence).
    (datetime(2026, 11, 25, 19, 0, tzinfo=timezone(timedelta(hours=-5))), True),   # 00:00Z
    (datetime(2026, 11, 26, 9, 0, tzinfo=timezone(timedelta(hours=14))), False),   # 19:00Z 25th
])
def test_cutover_reason_by_instant(ts, held):
    assert fc.is_cutover_hold(clock_at(ts)) is held


@pytest.mark.parametrize("bad", [
    lambda: datetime(2026, 11, 14, 12, 0),        # naive: wall-clock ambiguous
    lambda: "2026-11-14T12:00:00Z",                # not a datetime
    lambda: (_ for _ in ()).throw(OSError("rtc")),  # clock failure
])
def test_ambiguous_or_broken_clock_holds(bad):
    assert fc.is_cutover_hold(bad) is True


def test_no_env_bypass(monkeypatch):
    for k in ("FMP_CUTOVER_BYPASS", "STOCKBOT_FMP_CUTOVER_DISABLED",
              "FMP_CUTOVER_GUARD_NOW_EPOCH", "FMP_ALLOW_POST_CUTOVER"):
        monkeypatch.setenv(k, "1")
    assert fc.is_cutover_hold(clock_at(POST)) is True
    src = (REPO / "portfolio_automation" / "data_budget" / "fmp_cutover.py").read_text(encoding="utf-8")
    assert "os.environ" not in src and "getenv" not in src


def test_check_entrypoint_codes():
    assert fc.check_entrypoint("daily_safe", clock_at(PRE)) == 0
    assert fc.check_entrypoint("daily_safe", clock_at(POST)) == fc.CUTOVER_HOLD_EXIT_CODE == 3


# ---------------------------------------------------------------------------
# Transport: direct, generic dynamic, legacy v3/v4, retries, strict-live
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("budget", [0, -1, 230, 10**9])
def test_post_cutover_raw_get_refused_regardless_of_budget(tmp_path, net, budget):
    c = mk(tmp_path, clock_at(POST), daily_budget=budget)
    with pytest.raises(FMPCutoverRefused) as ei:
        c._raw_get("quote", {"symbol": "AAA"}, base_url=fmp_client.FMP_STABLE_BASE_URL)
    assert isinstance(ei.value, FMPError)
    assert net.calls == []
    assert KEY not in str(ei.value) and "apikey" not in str(ei.value)
    assert c.calls_today == 0


def test_post_cutover_generic_get_json_and_legacy_v3_refused(tmp_path, net):
    c = mk(tmp_path, clock_at(LATER), daily_budget=0)
    with pytest.raises(FMPCutoverRefused):
        c.get_json("/api/v4/social-sentiment", {"symbol": "AAA"})
    with pytest.raises(FMPCutoverRefused) as ei:
        c.get_sp500_constituents()  # legacy v3/sp500_constituent
    assert "legacy_v3_v4" in str(ei.value)
    # v3 batch → refused; its stable per-symbol fallback is ALSO refused → empty.
    assert not c.get_batch_profiles_v3(["AAA", "BBB"])
    assert net.calls == []


def test_post_cutover_public_wrappers_fail_closed_without_fake_prices(tmp_path, net):
    c = mk(tmp_path, clock_at(POST), daily_budget=0)
    quotes = c.get_batch_quotes(["AAA", "BBB"])
    assert not quotes  # missing, never zero-filled
    assert net.calls == []


def test_retry_crossing_cutoff_does_not_go_out(tmp_path, net):
    """Attempt 1 pre-cutover fails (503); the backoff crosses the cutoff; the
    retry must be refused, not sent."""
    times = iter([PRE, PRE, PRE, POST, POST, POST])
    c = mk(tmp_path, lambda: next(times), daily_budget=0, retry_max=3)
    net.error = urllib.error.HTTPError("u", 503, "x", {}, io.BytesIO(b""))
    with pytest.raises(FMPCutoverRefused):
        c._raw_get("quote", {"symbol": "AAA"}, base_url=fmp_client.FMP_STABLE_BASE_URL)
    assert len(net.calls) == 1


def test_post_cutover_strict_live_refused_before_on_attempt(tmp_path, net):
    c = mk(tmp_path, clock_at(POST), daily_budget=0)
    seen = []
    for fn in (c.get_dividend_adjusted_bars_strict_live, c.get_full_bars_strict_live):
        with pytest.raises(FMPCutoverRefused):
            fn("SPY", on_attempt=seen.append)
    with pytest.raises(FMPCutoverRefused):
        c._raw_get_once("historical-price-eod/dividend-adjusted", {"symbol": "SPY"},
                        base_url=fmp_client.FMP_STABLE_BASE_URL)
    assert seen == [] and net.calls == [] and c.calls_today == 0


def test_no_key_leak_in_logs(tmp_path, net, caplog):
    caplog.set_level("DEBUG")
    c = mk(tmp_path, clock_at(POST), daily_budget=0)
    assert c.get_profile("AAA") is None  # swallows FMPError → None
    with pytest.raises(FMPCutoverRefused) as ei:
        c.get_quote_short("AAA")
    assert KEY not in caplog.text and KEY not in str(ei.value)
    assert net.calls == []


# ---------------------------------------------------------------------------
# Disk cache after cutover: never serve fresh or stale; never delete
# ---------------------------------------------------------------------------

def _seed(tmp_path, key, data):
    c = mk(tmp_path, clock_at(PRE))
    c._cache.set(key, data)
    return c


def test_cache_fresh_and_stale_refused_after_cutover_files_kept(tmp_path, net):
    _seed(tmp_path, "quote_stable_AAA", [{"symbol": "AAA", "price": 123.0}])
    _seed(tmp_path, "profile_stable_AAA", [{"symbol": "AAA"}])
    pre = mk(tmp_path, clock_at(PRE))
    assert pre._cache.get("quote_stable_AAA", 3600) is not None  # rollback/pre unchanged

    post = mk(tmp_path, clock_at(POST), daily_budget=0)
    assert post._cache.get("quote_stable_AAA", 10**9) is None
    assert post._cache.get_stale("quote_stable_AAA") is None
    assert post.get_batch_quotes(["AAA"]) == {} or not post.get_batch_quotes(["AAA"])
    assert post.get_profile("AAA") is None
    with pytest.raises(FMPCutoverRefused):  # generic path: no cached rescue
        post.get_json("/stable/grades", {"symbol": "AAA"})
    assert net.calls == []
    assert (tmp_path / "cache" / "quote_stable_AAA.json").exists()  # archive not deleted


def test_budget_exhausted_post_cutover_gives_no_stale_rescue(tmp_path, net):
    _seed(tmp_path, "quote_short_AAA", {"symbol": "AAA", "price": 5})
    c = mk(tmp_path, clock_at(POST), daily_budget=1)
    c._counter.increment(5)
    assert c.get_quote_short("AAA") == {}
    with pytest.raises(CallBudgetExceeded):
        c._get_cached("sp", fmp_client._EP_V3_SP500, 60)


# ---------------------------------------------------------------------------
# Pre-cutover / rollback: behaviour unchanged
# ---------------------------------------------------------------------------

def test_pre_cutover_transport_unchanged_including_v3(tmp_path, net):
    c = mk(tmp_path, clock_at(JUST_BEFORE), daily_budget=0)
    assert c._raw_get("v3/sp500_constituent", {}) == [{"symbol": "AAA", "price": 1.0}]
    assert len(net.calls) == 1 and c.calls_today == 1
    assert c.get_json("/stable/grades", {"symbol": "AAA"}) is not None


def test_default_clock_is_pre_cutover_today_for_existing_suites(tmp_path, net):
    if datetime.now(UTC) >= fc.FMP_CUTOVER_HOLD_UTC:
        pytest.skip("real clock is past the hold; existing suites are expected to HOLD")
    c = FMPClient(api_key=KEY, cache_dir=tmp_path / "c", retry_base_delay=0)
    assert c._raw_get("quote", {"symbol": "AAA"}, base_url=fmp_client.FMP_STABLE_BASE_URL)


def test_vs002_strict_live_exact_endpoint_single_attempt_pre_cutover(tmp_path, net):
    c = mk(tmp_path, clock_at(PRE), daily_budget=0)
    seen = []
    c.get_dividend_adjusted_bars_strict_live("spy", on_attempt=seen.append)
    assert seen == ["SPY"] and len(net.calls) == 1
    url = net.calls[0]
    assert url.startswith(
        "https://financialmodelingprep.com/stable/historical-price-eod/dividend-adjusted?")
    assert "/full" not in url and "/api/v3" not in url
    # Failure stays a single attempt, counted, no /full fallback.
    net.calls.clear()
    net.error = urllib.error.HTTPError("u", 500, "x", {}, io.BytesIO(b""))
    with pytest.raises(FMPError):
        c.get_dividend_adjusted_bars_strict_live("SPY")
    assert len(net.calls) == 1 and "dividend-adjusted" in net.calls[0]


def test_no_network_or_broker_imports_in_guard_and_no_broker_in_client():
    def imports(p):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        out = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                out |= {a.name for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                out.add(n.module)
        return out
    guard = imports(REPO / "portfolio_automation" / "data_budget" / "fmp_cutover.py")
    assert not any(m.split(".")[0] in {"urllib", "requests", "socket", "http", "os"} for m in guard)
    client = imports(REPO / "fmp_client.py")
    assert not any("broker" in m or "execution" in m for m in client | guard)


# ---------------------------------------------------------------------------
# Pure Basic provider-window budget model
# ---------------------------------------------------------------------------

def _model(**kw):
    kw.setdefault("basic_use_authorized", True)
    kw.setdefault("upstream_usage_bytes", 0)
    return fc.BasicProviderWindowBudget(**kw)


def test_model_inactive_by_default_and_cache_never_admissible():
    m = fc.BasicProviderWindowBudget()
    assert m.admit(PRE, "quote").reason == "basic_use_not_authorized"
    assert m.cached_data_admission() == fc.Admission(False, "cached_data_rights_unresolved")


def test_model_rejects_legacy_v3_v4():
    m = _model()
    for ep, base in [("v3/profile/AAA", ""), ("api/v4/profile/all", ""),
                     ("profile", "https://financialmodelingprep.com/api/v3"),
                     ("/api/v3/quote/AAA", "https://financialmodelingprep.com")]:
        assert m.admit(PRE, ep, base).reason == "legacy_v3_v4_rejected", ep
    assert m.admit(PRE, "quote", fmp_client.FMP_STABLE_BASE_URL).allowed


def test_model_rolling_24h_exhaustion_ignores_fixed_reset():
    m = _model()
    t0 = datetime(2026, 12, 1, 19, 0, tzinfo=UTC)  # 14:00 EST, just before "3 PM reset"
    for i in range(250):
        m.record_attempt(t0 + timedelta(seconds=i))
    assert m.admit(t0 + timedelta(minutes=5), "quote").allowed is False
    # After the claimed 3 PM EST reset (20:00Z) — still refused (rolling window).
    assert m.admit(t0 + timedelta(hours=2), "quote").reason == "rolling_24h_call_limit"
    assert m.admit(t0 + timedelta(hours=24, seconds=251), "quote").allowed is True


def test_model_failed_attempts_and_retries_count():
    m = _model(daily_limit=3)
    t = datetime(2026, 12, 2, tzinfo=UTC)
    # One logical request: initial attempt fails + 2 retries = 3 attempts.
    for k in range(3):
        assert m.admit(t, "quote").allowed
        m.record_attempt(t + timedelta(seconds=k))  # recorded BEFORE HTTP, success or not
    assert m.admit(t + timedelta(seconds=5), "quote").reason == "rolling_24h_call_limit"


def test_model_dst_fall_back_uses_elapsed_utc_not_wall_clock():
    m = _model(daily_limit=1)
    edt, est = timezone(timedelta(hours=-4)), timezone(timedelta(hours=-5))
    m.record_attempt(datetime(2026, 10, 31, 12, 0, tzinfo=edt))       # 16:00Z
    # 2026-11-01 11:30 EST is 23.5 wall-hours later but 24.5h elapsed → expired.
    assert m.admit(datetime(2026, 11, 1, 11, 30, tzinfo=est), "quote").allowed
    # 10:30 EST is 23.5h elapsed → still inside the window.
    assert not m.admit(datetime(2026, 11, 1, 10, 30, tzinfo=est), "quote").allowed
    assert m.admit(datetime(2026, 11, 1, 11, 30), "quote").reason == "clock_naive_ambiguous"
    with pytest.raises(ValueError):
        m.record_attempt(datetime(2026, 11, 1, 11, 30))


def test_model_bandwidth_unknown_upstream_fails_closed_and_500mb_cap():
    t = datetime(2026, 12, 3, tzinfo=UTC)
    assert _model(upstream_usage_bytes=None).admit(t, "quote").reason == \
        "bandwidth_upstream_usage_unknown"
    m = _model(upstream_usage_bytes=400_000_000)
    m.record_bytes(t - timedelta(days=29), 99_999_999)
    assert m.admit(t, "quote").allowed
    m.record_bytes(t, 1)
    assert m.admit(t, "quote").reason == "bandwidth_30d_limit"
    # Bytes older than 30 days roll off.
    assert m.admit(t + timedelta(days=1, seconds=1), "quote").allowed


# ---------------------------------------------------------------------------
# Scheduled entrypoints (bash): early refusal, non-zero, no credential load
# ---------------------------------------------------------------------------

def _bash():
    if sys.platform == "win32":
        for p in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
            if Path(p).exists():
                return p
        return None  # avoid WSL's System32 bash (different filesystem view)
    return shutil.which("bash")


BASH = _bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="Git/POSIX bash not available")
FUTURE = str(fc.FMP_CUTOVER_HOLD_EPOCH + 86400 * 400)


def _run(args, env_extra=None, cwd=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("FMP_")}
    env.update(env_extra or {})
    return subprocess.run([BASH, *args], capture_output=True, text=True,
                          env=env, cwd=cwd, timeout=60)


@needs_bash
def test_bash_guard_holds_when_forced_forward():
    r = _run([(REPO / "scripts" / "fmp_cutover_guard.sh").as_posix(), "daily_safe"],
             {"FMP_CUTOVER_GUARD_NOW_EPOCH": FUTURE})
    assert r.returncode == 3 and "FMP_CUTOVER_HOLD entrypoint=daily_safe" in r.stderr


@needs_bash
def test_bash_guard_injected_past_cannot_bypass():
    r = _run([(REPO / "scripts" / "fmp_cutover_guard.sh").as_posix()],
             {"FMP_CUTOVER_GUARD_NOW_EPOCH": "0"})
    expected = 3 if datetime.now(UTC) >= fc.FMP_CUTOVER_HOLD_UTC else 0
    assert r.returncode == expected


def _fake_repo(tmp_path):
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "main.py").write_text("raise SystemExit('main.py must not run')\n")
    (root / "requirements.txt").write_text("")
    for s in ("run_daily_safe.sh", "run_monthly_universe_refresh.sh", "fmp_cutover_guard.sh"):
        shutil.copy(REPO / "scripts" / s, root / "scripts" / s)
    marker = (root / "SIDE_EFFECT").as_posix()
    (root / "scripts" / "preflight.sh").write_text(f"#!/usr/bin/env bash\ntouch '{marker}'\n")
    # Sourced (executed) by the monthly script if it ever reached .env loading.
    (root / ".env").write_text(f"touch '{marker}'\nFMP_API_KEY=should_not_load\n")
    return root


@needs_bash
def test_daily_entrypoint_refuses_before_preflight_and_env(tmp_path):
    root = _fake_repo(tmp_path)
    r = _run([(root / "scripts" / "run_daily_safe.sh").as_posix()],
             {"REPO_ROOT": root.as_posix(), "FMP_CUTOVER_GUARD_NOW_EPOCH": FUTURE},
             cwd=root)
    out = r.stdout + r.stderr
    assert r.returncode == 3, out
    assert "FMP_CUTOVER_HOLD entrypoint=daily_safe" in out and "DAILY RUN FAILED" in out
    assert not (root / "SIDE_EFFECT").exists()


@needs_bash
def test_monthly_entrypoint_refuses_before_lock_and_env(tmp_path):
    root = _fake_repo(tmp_path)
    r = _run([(root / "scripts" / "run_monthly_universe_refresh.sh").as_posix()],
             {"REPO_ROOT": root.as_posix(), "FMP_CUTOVER_GUARD_NOW_EPOCH": FUTURE,
              "PY": "/nonexistent/python"}, cwd=root)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "FMP_CUTOVER_HOLD monthly_universe_refresh" in r.stderr
    # FMP4-R2: the guard now runs BEFORE the log dir is created — the HOLD
    # line goes to stderr (cron mail / journal) and nothing is written.
    assert not (root / "logs").exists()
    assert not (root / "SIDE_EFFECT").exists()


def test_scripts_place_guard_before_credentials():
    daily = (REPO / "scripts" / "run_daily_safe.sh").read_text(encoding="utf-8")
    g = daily.index("fmp_cutover_guard daily_safe")
    assert g < daily.index('"$REPO_ROOT/scripts/preflight.sh"')
    assert g < daily.index('load_dotenv_file "$REPO_ROOT/.env"')
    assert g < daily.index("schwab_sync")
    monthly = (REPO / "scripts" / "run_monthly_universe_refresh.sh").read_text(encoding="utf-8")
    m = monthly.index("fmp_cutover_guard monthly_universe_refresh")
    assert m < monthly.index('. "$REPO_ROOT/.env"') and m < monthly.index("flock -n 9")


# Independent security regression additions after author implementation.

def test_injected_clock_cannot_rewind_real_postcutover_clock(monkeypatch, tmp_path, net):
    monkeypatch.setattr(fc, "_system_clock", clock_at(POST))
    assert fc.is_cutover_hold(clock_at(PRE))
    c = mk(tmp_path, clock_at(PRE), daily_budget=0)
    with pytest.raises(FMPCutoverRefused):
        c._raw_get("quote", {"symbol": "AAA"}, base_url=fmp_client.FMP_STABLE_BASE_URL)
    assert not net.calls


def test_dynamic_endpoint_and_cache_key_never_leak_secret_in_refusal(tmp_path, net, caplog):
    secret = "UNSAFE_SYNTHETIC_QUERY_TOKEN_X987"
    c = mk(tmp_path, clock_at(POST), daily_budget=0)
    with pytest.raises(FMPCutoverRefused) as exc:
        c.get_json("/stable/profile?apikey=" + secret, {"token": secret})
    assert secret not in str(exc.value)
    assert secret not in caplog.text
    assert not net.calls


@pytest.mark.parametrize("method", ["_raw_get", "_raw_get_once"])
def test_rate_limit_sleep_crossing_cutoff_is_blocked(tmp_path, net, method):
    state = {"at": JUST_BEFORE}
    c = mk(tmp_path, lambda: state["at"], daily_budget=0, retry_max=1)
    def cross_cutoff():
        state["at"] = POST
    c._rate_limit = cross_cutoff
    with pytest.raises(FMPCutoverRefused):
        getattr(c, method)("quote", {"symbol": "AAA"},
                           base_url=fmp_client.FMP_STABLE_BASE_URL)
    assert not net.calls
    assert c.calls_today == 0


def test_basic_model_never_accepts_inflated_limits_or_negative_upstream():
    t = datetime(2027, 1, 1, 16, tzinfo=UTC)
    b = fc.BasicProviderWindowBudget(
        basic_use_authorized=True, upstream_usage_bytes=0,
        daily_limit=999999, bandwidth_limit_bytes=999999999)
    for _ in range(250):
        b.record_attempt(t)
    assert not b.admit(t, "quote").allowed
    c = fc.BasicProviderWindowBudget(
        basic_use_authorized=True, upstream_usage_bytes=-100)
    assert not c.admit(t, "quote").allowed
    with pytest.raises(ValueError):
        b.record_bytes(t, -1)
