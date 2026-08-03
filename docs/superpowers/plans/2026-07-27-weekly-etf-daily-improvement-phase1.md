# Weekly ETF Daily Continuous-Improvement — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a non-freezing *daily* observe lane to the weekly ETF bundle subsystem — with a market-timezone session resolver and a strict write-allowlist — so the learning artifacts refresh daily without touching any weekly artifact or violating prediction immutability.

**Architecture:** A new `--daily-observe` run mode reuses the existing mature→evaluate→Strat-Lab steps with `do_freeze=False`, but routes all writes into a separate `outputs/weekly_etf_bundles/daily/` subtree. A new `trading_session` helper resolves the as-of date to the latest *completed* NYSE session (`America/New_York`). A double-gated, lock-aware `run_aux_stage` line adds it to the daily pipeline. Daily-tool-analysis gains read/triage coverage.

**Tech Stack:** Python 3.11 (`zoneinfo`), pytest, bash (`run_daily_safe.sh`, `flock`). Spec: `docs/superpowers/specs/2026-07-27-weekly-etf-daily-improvement-design.md` (`e4284893`).

> **IMPLEMENTATION STATUS — NOT IMPLEMENTED (annotated 2026-08-03, doc-audit).**
> This is a historical plan document; it records the intent as of 2026-07-27 and is
> preserved unchanged below. As of 2026-08-03 **none of it has been executed**:
> 0 of its 30 steps are checked, none of the three proposed modules exist under any
> name, `run.py` has no `--daily-observe` mode, `WEEKLY_ETF_BUNDLES_DAILY_ENABLED`
> appears nowhere in the repo, `scripts/run_weekly_etf_daily.sh` does not exist,
> and no `tests/test_weekly_etf_daily_*.py` / `tests/test_weekly_etf_trading_session.py`
> exists (the only weekly-ETF tests are `tests/test_weekly_etf_bundles_phase1..8.py`
> + `..._review_fixes.py`, all from the pre-plan Phase 1–8 build). The
> `outputs/weekly_etf_bundles/daily/` subtree was never created.
>
> Per-file status is annotated inline at each `Create:` line. The three proposed
> files below are dead references *because the plan was never run* — they are not
> stale renames of shipped code. One related capability did land elsewhere
> afterwards (see Task 1's annotation): the repo-level
> `portfolio_automation/market_session.py` (Reliability Program D2, `2de39107`,
> 2026-07-28, documented in `docs/market_session.md`) answers the
> latest-completed-NYSE-session question at a *different layer with a different
> signature*. A future implementer of this plan should evaluate reusing it instead
> of building a bundle-local resolver.

## Global Constraints

- Observe-only; `feeds_decision_engine=false` throughout; no `decision_engine.py`, allocation, or score-semantics changes.
- All writes stay in the `WEEKLY_ETF_BUNDLES` namespace (`outputs/weekly_etf_bundles/`); the daily lane writes ONLY under `outputs/weekly_etf_bundles/daily/`.
- Additive + backward compatible; the daily lane ships INERT (`WEEKLY_ETF_BUNDLES_DAILY_ENABLED` default `0`).
- Every new module has a paired test in `tests/`; run targeted tests before the suite.
- Timezone: resolve the as-of trading session against the latest *completed* session using canonical `America/New_York` — never UTC-naive `now()`.
- Interpreter: `.venv/bin/python`. Compile touched files with `python -m py_compile`.
- The daily lane must NEVER write/overwrite weekly `latest.json`, the weekly digest (MD/HTML), weekly `health.json`, any email artifact/dedup state, or any `predictions/**` file.

---

### Task 1: Trading-session resolver (timezone guardrail — spec §6)

**Files:**
- Create: `portfolio_automation/weekly_etf_bundles/trading_session.py` — **NOT IMPLEMENTED (2026-08-03).** No such file, and no equivalent under another name inside `weekly_etf_bundles/`. Closest as-built capability, built the next day for a *different* program: `portfolio_automation/market_session.py` (`latest_completed_session(ts: datetime) -> date`). It is **not a rename of this file** — it takes no `panel_dates` argument, returns a calendar `date` rather than a panel-date string, and deliberately avoids `zoneinfo` (fixed conservative 21:00 UTC close boundary) instead of using canonical `America/New_York`. Reusing it here would require reconciling those two differences.
- Test: `tests/test_weekly_etf_trading_session.py` — **NOT IMPLEMENTED.** (`tests/test_market_session.py` covers the repo-level helper instead.)

**Interfaces:**
- Produces: `latest_completed_session(now_utc: datetime, panel_dates: list[str], *, close_hour_et: int = 16) -> str | None` — returns the YYYY-MM-DD of the most recent panel date whose ET regular-session close (16:00 ET) is at or before `now_utc`. Returns the last panel date strictly before "today-ET" when today's session has not yet closed (premarket/intraday), and `None` if `panel_dates` is empty.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_weekly_etf_trading_session.py
from datetime import datetime, timezone
from portfolio_automation.weekly_etf_bundles.trading_session import latest_completed_session

DATES = ["2026-07-23", "2026-07-24", "2026-07-27"]  # Thu, Fri, Mon (26/27 weekend gap)

def _utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)

