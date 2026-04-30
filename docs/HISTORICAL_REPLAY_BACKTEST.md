# Historical Replay / Backtest Calibration

Implementation status: **v1 implemented as of 2026-04-30**.

## Purpose

Accelerate confidence calibration and performance attribution when live resolved
decision history is still sparse (< 20 resolved decisions).

## What This Is

An offline historical replay path that uses approved FMP historical end-of-day
data to generate proxy decision rows, resolve outcomes across 1d/3d/7d windows,
and produce source-tagged historical calibration and attribution reports.

## What This Is Not

- not auto-trading
- not live execution
- not broker integration
- not blind ML training
- not automatic policy promotion
- not a replacement for live outcome history
- not the live decision engine — v1 uses a proxy momentum rule, not exact replay

## Subsystem Location

```
portfolio_automation/historical_replay/
    __init__.py
    replay_data_loader.py        — universe loading + FMP price normalization
    replay_decision_simulator.py — momentum proxy decision generation
    replay_outcome_resolver.py   — forward-price outcome resolution
    replay_reports.py            — calibration + attribution JSON/MD writers
    replay_runner.py             — CLI orchestrator
```

## CLI Usage

```bash
# Full 90-day replay for config holdings
python -m portfolio_automation.historical_replay.replay_runner --days 90

# Dry run (no files written)
python -m portfolio_automation.historical_replay.replay_runner --days 90 --dry-run

# Override symbols and output directory
python -m portfolio_automation.historical_replay.replay_runner \
    --days 90 \
    --symbols AAPL,MSFT,NVDA \
    --output-dir outputs/backtest \
    --window-days 1,3,7
```

## Replay Flow

```text
FMP stable historical EOD prices
    -> replay_data_loader.py (normalize, oldest-first)
    -> replay_decision_simulator.py (proxy momentum rule)
    -> replay_outcome_resolver.py (resolve 1d/3d/7d forward)
    -> outputs/backtest/decision_outcomes_historical.jsonl
    -> replay_reports.py
    -> outputs/backtest/historical_calibration.json + .md
    -> outputs/backtest/historical_performance_attribution.json + .md
```

## Output Artifacts

All replay outputs write to `outputs/backtest/`, never `outputs/policy/`:

| File | Description |
|------|-------------|
| `outputs/backtest/decision_outcomes_historical.jsonl` | All replay decision rows (source="historical_replay") |
| `outputs/backtest/historical_calibration.json` | Hit-rate and return by confidence bucket, decision type, strategy |
| `outputs/backtest/historical_calibration.md` | Human-readable calibration report |
| `outputs/backtest/historical_performance_attribution.json` | Overall attribution + best/worst decisions |
| `outputs/backtest/historical_performance_attribution.md` | Human-readable attribution report |

## Source Tagging

Live and replay rows are always separated by their `source` field:

| Source | File | Written by |
|--------|------|------------|
| `"live"` | `outputs/policy/decision_outcomes.jsonl` | decision_outcome_tracker.py |
| `"historical_replay"` | `outputs/backtest/decision_outcomes_historical.jsonl` | replay_runner.py |

Reports must never silently mix live and replay metrics.

## Proxy Decision Rules (v1)

The v1 simulator uses a simple deterministic momentum rule.
This is NOT the live decision engine.

```
5d_return = (close[i] - close[i-5]) / close[i-5]
sma20     = average(close[i-19 : i+1])

if 5d_return > +3% and close > sma20   → BUY
if 5d_return < -3% and is_holding      → SELL
if 5d_return < -3% and not is_holding  → WAIT
else                                    → WAIT
```

Each row includes `lookback_features` with `return_5d`, `sma20`, `above_sma20`.

## Outcome Resolution

Outcomes are resolved using the longest available forward window:

- Prefer 7d → fallback to 3d → fallback to 1d
- `BUY` / `SCALE`: correct when forward return > 0
- `SELL` / `AVOID`: correct when forward return < 0
- `WAIT`: correct when `abs(forward_return) < 3%`
- `HOLD`: neutral, excluded from hit-rate

## FMP Endpoint Usage

Uses only the approved stable historical EOD endpoint:

- `FMPClient.get_historical_prices(symbol, years=N)` →
  `stable/historical-price-eod/full?symbol=X&from=YYYY-MM-DD`
- No premium endpoints
- Respects existing FMPClient caching and budget guardrails

## Safety Rules

- observe-only only
- replay is offline only; never called from `main.py`
- no execution behavior
- no policy auto-promotion from replay results
- no scoring changes inside replay
- no threshold tuning during replay generation
- no backtest result silently alters live recommendation behavior
- `outputs/policy/decision_outcomes.jsonl` is never read or modified

## Tests

```bash
pytest -q tests/test_historical_replay.py
```

37 tests covering:
- source tag integrity
- live JSONL isolation
- output path separation
- missing data graceful handling
- BUY / SELL / WAIT momentum signal generation
- 1d / 3d / 7d resolution windows
- WAIT threshold logic
- markdown rendering
- CLI dry-run no-write guarantee
- no LLM calls
- no premium FMP endpoint calls

## Limitations (v1)

- Proxy momentum rule does not replicate live decision engine logic
- No FMP api fetch quality differences vs live (caching may differ)
- Calendar-day offset for forward resolution (not strict trading-day offset)
- No GUI integration in v1

## Future Extensions

- GUI read-only integration under a "Backtest" tab
- Source-aware comparison view: live hit-rate vs historical hit-rate
- Longer replay windows (180d, 1y)
- Strategy-specific replay slices
- Replay-aware attribution by validation status and triage bucket
- Operator reports showing live vs replay divergence
