# Pattern-Loop Production Foundation (A+B+C) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Operationalize the observe-only Pattern-Improvement Loop in production: a monthly recompute cron, a deterministic OOS-window maturity countdown emitted as an artifact field, and monthly self-monitoring wiring — with zero change to protected scoring/decision logic or the observe-only/owner-gated invariants.

**Architecture:** A new pure function `oos_window_status` (calendar-day based, matching the walk-forward engine) is computed by `run_loop`, threaded through `run_poc` into `poc_simulation_results.json`, and surfaced by `backtest_health`. A new `scripts/pattern_loop_recheck.sh` runs `run_loop --history --live` and is invoked (non-blocking) by the existing monthly cron wrapper before the monthly analysis, which is extended to read the artifacts, print the countdown, and dispatch the health agent on RED.

**Tech Stack:** Python 3.12, stdlib `datetime`, pytest/unittest, bash (cron wrappers), Claude Code skill markdown.

**Spec:** `docs/superpowers/specs/2026-06-05-pattern-loop-production-foundation-design.md`

**Conventions for every task:** the VPS interpreter is `/opt/stockbot/.venv/bin/python` (bare `python` is NOT on PATH). Run tests with `/opt/stockbot/.venv/bin/python -m pytest`. All work on branch `feature/pattern-improvement-loop`. Do NOT merge to main or touch the live crontab (production boundary — operator gates that separately).

---

## File Structure

- `backtesting/walk_forward.py` — add pure `oos_window_status()` (owns window math; reuses `_parse_date`).
- `backtesting/poc_simulation_harness.py` — `run_poc` gains optional `oos_window` param, merged into the written payload.
- `backtesting/run_loop.py` — compute the block, pass to `run_poc`, include in returned summary.
- `backtesting/backtest_health.py` — surface `oos_window` in `details`.
- `scripts/pattern_loop_recheck.sh` — NEW standalone monthly recompute runner.
- `scripts/monthly_check.sh` — call the recheck (non-blocking) before the analysis.
- `.claude/commands/monthly-tool-analysis.md` — artifacts-read + maturity line + RED dispatch.
- `tests/test_walk_forward.py`, `tests/test_poc_simulation_harness.py`, `tests/test_run_loop.py`, `tests/test_backtest_health.py` — extend.
- `docs/CHANGELOG_DECISIONS.md`, `.agent/project_state.yaml` — note.

---

### Task 1: `oos_window_status` pure producer

**Files:**
- Modify: `backtesting/walk_forward.py` (add function after `walk_forward`)
- Test: `tests/test_walk_forward.py` (add a test class)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_walk_forward.py`:

```python
from backtesting.walk_forward import oos_window_status  # add to existing imports