def test_postclose_same_day_uses_today():
    # Mon 2026-07-27 20:30 UTC = 16:30 ET → session complete → 07-27
    assert latest_completed_session(_utc(2026, 7, 27, 20, 30), DATES) == "2026-07-27"

def test_premarket_uses_prior_session():
    # Mon 2026-07-27 12:00 UTC = 08:00 ET → today's close not reached → prior = 07-24
    assert latest_completed_session(_utc(2026, 7, 27, 12, 0), DATES) == "2026-07-24"

def test_weekend_uses_friday():
    # Sat 2026-07-25 18:00 UTC → last completed = Fri 07-24
    assert latest_completed_session(_utc(2026, 7, 25, 18, 0), DATES) == "2026-07-24"

def test_utc_midnight_boundary_still_prior_session():
    # Tue 2026-07-28 01:00 UTC = Mon 21:00 ET → Mon session complete → 07-27
    assert latest_completed_session(_utc(2026, 7, 28, 1, 0), DATES) == "2026-07-27"

def test_holiday_gap_falls_back_to_last_panel_date():
    # A date with no panel entry (holiday) resolves to the last panel date <= cutoff
    assert latest_completed_session(_utc(2026, 7, 27, 12, 0), ["2026-07-23", "2026-07-24"]) == "2026-07-24"

def test_empty_panel_returns_none():
    assert latest_completed_session(_utc(2026, 7, 27, 20, 30), []) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_weekly_etf_trading_session.py -v`
Expected: FAIL — `ModuleNotFoundError: portfolio_automation.weekly_etf_bundles.trading_session`.

- [ ] **Step 3: Write minimal implementation**

```python
# portfolio_automation/weekly_etf_bundles/trading_session.py
"""Resolve the latest COMPLETED trading session in canonical market time.

Observe-only helper. `market_data_date` upstream snaps to a panel date; this
narrows the as-of upper bound to the most recent session whose regular close
(16:00 America/New_York) is at or before `now`. Guards premarket, post-close,
weekend, holiday, DST, and UTC-boundary cases (the incomplete current bar is
never treated as complete).
"""
from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


def latest_completed_session(
    now_utc: datetime,
    panel_dates: list[str],
    *,
    close_hour_et: int = 16,
) -> str | None:
    """Most recent panel date whose 16:00-ET close is <= now. See module docstring."""
    if not panel_dates:
        return None
    now_et = now_utc.astimezone(_ET)
    today_et = now_et.date()
    close_today = datetime.combine(today_et, time(close_hour_et, 0), tzinfo=_ET)
    # If today's regular session has not closed yet, exclude today.
    cutoff = today_et if now_et >= close_today else _prev_calendar_day(today_et)
    cutoff_iso = cutoff.isoformat()
    completed = [d for d in sorted(panel_dates) if d <= cutoff_iso]
    return completed[-1] if completed else None


