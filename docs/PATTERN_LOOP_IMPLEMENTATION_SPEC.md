# Pattern-Improvement Loop — Implementation Spec

**Status:** execution-ready spec (no code changes performed by this document).
**Companion to:** [`PRODUCTION_READINESS_PLAN.md`](PRODUCTION_READINESS_PLAN.md),
[`ARCHITECTURE_MAP.md`](ARCHITECTURE_MAP.md),
[`TECH_DEBT_AUDIT.md`](TECH_DEBT_AUDIT.md).

This is the concrete, step-by-step plan to turn the observe-only measurement POC
(`backtesting/poc_simulation_harness.py`) into a real loop that backtests
**your actual history**, measures which patterns work, and — only behind an
explicit approval gate — refines the signal weights that drive pattern
recognition.

---

## How to use this spec

- **One step = one scoped task.** Each step below is sized to be a single
  Claude Code session: it names exact files, functions, tests, validation
  commands, and acceptance criteria.
- **Additive and reversible.** Every step adds modules; none rewrites protected
  logic. Steps 0–4 and 6 are **observe-only**. **Step 5 is the only protected
  step** and must not begin without explicit owner approval.
- **Each step ends with the repo's Final Report** (see `CLAUDE.md`) and a green
  test run.

### The approval gate (read this first)

```
 Step 0      Steps 1–4              Step 5                  Step 6
 baseline →  MEASURE + PROPOSE  →   APPLY (gated)       →   HEALTH CHECK
 (run once)  observe-only           PROTECTED               observe-only
 ───────     ──────────────         ───────────────         ────────────
 ✅ safe      ✅ safe to build       🔒 owner approval        ✅ safe
                                       required first
```

Up to and including Step 4 the system only *writes proposals*. Nothing changes
how decisions are computed until Step 5, which edits `config/signal_registry.yaml`
weights and therefore requires sign-off per `CLAUDE.md` ("Protected Semantics").

---

## What already exists (extend, do not duplicate)

The repo already has most of the scaffolding; this loop connects the pieces and
adds out-of-sample rigor.

| Concern | Existing module / artifact | Role in this loop |
|---|---|---|
| Backtest engine | `backtesting/fmp_backtester.py` (`FMPBacktester`) | Forward-return + calibration core |
| POC harness | `backtesting/poc_simulation_harness.py` | Added Sharpe-like / baseline / per-pattern metrics |
| Live signals | `outputs/latest/watchlist_signals.json` (`{results, alerts}`); rows are `WatchlistRow` in `watchlist_scanner/models.py` | Real signals to replay |
| Per-signal calibration | `signal_results[]` in the signals contract (`hit_rate`, `calibration_gap`, `suggested_review`) | Existing per-pattern feedback to align with |
| Weight tuning | `watchlist_scanner/weight_tuning.py` (`build_weight_tuning_suggestions`, `generate_weight_tuning_report` → `outputs/performance/weight_tuning_suggestions.json`) | Existing suggestion format to extend |
| Governed apply precedent | `portfolio_automation/retune_auto_apply.py`, `retune_suggestions.py` | Pattern to mirror for the gated apply |
| Signal weights | `config/signal_registry.yaml` (`signals[].signal_id`, `default_weight`, `confidence_floor`, `enabled`) | The thing Step 5 edits (protected) |
| Regime | `market_regime.py` (`detect_market_regime(...)` → `regime_label`) | Regime conditioning in Step 3 |
| Governance | `portfolio_automation/data_governance.py` (`OutputNamespace`, `safe_write_json/_text`) | All writes go through here |

> Before coding any step, open the listed modules and confirm current
> signatures — treat the names above as the entry points, not frozen contracts.

---

## Conventions every step must follow

- **Namespaces:** backtest/replay outputs → `OutputNamespace.HISTORICAL`
  (`outputs/backtest/`); proposal artifacts → `OutputNamespace.POLICY`
  (`outputs/policy/`). Never write to `latest/` from a backtest/replay path.
- **`observe_only: true`** hardcoded in every new artifact (and `proposed_only:
  true` for tuning proposals).
- **Determinism:** seed all stochastic steps; pin any new dependency.
- **FMP compliance:** only the approved historical EOD endpoint via `FMPClient`;
  respect the call budget and cache (`docs/FMP_COMPLIANCE.md`).
- **Tests:** every step adds tests asserting behavior under a **healthy** and a
  **degraded** fixture state (per the `CLAUDE.md` analysis+health rule).
- **Validation per step:** `python -m py_compile <files>`, then targeted
  `python -m pytest tests/<new_test> -q`, then the relevant broader suite.

