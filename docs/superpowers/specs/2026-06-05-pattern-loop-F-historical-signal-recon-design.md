# Sub-project F — Historical Signal Reconstruction (look-ahead-safe) — Design

- **Date:** 2026-06-05
- **Branch:** fresh branch off main `6fbe45a6` (main now contains the merged Foundation+D+E; the old feature branch is behind)
- **Status:** Design approved; awaiting spec review → writing-plans.
- **Sequence:** Foundation + D + E (merged to main) → **F (this)**.

## Why F exists

The Pattern-Loop's walk-forward window is gated by the span of **signal** dates, not price
dates. Only ~39 days of live signals exist (2026-04-28→), so `oos_window.folds_possible`
is false until ~2027-03 and the loop produces zero proposals — auto-apply (E) is a no-op.

FMP provides 5y of **prices**, but not historical signals (the live scanner generates
those, and it pulls live quotes — it is not built for point-in-time replay). F reconstructs
a multi-year historical **signal** set from the price archive, point-in-time, so the OOS
window matures NOW. The existing loop then runs unchanged on it (walk-forward → proposals →
E auto-apply).

**Operator decisions (2026-06-05):** reconstruct **pattern families now, score/confidence
later** (hybrid); and once F's window is mature AND the look-ahead audit is clean,
auto-apply runs **full-auto on reconstructed evidence** (same GPT+gates path as live). The
**look-ahead audit is the load-bearing safety gate** — reconstructed evidence may drive an
auto-apply ONLY when it passes.

## Cardinal risk: look-ahead bias

A reconstruction is valid only if each date D's signals use ONLY data available as of D.
If future data leaks into signal generation, the "OOS" folds are fake-clean and auto-apply
would tune weights to hindsight → confidently worse live picks. F controls this two ways:
(1) the reconstructor consumes only trailing rows ≤ D by construction; (2) an explicit
**truncation-equality audit** proves it. Forward returns used as outcomes are future by
definition — that is the label, not leakage.

## Non-goals
- No change to scoring math / `decision_engine.py` / score semantics.
- No faithful `signal_score`/`confidence` reconstruction (deferred; emitted null/proxy).
- No live-scanner refactor (rejected: large, high look-ahead surface).
- No new mutating behavior beyond the existing E path (F only produces evidence + one
  fail-closed gate addition).

## Architecture (isolated, testable units)

```
historical_backfill (REUSE) → outputs/backtest/historical/<TICKER>_5y.json   [prices]
        │
historical_signal_recon (NEW, pure) ── point-in-time, trailing rows ≤ D ──► reconstructed
        │                                                                     snapshots:
        │                                                  outputs/backtest/recon/<date>/watchlist_signals.json
        ├── assert_no_lookahead (NEW) ──► outputs/backtest/reconstruction_audit.json {look_ahead_clean}
        ▼
run_loop --history outputs/backtest/recon  (EXISTING loader) ──► oos_window matures
        → walk_forward folds → signal_weight_proposals → auto_apply (E)
                                                              └── NEW gate: reconstructed evidence
                                                                  requires look_ahead_clean==true
```

### 1. Price archive (reuse)
`portfolio_automation.historical_backfill.run_historical_backfill(root, ...)` already pulls
5y daily OHLCV via FMP → `outputs/backtest/historical/<TICKER>_5y.json`. F runs it for the
current universe (`build_universe`, ~40 tickers) before reconstruction. Archive is currently
empty (weekend cron deferred); F populates it. No new code.

### 2. `backtesting/historical_signal_recon.py` (new, pure)
- `reconstruct_signals(price_rows, *, thresholds, today=None) -> list[dict]`: given one
  ticker's chronologically-sorted OHLCV rows, walk each date D using ONLY rows ≤ D and emit
  a signal when a pattern fires, using `config.json event_thresholds`:
  - `STRONG_MOVE_UP/DOWN` — trailing 1-day return `|Δ| ≥ strong_move_pct (3.0%)`; direction
    by sign (feeds Step-1b directional registry ids).
  - `VOLUME_SPIKE` — `volume / trailing_avg_volume(N) ≥ volume_spike_factor (2.0)`.
  - (BREAKOUT_PROXY / VOLATILITY_EXPANSION optional, same pattern; YAGNI — start with the
    two families that carry the live signal volume.)
  - Emit `{ticker, scan_time: D.isoformat(), alert_basis:[...], pattern,
    patterns:[...], signal_score: null, confidence_score: null,
    source: "historical_reconstruction"}`. (`_map_basis`/`_representative_pattern` in
    `signal_sources` already turn alert_basis → registry families; F emits the same
    `alert_basis` tags the live scanner uses: `price_move`, `volume_spike`.)