def _prev_calendar_day(d):
    from datetime import timedelta
    return d - timedelta(days=1)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_weekly_etf_trading_session.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/weekly_etf_bundles/trading_session.py tests/test_weekly_etf_trading_session.py
git commit -m "feat(weekly-etf): market-tz latest-completed-session resolver (phase1 timezone guardrail)"
```

---

### Task 2: Use the session resolver as the default as-of in the run path

**Files:**
- Modify: `portfolio_automation/weekly_etf_bundles/run.py` (the `run(...)` entry, where `as_of` defaults)
- Test: `tests/test_weekly_etf_run_asof.py`

**Interfaces:**
- Consumes: `trading_session.latest_completed_session` (Task 1).
- Produces: when `as_of is None`, `run(...)` derives the as-of upper bound from `latest_completed_session(now_utc, panel.dates)` before analysis, so a UTC-cron firing premarket cannot select an unclosed session.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_weekly_etf_run_asof.py
from portfolio_automation.weekly_etf_bundles import run as R

def test_default_asof_uses_session_resolver(monkeypatch):
    called = {}
    def fake_latest(now_utc, dates, **kw):
        called["dates"] = list(dates)
        return "2026-07-24"
    monkeypatch.setattr(R, "latest_completed_session", fake_latest, raising=False)
    # resolve_as_of is the small seam under test (see impl)
    got = R.resolve_as_of(None, panel_dates=["2026-07-23", "2026-07-24", "2026-07-27"])
    assert got == "2026-07-24"
    assert called["dates"]  # resolver consulted

def test_explicit_asof_is_respected():
    assert R.resolve_as_of("2026-07-20", panel_dates=["2026-07-20", "2026-07-24"]) == "2026-07-20"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_weekly_etf_run_asof.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'resolve_as_of'`.

- [ ] **Step 3: Write minimal implementation**

Add the import and a small seam to `run.py`, and call it where `as_of` is resolved:

```python
# top of run.py, with the other weekly_etf_bundles imports
from portfolio_automation.weekly_etf_bundles.trading_session import latest_completed_session
from datetime import datetime, timezone

def resolve_as_of(as_of: str | None, *, panel_dates: list[str]) -> str | None:
    """Explicit as_of wins; otherwise snap to the latest COMPLETED ET session."""
    if as_of:
        return as_of
    return latest_completed_session(datetime.now(timezone.utc), panel_dates)
```

Then, in `run(...)`, after the price panel is built and before analysis, replace the raw `as_of` use with:

```python
    as_of = resolve_as_of(as_of, panel_dates=list(getattr(panel, "dates", []) or []))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_weekly_etf_run_asof.py -v`
