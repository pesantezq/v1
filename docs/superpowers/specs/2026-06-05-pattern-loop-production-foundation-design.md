# Pattern-Improvement Loop — Production Foundation (A+B+C) — Design

- **Date:** 2026-06-05
- **Branch:** feature/pattern-improvement-loop
- **Status:** Approved design; spec under user review before implementation-plan.
- **Author:** Claude Code (brainstormed with operator)

## Context

The Pattern-Improvement Loop (`backtesting/run_loop.py`, Steps 1→5) is feature-complete
and observe-only. Steps 1–4 are on `main`; the Step 5 protected-score value-regression
gate (commit `639fdba0`) is the only commit ahead on this branch.

First `real_signals_live` run (2026-06-05) confirmed correct behavior:
- POC headline looks strong (62.85% hit, +0.0893% edge) **but is in-sample** over a
  ~38-day window.
- The walk-forward OOS layer — which gates the weight proposals — correctly returned
  `oos_n=0` for every signal, so `proposed_count=0`. Root cause: real signal history
  spans ~38 calendar days, far short of the walk-forward window. NOTE: `walk_forward`
  measures its window in **calendar-day ordinals** (`backtesting/walk_forward.py:126-136`
  use `date.toordinal()`; `while cursor + train_days <= latest`), so `train_days=252`
  and `test_days=63` are CALENDAR days, not trading days. The first fold's loop iterates
  once span ≥ `train_days` (252 cal days); the first test window is fully inside observed
  history once span ≥ `train_days + test_days` (315 cal days).
- **Out-of-sample evidence — and therefore any actionable weight proposal — cannot
  exist until signal history reaches ~315 calendar days. From the 2026-04-28 earliest
  signal: first folds begin forming ~2027-01-05 (span 252), full first window ~2027-03-09
  (span 315).**

The roadmap's `next_official_step` is `observe_and_iterate`; this loop is the engine of
that. "Adding it to production" is not about enabling changes — it is about scheduling
the loop to run and accumulate, and observing its maturity, while every change stays
human/owner-gated and observe-only.

This spec covers only the **Foundation** sub-projects (A+B+C). Two later sub-projects
are sequenced after it and get their own spec → plan → implementation cycles:
- **D — new feedback loops** (calibration-correction + regime/pattern re-tagging;
  observe-only, proposes-only). Pays off this year, independent of the OOS clock.
- **E — full auto-apply** (inert until ~mid-2027). Removes the human from the registry
  apply path. **Changes documented hard invariants** (observe-only, owner-gated Step 5)
  and therefore requires amending `CLAUDE.md` + docs. Operator decision (2026-06-05):
  the auto-approver is a **GPT API call** (LLM-as-judge) layered ON TOP of the existing
  deterministic gates — it may veto or approve-within-bounds, never widen a bound — and
  must budget/cap its AI spend (cost model: FMP free, AI paid).

## Non-goals (Foundation)

- No change to protected scoring/decision/allocation logic.
- No change to `observe_only` / owner-gated semantics (that is sub-project E).
- No new feedback loops (that is sub-project D).
- No auto-apply (sub-project E).
- No broker/execution/trading behavior.

## Architecture

Three additive pieces threaded into the existing monthly tier:

```
existing daily cron (0 9 * * *)  ──► outputs/history/<date> snapshots accrue
                                          │  (signal history grows ~5 days/week)
existing monthly cron (30 9 1 * *) ──► scripts/monthly_check.sh
   │  (B) calls scripts/pattern_loop_recheck.sh  [NEW]  (non-blocking)
   │        └─► .venv/bin/python -m backtesting.run_loop --history --live
   │              └─► writes poc_simulation_results.{json,md} (HISTORICAL)
   │                  + signal_weight_proposals.json (POLICY)
   │                  + (C1) oos_window block in poc_simulation_results.json
   └─ then ──► claude --print /monthly-tool-analysis
                 └─► (C2) reads the two artifacts, prints maturity countdown,
                          dispatches portfolio-backtest-health on RED
```

### A — Land code
Merge commit `639fdba0` (Step 5 gate + tests/docs) into `main`. Mechanism is an operator
choice (queued `gh pr create`, or direct merge under `dev_on_vps`). Not blocking B/C
because `run_loop` and `walk_forward` are already on `main`.

### B — Scheduled recompute
- **New `scripts/pattern_loop_recheck.sh`** — mirrors `scripts/monthly_check.sh`
  conventions: `set -uo pipefail`; `export HOME`/`PATH`; `load_dotenv_file ./.env`
  (same parser as `monthly_check.sh`); `cd /opt/stockbot`; log to
  `logs/pattern_loop_recheck_$(date -u +%Y-%m).log`. Runs
  `.venv/bin/python -m backtesting.run_loop --history --live`. FMP-only (free), no AI.
  Standalone and idempotent → runnable on demand.
- **Modified `scripts/monthly_check.sh`** — invoke `pattern_loop_recheck.sh` BEFORE the
  `claude --print /monthly-tool-analysis` call, non-blocking
  (`"$REPO_ROOT/scripts/pattern_loop_recheck.sh" >> "$LOG_FILE" 2>&1 || echo "...recheck failed, continuing"`),
  so the analysis always reads fresh artifacts.
- **No new cron entry** — reuses the `30 9 1 * *` monthly slot. (Alternative: standalone
  weekly cron — rejected; the OOS window moves ~5 days/week, monthly recompute is
  sufficient and keeps the cron surface minimal.)
- Fully observe-only/proposes-only; Step 5 untouched.