---

## Step 0 — Baseline run (prerequisite, no code)

**Goal:** produce real artifacts to backtest against and confirm the data layer
wires up (the audit found empty DB tables / no live artifacts in this checkout).

**Do:** `bash scripts/preflight.sh` → `python main.py --dry-run` →
`bash scripts/run_daily_safe.sh`. Then confirm `outputs/latest/watchlist_signals.json`
exists with a non-empty `results[]`, and re-check the SQLite tables.

**Acceptance:** `watchlist_signals.json` present and parseable; at least some
`results[]` rows carry `signal_score`, `confidence_score`, `scan_time`.
**Boundary:** none (operator run). **Effort:** S.

---

## Step 1 — Real-signal ingestion (observe-only)

**Goal:** let the harness replay real signals instead of synthetic ones.

**Create:** `backtesting/signal_sources.py`
```python
def load_signals_from_artifact(
    path: str = "outputs/latest/watchlist_signals.json",
) -> list[dict]:
    """Read {results:[...]} and normalize each row to the harness signal shape:
    {ticker, scan_time, signal_score, confidence_score, pattern}. Returns [] if
    the file is missing/empty (degraded). 'pattern' comes from the row's
    signal/event type when present, else 'UNKNOWN'."""

def load_historical_signal_snapshots(dir: str = "outputs/history") -> list[dict]:
    """Optional: aggregate dated snapshots for a longer signal history."""
```
**Modify:** `poc_simulation_harness.run_poc(...)` — add `signals_source:
str | None = None`; when set, load real signals via the loader (keep synthetic
as the default fallback). No change to metric logic.

**Tests:** `tests/test_signal_sources.py` — healthy (a fixture
`watchlist_signals.json` → normalized rows) and degraded (missing file → `[]`,
no crash).

**Acceptance:** `run_poc(signals_source=...)` reproduces real signals through the
existing metric path; synthetic still works. **Boundary:** observe-only.
**Effort:** S–M.

---

## Step 2 — Walk-forward / out-of-sample engine (observe-only)

**Goal:** stop reporting in-sample numbers; evaluate only out-of-sample, with
sample-size gating.

**Create:** `backtesting/walk_forward.py`
```python
def walk_forward(
    signals: list[dict], bt, *,            # bt: an FMPBacktester
    train_days: int = 252, test_days: int = 63, step_days: int = 63,
    forward_days: int = 10, min_signals_per_fold: int = 30,
) -> dict:
    """Split signals by scan_time into rolling train/test folds; evaluate each
    test fold via bt.simulate_signal_performance; return per-fold + aggregated
    OUT-OF-SAMPLE metrics, each annotated with n and a Wilson 95% CI on hit
    rate. Folds below min_signals_per_fold are reported as 'insufficient'."""
```
Implement a dependency-free Wilson interval helper for the CI (no scipy).

**Tests:** `tests/test_walk_forward.py` — healthy (trending fixture → folds
populated, CIs present) and degraded (too few signals → all folds 'insufficient',
no crash).

**Acceptance:** harness can emit OOS aggregate + per-fold metrics with N and CIs.
**Boundary:** observe-only. **Effort:** M.

---

## Step 3 — Regime conditioning (observe-only)

**Goal:** answer "do these patterns hold in drawdowns vs. normal regimes?"

**Create:** `backtesting/regime_tagging.py`
```python
def tag_signal_regime(signal: dict, price_series: dict) -> str:
    """Classify the regime as of the signal's entry date (read-only reuse of
    market_regime.detect_market_regime / the drawdown classifier). Returns a
    regime_label; 'unknown' when inputs are insufficient."""
```
**Modify:** the harness report to break every metric (hit rate, avg return,
Sharpe-like, calibration) down by regime bucket.

**Tests:** `tests/test_regime_tagging.py` — healthy (synthetic drawdown path →
expected label) and degraded (insufficient inputs → 'unknown').

**Acceptance:** per-regime metric tables appear in the harness JSON/MD.
**Boundary:** observe-only (read-only use of `market_regime`). **Effort:** M.

---

## Step 4 — Pattern efficacy → tuning *proposal* (observe-only; the loop's edge)

**Goal:** convert OOS, regime-aware, per-pattern efficacy into *proposed* weight
deltas for `signal_registry.yaml` — written as a review artifact, never applied.