Then regression: `.venv/bin/python -m pytest tests/test_weekly_etf_bundles_phase1.py tests/test_weekly_etf_bundles_phase2.py -q`
Expected: PASS; no regressions in the phase1/phase2 suites.

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/weekly_etf_bundles/run.py tests/test_weekly_etf_run_asof.py
git commit -m "feat(weekly-etf): resolve default as-of via latest-completed ET session"
```

---

### Task 3: `--daily-observe` mode with daily-subtree routing + write-allowlist (spec §4, §9)

**Files:**
- Modify: `portfolio_automation/weekly_etf_bundles/run.py` (add `--daily-observe` arg; mode → `do_freeze=False`, `do_mature=True`, `do_evaluate=True`, `do_strat=True`; route writes to the daily subtree)
- Create: `portfolio_automation/weekly_etf_bundles/daily_paths.py` (the write-allowlist + daily subtree path helper) — **NOT IMPLEMENTED (2026-08-03).** No such file and no equivalent under another name; `daily_write_path` / `is_allowed` exist nowhere in the repo, and `run.py` has no `--daily-observe` argument. The `outputs/weekly_etf_bundles/daily/` subtree does not exist.
- Test: `tests/test_weekly_etf_daily_lane.py` — **NOT IMPLEMENTED.**

**Interfaces:**
- Consumes: existing `mature_all_outcomes`, `build_scorecard`, `build_calibration`, `build_attribution`, `build_health`, `SL.run_strat_lab_comparison`.
- Produces: `daily_paths.daily_write_path(root, filename) -> Path` (returns `outputs/weekly_etf_bundles/daily/<filename>`) and `daily_paths.is_allowed(filename) -> bool`. A daily-observe run writes ONLY `daily/{scorecard,calibration,attribution,health,evidence}.json`; it never freezes and never writes weekly paths.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_weekly_etf_daily_lane.py
import json, hashlib
from pathlib import Path
from portfolio_automation.weekly_etf_bundles import run as R

def _hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()

def test_daily_observe_writes_only_daily_subtree_and_never_freezes(tmp_path, monkeypatch):
    root = tmp_path
    # Arrange a frozen weekly prediction + a weekly latest.json + weekly health.
    (root / "outputs/weekly_etf_bundles/predictions").mkdir(parents=True)
    pred = root / "outputs/weekly_etf_bundles/predictions/2026-07-27.json"
    pred.write_text(json.dumps({"market_data_date": "2026-07-27", "content_hash": "abc", "predictions": []}))
    weekly_latest = root / "outputs/weekly_etf_bundles/latest.json"
    weekly_latest.write_text(json.dumps({"weekly": True}))
    weekly_health = root / "outputs/weekly_etf_bundles/health.json"
    weekly_health.write_text(json.dumps({"status": "AMBER"}))
    pred_hash, latest_hash, health_hash = _hash(pred), _hash(weekly_latest), _hash(weekly_health)

    R.run(root=str(root), as_of="2026-07-27", mode="daily-observe", write_files=True)

    # Weekly artifacts untouched (byte-identical).
    assert _hash(pred) == pred_hash
    assert _hash(weekly_latest) == latest_hash
    assert _hash(weekly_health) == health_hash
    # Daily subtree written.
    assert (root / "outputs/weekly_etf_bundles/daily/health.json").exists()
    assert not (root / "outputs/weekly_etf_bundles/daily").joinpath("..", "predictions", "2026-07-28.json").exists()

def test_daily_write_allowlist_rejects_weekly_names():
    from portfolio_automation.weekly_etf_bundles.daily_paths import is_allowed
    assert is_allowed("health.json") is True
    assert is_allowed("scorecard.json") is True
    assert is_allowed("latest.json") is False   # weekly file name reused under daily/ is still denied at source
    assert is_allowed("predictions/2026-07-28.json") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_weekly_etf_daily_lane.py -v`
Expected: FAIL — `daily_paths` missing and/or `mode="daily-observe"` unsupported.

- [ ] **Step 3: Write minimal implementation**

```python
# portfolio_automation/weekly_etf_bundles/daily_paths.py
"""Write-allowlist for the non-freezing daily lane (spec §4, §9).

The daily lane may write ONLY these derived artifacts, and only under the
`daily/` subtree. Weekly artifact names and any predictions path are denied at
the source so a coding mistake cannot clobber a weekly file."""
from __future__ import annotations

from pathlib import Path

_DAILY_SUBDIR = "daily"
_ALLOWED = {"scorecard.json", "calibration.json", "attribution.json",
            "health.json", "evidence.json"}


def is_allowed(filename: str) -> bool:
    return filename in _ALLOWED


def daily_write_path(root: str | Path, filename: str) -> Path:
    if not is_allowed(filename):
        raise ValueError(f"daily lane may not write {filename!r} (allowlist: {sorted(_ALLOWED)})")
    return Path(root) / "outputs" / "weekly_etf_bundles" / _DAILY_SUBDIR / filename
```

In `run.py`: add the CLI flag and mode dispatch.