class TestOosWindowStatus:
    def _sig(self, iso: str) -> dict:
        return {"ticker": "AAA", "scan_time": iso}

    def test_short_history_not_yet_mature(self):
        sigs = [self._sig("2026-04-28"), self._sig("2026-05-15"), self._sig("2026-06-05")]
        ow = oos_window_status(sigs, today=date(2026, 6, 5))
        assert ow["calendar_days_observed"] == 38
        assert ow["first_fold_threshold_days"] == 252
        assert ow["full_window_days"] == 315
        assert ow["folds_possible"] is False
        assert ow["days_until_full_window"] == 277
        assert ow["full_window_eta"] == "2027-03-09"
        assert ow["earliest_signal"] == "2026-04-28"
        assert ow["latest_signal"] == "2026-06-05"
        assert ow["estimate"] is True

    def test_first_fold_boundary(self):
        early = date(2026, 1, 1)
        ow = oos_window_status(
            [self._sig(early.isoformat()),
             self._sig((early + timedelta(days=252)).isoformat())],
            today=date(2026, 9, 10),
        )
        assert ow["calendar_days_observed"] == 252
        assert ow["folds_possible"] is True
        assert ow["days_until_full_window"] == 63

    def test_mature_window_zero_remaining(self):
        ow = oos_window_status(
            [self._sig("2026-01-01"), self._sig("2027-01-01")],
            today=date(2027, 1, 1),
        )
        assert ow["folds_possible"] is True
        assert ow["days_until_full_window"] == 0
        assert ow["full_window_eta"] == "2027-01-01"

    def test_empty_signals_never_raises(self):
        ow = oos_window_status([], today=date(2026, 6, 5))
        assert ow["calendar_days_observed"] == 0
        assert ow["folds_possible"] is False
        assert ow["full_window_eta"] is None
        assert ow["earliest_signal"] is None

    def test_undatable_signals_treated_as_empty(self):
        ow = oos_window_status([{"ticker": "AAA"}, {"scan_time": "not-a-date"}],
                               today=date(2026, 6, 5))
        assert ow["calendar_days_observed"] == 0
        assert ow["folds_possible"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/opt/stockbot/.venv/bin/python -m pytest tests/test_walk_forward.py::TestOosWindowStatus -v`
Expected: FAIL — `ImportError: cannot import name 'oos_window_status'`.

- [ ] **Step 3: Implement `oos_window_status`**

Add to `backtesting/walk_forward.py` (after `walk_forward`, before EOF):

```python
def oos_window_status(
    signals: list[dict],
    *,
    train_days: int = 252,
    test_days: int = 63,
    today: date | None = None,
) -> dict[str, Any]:
    """Calendar-day maturity countdown for the walk-forward OOS window.

    ``walk_forward`` measures its window in calendar-day ordinals (it compares
    ``date.toordinal()`` values), so ``train_days``/``test_days`` are CALENDAR
    days. The first fold's loop iterates once the observed span reaches
    ``train_days``; the first test window sits fully inside observed history once
    the span reaches ``train_days + test_days``. This reports how far the
    accumulated signal history is from that point.

    Pure and total: empty or undatable input yields ``calendar_days_observed=0``
    and ``folds_possible=False`` and never raises. ``today`` is injectable for
    deterministic tests (the caller passes ``date.today()``). The ETA is a
    calendar-day projection, flagged ``estimate: True``.
    """
    full_window_days = train_days + test_days
    dates = sorted(
        d for d in (_parse_date(s.get("scan_time") or s.get("signal_date")) for s in signals)
        if d is not None
    )
    if not dates:
        return {
            "calendar_days_observed": 0,
            "first_fold_threshold_days": train_days,
            "full_window_days": full_window_days,
            "folds_possible": False,
            "days_until_full_window": full_window_days,
            "full_window_eta": None,
            "earliest_signal": None,
            "latest_signal": None,
            "estimate": True,
        }
    earliest, latest = dates[0], dates[-1]
    observed = latest.toordinal() - earliest.toordinal()
    days_remaining = max(0, full_window_days - observed)
    ref = today or date.today()
    eta = date.fromordinal(ref.toordinal() + days_remaining).isoformat()
    return {
        "calendar_days_observed": observed,
        "first_fold_threshold_days": train_days,
        "full_window_days": full_window_days,
        "folds_possible": observed >= train_days,
        "days_until_full_window": days_remaining,
        "full_window_eta": eta,
        "earliest_signal": earliest.isoformat(),
        "latest_signal": latest.isoformat(),
        "estimate": True,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/opt/stockbot/.venv/bin/python -m pytest tests/test_walk_forward.py -v`
Expected: PASS (new class + all existing walk_forward tests).

- [ ] **Step 5: Compile + commit**

```bash
/opt/stockbot/.venv/bin/python -m py_compile backtesting/walk_forward.py
git add backtesting/walk_forward.py tests/test_walk_forward.py
git commit -m "feat(backtesting): oos_window_status calendar-day maturity countdown"
```

---

### Task 2: Thread `oos_window` through `run_poc`

**Files:**
- Modify: `backtesting/poc_simulation_harness.py:183-266` (`run_poc` signature + payload merge)
- Test: `tests/test_poc_simulation_harness.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_poc_simulation_harness.py` (uses synthetic mode → no FMP, no write):

```python
def test_run_poc_includes_oos_window_when_provided():
    from backtesting.poc_simulation_harness import run_poc
    ow = {"calendar_days_observed": 38, "folds_possible": False}
    payload = run_poc(n_signals=12, n_symbols=4, seed=1, write=False, oos_window=ow)
    assert payload["oos_window"] == ow


def test_run_poc_omits_oos_window_by_default():
    from backtesting.poc_simulation_harness import run_poc
    payload = run_poc(n_signals=12, n_symbols=4, seed=1, write=False)
    assert "oos_window" not in payload
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/opt/stockbot/.venv/bin/python -m pytest tests/test_poc_simulation_harness.py::test_run_poc_includes_oos_window_when_provided -v`
Expected: FAIL — `TypeError: run_poc() got an unexpected keyword argument 'oos_window'`.

- [ ] **Step 3: Implement the param + merge**

In `backtesting/poc_simulation_harness.py`, add the param to the `run_poc` signature (after `signals: list[dict] | None = None`):

```python
            signals: list[dict] | None = None,
            oos_window: dict[str, Any] | None = None) -> dict[str, Any]:
```

Then, immediately before `if write:` (currently line 264), insert:

```python
    if oos_window is not None:
        payload["oos_window"] = oos_window
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/opt/stockbot/.venv/bin/python -m pytest tests/test_poc_simulation_harness.py -v`
Expected: PASS (new tests + all existing harness tests).

- [ ] **Step 5: Compile + commit**

```bash
/opt/stockbot/.venv/bin/python -m py_compile backtesting/poc_simulation_harness.py
git add backtesting/poc_simulation_harness.py tests/test_poc_simulation_harness.py
git commit -m "feat(backtesting): run_poc accepts optional oos_window for the results artifact"
```

---

### Task 3: Wire the countdown into `run_loop`

**Files:**
- Modify: `backtesting/run_loop.py` (import, compute, pass to `run_poc`, add to summary)
- Test: `tests/test_run_loop.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_run_loop.py` (reuses the module's `_results_artifact` helper, which dates rows ~40 days back → an immature window):

```python
class TestRunLoopOosWindow(unittest.TestCase):
    def test_run_loop_summary_includes_oos_window(self):
        with tempfile.TemporaryDirectory() as td:
            art = Path(td) / "watchlist_signals.json"
            _results_artifact(art, basis=["strong_move"], n=40)
            out = run_loop(signals_source=str(art), history_dir=None, live=False,
                           write=False, base_dir=td)
        self.assertEqual(out["status"], "ok")
        ow = out["oos_window"]
        self.assertIn("calendar_days_observed", ow)
        self.assertFalse(ow["folds_possible"])  # ~40-day spread is far short of 252
        self.assertEqual(ow["full_window_days"], 315)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/opt/stockbot/.venv/bin/python -m pytest tests/test_run_loop.py::TestRunLoopOosWindow -v`
Expected: FAIL — `KeyError: 'oos_window'`.

- [ ] **Step 3: Implement the wiring**

In `backtesting/run_loop.py`:

(a) Extend the walk_forward import (currently `from backtesting.walk_forward import walk_forward`):

```python
from backtesting.walk_forward import oos_window_status, walk_forward
```

(b) Add a stdlib import at the top of the module (with the other imports):

```python
from datetime import date
```

(c) In `run_loop`, replace the `run_poc(...)` call (the Steps 1/1b/3 block) with a version that computes and passes the window:

```python
        # Steps 1/1b/3 — POC simulation metrics artifact (reuses run_poc).
        window = oos_window_status(signals, train_days=train_days, test_days=test_days,
                                   today=date.today())
        poc = run_poc(signals=signals, live=live, seed=seed, forward_days=forward_days,
                      write=write, base_dir=base_dir, oos_window=window)
```

(d) Add `oos_window` to the returned `"ok"` dict (after the `"poc": {...}` block):

```python
            "oos_window": window,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/opt/stockbot/.venv/bin/python -m pytest tests/test_run_loop.py -v`
Expected: PASS (new test + all existing run_loop tests, incl. the registry-byte-identical invariant).

- [ ] **Step 5: Compile + commit**

```bash
/opt/stockbot/.venv/bin/python -m py_compile backtesting/run_loop.py
git add backtesting/run_loop.py tests/test_run_loop.py
git commit -m "feat(backtesting): run_loop computes + emits oos_window maturity block"
```

---

### Task 4: Surface `oos_window` in `backtest_health`

**Files:**
- Modify: `backtesting/backtest_health.py:85-101` (read the field into `details`)
- Test: `tests/test_backtest_health.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_backtest_health.py`, extend the `_write_results` helper to accept an optional `oos_window`, then add a test. Add the param to `_write_results` (default `None`) and, inside it, after building `payload`:

```python
def _write_results(backtest_dir: Path, *, generated_at: str, evaluated: int,
                   regimes: list[str], slope: float, oos_window: dict | None = None) -> None:
    ...
    if oos_window is not None:
        payload["oos_window"] = oos_window
    (backtest_dir / "poc_simulation_results.json").write_text(json.dumps(payload), encoding="utf-8")
```

Then append a test:

```python
def test_oos_window_surfaced_in_details(tmp_path):
    bt = tmp_path / "backtest"
    _write_results(bt, generated_at=_NOW.isoformat(), evaluated=120,
                   regimes=["risk_on", "neutral"], slope=0.3,
                   oos_window={"calendar_days_observed": 38, "folds_possible": False})
    prop = tmp_path / "policy" / "signal_weight_proposals.json"
    _write_proposals(prop, proposed_count=1)
    out = assess_backtest_health(backtest_dir=str(bt), proposals_path=str(prop), now=_NOW)
    assert out["details"]["oos_window"] == {"calendar_days_observed": 38, "folds_possible": False}


def test_oos_window_absent_tolerated(tmp_path):
    bt = tmp_path / "backtest"
    _write_results(bt, generated_at=_NOW.isoformat(), evaluated=120,
                   regimes=["risk_on", "neutral"], slope=0.3)
    prop = tmp_path / "policy" / "signal_weight_proposals.json"
    _write_proposals(prop, proposed_count=1)
    out = assess_backtest_health(backtest_dir=str(bt), proposals_path=str(prop), now=_NOW)
    assert out["details"]["oos_window"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/opt/stockbot/.venv/bin/python -m pytest tests/test_backtest_health.py::test_oos_window_surfaced_in_details tests/test_backtest_health.py::test_oos_window_absent_tolerated -v`
Expected: FAIL — `KeyError: 'oos_window'` (not yet in details).

- [ ] **Step 3: Implement the surfacing**

In `backtesting/backtest_health.py`, inside the `else` branch where `results` is a dict (after `perf = results.get("performance") or {}` and the `details["evaluated"]` line, e.g. right after line 88), add:

```python
        details["oos_window"] = results.get("oos_window")
```

(`results.get` returns `None` when absent → tolerated.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `/opt/stockbot/.venv/bin/python -m pytest tests/test_backtest_health.py -v`
Expected: PASS (new tests + all existing health tests; tiers unchanged).

- [ ] **Step 5: Compile + commit**

```bash
/opt/stockbot/.venv/bin/python -m py_compile backtesting/backtest_health.py
git add backtesting/backtest_health.py tests/test_backtest_health.py
git commit -m "feat(backtesting): surface oos_window maturity in backtest_health details"
```

---

### Task 5: `scripts/pattern_loop_recheck.sh` runner

**Files:**
- Create: `scripts/pattern_loop_recheck.sh`

- [ ] **Step 1: Write the script**

Create `scripts/pattern_loop_recheck.sh` (mirrors `scripts/monthly_check.sh` conventions: PATH/HOME, the same dotenv parser, venv python, monthly log). Note: uses `set -uo pipefail` WITHOUT `-e` so a non-zero from the loop is logged, not fatal.

```bash
#!/usr/bin/env bash
# Pattern-Improvement Loop monthly recompute (observe-only, proposes-only).
#
# Runs `run_loop --history --live` so signal_weight_proposals.json and
# poc_simulation_results.json (incl. the oos_window maturity block) stay fresh
# as signal history accumulates. FMP-only (free); no AI/LLM spend. Step 5
# (apply) is never invoked — this only ever proposes.
#
# Best-effort: logs and exits non-zero on failure; the caller treats it as
# non-blocking. Intended to be invoked by monthly_check.sh before the analysis.

set -uo pipefail

export HOME="${HOME:-/root}"
export PATH="/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

REPO_ROOT="/opt/stockbot"
cd "${REPO_ROOT}" || { echo "FATAL: cannot cd to ${REPO_ROOT}" >&2; exit 2; }

PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
LOG_DIR="${REPO_ROOT}/logs"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/pattern_loop_recheck_$(date -u +%Y-%m).log"

# Load .env (same minimal parser as monthly_check.sh) so FMP_API_KEY is set.
load_dotenv_file() {
  local env_file="$1"
  local line trimmed key value
  [ -f "$env_file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    trimmed="${line#"${line%%[![:space:]]*}"}"
    [ -z "$trimmed" ] && continue
    [ "${trimmed:0:1}" = "#" ] && continue
    trimmed="${trimmed#export }"
    [[ "$trimmed" != *=* ]] && continue
    key="${trimmed%%=*}"
    value="${trimmed#*=}"
    if [[ "$value" =~ ^\".*\"$ ]] || [[ "$value" =~ ^\'.*\'$ ]]; then
      value="${value:1:${#value}-2}"
    fi
    export "$key=$value"
  done < "$env_file"
}
load_dotenv_file "${REPO_ROOT}/.env"

if [ ! -x "${PYTHON_BIN}" ]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] FATAL: venv python not found at ${PYTHON_BIN}" >> "${LOG_FILE}"
  exit 3
fi

{
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] pattern_loop_recheck.sh starting"
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] invoking run_loop --history --live"
} >> "${LOG_FILE}"

"${PYTHON_BIN}" -m backtesting.run_loop --history --live >> "${LOG_FILE}" 2>&1
RC=$?

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] pattern_loop_recheck.sh done exit=${RC}" >> "${LOG_FILE}"
exit "${RC}"
```

- [ ] **Step 2: Make executable + syntax-check**

```bash
chmod +x scripts/pattern_loop_recheck.sh
bash -n scripts/pattern_loop_recheck.sh && echo "syntax ok"
```
Expected: `syntax ok`.

- [ ] **Step 3: Commit**

```bash
git add scripts/pattern_loop_recheck.sh
git commit -m "feat(scripts): pattern_loop_recheck.sh monthly recompute runner (observe-only)"
```

(Live execution is validated on the VPS in the final VPS-validation block, not here — it makes real FMP calls.)

---

### Task 6: Invoke the recheck from `monthly_check.sh`

**Files:**
- Modify: `scripts/monthly_check.sh` (insert a non-blocking recheck call before the `claude --print` line)

- [ ] **Step 1: Add the recheck call**

In `scripts/monthly_check.sh`, immediately BEFORE the block that invokes
`"${CLAUDE_BIN}" --print "/monthly-tool-analysis"`, insert:

```bash
# Refresh the Pattern-Loop artifacts (observe-only, FMP-only) so the analysis
# below reads a current poc_simulation_results.json + signal_weight_proposals.json.
# Non-blocking: a failure here must not stop the monthly analysis.
{
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] running pattern_loop_recheck.sh (non-blocking)"
} >> "${LOG_FILE}"
"${REPO_ROOT}/scripts/pattern_loop_recheck.sh" >> "${LOG_FILE}" 2>&1 \
  || echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] WARN: pattern_loop_recheck.sh failed; continuing" >> "${LOG_FILE}"
