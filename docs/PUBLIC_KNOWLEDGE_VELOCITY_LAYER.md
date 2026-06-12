# Public Knowledge Velocity Layer — Crowd Intelligence Radar

> **GUI label:** Crowd Radar · **Internal slug:** `public_knowledge_velocity_layer`
> **Status:** sandbox-only · observe-only · default-disabled

## 1. Purpose

The Public Knowledge Velocity Layer ("Crowd Radar") classifies the **state of
public knowledge** around tickers from API-compliant public discussion sources
(Reddit finance communities first; other forums later).

It does **not** trade meme stocks. It answers a research question:

> *Is public attention forming before the market fully prices a catalyst, or is
> this just late-stage crowd herding?*

It maps observable discussion behavior into eight research states (below), scores
each with a confidence band, and emits a research-oriented `recommended_next_step`.
It never emits a trade instruction.

## 2. Hard boundaries (acceptance invariants)

- **Sandbox-only.** All artifacts are written through `OutputNamespace.SANDBOX`
  (`outputs/sandbox/discovery/`). The run-mode governance layer structurally
  forbids `DAILY` / `MANUAL_UPDATE` / `WEEKLY_REVIEW` from writing this namespace
  (`can_write_sandbox=False`). Only `DISCOVERY` / `BACKTEST` modes may write it.
- **Observe-only.** Every artifact hardcodes `observe_only: true`, `no_trade: true`,
  `not_recommendation: true`, `sandbox_only: true`.
- **No trade path.** Crowd signals may *only* raise/lower a capped
  `crowd_research_priority_score`. They may **not** independently trigger BUY,
  SELL, HOLD, REBALANCE, TRIM, SCALE, PROMOTE, or any allocation change. The
  `recommended_next_step` vocabulary is research-only.
- **No official-portfolio mutation.** The layer never writes
  `outputs/latest/decision_plan.json`, `portfolio_snapshot.json`, or
  `config/signal_registry.yaml`.
- **Disable-able.** Off unless `config.json crowd_radar.enabled=true`. A
  `config/crowd_radar.DISABLED` file or `STOCKBOT_CROWD_RADAR_DISABLED=1` env var
  is a hard kill-switch. Missing Reddit credentials degrade gracefully (status
  `no_credentials`), never crash the daily run.
- **No model training on raw user content.** Raw post bodies are processed
  transiently for ticker extraction / DD scoring and are **not** persisted unless
  the source's `raw_text_storage_allowed=true`. Derived features are persisted.

## 3. Crowd knowledge states

| State | Meaning |
|---|---|
| `dormant_noise` | low activity, low evidence, no signal |
| `emerging_dd` | early rising attention with useful DD / evidence |
| `crowd_validation` | multiple independent authors converging on a thesis with external support |
| `hype_acceleration` | fast mention growth, weak evidence, meme/FOMO language rising |
| `reflexive_squeeze_risk` | social velocity + unusual volume/options/short-interest context |
| `known_news_echo` | crowd reacting *after* already-public news or a price move |
| `crowd_exhaustion` | attention peaked, debate quality dropping, late buyers dominate |
| `contrarian_neglect` | good external/fundamental setup but low crowd attention |

## 4. Recommended next steps (research-only vocabulary)

`ignore` · `monitor` · `send_to_discovery_review` · `requires_news_validation`
· `requires_backtest` · `flag_as_hype_risk`

Never a trade verb.

## 5. Module layout

```
portfolio_automation/social_intelligence/
  base.py                      # RawPost, status enums, observe-only flags
  source_registry.py           # source governance + social_source_compliance.json
  reddit_connector.py          # API-compliant, feature-gated, graceful-disabled
  ticker_extractor.py          # robust extraction + confidence/match_type/fp-risk
  feature_aggregation.py       # mention velocity z-score, dd_density, concentration
  crowd_state_classifier.py    # the 8-state classifier
  social_signal_backtest.py    # forward returns vs SPY/QQQ/sector, sample gating
  public_knowledge_velocity.py # top-level run_* orchestrator + artifact writer
```