```python
    # in the argparse block
    parser.add_argument("--daily-observe", action="store_true",
                        help="Non-freezing daily lane: mature+evaluate+strat into daily/ subtree")
    # in _resolve_mode(args) — BEFORE the analysis_only branch
    if args.daily_observe:
        return "daily-observe", False, True, False   # (mode, do_send, do_dry, force)
```

Extend the `do_*` derivation and write routing:

```python
    do_freeze = mode in ("full",)                                  # daily-observe NEVER freezes
    do_strat = mode in ("full", "evaluate", "daily-observe")
    do_mature = mode in ("full", "mature-outcomes", "evaluate", "daily-observe")
    do_evaluate = mode in ("full", "evaluate", "daily-observe")
    daily_lane = mode == "daily-observe"
```

Wherever the evaluate step calls `_write(root_path, "scorecard.json", ...)` etc., branch on `daily_lane` to use `daily_paths.daily_write_path`:

```python
    from portfolio_automation.weekly_etf_bundles import daily_paths
    def _emit(name, obj):
        if daily_lane:
            p = daily_paths.daily_write_path(root_path, name); p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(obj, indent=2)); return
        _write(root_path, name, obj, is_json=True)
    # then: _emit("scorecard.json", scorecard) / "calibration.json" / "attribution.json" / "health.json"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_weekly_etf_daily_lane.py -v`
Then: `.venv/bin/python -m pytest tests/test_weekly_etf_bundles_phase1.py -q`
Expected: PASS; no regression.

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/weekly_etf_bundles/daily_paths.py portfolio_automation/weekly_etf_bundles/run.py tests/test_weekly_etf_daily_lane.py
git commit -m "feat(weekly-etf): --daily-observe non-freezing lane with daily-subtree write-allowlist"
```

---

### Task 4: Daily pipeline stage via `run_aux_stage`, double-gated + lock precedence (spec §10)

**Files:**
- Create: `scripts/run_weekly_etf_daily.sh` (the daily wrapper: reads env gates, respects the weekly lock, runs `--daily-observe`)
- Modify: `scripts/run_daily_safe.sh` (add one `run_aux_stage` line after the existing observe-only aux stages)
- Test: `tests/test_weekly_etf_daily_shell.py` (invokes the wrapper as a subprocess)

**Interfaces:**
- Consumes: `WEEKLY_ETF_BUNDLES_ENABLED`, new `WEEKLY_ETF_BUNDLES_DAILY_ENABLED` (default 0); the weekly lock `/var/lock/stockbot-weekly-etf-bundles.lock`.
- Produces: `scripts/run_weekly_etf_daily.sh` exits 0 and no-ops when either gate is off or the weekly lock is held; otherwise runs `python -m ...run --daily-observe`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_weekly_etf_daily_shell.py
import subprocess, os, textwrap
from pathlib import Path

WRAP = Path("scripts/run_weekly_etf_daily.sh")

def _run(env_extra):
    env = dict(os.environ); env.update(env_extra); env["REPO_ROOT"] = os.getcwd()
    return subprocess.run(["bash", str(WRAP)], capture_output=True, text=True, env=env)

def test_noop_when_daily_gate_off():
    r = _run({"WEEKLY_ETF_BUNDLES_ENABLED": "1", "WEEKLY_ETF_BUNDLES_DAILY_ENABLED": "0"})
    assert r.returncode == 0
    assert "disabled" in (r.stdout + r.stderr).lower()

def test_noop_when_master_gate_off():
    r = _run({"WEEKLY_ETF_BUNDLES_ENABLED": "0", "WEEKLY_ETF_BUNDLES_DAILY_ENABLED": "1"})
    assert r.returncode == 0
    assert "disabled" in (r.stdout + r.stderr).lower()

def test_script_parses_clean():
    r = subprocess.run(["bash", "-n", str(WRAP)], capture_output=True, text=True)
    assert r.returncode == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_weekly_etf_daily_shell.py -v`
Expected: FAIL — `scripts/run_weekly_etf_daily.sh` does not exist.