```

- [ ] **Step 2: Syntax-check**

```bash
bash -n scripts/monthly_check.sh && echo "syntax ok"
```
Expected: `syntax ok`.

- [ ] **Step 3: Commit**

```bash
git add scripts/monthly_check.sh
git commit -m "feat(scripts): monthly_check runs pattern_loop_recheck before analysis (non-blocking)"
```

---

### Task 7: Self-monitoring wiring in `monthly-tool-analysis.md`

**Files:**
- Modify: `.claude/commands/monthly-tool-analysis.md`

Read the file first to place edits in the existing sections (artifacts-read list, body grammar, dispatch logic). Apply these three additive edits using the file's existing wording/format.

- [ ] **Step 1: Add the two artifacts to the Step-1 artifacts-read list**

Add list entries:
```
- `outputs/backtest/poc_simulation_results.json` — Pattern-Loop OOS sim (read `oos_window`, `performance.evaluated`, `calibration.calibration_slope`, `added_metrics.per_regime`).
- `outputs/policy/signal_weight_proposals.json` — Step 4 weight proposals (read `summary.proposed_count`).
```

- [ ] **Step 2: Add the maturity-countdown body-grammar line**

In the body-grammar / report section add:
```
- Pattern-Loop OOS window: `{oos_window.calendar_days_observed}/{oos_window.full_window_days}` calendar days; folds_possible=`{oos_window.folds_possible}`; first full window ~`{oos_window.full_window_eta}`.
  While `folds_possible=false`, a `proposed_count` of 0 is EXPECTED and healthy (not a failure) — the loop cannot produce out-of-sample evidence until the window matures (~2027). Report it as GREEN/"accruing", not RED.