### C1 — Maturity-countdown producer (deterministic)
- **New pure function** `oos_window_status(signals, *, train_days=252, test_days=63,
  today=None)` in `backtesting/walk_forward.py` (it owns the window math; reuses its
  `_parse_date` and the same `scan_time`/`signal_date` keys). **Calendar-day based, to
  match the engine** (`walk_forward` compares `date.toordinal()` values, so `train_days`/
  `test_days` are calendar days). Computes the span between earliest and latest datable
  signal and returns:
  ```json
  {
    "calendar_days_observed": 38,
    "first_fold_threshold_days": 252,
    "full_window_days": 315,
    "folds_possible": false,
    "days_until_full_window": 277,
    "full_window_eta": "2027-03-09",
    "earliest_signal": "2026-04-28",
    "latest_signal": "2026-06-05",
    "estimate": true
  }
  ```
  where `folds_possible = calendar_days_observed >= train_days`,
  `days_until_full_window = max(0, train_days + test_days - calendar_days_observed)`,
  `full_window_eta = today + days_until_full_window`. `today` is injectable for
  deterministic tests (caller passes `date.today()`; no argless `now()` in the pure
  core). Empty/undatable signals → `calendar_days_observed: 0, folds_possible: false`
  (never raises). The ETA is labeled `estimate: true` to avoid false precision.
- **Modified `backtesting/poc_simulation_harness.py`** — `run_poc` gains an optional
  `oos_window: dict | None = None` param; when provided it is added to `payload` before
  the artifact is written. Default `None` keeps every existing caller byte-identical.
- **Modified `backtesting/run_loop.py`** — compute `oos_window_status` on the loaded
  signals, pass it to `run_poc(oos_window=...)`, and include the block in the returned
  summary.
- **Modified `backtesting/backtest_health.py`** — surface `results.get("oos_window")` in
  `details["oos_window"]` (tolerates absence → `null`). No change to GREEN/AMBER/RED tiers.

### C2 — Self-monitoring wiring (cadence-match requirement)
Because the loop now runs monthly (B), its health check moves to the monthly cadence per
the CLAUDE.md "Analysis + Health Coverage Requirement". Extend
`.claude/commands/monthly-tool-analysis.md`:
- add `outputs/backtest/poc_simulation_results.json` + `outputs/policy/signal_weight_proposals.json`
  to the Step-1 artifacts-read list;
- add a body-grammar line: `OOS window: {trading_days_observed}/315 trading days,
  first folds ~{first_fold_eta}` (with a "not yet mature — zero proposals expected" note
  while `folds_possible=false`, so a healthy zero is not misread as a failure);
- add a dispatch trigger to the `portfolio-backtest-health` agent when the artifact is
  RED: stale (age beyond one month), degenerate (all-`unknown` regimes), looks-fresh-but-
  empty (`evaluated==0`), or calibration-slope flipped.

## Data flow

1. Daily cron writes `outputs/history/<date>` snapshots (existing, unchanged).
2. Monthly cron → `monthly_check.sh` → `pattern_loop_recheck.sh` → `run_loop --history
   --live` reads all snapshots, recomputes, writes `poc_simulation_results.{json,md}`
   (incl. `oos_window`) + `signal_weight_proposals.json`.
3. `monthly_check.sh` → `/monthly-tool-analysis` reads those artifacts, prints the
   maturity countdown, dispatches `portfolio-backtest-health` on RED.

## Error handling

- `pattern_loop_recheck.sh`: non-blocking inside `monthly_check.sh` (failure logged, the
  monthly analysis still runs). `run_loop` already degrades to a status dict and never
  raises.
- `oos_window_status`: pure, total function — undatable/empty signals → `folds_possible:
  false, trading_days_observed: 0` (never raises), mirroring `walk_forward`'s
  empty-input contract.
- `backtest_health.py`: missing `oos_window` field tolerated (older artifacts) — surfaced
  as `null`, no exception.

## Testing

- **New `tests/test_oos_window_status.py`:**
  - short history (≈38 days) → `folds_possible:false`, `days_remaining>0`,
    `trading_days_observed≈27`.
  - long history (≥315 trading days) → `folds_possible:true`, `days_remaining:0`.
  - empty/undatable signals → `trading_days_observed:0`, no raise.
  - deterministic via injected `today`.
- **Extend `tests/test_backtest_health.py`:** assert `oos_window` is surfaced under both
  a mature and an immature fixture artifact; assert tiers unchanged.
- Shell scripts: VPS validation commands (repo convention — shell not unit-tested).
- Targeted tests first, then full suite (`python -m pytest -q`) must stay green.

## Files

**New:**
- `scripts/pattern_loop_recheck.sh`
- `tests/test_oos_window_status.py`
- `docs/superpowers/specs/2026-06-05-pattern-loop-production-foundation-design.md` (this file)

**Modified:**
- `scripts/monthly_check.sh`
- `backtesting/walk_forward.py` (additive: `oos_window_status`)
- `backtesting/poc_simulation_harness.py` (additive: optional `oos_window` param on `run_poc`)
- `backtesting/run_loop.py` (additive: `oos_window` in summary + artifact)
- `backtesting/backtest_health.py` (additive: surface `oos_window`)
- `.claude/commands/monthly-tool-analysis.md`
- `tests/test_backtest_health.py`
- `docs/CHANGELOG_DECISIONS.md` (+ `.agent/project_state.yaml` note)

## Risks

- `walk_forward.py` / `run_loop.py` / `backtest_health.py` are core backtesting modules,
  but every change is additive (new function, new artifact field) and touches no
  protected scoring/decision logic. Low risk.
- Trading-day count is an estimate (calendar→business-day); ETA labeled approximate.
- VPS `gh` is unauthenticated → sub-project A (merge) may proceed as a direct merge
  instead of a PR. Operator choice; does not block B/C.

## Sequencing after Foundation

D (new feedback loops) → E (full auto-apply, GPT-approver, inert until ~2027, amends
CLAUDE.md invariants). Each its own spec.