- [ ] **Step 3: Write minimal implementation**

```bash
# scripts/run_weekly_etf_daily.sh
#!/usr/bin/env bash
# Non-freezing DAILY lane for the weekly ETF subsystem (spec §10). Observe-only;
# ships INERT. Double-gated; yields to the weekly freeze via the shared lock.
set -euo pipefail
REPO_ROOT="${REPO_ROOT:-/opt/stockbot}"; cd "$REPO_ROOT"
if [ -f "$REPO_ROOT/.env" ]; then set -a; . "$REPO_ROOT/.env"; set +a; fi

_M="${WEEKLY_ETF_BUNDLES_ENABLED:-0}"; _D="${WEEKLY_ETF_BUNDLES_DAILY_ENABLED:-0}"
_on() { [ "$1" = "1" ] || [ "$1" = "true" ] || [ "$1" = "yes" ]; }
if ! _on "$_M" || ! _on "$_D"; then
  echo "$(date -u +%FT%TZ) weekly_etf_daily: disabled (need WEEKLY_ETF_BUNDLES_ENABLED=1 and WEEKLY_ETF_BUNDLES_DAILY_ENABLED=1) — skipping"; exit 0
fi

# Weekly freeze takes precedence: if the weekly runner holds its lock, skip (do NOT block).
WEEKLY_LOCK="/var/lock/stockbot-weekly-etf-bundles.lock"
if [ -e "$WEEKLY_LOCK" ]; then
  exec 8>"$WEEKLY_LOCK"
  if ! flock -n 8; then echo "$(date -u +%FT%TZ) weekly_etf_daily: weekly runner active — skipping"; exit 0; fi
  flock -u 8
fi
# Own daily lock so two daily runs never overlap.
exec 9>"${REPO_ROOT}/.weekly_etf_daily.lock"
flock -n 9 || { echo "weekly_etf_daily: another daily run holds the lock — skipping"; exit 0; }

[ -d "$REPO_ROOT/.venv" ] && source "$REPO_ROOT/.venv/bin/activate"
python -m portfolio_automation.weekly_etf_bundles.run --root "$REPO_ROOT" --daily-observe \
  || printf 'weekly_etf_daily: non-fatal failure (observe-only)\n'
```

Make it executable and add the daily-pipeline line. In `scripts/run_daily_safe.sh`, after the last observe-only `run_aux_stage` block:

```bash
run_aux_stage "Weekly ETF daily observe lane" \
    bash "${REPO_ROOT}/scripts/run_weekly_etf_daily.sh"
```