```

- [ ] **Step 3: Add the RED dispatch trigger**

In the dispatch-logic section add:
```
- Dispatch `portfolio-backtest-health` when the Pattern-Loop artifact is RED:
  `poc_simulation_results.json` missing, `performance.evaluated == 0` (looks-fresh-but-empty),
  every `added_metrics.per_regime[].regime == "unknown"` (degenerate), or
  `calibration.calibration_slope < 0` (flipped). Do NOT dispatch merely because
  `proposed_count == 0` while `oos_window.folds_possible == false` — that is the expected pre-maturity state.
```

- [ ] **Step 4: Commit**

```bash
git add .claude/commands/monthly-tool-analysis.md
git commit -m "feat(analysis): monthly tier reads Pattern-Loop artifacts + OOS maturity countdown + RED dispatch"
```

---

### Task 8: Docs + full suite

**Files:**
- Modify: `docs/CHANGELOG_DECISIONS.md` (prepend a dated entry)
- Modify: `.agent/project_state.yaml` (add a one-line note under the recent-changes/log section — do NOT change `next_official_step`, which stays `observe_and_iterate`)

- [ ] **Step 1: Add a CHANGELOG entry**

Prepend to `docs/CHANGELOG_DECISIONS.md` (match the file's existing entry format):
```
## 2026-06-05 — Pattern-Loop production Foundation (A+B+C)
- Monthly recompute: `scripts/pattern_loop_recheck.sh` runs `run_loop --history --live`,
  invoked non-blocking by `monthly_check.sh` before the monthly analysis. Observe-only,
  FMP-only (no AI spend), Step 5 never invoked.