- `reconstruct_universe(archive_dir, recon_dir, *, thresholds) -> dict`: iterate the
  archive, write snapshot-compatible `recon_dir/<date>/watchlist_signals.json`
  (`{results:[...]}` — the shape `signal_sources.load_historical_signal_snapshots` reads),
  return a summary `{tickers, dates, signals_total, span_days}`. Pure/total; per-ticker
  failure skipped, never raises.

### 3. Look-ahead audit — `assert_no_lookahead` (new, the safety gate)
For a sample of dates D, assert: signals computed from the FULL series, filtered to date D,
EQUAL signals computed from the series TRUNCATED at D. Any mismatch ⇒ leakage. Writes
`outputs/backtest/reconstruction_audit.json {look_ahead_clean: bool, dates_checked,
mismatches:[...], generated_at}`. Backed by a unit test that INJECTS a future-peek
(a reconstructor variant that reads D+1) and asserts the audit catches it.

### 4. `run_loop` integration
No signature change needed — `--history outputs/backtest/recon` uses the existing
`load_historical_signal_snapshots`. Forward-return outcomes come from the archived prices
via the backtester's provider (offline/deterministic over the archive; no live calls). The
reconstructed span (5y) clears the 315-day window → `folds_possible=true` → real folds →
proposals. A thin `scripts/pattern_loop_reconstruct.sh` (mirrors `pattern_loop_recheck.sh`)
runs backfill → reconstruct → audit → `run_loop --history recon`.

### 5. E gate addition (fail-closed)
`backtesting/auto_apply.py` gains one precondition (after G2): if the run's evidence is
reconstructed (provenance/source indicates) AND `reconstruction_audit.look_ahead_clean` is
not true → `status="reconstruction_unverified"` (no-op). When clean, proceeds full-auto per
the operator decision. Surfaced by `backtest_health` (new detail `reconstruction`) and the
`/pattern-loop-analysis` skill.

## Data flow
backfill prices → reconstruct point-in-time signals → audit look-ahead → run_loop on recon
snapshots → matured oos_window + real proposals → auto_apply (gated on audit clean) →
GPT approver + score-invariance gates → reversible registry apply.

## Error handling
Every new function pure/total, degrades to a status dict, never raises. `run_loop`
integration + recon step non-blocking. Missing archive → recon summary `{status:"no_prices"}`.
Audit failure → `look_ahead_clean=false` → auto-apply fail-closed.

## Testing
- `tests/test_historical_signal_recon.py`: threshold correctness (a +3.1% day →
  STRONG_MOVE_UP; −3.1% → STRONG_MOVE_DOWN; 2.1× volume → VOLUME_SPIKE; sub-threshold →
  nothing); determinism; trailing-window-only (date D unaffected by appending future rows);
  empty/short series → no signals, no raise.
- `tests/test_lookahead_audit.py`: clean reconstructor → `look_ahead_clean=true`; a
  future-peek variant → audit FAILS with mismatches (the critical safety test).
- `tests/test_recon_matures_window.py`: a synthetic multi-year archive → reconstruct → the
  loaded snapshots make `oos_window_status(...).folds_possible == true` and `walk_forward`
  yields ≥1 OOS fold.
- `tests/test_auto_apply.py` extension: reconstructed evidence + `look_ahead_clean=false`
  → `reconstruction_unverified`; + clean → proceeds (with injected approver).
- Full suite green.

## Files
**New:** `backtesting/historical_signal_recon.py`, `scripts/pattern_loop_reconstruct.sh`,
`tests/test_historical_signal_recon.py`, `tests/test_lookahead_audit.py`,
`tests/test_recon_matures_window.py`, this spec + plan, `docs/PATTERN_LOOP_RECONSTRUCTION.md`.
**Modified:** `backtesting/auto_apply.py` (reconstruction-unverified gate),
`backtesting/backtest_health.py` (surface `reconstruction`),
`.claude/commands/pattern-loop-analysis.md` (read recon audit),
`docs/CHANGELOG_DECISIONS.md`, `.agent/project_state.yaml`.

## Risks
- Reconstructed signals are a price-only PROXY (no score/confidence) — acceptable because
  weight tuning is pattern-family-based and the families ARE price/volume events; calibration
  logic stays gated until a faithful score pass.
- Threshold/fidelity drift vs the live scanner — documented `event_thresholds` are the
  contract; differences mean the weights tune a faithful proxy, not the exact live emitter.
- Look-ahead is the existential risk — mitigated by construction + the truncation-equality
  audit as a hard auto-apply precondition (fail-closed).
- Auto-apply then mutates protected weights autonomously on reconstructed evidence — this is
  the operator-approved E exception; every event remains audited, health-flagged, and
  dispatched for review (oversight preserved).
