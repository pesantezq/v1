"""FMP4-R1 — cutover-safety repair (issue #78), synthetic only.

No live FMP network, no secrets, no real data/cache reads, no broker. Every
filesystem fixture lives under ``tmp_path``; every HTTP seam is a tripwire.
The simulated system clock is pinned pre-HOLD by ``tests/conftest.py`` and is
moved to Nov 27 2026 / Jan 2027 explicitly where a test needs it.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from portfolio_automation.data_budget import fmp_cutover as fc

REPO = Path(__file__).resolve().parents[1]
UTC = timezone.utc
PRE = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
JUST_BEFORE = fc.FMP_CUTOVER_HOLD_UTC - timedelta(seconds=1)
HOLD = fc.FMP_CUTOVER_HOLD_UTC
NOV27 = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)
JAN27 = datetime(2027, 1, 15, 15, 0, tzinfo=UTC)
POST_TIMES = [HOLD, NOV27, JAN27]


def _system_at(monkeypatch, ts):
    monkeypatch.setattr(fc, "_system_clock", lambda: ts)


@pytest.fixture
def no_http(monkeypatch):
    import urllib.request

    def boom(*a, **k):
        raise AssertionError("outbound HTTP attempted")

    monkeypatch.setattr(urllib.request, "urlopen", boom)


# ---------------------------------------------------------------------------
# Clock semantics: deterministic future dates, production max() rule intact
# ---------------------------------------------------------------------------

def test_conftest_pins_simulated_system_clock_pre_hold():
    assert fc._system_clock() == PRE
    assert fc.cutover_hold_reason() is None


@pytest.mark.parametrize("ts,held", [(JUST_BEFORE, False), (HOLD, True),
                                     (NOV27, True), (JAN27, True)])
def test_simulated_system_now_boundaries(monkeypatch, ts, held):
    _system_at(monkeypatch, ts)
    assert fc.is_cutover_hold() is held


@pytest.mark.parametrize("ts", [NOV27, JAN27])
def test_simulated_future_system_now_cannot_be_rewound_by_injected_clock(monkeypatch, ts):
    _system_at(monkeypatch, ts)
    assert fc.is_cutover_hold(lambda: PRE) is True        # max(real, injected)
    assert fc.check_entrypoint("x", lambda: PRE) == 3


# ---------------------------------------------------------------------------
# Shared direct-cache accessor
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("p,held", [
    ("data/fmp_cache/profile_stable_AAA.json", True),
    (Path("/opt/stockbot/data/FMP_CACHE/top100_watchlist.json"), True),
    ("outputs/latest/theme_signals.json", False),
    ("data/fmp_cache_notes.txt", False),
])
def test_rights_held_path_classification(p, held):
    assert fc.is_rights_held_fmp_path(p) is held


def test_cache_read_accessor_pre_and_post(monkeypatch):
    p = "data/fmp_cache/profile_stable_AAA.json"
    assert fc.fmp_cache_read_hold_reason(p) is None
    _system_at(monkeypatch, JUST_BEFORE)
    assert fc.fmp_cache_read_hold_reason(p) is None
    for ts in POST_TIMES:
        _system_at(monkeypatch, ts)
        assert fc.fmp_cache_read_hold_reason(p).startswith("fmp_cutover_hold:")
        assert fc.fmp_cache_read_hold_reason("outputs/latest/x.json") is None


# ---------------------------------------------------------------------------
# Inherited stale price (P0)
# ---------------------------------------------------------------------------

class _CountingClient:
    def __init__(self):
        self.calls = 0

    def get_prices(self, symbols):
        self.calls += 1
        return {}          # what FMPMarketClient yields after an FMP refusal


def _holding():
    from utils import Holding
    return Holding(symbol="AAA", shares=10, target_weight=1.0, asset_class="equity",
                   current_price=100.0, market_value=1000.0)


def test_pre_hold_failed_fetch_keeps_previous_price_unchanged():
    import market_data
    c = _CountingClient()
    hs, failed = market_data.update_holdings_with_prices([_holding()], c)
    assert c.calls == 1 and failed == ["AAA"]
    assert hs[0].current_price == 100.0 and hs[0].market_value == 1000.0


@pytest.mark.parametrize("ts", POST_TIMES)
def test_post_hold_inherited_price_is_cleared_without_fetch(monkeypatch, ts, no_http):
    import market_data
    _system_at(monkeypatch, ts)
    c = _CountingClient()
    hs, failed = market_data.update_holdings_with_prices([_holding()], c)
    assert c.calls == 0 and failed == ["AAA"]
    assert hs[0].current_price is None and hs[0].market_value is None
    assert [h for h in hs if h.current_price is not None] == []   # main's predicate


def test_run_portfolio_update_stops_before_broker_pricing_and_outputs(monkeypatch, tmp_path, no_http):
    import main as M
    from portfolio_automation import holdings_resolver, run_status
    _system_at(monkeypatch, NOV27)

    def tripwire(*a, **k):
        raise AssertionError("must not run under the HOLD")

    monkeypatch.setattr(holdings_resolver, "apply_broker_overlay_to_config", tripwire)
    monkeypatch.setattr(M, "create_market_client", tripwire)
    monkeypatch.setattr(M, "update_holdings_with_prices", tripwire)
    monkeypatch.setattr(M, "generate_portfolio_summary", tripwire)
    statuses = []
    monkeypatch.setattr(run_status, "write_pipeline_run_status",
                        lambda s: statuses.append(s) or {})
    cfg = SimpleNamespace(output={"csv_path": str(tmp_path / "o" / "s.csv")})
    r = M.run_portfolio_update(cfg, run_mode="daily", output_dir=tmp_path / "o",
                               logger=logging.getLogger("t"))
    assert r["success"] is False
    assert r["data_mode"] == fc.DATA_UNAVAILABLE and r["degraded_mode"] is True
    assert r["summary"] is None and r["recommendations"] is None
    assert r["decision_plan"] == [] and any("FMP_CUTOVER_HOLD" in e for e in r["errors"])
    assert not (tmp_path / "o").exists()


def test_main_exits_3_before_run_lock(monkeypatch, tmp_path, no_http):
    import main as M
    _system_at(monkeypatch, JAN27)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["main.py", "--run-mode", "daily"])

    def tripwire(*a, **k):
        raise AssertionError("run lock / state must not be touched under the HOLD")

    monkeypatch.setattr(M, "acquire_run_lock", tripwire)
    assert M.main() == 3
    assert not (tmp_path / "data").exists()


# ---------------------------------------------------------------------------
# Scanner: no fallback save / no daily re-stamp of stale prices
# ---------------------------------------------------------------------------

def _wl(tmp_path):
    p = tmp_path / "data" / "fmp_cache" / "top100_watchlist.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"updated_at": "2026-11-20T00:00:00",
                             "candidates": [{"symbol": "AAA", "price": 100.0,
                                             "score": 50.0}]}), encoding="utf-8")
    return p


def test_scanner_pre_hold_unchanged(tmp_path):
    from scanner.candidate_scanner import CandidateScanner
    s = CandidateScanner(watchlist_path=_wl(tmp_path))
    rows = s.load_watchlist()
    assert [r["symbol"] for r in rows] == ["AAA"]
    out, _ = s.daily_refresh(rows, {})
    assert out and out[0]["price"] == 100.0


@pytest.mark.parametrize("ts", [NOV27, JAN27])
def test_scanner_post_hold_refuses_restamp_save_and_load(monkeypatch, tmp_path, ts):
    from scanner.candidate_scanner import CandidateScanner
    from scanner.fallback_watchlist import FallbackWatchlist
    p = _wl(tmp_path)
    before = p.read_bytes()
    _system_at(monkeypatch, ts)
    s = CandidateScanner(watchlist_path=p)
    assert s.load_watchlist() == [] and s.load_watchlist_screening() is None
    assert s.daily_refresh([{"symbol": "AAA", "price": 100.0, "score": 50.0}], {}) == ([], [])
    s.save_watchlist([{"symbol": "AAA", "price": 100.0, "score": 50.0}])
    FallbackWatchlist({}, watchlist_path=p).save([{"symbol": "AAA"}])
    assert p.read_bytes() == before            # not re-stamped, not deleted


# ---------------------------------------------------------------------------
# Direct rights-held cache readers (P1)
# ---------------------------------------------------------------------------

def _cache_root(tmp_path):
    root = tmp_path / "repo"
    c = root / "data" / "fmp_cache"
    c.mkdir(parents=True)
    (root / "outputs").mkdir()
    (c / "profile_stable_AAA.json").write_text(
        json.dumps({"data": [{"symbol": "AAA", "sector": "Technology",
                              "industry": "Semiconductors"}]}), encoding="utf-8")
    (c / "top100_watchlist.json").write_text(
        json.dumps({"candidates": [{"symbol": "AAA", "score": 50}]}), encoding="utf-8")
    return root


def _readers(root):
    from portfolio_automation import (cash_deployment_plan, historical_backfill,
                                      retune_impact_tracker, universe_sanitation)
    from portfolio_automation.flock_intelligence import data_sources
    return {
        "flock_sector": data_sources.load_ticker_sector(root, "AAA"),
        "backfill_top100": historical_backfill._load_top100_symbols(root),
        "sanitation_top100": universe_sanitation._load_fmp_top100(root),
        "sanitation_sector": universe_sanitation._load_sector(root, "AAA"),
        "retune_sector": retune_impact_tracker._load_ticker_sector(root, "AAA"),
        "cash_sector_map": cash_deployment_plan._sector_map_from_cache(root / "outputs", ["AAA"]),
    }


def test_direct_cache_readers_pre_hold_unchanged(monkeypatch, tmp_path):
    root = _cache_root(tmp_path)
    monkeypatch.chdir(root)
    r = _readers(root)
    assert r["flock_sector"] == "Technology"
    assert r["backfill_top100"] == ["AAA"]
    assert [c["symbol"] for c in r["sanitation_top100"]] == ["AAA"]
    assert r["sanitation_sector"] != "Unknown" and r["retune_sector"] != "Unknown"
    assert r["cash_sector_map"] == {"AAA": "Semiconductors"}


@pytest.mark.parametrize("ts", POST_TIMES)
def test_direct_cache_readers_post_hold_deny_without_opening(monkeypatch, tmp_path, ts):
    root = _cache_root(tmp_path)
    monkeypatch.chdir(root)
    _system_at(monkeypatch, ts)
    opened = []
    real_read_text = Path.read_text

    def spy(self, *a, **k):
        if "fmp_cache" in self.parts:
            opened.append(self)
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", spy)
    r = _readers(root)
    assert r == {"flock_sector": None, "backfill_top100": [], "sanitation_top100": [],
                 "sanitation_sector": "Unknown", "retune_sector": "Unknown",
                 "cash_sector_map": {}}
    assert opened == []
    assert len(list((root / "data" / "fmp_cache").iterdir())) == 2   # not deleted


# ---------------------------------------------------------------------------
# Manual probe bypassing FMPClient
# ---------------------------------------------------------------------------

def _load_probe():
    spec = importlib.util.spec_from_file_location(
        "_probe_fmp_r1", REPO / "scripts" / "probe_fmp_crowd_endpoints.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_probe_refuses_before_secret_and_http(monkeypatch, tmp_path, no_http):
    probe = _load_probe()
    _system_at(monkeypatch, NOV27)

    def no_secret(*a, **k):
        raise AssertionError("secret read under the HOLD")

    monkeypatch.setattr(probe, "get_secret", no_secret)
    assert probe.main(str(tmp_path)) == 3
    status, body, err = probe._make_http_get_status("SYNTHETIC")("/stable/x", {})
    assert status is None and body is None and err.startswith("fmp_cutover_hold:")
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Scheduled / manual shell entrypoints
# ---------------------------------------------------------------------------

def _bash():
    if sys.platform == "win32":
        for p in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
            if Path(p).exists():
                return p
        return None
    return shutil.which("bash")


BASH = _bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="Git/POSIX bash not available")

ENTRYPOINTS = {
    "run_sims_daily.sh": "sims_daily",
    "rerun_today_safe.sh": "rerun_today_safe",
    "run_daily.sh": "daily",
    "run_daily_safe.sh": "daily_safe",
    "run_daily_sandbox_safe.sh": "daily_sandbox_safe",
    "run_weekly_safe.sh": "weekly_safe",
    "discovery_pulse.sh": "discovery_pulse",
    "run_weekly_etf_bundles.sh": "weekly_etf_bundles",
    "historical_backfill.sh": "historical_backfill",
    "pattern_loop_recheck.sh": "pattern_loop_recheck",
    "pattern_loop_reconstruct.sh": "pattern_loop_reconstruct",
    "server_run_pipeline.sh": "server_run_pipeline",
    "run_monthly_universe_refresh.sh": "monthly_universe_refresh",
}
EPOCH = {"just_before": fc.FMP_CUTOVER_HOLD_EPOCH - 1, "hold": fc.FMP_CUTOVER_HOLD_EPOCH,
         "nov27": int(NOV27.timestamp()), "jan27": int(JAN27.timestamp())}


def _run(args, env_extra, cwd):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("FMP_", "BASH_ENV", "REPO_ROOT"))}
    env.update(env_extra)
    return subprocess.run([BASH, *args], capture_output=True, text=True,
                          env=env, cwd=cwd, timeout=60)


def _fake_release(tmp_path, script, with_guard=True):
    """A synthetic release dir: the script (+ guard), a tripwire main.py and a
    tripwire .env. The checkout root is a DIFFERENT dir (root/release mismatch)."""
    rel = tmp_path / "release"
    (rel / "scripts").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / script, rel / "scripts" / script)
    if with_guard:
        shutil.copy(REPO / "scripts" / "fmp_cutover_guard.sh", rel / "scripts" / "fmp_cutover_guard.sh")
    root = tmp_path / "root"
    root.mkdir()
    marker = (tmp_path / "SIDE_EFFECT").as_posix()
    for d in (rel, root):
        (d / "main.py").write_text("raise SystemExit('main.py must not run')\n")
        (d / "requirements.txt").write_text("")
        (d / ".env").write_text(f"touch '{marker}'\nFMP_API_KEY=should_not_load\n")
    return rel, root


@needs_bash
@pytest.mark.parametrize("script", sorted(ENTRYPOINTS))
@pytest.mark.parametrize("when", ["hold", "nov27", "jan27"])
def test_entrypoint_holds_first_with_exit_3(tmp_path, fmp_bash_clock, script, when):
    # FMP4-R2: the TRUSTED clock is pinned via a temp release copy (no BASH_ENV).
    rel, root = _fake_release(tmp_path, script, with_guard=False)
    pinned = fmp_bash_clock([], EPOCH[when])
    shutil.copy(pinned / "scripts" / "fmp_cutover_guard.sh", rel / "scripts" / "fmp_cutover_guard.sh")
    env = {"REPO_ROOT": root.as_posix(), "PY": "/nonexistent/python",
           "FMP_CUTOVER_GUARD_NOW_EPOCH": "0"}
    r = _run([(rel / "scripts" / script).as_posix()], env, cwd=root)
    out = r.stdout + r.stderr
    assert r.returncode == 3, out
    assert f"FMP_CUTOVER_HOLD entrypoint={ENTRYPOINTS[script]}" in out or \
        f"FMP_CUTOVER_HOLD {ENTRYPOINTS[script]}" in out, out
    assert "reason=post_cutover_rights_unresolved" in out, out   # time, not distrust
    assert not (tmp_path / "SIDE_EFFECT").exists()             # no .env / creds
    if script != "run_daily_safe.sh":   # FMP4-R2: monthly no longer logs first
        assert not (root / "logs").exists() and not (rel / "logs").exists()


@needs_bash
@pytest.mark.parametrize("script", sorted(ENTRYPOINTS))
def test_entrypoint_missing_guard_fails_closed(tmp_path, script):
    # FMP4-R2: every guarded launcher maps a missing helper to HOLD exit 3 (not 1).
    rel, root = _fake_release(tmp_path, script, with_guard=False)
    r = _run([(rel / "scripts" / script).as_posix()],
             {"REPO_ROOT": root.as_posix(), "PY": "/nonexistent/python"}, cwd=root)
    out = r.stdout + r.stderr
    assert r.returncode == 3, out
    assert "reason=guard_missing" in out, out
    assert not (tmp_path / "SIDE_EFFECT").exists()
    if script != "run_daily_safe.sh":
        assert not (root / "logs").exists() and not (rel / "logs").exists()


@needs_bash
@pytest.mark.parametrize("when,rc", [("just_before", 0), ("hold", 3), ("jan27", 3)])
def test_bash_guard_boundary_with_pinned_system_clock(fmp_bash_clock, when, rc):
    rel = fmp_bash_clock([], EPOCH[when])
    r = _run([(rel / "scripts" / "fmp_cutover_guard.sh").as_posix(), "probe"], {}, cwd=REPO)
    assert r.returncode == rc, r.stdout + r.stderr


def test_guard_precedes_env_lock_venv_in_every_entrypoint():
    # Logs may precede the guard only where the HOLD line itself is logged.
    logged_first = {"run_daily_safe.sh"}
    for script, name in ENTRYPOINTS.items():
        lines = (REPO / "scripts" / script).read_text(encoding="utf-8").splitlines()
        g = next(i for i, ln in enumerate(lines) if f"fmp_cutover_guard {name}" in ln)
        code_before = [ln for ln in lines[:g] if not ln.lstrip().startswith("#")]
        guard_src = [ln for ln in code_before if "fmp_cutover_guard.sh" in ln]
        assert guard_src and all('BASH_SOURCE[0]' in ln for ln in guard_src), script
        bad = (".env", "flock", ".venv", "python")
        bad += () if script in logged_first else ("mkdir", "main.py")
        offending = [ln for ln in code_before if any(b in ln for b in bad)]
        assert offending == [], (script, offending)