- New deterministic `oos_window_status` (calendar-day maturity countdown) emitted in
  `poc_simulation_results.json` and surfaced by `backtest_health`.
- `monthly-tool-analysis` reads the Pattern-Loop artifacts, prints the OOS maturity
  countdown, and treats `proposed_count==0` while `folds_possible==false` as healthy.
- No protected scoring/decision change; observe-only + owner-gated invariants unchanged.
  First OOS folds ~2027-01; full window ~2027-03.
```

- [ ] **Step 2: Add the project_state note**

Add a one-line dated note in the appropriate recent-changes list of `.agent/project_state.yaml` (do not alter `next_official_step`):
```
  - pattern_loop_production_foundation  # 2026-06-05 — monthly recompute cron wiring + oos_window maturity countdown + monthly health dispatch; observe-only, next_official_step unchanged
```

- [ ] **Step 3: Run the FULL suite**

Run: `/opt/stockbot/.venv/bin/python -m pytest -q`
Expected: all pass (no regressions; new tests included).

- [ ] **Step 4: Commit**

```bash
git add docs/CHANGELOG_DECISIONS.md .agent/project_state.yaml
git commit -m "docs: record Pattern-Loop production Foundation (A+B+C)"
```

---

## Self-Review

**Spec coverage:**
- B (monthly recompute) → Tasks 5, 6.
- C1 (deterministic countdown producer) → Tasks 1, 2, 3, 4.
- C2 (self-monitoring wiring) → Task 7.
- A (land code / merge) → intentionally NOT in this plan; it is the operator-gated production step, executed after go-ahead.
- Tests for every new Python unit → Tasks 1–4. Shell scripts → VPS validation (Task 5 note + final block), per repo convention.
- Docs/coverage requirement → Tasks 7 (cadence-matched health check) + 8.

**Placeholder scan:** none — every code/step shows the actual content. Task 7 edits are described against the file's existing sections because the exact surrounding markdown must be read at edit time; the inserted text is given verbatim.

**Type/name consistency:** `oos_window_status` (Task 1) is imported and called identically in Task 3; the `oos_window` dict key is written by Task 2, produced by Task 3, read by Task 4; field names (`calendar_days_observed`, `folds_possible`, `full_window_days`, `full_window_eta`, `days_until_full_window`, `first_fold_threshold_days`, `earliest_signal`, `latest_signal`, `estimate`) are identical across producer, tests, and consumers.

## Production boundary (NOT in this plan — operator go-ahead required)

- Merge `feature/pattern-improvement-loop` → `main` (sub-project A).
- Confirm the live VPS cron picks up `monthly_check.sh` (no crontab change needed — reuses the `30 9 1 * *` slot).
- VPS validation commands will be generated for the operator after implementation (`portfolio-vps-validation`).
