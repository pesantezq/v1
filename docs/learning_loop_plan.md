# Pattern-Recognition Learning Loop — Plan

**Status:** plan only · proposed 2026-05-28 · advisory observe-only

This document describes the learning loop that uses the dynamic universe
top-100 (produced by `portfolio_automation/universe_sanitation.py`) as the
training signal source for a pattern-recognition engine. The goal is to
let the system observe which *kinds* of candidate selections become
winners or losers over time, and use that learned signal to (a) refine
the ExtendedWatchlist promotion gate, (b) inform the decision-plan
prioritization, and (c) surface higher-conviction watch/buy candidates
in the daily memo.

**This is observe-only design.** No part of this loop modifies
`decision_plan.json` semantics, scoring weights, or recommendation
logic. Outputs are advisory: the system surfaces *what it learned* and
*what it would change* — the operator decides whether to apply it.

---

## 1. Why this loop exists

Today the universe sanitation step ranks ~40 tickers per day with a
fixed weight formula (sources 40% + theme 30% + hit-rate 20% + fmp 10%).
Those weights were chosen by hand. The same is true of the
ExtendedWatchlist promotion gate (`confidence ≥ 0.80` AND `≥ 2 themes`)
and the decision engine's scoring weights.

The system has been *collecting* outcome data the whole time
(`signal_outcomes.csv` has 572 rows of forward 1d/3d/7d returns) and
*producing* candidate snapshots (`top100_daily.json` archived per-day in
`outputs/history/<date>/`). What it doesn't do is *close the loop* —
join snapshots to outcomes, learn which rationale tags actually
predict winners, and feed that back into the gates and weights.

That join is the learning loop.

## 2. Inputs already in place

| Input | Path | Cadence | Contains |
|---|---|---|---|
| Daily top-100 snapshots | `outputs/history/<date>/top100_daily.json` (will be, post archival) | daily | symbol × rationale_tags × score × sources |
| Live signal outcomes | `outputs/performance/signal_outcomes.csv` | append-only | ticker × signal_time × forward 1d/3d/7d return |
| Gauge fingerprints | `data/gauge_versions.jsonl` | per knob change | era boundaries for attribution |
| FMP profile cache | `data/fmp_cache/profile_stable_*.json` | weekly | sector enrichment |
| Theme catalog | `data/themes_catalog.json` | manual | theme → ticker map |
| Decision plan archive | `outputs/history/<date>/decision_plan.json` | daily | what the system actually recommended |

What's missing: the *join* — a producer that consumes the above and
emits per-tag efficacy metrics + retune suggestions.

## 3. What gets learned

For each `rationale_tag` produced by `universe_sanitation` (see the
`_build_rationale` function), the loop computes:

- **Cumulative samples**: how many tickers carried this tag across all
  snapshots in the lookback window
- **Hit-rate 1d/3d/7d**: of those tickers, what fraction had a positive
  forward outcome at each horizon
- **Mean return 1d/3d/7d**: average forward outcome
- **Sharpe-like ratio**: mean / stdev of forward returns
- **Significance**: Wilson 95% CI on hit-rate; sample-size warnings
  when n < 30

Tags fall into three categories with different consumer logic:

| Tag family | Examples | Consumer |
|---|---|---|
| **Source presence** | `source:static`, `source:theme_candidate`, `source:fmp_top100`, `source:recent_signal` | Re-weight sanitation score |
| **Quality marker** | `high_theme_confidence`, `multi_source_confluence`, `high_hit_rate_1d` | Adjust ExtendedWatchlist promotion gate |
| **Discovery posture** | `net_new_discovery`, `single_source`, `established_static_seed`, `sector:Technology` | Inform memo "candidates to watch" ranking |

Per-tag efficacy is the building block; all three consumers read the
same metric table.

## 4. Cadences

| Cadence | What it learns | Schedule | Trigger |
|---|---|---|---|
| **Weekly** (7-day rolling) | Short-term noise filter — which tags survived last week's regime | Monday 08:30 UTC (right after `run_weekly_safe.sh` rebuilds top100_watchlist) | New cron entry |
| **Monthly** (30-day rolling) | Mid-term stability — which tag combinations consistently predict | Monday 08:30 UTC on first Monday of the month | Same cron, dispatch logic |
| **Yearly** (365-day rolling) | Regime-aware patterns — does `sector:Technology` work only in bull regimes? | First Monday of January, or on-demand | Same cron, dispatch logic |