```bash
chmod +x scripts/run_weekly_etf_daily.sh
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_weekly_etf_daily_shell.py -v`
Then: `bash -n scripts/run_daily_safe.sh && echo "daily_safe parses"`
Expected: PASS; `run_daily_safe.sh` parses clean.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_weekly_etf_daily.sh scripts/run_daily_safe.sh tests/test_weekly_etf_daily_shell.py
git commit -m "feat(weekly-etf): double-gated daily aux stage with weekly-lock precedence"
```

---

### Task 5: Daily-tool-analysis coverage for the daily lane (Analysis+Health Coverage Requirement)

**Files:**
- Modify: `.claude/commands/daily-tool-analysis.md` (artifacts read + a body-grammar line + dispatch note)
- Create: `portfolio_automation/weekly_etf_bundles/daily_health.py` (`assess_daily_lane(root) -> dict` — the deterministic signal the skill reads) — **NOT IMPLEMENTED (2026-08-03).** No such file; `assess_daily_lane` exists nowhere in the repo. Note this is **not** a rename of the existing `portfolio_automation/weekly_etf_bundles/health.py`: that module predates this plan (added 2026-07-25 in weekly-ETF Phase 7, `5b522470`), covers the **weekly** lane, and is consumed *by* this plan as an existing input (`build_health`, cited in Task 3's "Consumes" list). `daily_health.py` was always a proposed new sibling of it.
- Test: `tests/test_weekly_etf_daily_health.py` — **NOT IMPLEMENTED.**

**Interfaces:**
- Consumes: `outputs/weekly_etf_bundles/daily/health.json`, and the invariant fields in `daily/*.json`.
- Produces: `assess_daily_lane(root) -> {"status": "GREEN|AMBER|RED", "weekly_etf_daily_ran": bool, "content_liveness_ok": bool, "invariant_ok": bool, "reasons": list[str]}`. RED iff an invariant is breached (`feeds_decision_engine` true anywhere in the daily artifacts); AMBER on not-run or `status==ok`+0-tickers content_liveness; else GREEN.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_weekly_etf_daily_health.py
import json
from pathlib import Path
from portfolio_automation.weekly_etf_bundles.daily_health import assess_daily_lane

def _write(root, obj):
    d = root / "outputs/weekly_etf_bundles/daily"; d.mkdir(parents=True, exist_ok=True)
    (d / "health.json").write_text(json.dumps(obj))

def test_green_when_ran_clean(tmp_path):
    _write(tmp_path, {"status": "ok", "tickers_scored": 23, "feeds_decision_engine": False})
    r = assess_daily_lane(str(tmp_path))
    assert r["status"] == "GREEN" and r["weekly_etf_daily_ran"] is True

def test_amber_when_not_run(tmp_path):
    r = assess_daily_lane(str(tmp_path))
    assert r["status"] == "AMBER" and r["weekly_etf_daily_ran"] is False

def test_amber_on_looks_fresh_but_empty(tmp_path):
    _write(tmp_path, {"status": "ok", "tickers_scored": 0, "feeds_decision_engine": False})
    assert assess_daily_lane(str(tmp_path))["status"] == "AMBER"

def test_red_on_invariant_breach(tmp_path):
    _write(tmp_path, {"status": "ok", "tickers_scored": 23, "feeds_decision_engine": True})
    r = assess_daily_lane(str(tmp_path))
    assert r["status"] == "RED" and r["invariant_ok"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_weekly_etf_daily_health.py -v`
Expected: FAIL — `daily_health` module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# portfolio_automation/weekly_etf_bundles/daily_health.py
"""Deterministic health signal for the daily ETF observe lane (never raises)."""
from __future__ import annotations

import json
from pathlib import Path


def assess_daily_lane(root: str | Path) -> dict:
    p = Path(root) / "outputs" / "weekly_etf_bundles" / "daily" / "health.json"
    reasons: list[str] = []
    if not p.exists():
        return {"status": "AMBER", "weekly_etf_daily_ran": False,
                "content_liveness_ok": True, "invariant_ok": True,
                "reasons": ["daily lane not run"]}
    try:
        d = json.loads(p.read_text())
    except Exception as exc:
        return {"status": "AMBER", "weekly_etf_daily_ran": True,
                "content_liveness_ok": False, "invariant_ok": True,
                "reasons": [f"unreadable daily health: {exc}"]}
    invariant_ok = d.get("feeds_decision_engine", False) is False
    live_ok = not (d.get("status") == "ok" and int(d.get("tickers_scored", 0)) == 0)
    if not invariant_ok:
        reasons.append("feeds_decision_engine invariant breached")
        status = "RED"
    elif not live_ok:
        reasons.append("looks-fresh-but-empty (0 tickers scored)")
        status = "AMBER"
    else:
        status = "GREEN"
    return {"status": status, "weekly_etf_daily_ran": True,
            "content_liveness_ok": live_ok, "invariant_ok": invariant_ok,
            "reasons": reasons}
```

Then add to `.claude/commands/daily-tool-analysis.md`: (a) a Step-1 artifacts-read entry for `outputs/weekly_etf_bundles/daily/health.json` via `assess_daily_lane`; (b) a Step-4 body-grammar line `"Weekly-ETF-daily: {status} · ran={weekly_etf_daily_ran} · {reasons}"`; (c) a dispatch note: RED (invariant breach) escalates; AMBER `champion_swap_pending` is reserved for Phase 2.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_weekly_etf_daily_health.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add portfolio_automation/weekly_etf_bundles/daily_health.py tests/test_weekly_etf_daily_health.py .claude/commands/daily-tool-analysis.md
git commit -m "feat(weekly-etf): daily-lane health assessor + daily-tool-analysis coverage"
```

---

### Task 6: Phase-1 integration verification

**Files:** none (verification only)

- [ ] **Step 1: Full targeted suite**

Run: `.venv/bin/python -m pytest tests/test_weekly_etf_trading_session.py tests/test_weekly_etf_run_asof.py tests/test_weekly_etf_daily_lane.py tests/test_weekly_etf_daily_shell.py tests/test_weekly_etf_daily_health.py tests/test_weekly_etf_bundles_phase1.py tests/test_weekly_etf_bundles_phase2.py tests/test_weekly_etf_bundles_phase3.py -q`
Expected: PASS, no regressions.

- [ ] **Step 2: Prove inert-by-default end to end**

Run: `WEEKLY_ETF_BUNDLES_DAILY_ENABLED=0 bash scripts/run_weekly_etf_daily.sh`
Expected: prints `disabled … skipping`, exit 0.

- [ ] **Step 3: Prove the daily lane leaves weekly artifacts untouched**

Run (with both gates on, in a scratch checkout or after capturing hashes): compare mtimes/hashes of `outputs/weekly_etf_bundles/{latest.json,health.json,predictions/*.json}` before and after a `--daily-observe` run; assert unchanged.

- [ ] **Step 4: Compile**

Run: `.venv/bin/python -m py_compile portfolio_automation/weekly_etf_bundles/trading_session.py portfolio_automation/weekly_etf_bundles/daily_paths.py portfolio_automation/weekly_etf_bundles/daily_health.py portfolio_automation/weekly_etf_bundles/run.py`

- [ ] **Step 5: Commit any doc/config touch-ups**

```bash
git commit -am "chore(weekly-etf): phase1 verification touch-ups" || echo "nothing to commit"
```

(Note: prefer explicit `git add <paths>` over `-am` per repo convention; use `-am` only if the tree is clean of unrelated changes.)

---

## Subsequent plans (Phase 2 & 3 — scoped, planned separately)

Per the spec's phasing and the skill's scope check, these get their own plans, written against the real Phase-1 code:

- **Phase 2 plan** — `champion_state.json` (versioned, CAS) + weekly freeze reads active champion; evidence → durable `weekly_etf_champion_change` proposal with the §7 statistical unit (weekly cohorts, correlation + horizon-overlap handling, Holm), the 5 anti-overfit gates, §5 stable-id/dedup/cooldown/hysteresis, and the §11 evidence schema. Proposal emission only; inert until data matures. `champion_swap_pending` AMBER added to daily-tool-analysis.
- **Phase 3 plan** — human-gated apply/rollback reconciler: the §8 state machine (`pending→approved_unapplied→applied→rollback_pending→rolled_back`/`rollback_conflict`/`failure`), exactly-once idempotency key, CAS apply, veto rollback, and the full audit schema.

## Self-Review

- **Spec coverage (Phase 1 subset):** §6 timezone → Tasks 1–2; §4/§9 write-allowlist + daily subtree + no-freeze → Task 3; §10 run_aux_stage + lock precedence + inert sub-flag → Task 4; §12 analysis+health → Task 5; §16 invariant (`feeds_decision_engine=false`) → asserted in Tasks 3 & 5. §4 champion_state, §5 lifecycle, §7 gates, §8 reconciler, §11 evidence schema → deferred to Phase 2/3 plans (explicitly noted).
- **Placeholder scan:** none — every code/test step carries real content.
- **Type consistency:** `latest_completed_session` (Task 1) is consumed by `resolve_as_of` (Task 2); `daily_write_path`/`is_allowed` (Task 3) names match their test; `assess_daily_lane` return keys match the test and the skill body-grammar line.
