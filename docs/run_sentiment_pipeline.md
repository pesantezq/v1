# Social Sentiment Pipeline Runner (Stage 9c4)

## Purpose

`portfolio_automation/social_sentiment/run_sentiment_pipeline.py` is the CLI
runner that wires the social-sentiment pipeline into the daily run as
**Stage 9c4**. It chooses the ticker set, double-gates on config, loads attention
data, and invokes `run_social_sentiment_pipeline`.

---

## Two-Lane Governance

This runner is part of the **simulation-active / production-gated / sandbox-only**
social-sentiment lane. It never feeds `outputs/latest/decision_plan.json` and
never touches any score semantics (`feeds_decision_engine=false`,
`sandbox_only=true`). Output is a single JSON status line to stdout.

---

## Behavior

1. **Config gate (double):** exits early with `status="disabled"` unless both
   `crowd_radar.enabled` and `crowd_radar.simulation_social_sentiment.enabled` are
   true. Both default to `false` for the gate check (default-disabled lane).
2. **Ticker selection:** reads the top-N tickers (by `mention_velocity`, default
   `--top-n 25`) from `outputs/sandbox/discovery/crowd_multi_source_velocity.json`
   (written by Stage 9c1), then appends all current portfolio holdings (shares > 0)
   from `config.json` so the sentiment tilt always covers actual holdings even when
   their crowd velocity is low. De-duplicated, velocity order first.
3. **Attention data:** builds `{ticker: mention_velocity}` from the same velocity
   artifact for the Phase-9 crowd-bus extension.
4. **Run:** calls `run_social_sentiment_pipeline(tickers, root, cfg=crowd_cfg,
   attention_data=...)` and prints a compact status line.

Exit code is `0` for `ok` / `insufficient_data` / `disabled` / `skipped`, else `1`.
If the velocity artifact is missing it logs a warning and proceeds with portfolio
tickers only; if no tickers result it prints `status="skipped"`.

---

## CLI

```bash
.venv/bin/python -m portfolio_automation.social_sentiment.run_sentiment_pipeline \
  --root . --top-n 25 --run-mode discovery
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--root` | `.` | repository root |
| `--top-n` | `25` | top-N velocity tickers to score |
| `--run-mode` | `discovery` | run-mode label |

---

## Related Modules

`pipeline` (`run_social_sentiment_pipeline`, the work it invokes) · Stage 9c1
multi-source crowd runner (produces `crowd_multi_source_velocity.json`) ·
`source_health`.

---

## Tests

Covered under `tests/` with the social-sentiment suite
(`python -m pytest -q tests -k sentiment`).