**Create:** `backtesting/tuning_proposals.py`
```python
def propose_weight_changes(
    per_pattern_oos: list[dict],           # from Steps 2–3
    registry_path: str = "config/signal_registry.yaml",
    *, min_n: int = 50, max_abs_delta: float = 0.05,
) -> dict:
    """For each signal_id with sufficient OOS sample, compute a SMALL proposed
    delta to default_weight (bounded by max_abs_delta, result clamped to [0,1]),
    with rationale, sample size, OOS hit rate + CI, calibration check, and a
    noise-control comparison. Returns a proposal payload; does NOT edit the
    registry."""
```
**Write** the payload via `safe_write_json(OutputNamespace.POLICY,
"signal_weight_proposals.json", ...)` with `observe_only: true` and
`proposed_only: true`. Align the schema with the existing
`weight_tuning_suggestions.json` so downstream tooling can read both.

**Tests:** `tests/test_tuning_proposals.py` — healthy (clear edge → bounded
non-zero proposal with rationale) and degraded (N below `min_n` → no proposal,
status 'insufficient_evidence'); assert the registry file is **unchanged**.

**Acceptance:** a proposals artifact exists; `signal_registry.yaml` byte-identical
before/after. **Boundary:** observe-only — proposes only. **Effort:** M.

> This is the deliberate stopping point for autonomous work. Everything above is
> safe to build and run repeatedly. Do not proceed to Step 5 without sign-off.

---

## Step 5 — Governed apply path (🔒 PROTECTED — owner approval required)

**Goal:** apply approved proposals to `signal_registry.yaml` weights, reversibly
and auditable. **This changes how decisions are computed**, so per `CLAUDE.md`
it must not start until the owner explicitly approves the scope.

**Design (mirror `portfolio_automation/retune_auto_apply.py`):**
- Trigger only when a signed `approved_weight_changes.json` exists (operator-
  created), listing exact `signal_id`s and approved deltas.
- Enforce caps (`max_abs_delta`), clamp to `[0,1]`, refuse anything not present
  in the approved file.
- Write the new registry atomically; snapshot the prior version under
  `config/history/`; write an audit record to `outputs/policy/`.
- Provide a one-command revert to the prior snapshot.

**Tests (required before any merge):** apply within caps; reject
unapproved/over-cap; revert restores byte-identical prior; **regression tests on
`decision_engine` proving decisions change only as intended** and the six
protected scores keep their semantics.

**Acceptance:** opt-in, bounded, fully reversible, audited; full suite green.
**Boundary:** PROTECTED — explicit approval gate. **Effort:** L.

---

## Step 6 — Analysis-health pairing (observe-only; required by `CLAUDE.md`)

**Goal:** satisfy the repo rule that every feature is paired with a health check.

**Do:** extend `.claude/commands/yearly-tool-analysis.md` (backtest cadence is
yearly/lifetime, Quant lens) and/or add a `portfolio-backtest-health` agent that
reads `outputs/backtest/` + `outputs/policy/signal_weight_proposals.json` and
flags: stale results, degenerate output (e.g., all-`unknown` regimes), `n` below
threshold, or calibration slope that has flipped. Add a content_liveness check
for "looks-fresh-but-empty" backtest artifacts.

**Tests:** assert the check returns healthy vs. degraded status on fixtures.
**Boundary:** observe-only. **Effort:** M.

---

## Dependency order

```
Step 0 (baseline run)
   └─> Step 1 (real signals) ─> Step 2 (walk-forward) ─> Step 3 (regime)
                                          └─────────────┬─> Step 4 (proposals)
                                                         └─> Step 6 (health check)
                                                              Step 5 (apply) ⟵ owner approval, after Step 4
```

Steps 1→2→3 are sequential; 4 and 6 depend on 3; 5 depends on 4 **and** explicit
approval.

---

## Cross-cutting verification discipline

- **Out-of-sample only** for any efficacy claim (Step 2 enforces it).
- **Always report a baseline** (the harness's random-entry control) and a
  **noise control** (`--edge 0.0`) so a non-effect reads as a non-effect.
- **Sample-size / significance gating** (min-N + Wilson CIs); suppress
  conclusions below threshold.
- **Multiple-comparisons caution:** testing many `signal_id`s inflates false
  positives — hold out a final test window or correct for it.
- **No look-ahead / survivorship:** point-in-time data and universe only.
- **Determinism:** seeded, reproducible to the byte.

---

## Per-step definition of done

1. New module(s) added; nothing protected modified (except Step 5, post-approval).
2. Healthy + degraded tests added and green; touched files `py_compile` clean.
3. New artifacts carry `observe_only: true` and the correct namespace.
4. Final Report emitted (per `CLAUDE.md`), including VPS validation block.
5. For Step 4+: confirmation that `config/signal_registry.yaml` is unchanged
   (until Step 5's approved, audited edit).
