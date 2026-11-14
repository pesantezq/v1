"""FMP4-R2 — HOLD-safety repair (NSTAR-FMP4-R2-HOLD-SAFETY), synthetic only.

Covers: the remaining direct paid-cache reader (theme engine), read-time
FMP_CUTOVER_HOLD / DATA_UNAVAILABLE status on operator surfaces (tools.status,
GUI v2 Today) that refuse to label an outdated decision_plan.json current, the
hardened bash trusted clock (date function / exported BASH_FUNC / BASH_ENV /
PATH / hash / alias / unavailable / pre-floor), and the monthly launcher's early
exit 3. Deterministic at Nov 25 / Nov 26 / Nov 27 2026. No live FMP network, no
secrets, no real data/cache reads, no broker, no trades; every fixture lives
under ``tmp_path`` and every HTTP seam is a tripwire.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from portfolio_automation.data_budget import fmp_cutover as fc

REPO = Path(__file__).resolve().parents[1]
UTC = timezone.utc
NOV25 = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
NOV26 = fc.FMP_CUTOVER_HOLD_UTC                       # 2026-11-26T00:00Z
NOV27 = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)
POST = [NOV26, NOV27]
EPOCH = {"nov25": int(NOV25.timestamp()), "nov26": fc.FMP_CUTOVER_HOLD_EPOCH,
         "nov27": int(NOV27.timestamp())}
PLAN_TS = "2026-11-25T14:05:00+00:00"                 # last pre-HOLD plan


def _system_at(monkeypatch, ts):
    monkeypatch.setattr(fc, "_system_clock", lambda: ts)


@pytest.fixture
def no_net(monkeypatch):
    import socket
    import urllib.request

    def boom(*a, **k):
        raise AssertionError("outbound network attempted")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)


# ---------------------------------------------------------------------------
# Pure read-time helper
# ---------------------------------------------------------------------------

def test_latest_artifact_status_none_pre_hold(monkeypatch):
    _system_at(monkeypatch, NOV25)
    assert fc.latest_artifact_hold_status(PLAN_TS) is None
    assert fc.latest_artifact_hold_status(None) is None


@pytest.mark.parametrize("ts", POST)
@pytest.mark.parametrize("gen,cls", [
    (PLAN_TS, "generated_before_hold"),
    ("2026-11-26T09:00:00Z", "generated_during_hold"),
    ("2026-11-25T14:05:00", "generated_at_unknown"),      # naive -> unknown
    ("garbage", "generated_at_unknown"),
    (None, "generated_at_unknown"),
])
def test_latest_artifact_never_current_under_hold(monkeypatch, ts, gen, cls):
    _system_at(monkeypatch, ts)
    s = fc.latest_artifact_hold_status(gen)
    assert s["status"] == "FMP_CUTOVER_HOLD" and s["data_mode"] == "DATA_UNAVAILABLE"
    assert s["is_current"] is False and s["artifact_age_class"] == cls
    assert s["reason"].startswith("fmp_cutover_hold:")


def test_latest_artifact_status_broken_clock_is_hold(monkeypatch):
    def broken():
        raise OSError("rtc")
    monkeypatch.setattr(fc, "_system_clock", broken)
    assert fc.latest_artifact_hold_status(PLAN_TS)["is_current"] is False


# ---------------------------------------------------------------------------
# Theme engine — remaining direct paid-cache reader
# ---------------------------------------------------------------------------

def _theme_root(tmp_path):
    c = tmp_path / "data" / "fmp_cache"
    c.mkdir(parents=True)
    (c / "sp500_constituents.json").write_text(
        json.dumps({"data": [{"symbol": "BBB"}, {"symbol": "AAA"}]}), encoding="utf-8")
    return tmp_path


def test_theme_sp500_pre_hold_unchanged(monkeypatch, tmp_path, no_net):
    from theme_engine.__main__ import _load_sp500_from_cache
    root = _theme_root(tmp_path)
    _system_at(monkeypatch, NOV25)
    status: dict = {}
    assert _load_sp500_from_cache(str(root), status=status) == ["AAA", "BBB"]
    assert status == {}
    assert _load_sp500_from_cache(str(root)) == ["AAA", "BBB"]   # old signature


@pytest.mark.parametrize("ts", POST)
def test_theme_sp500_held_without_opening(monkeypatch, tmp_path, ts, no_net):
    from theme_engine.__main__ import _load_sp500_from_cache
    root = _theme_root(tmp_path)
    _system_at(monkeypatch, ts)
    touched = []
    real_read, real_exists = Path.read_text, Path.exists

    def spy_read(self, *a, **k):
        if "fmp_cache" in self.parts:
            touched.append(("read", self))
        return real_read(self, *a, **k)

    def spy_exists(self, *a, **k):
        if "fmp_cache" in self.parts:
            touched.append(("exists", self))
        return real_exists(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", spy_read)
    monkeypatch.setattr(Path, "exists", spy_exists)
    status: dict = {}
    assert _load_sp500_from_cache(str(root), status=status) == []
    assert touched == []
    assert status["data_mode"] == "DATA_UNAVAILABLE"
    assert status["reason"].startswith("fmp_cutover_hold:")
    monkeypatch.undo()
    assert (root / "data" / "fmp_cache" / "sp500_constituents.json").exists()  # kept


# ---------------------------------------------------------------------------
# Stale latest artifacts on operator surfaces
# ---------------------------------------------------------------------------

def _latest_repo(tmp_path):
    root = tmp_path / "stockbot"
    latest = root / "outputs" / "latest"
    latest.mkdir(parents=True)
    (root / "main.py").write_text("")
    (latest / "decision_plan.json").write_text(json.dumps(
        {"generated_at": PLAN_TS, "decisions": [{"symbol": "AAA", "action": "HOLD"}]}),
        encoding="utf-8")
    (latest / "pipeline_run_status.json").write_text(json.dumps(
        {"generated_at": PLAN_TS, "success": True, "exit_code": 0, "run_id": "r1"}),
        encoding="utf-8")
    (latest / "daily_run_status.json").write_text(json.dumps(
        {"generated_at": PLAN_TS, "overall_status": "ok"}), encoding="utf-8")
    return root


def _tree_digest(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_status_pre_hold_has_no_hold_check(monkeypatch, tmp_path, no_net):
    from tools.status import collect_status
    root = _latest_repo(tmp_path)
    _system_at(monkeypatch, NOV25)
    report = collect_status(root)
    assert "fmp_cutover_hold" not in [c.name for c in report.checks]


@pytest.mark.parametrize("ts", POST)
def test_status_refuses_outdated_plan_as_current(monkeypatch, tmp_path, ts, no_net):
    from tools.status import collect_status, render_text
    root = _latest_repo(tmp_path)
    before = _tree_digest(root)
    _system_at(monkeypatch, ts)
    report = collect_status(root)
    hold = report.checks[0]
    assert hold.name == "fmp_cutover_hold" and hold.severity == "FAIL"
    assert "FMP_CUTOVER_HOLD" in hold.message and "DATA_UNAVAILABLE" in hold.message
    assert "NOT current" in hold.message and "unknown" in hold.message
    assert hold.details["is_current"] is False
    assert hold.details["artifact_age_class"] == "generated_at_unknown"
    assert report.overall_severity == "FAIL"
    assert report.to_dict()["no_trade"] is True
    assert "FMP_CUTOVER_HOLD" in render_text(report)
    assert _tree_digest(root) == before                      # read-only


@pytest.mark.parametrize("ts", POST)
def test_status_hold_without_plan(monkeypatch, tmp_path, ts, no_net):
    from tools.status import collect_status
    root = _latest_repo(tmp_path)
    (root / "outputs" / "latest" / "decision_plan.json").unlink()
    _system_at(monkeypatch, ts)
    hold = collect_status(root).checks[0]
    assert hold.severity == "FAIL" and hold.details["decision_plan_present"] is False


def test_status_hold_probe_error_fails_closed(monkeypatch, tmp_path):
    import tools.status as st
    root = _latest_repo(tmp_path)

    def boom(_root):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(st, "probe_fmp_cutover_hold", boom)
    hold = st.collect_status(root).checks[0]
    assert hold.name == "fmp_cutover_hold" and hold.severity == "FAIL"


def test_gui_today_pre_hold_unchanged(monkeypatch, tmp_path, no_net):
    from gui_v2.data.dash_today import collect_today_view
    root = _latest_repo(tmp_path)
    _system_at(monkeypatch, NOV25)
    view = collect_today_view(root)
    dc = next(c for c in view["cards"] if c["title"] == "Decision core")
    assert dc["label"] == "present" and dc["status"] == "ok"
    assert "fmp_cutover_hold" not in view


@pytest.mark.parametrize("ts", POST)
def test_gui_today_refuses_outdated_plan_as_current(monkeypatch, tmp_path, ts, no_net):
    from gui_v2.data.dash_today import collect_today_view
    root = _latest_repo(tmp_path)
    before = _tree_digest(root)
    _system_at(monkeypatch, ts)
    view = collect_today_view(root)
    dc = next(c for c in view["cards"] if c["title"] == "Decision core")
    assert dc["label"] == "FMP_CUTOVER_HOLD" and dc["status"] == "red"
    assert "DATA_UNAVAILABLE" in dc["summary"] and "NOT current" in dc["summary"]
    assert dc["label"] != "present"
    assert view["fmp_cutover_hold"]["is_current"] is False
    assert view["observe_only"] is True
    assert _tree_digest(root) == before                      # read-only


@pytest.mark.parametrize("ts", POST)
def test_gui_today_route_shows_hold(monkeypatch, tmp_path, ts, no_net):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import gui_v2.app as app_mod
    root = _latest_repo(tmp_path)
    monkeypatch.setattr(app_mod, "REPO_ROOT", root)
    _system_at(monkeypatch, ts)
    r = TestClient(app_mod.app).get("/dashboard/today")
    assert r.status_code == 200
    assert "FMP_CUTOVER_HOLD" in r.text or "Fmp Cutover Hold" in r.text


# ---------------------------------------------------------------------------
# Bash trusted clock
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


def _run(args, env_extra=None, cwd=None):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("FMP_", "BASH_ENV", "BASH_FUNC_", "REPO_ROOT"))}
    env.update(env_extra or {})
    return subprocess.run([BASH, *args], capture_output=True, text=True,
                          env=env, cwd=cwd or REPO, timeout=60)


def _guard(rel):
    return (rel / "scripts" / "fmp_cutover_guard.sh").as_posix()


def _bash_env(tmp_path, body, name="bash_env.sh"):
    f = tmp_path / name
    f.write_text(body, encoding="utf-8", newline="\n")
    return {"BASH_ENV": f.as_posix()}


@needs_bash
@pytest.mark.parametrize("when,rc", [("nov25", 0), ("nov26", 3), ("nov27", 3)])
def test_trusted_clock_boundaries(fmp_bash_clock, when, rc):
    r = _run([_guard(fmp_bash_clock([], EPOCH[when])), "probe"])
    assert r.returncode == rc, r.stdout + r.stderr


@needs_bash
@pytest.mark.parametrize("claim", ["nov25"])
def test_bash_env_date_function_cannot_admit(fmp_bash_clock, fmp_bash_date, claim):
    # Trusted clock says Nov 27; a BASH_ENV `date` function claims Nov 25.
    rel = fmp_bash_clock([], EPOCH["nov27"])
    r = _run([_guard(rel), "probe"], fmp_bash_date(EPOCH[claim]))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "reason=clock_function_shadows_date" in r.stderr


@needs_bash
def test_shadowing_date_function_holds_even_pre_cutover(fmp_bash_clock, fmp_bash_date):
    # Fail closed: an untrusted clock is a HOLD whatever the real time is.
    rel = fmp_bash_clock([], EPOCH["nov25"])
    r = _run([_guard(rel), "probe"], fmp_bash_date(EPOCH["nov25"]))
    assert r.returncode == 3 and "clock_function_shadows_date" in r.stderr


@needs_bash
def test_exported_bash_func_date_is_detected(fmp_bash_clock):
    # Pre-HOLD trusted clock, so only DETECTION of the imported function can
    # produce the HOLD (a post-HOLD clock would make this test vacuous).
    env = {"BASH_FUNC_date%%": f"() {{  echo {EPOCH['nov25']}\n}}"}
    try:
        probe = _run(["-c", "declare -F date"], env)
    except (ValueError, OSError) as exc:                 # platform env-name limits
        pytest.skip(f"cannot export BASH_FUNC_ env on this platform: {exc}")
    if probe.returncode != 0:
        pytest.skip("this bash/platform did not import the exported function")
    rel = fmp_bash_clock([], EPOCH["nov25"])
    r = _run([_guard(rel), "probe"], env)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "reason=clock_function_shadows_date" in r.stderr


@needs_bash
@pytest.mark.parametrize("body,reason", [
    ("command() { echo {e}; }\n", "clock_function_shadows_command"),
    ("builtin() { return 1; }\n", "clock_function_shadows_builtin"),
    ("function /usr/bin/date {{ echo {e}; }}\n", "clock_function_shadows__usr_bin_date"),
])
def test_bash_env_shadowing_builtins_holds(tmp_path, fmp_bash_clock, body, reason):
    rel = fmp_bash_clock([], EPOCH["nov27"])
    r = _run([_guard(rel), "probe"],
             _bash_env(tmp_path, body.replace("{e}", str(EPOCH["nov25"]))
                       .replace("{{", "{").replace("}}", "}")))
    assert r.returncode == 3, r.stdout + r.stderr
    assert f"reason={reason}" in r.stderr, r.stderr


@needs_bash
def test_path_hash_and_alias_cannot_replace_trusted_clock(tmp_path, fmp_bash_clock):
    fake = tmp_path / "evilbin" / "date"
    fake.parent.mkdir()
    fake.write_text(f"#!/bin/sh\necho {EPOCH['nov25']}\n", encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    rel = fmp_bash_clock([], EPOCH["nov27"])
    env = _bash_env(tmp_path, (
        f"hash -p '{fake.as_posix()}' date\n"
        "shopt -s expand_aliases\n"
        f"alias date='echo {EPOCH['nov25']}'\n"))
    env["PATH"] = fake.parent.as_posix() + os.pathsep + os.environ.get("PATH", "")
    r = _run([_guard(rel), "probe"], env)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "reason=post_cutover_rights_unresolved" in r.stderr


def _guard_with_bins(tmp_path, bins: str) -> str:
    from tests.conftest import FMP_GUARD_TRUSTED_BINS_LINE
    text = (REPO / "scripts" / "fmp_cutover_guard.sh").read_text(encoding="utf-8")
    out = tmp_path / "guard_variant.sh"
    out.write_text(text.replace(FMP_GUARD_TRUSTED_BINS_LINE, f'local bins="{bins}"'),
                   encoding="utf-8", newline="\n")
    return out.as_posix()


def _fake_date(tmp_path, output: str) -> str:
    f = tmp_path / "odd" / "date"
    f.parent.mkdir(exist_ok=True)
    f.write_text(f"#!/bin/sh\necho '{output}'\n", encoding="utf-8", newline="\n")
    f.chmod(0o755)
    return f.as_posix()


@needs_bash
def test_date_unavailable_fails_closed(tmp_path):
    r = _run([_guard_with_bins(tmp_path, "/nonexistent/date /also/missing/date"), "probe"])
    assert r.returncode == 3 and "reason=clock_unreadable" in r.stderr


@needs_bash
@pytest.mark.parametrize("output,reason", [("not-a-number", "clock_unreadable"),
                                           ("0", "clock_before_floor"),
                                           ("1700000000", "clock_before_floor")])
def test_garbage_or_rewound_clock_fails_closed(tmp_path, output, reason):
    r = _run([_guard_with_bins(tmp_path, _fake_date(tmp_path, output)), "probe"])
    assert r.returncode == 3 and f"reason={reason}" in r.stderr, r.stderr


@needs_bash
def test_forward_only_injection_preserved(fmp_bash_clock):
    pre = fmp_bash_clock([], EPOCH["nov25"])
    r = _run([_guard(pre), "probe"], {"FMP_CUTOVER_GUARD_NOW_EPOCH": str(EPOCH["nov27"])})
    assert r.returncode == 3                                   # can force a HOLD
    post = fmp_bash_clock([], EPOCH["nov27"])
    r = _run([_guard(post), "probe"], {"FMP_CUTOVER_GUARD_NOW_EPOCH": str(EPOCH["nov25"])})
    assert r.returncode == 3                                   # never lift one


def test_guard_has_no_unqualified_date_call():
    text = (REPO / "scripts" / "fmp_cutover_guard.sh").read_text(encoding="utf-8")
    code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    assert not [ln for ln in code if re.search(r"\$\(\s*date\b|^\s*date\b", ln)]
    assert any('\\command "$bin" -u +%s' in ln for ln in code)


# ---------------------------------------------------------------------------
# Monthly launcher: early HOLD rc 3 before any write / secret
# ---------------------------------------------------------------------------

def _monthly_root(tmp_path, rel):
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    marker = (tmp_path / "SIDE_EFFECT").as_posix()
    (root / ".env").write_text(f"touch '{marker}'\nFMP_API_KEY=should_not_load\n")
    (root / "main.py").write_text("raise SystemExit('main.py must not run')\n")
    return root


@needs_bash
@pytest.mark.parametrize("when", ["nov26", "nov27"])
def test_monthly_holds_before_log_dir_and_env(tmp_path, fmp_bash_clock, when):
    rel = fmp_bash_clock(["run_monthly_universe_refresh.sh"], EPOCH[when])
    root = _monthly_root(tmp_path, rel)
    r = _run([(rel / "scripts" / "run_monthly_universe_refresh.sh").as_posix()],
             {"REPO_ROOT": root.as_posix(), "PY": "/nonexistent/python"}, cwd=root)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "FMP_CUTOVER_HOLD entrypoint=monthly_universe_refresh" in r.stderr
    assert not (root / "logs").exists() and not (tmp_path / "SIDE_EFFECT").exists()


@needs_bash
def test_monthly_missing_guard_is_rc3_not_rc1(tmp_path):
    rel = tmp_path / "release"
    (rel / "scripts").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "run_monthly_universe_refresh.sh", rel / "scripts")
    root = _monthly_root(tmp_path, rel)
    r = _run([(rel / "scripts" / "run_monthly_universe_refresh.sh").as_posix()],
             {"REPO_ROOT": root.as_posix()}, cwd=root)
    assert r.returncode == 3 and "reason=guard_missing" in r.stderr, r.stderr
    assert not (root / "logs").exists() and not (tmp_path / "SIDE_EFFECT").exists()


def test_monthly_guard_is_first_action():
    lines = (REPO / "scripts" / "run_monthly_universe_refresh.sh").read_text(
        encoding="utf-8").splitlines()
    g = next(i for i, ln in enumerate(lines) if "fmp_cutover_guard monthly_universe_refresh" in ln)
    code_before = [ln for ln in lines[:g] if ln.strip() and not ln.lstrip().startswith("#")]
    assert not [ln for ln in code_before
                if any(b in ln for b in ("mkdir", "LOG_", ".env", "flock", "REPO_ROOT"))]