## 6. Artifacts (all under `outputs/sandbox/discovery/`)

| File | Contents |
|---|---|
| `social_source_compliance.json` | source-governance registry + compliance status |
| `public_knowledge_velocity.json` | per-ticker velocity features + run metadata |
| `crowd_knowledge_state.json` | per-ticker `crowd_state` + confidence + risk flags |
| `social_signal_backtest.json` | forward-return evaluation by state (sample-gated) |
| `crowd_radar_summary.md` | operator-readable summary |

Every JSON carries the shared envelope: `run_id`, `run_mode`, `created_at`,
`schema_version`, `source`, `source_status`, `data_quality_status`, `observe_only`,
`no_trade`, `not_recommendation`, `sandbox_only`, `warnings`, `records`.

## 7. Status vocabulary (data-quality / source)

`ok` · `disabled` · `degraded` · `no_credentials` · `rate_limited`
· `source_terms_blocked` · `insufficient_data` · `error`

## 8. Configuration

```jsonc
// config.json
"crowd_radar": {
  "enabled": false,                 // master switch (default OFF)
  "sources": ["reddit"],
  "subreddits": ["wallstreetbets", "stocks", "investing"],
  "max_posts_per_source": 200,
  "min_mentions_for_state": 3,
  "min_backtest_sample": 20,        // states below this are "insufficient_data"
  "research_priority_cap": 10.0     // crowd_research_priority_score hard ceiling
}
```

Reddit credentials (optional; absence → `no_credentials`):
`REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT`.

Kill-switch: `config/crowd_radar.DISABLED` file **or** `STOCKBOT_CROWD_RADAR_DISABLED=1`.

## 9. Pipeline integration

`scripts/run_daily_safe.sh` Stage 9c — runs in `discovery` run-mode, wrapped
non-blocking via `run_aux_stage`. When disabled / no credentials it writes a
degraded artifact and no-ops the network. Never aborts the pipeline.

## 9a. Velocity history + market-context join (why states need maturity)

Two classifier inputs cannot come from a single run of post text:

- **Mention velocity** is a z-score, so it needs a baseline. The orchestrator
  persists a rolling per-ticker daily-count ledger
  (`crowd_mention_history.json`, window 20) and feeds the *prior* window to the
  aggregator, then appends today's counts. **Consequence:** the first run (and
  any run against a zero-variance baseline) yields `velocity_z = 0`, so the
  velocity-dependent states (`emerging_dd`, `hype_acceleration`,
  `crowd_exhaustion`) only become reachable after ~2+ days of varied history.
  This is an honest maturity curve, not a bug.
- **Market context** (`external_news_match`, `price_move_before_social_spike`,
  `volume_confirmation`) is joined for free from artifacts the daily pipeline
  already produces — `news_intelligence.json` (entity_key + related_tickers) and
  `watchlist_signals.json` (price_change_pct + volume_spike). No FMP/network call.
  This unlocks `crowd_validation` (external support), `contrarian_neglect`, and
  `known_news_echo`.

**`reflexive_squeeze_risk` stays dormant** until a short-interest / options feed
is wired: `options_or_short_interest_context` is `None` because no free in-repo
artifact carries short interest. Wiring that feed is a clean future follow-up;
the state and its detection logic are already in place.

## 10. Backtest / efficacy gating

No crowd state is treated as reliable below `min_backtest_sample` forward-return
observations; such states are labeled `insufficient_data` and may influence only
"research priority", never confidence in any official score. Forward returns are
compared against SPY, QQQ, a sector ETF when known, and the same-ticker baseline.

## 11. Failure modes

All failure modes degrade to a written artifact with a `source_status` /
`data_quality_status` from §7 and a populated `warnings` list. The official
portfolio outputs are never touched. See `docs/PIPELINE_RUNBOOK.md`.

## 12. Analysis + health coverage

`crowd_knowledge_state.json` is consumed by `/daily-tool-analysis` (market-discovery
lens, `portfolio-discovery-health` agent) with a content-liveness check that flags
"looks-fresh-but-empty" (status `ok` but zero records).