The yearly view is the highest-noise but also the most informative for
regime correlation. It's deliberately rare to avoid overfitting to short
runs.

## 5. New artifacts (proposed)

All under `outputs/latest/` (observe-only contract):

- `pattern_efficacy_weekly.json` + `.md` — per-tag hit-rate table over
  last 7 days. Includes sample-size warnings, CIs, and "promote/demote"
  recommendations for tags whose efficacy diverges from neutral by ≥2σ.
- `pattern_efficacy_monthly.json` + `.md` — same shape, 30-day lookback.
- `pattern_efficacy_yearly.json` + `.md` — same shape, 365-day lookback;
  partitioned by gauge fingerprint (so we don't average across retunes).
- `gate_retune_suggestions.json` — operator-facing recommendation:
  *"raise confidence_threshold from 0.80 to 0.82 because tags
  `high_theme_confidence + multi_source_confluence` outperform
  `high_theme_confidence` alone by 18pp"*. Recommendation only — never
  applied automatically.

## 6. New module (proposed)

`portfolio_automation/pattern_learning.py` — single producer module
following the established observability pattern (pure functions, JSON +
MD artifacts, observe-only hardcoded, degrades safely):

```python
build_pattern_efficacy(
    *,
    root: str | Path = ".",
    lookback_days: int = 7,
) -> dict[str, Any]
```

The function:
1. Walks `outputs/history/<date>/top100_daily.json` for the lookback window.
2. For each snapshot row, looks up forward outcomes from
   `signal_outcomes.csv` keyed by (symbol, signal_time) where
   signal_time falls within +24h of the snapshot date.
3. Aggregates by `rationale_tag` → hit_rate / mean_return / Sharpe / CI.
4. Compares each tag's efficacy against the global baseline (universe
   average); flags `+2σ` outperformers and `-2σ` underperformers.
5. Writes `pattern_efficacy_<cadence>.json` + `.md`.

`run_pattern_learning(root, cadence)` is the orchestrator; cadence is
one of {weekly, monthly, yearly}.

## 7. Consumer 1 — Universe sanitation reweighting (advisory)

The sanitation's score formula is currently:

```
score = 0.40·sources + 0.30·theme_conf + 0.20·hit_rate + 0.10·fmp
```

The learning loop emits `gate_retune_suggestions.json:weight_proposals`:

```json
{
  "current": {"sources": 0.40, "theme": 0.30, "hit_rate": 0.20, "fmp": 0.10},
  "proposed": {"sources": 0.35, "theme": 0.35, "hit_rate": 0.22, "fmp": 0.08},
  "rationale": "theme_candidate-only tickers carried +18pp lift over universe baseline; raising theme weight 0.30 → 0.35. fmp_top100 alone showed −4pp; reducing fmp weight 0.10 → 0.08.",
  "n_samples": 234,
  "confidence_interval": "Wilson 95%",
  "would_apply_at": null
}
```

The operator (or a CLAUDE.md-approved auto-apply rule for low-magnitude
changes) accepts/rejects. **Auto-apply is out of scope for v1.**

## 8. Consumer 2 — ExtendedWatchlist gate retune (advisory)

The promotion gate is currently:
- `confidence_threshold` = 0.80
- `reinforcement` = `≥2 themes OR sources: ["direct"]`
- `max_symbols` = 3
- `ttl_days` = 7

The learning loop emits `gate_retune_suggestions.json:promotion_gate`:

```json
{
  "current_threshold": 0.80,
  "proposed_threshold": 0.78,
  "rationale": "Promoted tickers had 64% hit-rate vs 58% for tickers just below threshold; lowering 0.80 → 0.78 captures ~20% more candidates per month without efficacy degradation.",
  "alternate_proposal": {
    "reinforcement": "≥1 theme + 'recent_signal' source",
    "rationale": "single-theme candidates with concurrent recent_signal evidence outperform multi-theme alone."
  }
}
```

Again advisory only — operator applies via `config.json` edit if accepted.

## 9. Consumer 3 — Memo integration (advisory; ship in v2)

The daily memo currently shows up to 5 decisions and 3 risk items. The
learning loop adds a new section:

```markdown
## Watch list — pattern-confirmed candidates (advisory)

These tickers entered the universe today via tag combinations that have
historically outperformed the baseline by ≥10pp at 1d horizon over the
last 30 days:

1. **CRWD** — net_new_discovery + theme:Cybersecurity (n=12, 67% hit-rate)
2. **LMT** — net_new_discovery + theme:Defense (n=8, 75% hit-rate)
3. **XOM** — fmp_scored + theme:Energy_Transition (n=22, 64% hit-rate)

Source: pattern_efficacy_monthly.json. Sample sizes below 30 are flagged.
```

This is the *user-facing* payoff: every morning, the operator sees not
just "QQQ is overweight" but also "these 3 names just lit up tag
combinations that have been working for the past month."

## 10. Risks + guardrails

- **Overfitting to short lookbacks**: weekly tag efficacy on n<30
  samples is noise. Wilson CIs + sample-size warnings prevent the
  weight-proposal generator from emitting low-confidence retunes.
- **Lookback bias**: a tag that worked in a bull regime may not in a
  bear regime. The yearly view explicitly partitions by gauge
  fingerprint AND by `volatility_regime` label so the operator sees
  regime-conditional efficacy.
- **Tag inflation**: too many tags fragment the sample. v1 caps tag
  granularity at the current 11 categories. Adding new tags requires
  explicit user approval and a back-fill of historical snapshots.
- **Auto-apply temptation**: no consumer auto-applies. CLAUDE.md
  protected-semantics rule means score weights and gate logic are
  user-approval-gated. The loop *recommends*; the operator *applies*.

## 11. Phased roadmap

**Phase 1 — Rationale enrichment** ✅ shipped 2026-05-28
- `_build_rationale` in universe_sanitation produces `reason`,
  `rationale_tags`, `contributing_signals` per top-100 row.
- Tag taxonomy is stable; archival in `outputs/history/<date>/` already
  in place via existing pipeline archival.

**Phase 2 — Snapshot+outcome join** (next, ~2 hours)
- Build `portfolio_automation/pattern_learning.py` with
  `build_pattern_efficacy` for the 3 cadences.
- Wire into `run_weekly_safe.sh` (Monday) for weekly+monthly+yearly emits.
- Tests for the join logic (per-tag aggregation, lookback windowing,
  CI computation).

**Phase 3 — Retune suggestion artifacts** (~1 hour)
- Add `gate_retune_suggestions.json` producer that consumes
  pattern_efficacy outputs and emits weight/threshold proposals.
- New content_liveness check: `pattern_learning.tag_coverage` (warns
  when any source tag has fewer than the min-sample-size threshold).

**Phase 4 — Memo integration** (~1.5 hours)
- New `watchlist_scanner/daily_memo.py` section that reads
  `pattern_efficacy_monthly.json` and adds the "Watch list —
  pattern-confirmed candidates" section.
- Compact-contract preserved (max 5 watch entries, all advisory).

**Phase 5 — Operator workflow** (no code, just docs)
- Document the operator review cadence for `gate_retune_suggestions.json`
  in `docs/PIPELINE_RUNBOOK.md`.
- Decide auto-apply threshold (if any): e.g., weight changes < ±0.05
  with n ≥ 200 might be auto-applicable; everything else requires
  explicit approval.

## 12. Open questions for the operator

These need a decision before Phase 2 implementation:

1. **Yearly partition**: should yearly view partition by
   `gauge_version_fingerprint` OR by `volatility_regime` OR both? Both
   is most informative; both is also the smallest sample size per cell.
2. **Memo cap**: how many pattern-confirmed candidates to surface in the
   memo? 3 is most-decisive but might miss diversification; 5 fills the
   existing "max-5 decisions" budget exactly.
3. **Auto-apply policy**: should low-magnitude retunes (e.g., weight
   change < ±0.05 with n ≥ 200) auto-apply, or always require operator
   approval? CLAUDE.md "protected semantics" lean toward always-approve;
   operator workflow may prefer auto-apply for trivia.
4. **Tag taxonomy lock**: is the current 11-tag taxonomy frozen for v1,
   or are there additional tags worth adding before Phase 2 starts?
   (e.g., `news_tailwind`, `earnings_within_7d`, etc.)

---

_This plan is observe-only architecture. All consumer changes
(re-weighting, gate retune, memo addition) ship as advisory artifacts
the operator reviews before applying. The learning loop never modifies
decision_plan.json semantics directly._
