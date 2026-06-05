# Pattern-Loop Historical Signal Reconstruction (sub-project F)

> Reconstructs a multi-year **signal** history from the 5y price archive so the
> walk-forward OOS window matures now, instead of waiting ~315 live calendar days
> (~2027-03). Observe-only producers; the only mutation is the existing E auto-apply,
> which is fail-closed against reconstructed evidence unless the look-ahead audit is clean.

## Why

The loop's OOS window is gated by the span of **signal** dates, not price dates. Live
signals exist only from 2026-04-28. FMP provides 5y of **prices** but not historical
signals (the live scanner uses live quotes and isn't replayable). F reconstructs the
signals the scanner *would have* emitted, point-in-time, from the price archive.

## The pipeline

```
historical_backfill ──► outputs/backtest/historical/<TICKER>_5y.json        (prices)
historical_signal_recon.reconstruct_universe ──► outputs/backtest/recon/<date>/watchlist_signals.json
assert_no_lookahead ──► outputs/backtest/reconstruction_audit.json          (safety gate)
run_loop --history outputs/backtest/recon --live ──► matured oos_window → proposals → auto_apply
```

Runner: `scripts/pattern_loop_reconstruct.sh` (backfill → reconstruct → audit → run_loop).

## What it reconstructs (hybrid fidelity)

Pattern families from trailing OHLCV using `config.json event_thresholds`:
- `STRONG_MOVE_UP/DOWN` — prior-close return `|Δ| ≥ strong_move_pct` (3.0%); direction by sign.
- `VOLUME_SPIKE` — `volume / trailing_avg(vol_window) ≥ volume_spike_factor` (2.0×).

`signal_score` and `confidence_score` are **deferred** (emitted `null`) — weight tuning is
pattern-family-based, which is exactly what's reconstructed. Calibration-based logic stays
gated until a later faithful score pass. Each signal carries `source:"historical_reconstruction"`.

## The look-ahead audit (load-bearing safety)

`assert_no_lookahead` proves the reconstruction used only data ≤ D via a **truncation-equality
invariant**: for sampled dates D, the signals emitted for D from the FULL series must equal
those from the series TRUNCATED at D. Any difference ⇒ future data leaked ⇒
`look_ahead_clean=false`. A unit test injects a future-peek and asserts the audit catches it.

## Auto-apply gate (fail-closed)

`auto_apply.maybe_auto_apply` gate **G2b**: when `evidence_source=="historical_reconstruction"`,
it proceeds only if `reconstruction_audit.look_ahead_clean is True`; otherwise
`status="reconstruction_unverified"` (no apply). Per the operator decision (2026-06-05),
when the audit is clean auto-apply runs **full-auto** on reconstructed evidence — same GPT
approver + score-invariance + drift gates as live evidence.

## Oversight

`backtest_health` surfaces `details.reconstruction` and raises RED
`reconstruction_lookahead_dirty` when the audit failed. `/pattern-loop-analysis` reads the
audit and escalates on a dirty result. Every auto-apply event (incl. on reconstructed
evidence) remains audited and dispatched for review.

## Activation (operator)

1. Run `scripts/pattern_loop_reconstruct.sh` (populates archive, reconstructs, audits, runs loop).
2. Confirm `outputs/backtest/reconstruction_audit.json look_ahead_clean == true`.
3. Confirm the matured `oos_window.folds_possible == true` and review the proposals.
4. Set `config.json backtesting.auto_apply.enabled = true` (+ no kill-switch) to let auto-apply act.

Until step 4, the reconstruction only produces evidence + proposals; nothing is applied.
