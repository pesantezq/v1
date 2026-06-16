# Unified Crowd Intelligence Bus — Design Spec

**Date:** 2026-06-16
**Branch:** `feat/unified-crowd-bus`
**Status:** approved by operator (full spec provided), implementing
**Lane:** Simulation/Test (active) — production stays human-gated; no production behavior change without an approved promotion proposal.

## Problem

An audit found **two parallel, live, but disjoint crowd subsystems**:

| | Lane A — `social_intelligence` | Lane B — `crowd_intelligence` |
|---|---|---|
| Nickname | Crowd Radar | FMP Starter crowd lane |
| Source | ApeWisdom (retail) | FMP Starter (analyst/attention/congress/insider/news) |
| Namespace | `outputs/sandbox/discovery/` | `outputs/latest/` |
| Key artifact | `crowd_multi_source_velocity.json` | `crowd_intelligence{,_status}.json` |
| Strength | richly consumed (GUI, memo, Flock, sim-gov, health) | entitlement-aware, 5 categories, 46 symbols |
| Weakness | `source_breadth:1`, cross-source features `null`, dead Reddit path | only 2 GUI consumers, **no health/registry/wiring coverage** |

They never cross-reference, so Lane A's cross-source features (`source_agreement`, `retail_attention_vs_*`) stay `null`, and Lane B is under-consumed and uncovered.

## Goal

A normalized **Unified Crowd Intelligence Bus** that joins both lanes by ticker — preserving both lanes intact — and becomes the *preferred* crowd input (with fallback) for simulation, Flock, watchlist sim, advisory sim context, Portfolio cards, the Crowd page, the daily memo, and health/wiring checks.

## Governance (hard constraints)

- Both existing lanes preserved; all existing artifacts/consumers keep working.
- Unified layer is **additive** and **observe-only at the production boundary**. It may change *simulation* outputs (sim lane is active); production consumes unified logic only after a human-approved promotion proposal. AI/product review may *recommend* but never self-approve.
- No paid dependencies. Do not re-enable paid `social_sentiment` unless the entitlement probe reports `AVAILABLE`.
- Daily consolidated AI/product review stays under the existing **$0.50/day** budget — inject unified-crowd evidence into the existing packet; **no second AI call**.
- No Reddit scraping / no ToS bypass; the dead Reddit-direct path stays disabled and is relabeled honestly.
- Writes go through `OutputNamespace` (`outputs/latest/` = LATEST). `observe_only` hardcoded true on unified artifacts.

## New modules (under `portfolio_automation/crowd_intelligence/`)

- `unified_schema.py` — dataclasses + field contract for the unified row + status; null/0 conventions; `crowd_state` vocabulary.
- `unified_bus.py` — pure join + cross-source metric computation (no I/O).
- `unified_loader.py` — read Lane A + Lane B artifacts into normalized inputs; tolerate missing/stale/empty lanes.
- `unified_writer.py` — assemble + `safe_write_*` the unified artifact + status; non-blocking `run(root)` entrypoint for the pipeline.
- A `read_unified_crowd(root)` loader used by consumers (fallback chain).

## Artifacts

- `outputs/latest/unified_crowd_intelligence.json` — one row per ticker (schema below).
- `outputs/latest/unified_crowd_intelligence_status.json` — health/summary envelope.

### Per-ticker row schema

`ticker, generated_at, source_lanes_present{social_intelligence,crowd_intelligence}, enabled_categories[], disabled_categories[], source_breadth_total, source_breadth_social, source_breadth_fmp, retail_attention_score, fmp_attention_score, news_score, analyst_score, insider_score, congress_score, social_sentiment_score, social_sentiment_status, cross_source_confirmation_score, cross_source_divergence_score, retail_vs_fmp_attention_delta, crowd_confidence, crowd_state, explanation, warnings[], evidence_refs[]`

- `social_sentiment_score`: **`null`** when PLAN_LOCKED (not 0). `social_sentiment_status` ∈ {`AVAILABLE`,`PLAN_LOCKED`,`disabled`,`unknown`}.
- All scores normalized to **0.0–1.0** (attention/news/analyst/insider/congress, confirmation, divergence). `retail_vs_fmp_attention_delta` ∈ **−1.0..+1.0** (retail-minus-fmp).

## Join logic (`unified_bus.py`)

Outer join by ticker. Tickers in only one lane are **kept** (not dropped) with lowered `crowd_confidence` and a `warning` (`lane_a_only` / `lane_b_only`). Inputs:

- Lane A (ApeWisdom) → `retail_attention_score` (from mention_velocity, normalized), `source_breadth_social`, social confidence, evidence_refs.
- Lane B (FMP) → `fmp_attention_score, news_score, analyst_score, insider_score, congress_score`, `enabled/disabled_categories`, `source_breadth_fmp` (= count of active FMP categories), entitlement status, evidence_refs.

### Cross-source metrics (deterministic, documented)

Let `r = retail_attention_score` (0..1, null→treated absent), `f = fmp_attention_score` (0..1), and `ctx = mean(present of {news,analyst,insider,congress})` (FMP support breadth-weighted).

- **`cross_source_confirmation_score`** = weighted blend that is high when retail attention is present/rising AND FMP attention/context is also positive AND breadth_total ≥ 2 AND data fresh. Formula: `confirmation = freshness * min(r, max(f, ctx)) * breadth_factor`, where `breadth_factor = clamp(source_breadth_total/2, 0..1)`. Both sides must be non-trivial → uses `min(r, ...)`.
- **`cross_source_divergence_score`** = high when one side is high and the other weak/missing/low-breadth. Formula: `divergence = freshness * max(r*(1-max(f,ctx)), f*(1-r)) ` boosted when `source_breadth_total == 1`. Captures "retail hype, no FMP confirm" and "FMP active, retail quiet."
- **`retail_vs_fmp_attention_delta`** = `r - f` when both present; sign+magnitude shows alignment/lag/divergence. Null-handling: if one side absent, delta = that-side bias with a `one_lane_only` warning.
- **`crowd_confidence`** = `freshness * (0.4*breadth_factor + 0.3*confirmation + 0.3*max(side_confidences))`, reduced for single-lane.

### `crowd_state` classification (priority order)

1. `insufficient_data` — no usable signal either lane.
2. `confirmed_attention` — confirmation ≥ τ_hi AND breadth_total ≥ 2.
3. `divergent_attention` — divergence ≥ τ_hi.
4. `retail_only_attention` — Lane A present & strong, Lane B absent/weak.
5. `institutional_context_only` — Lane B context present, retail quiet/absent.
6. `broad_context_support` — multiple FMP categories active + mild retail, not strongly confirmed.
7. `caution_low_breadth` — breadth_total == 1 and some signal.

Thresholds in `unified_schema.py` as named constants with rationale comments.

## Health + registry coverage

- New deterministic health assessor (extend the FMP crowd lane + unified bus): checks `crowd_intelligence_status.json` exists + `overall_status ∈ {ok,degraded,failed}`, enabled/disabled categories present, PLAN_LOCKED social_sentiment explained, `unified_crowd_intelligence.json` exists + ≥1 ticker when source data exists, no-crash on empty lane, stale-artifact detection, source-breadth sanity.
- Add `daily-tool-analysis.md` coverage: artifacts-read entries, a Lane-B + Unified body line (same grammar as the 6k Crowd-Radar line), content_liveness for looks-fresh-but-empty.
- Add `artifact_registry.yaml` entries (role/cadence/producer/consumer/namespace) for `crowd_intelligence{,_status}.json` and `unified_crowd_intelligence{,_status}.json`, and `pipeline_wiring_probe` attribution → clears producer-without-consumer debt.

## Consumer migration (fallback chain)

`read_unified_crowd(root)` resolves in order: **(1)** `unified_crowd_intelligence.json` → **(2)** `crowd_intelligence` FMP lane → **(3)** `social_intelligence` ApeWisdom lane → **(4)** honest empty state. Existing direct readers keep working (compatibility path). Updated to prefer unified: Crowd page, Portfolio advisory context, Flock, sim-governance, watchlist sim, daily memo, health/status.

## GUI

- Crowd page: new **"Unified Crowd Intelligence"** section — total/A/B/overlap ticker counts, source breadth, enabled/disabled FMP categories, social_sentiment entitlement status, and top lists (confirmed / retail-only / divergent / institutional-context-only). Label clearly: ApeWisdom = retail attention; FMP = market/context attention; unified = joined.
- Portfolio advisory cards: per-ticker Crowd State / Retail Attention / FMP Context / Confirmation / Divergence-Caution / Explanation. Display/sim context only unless promoted.

## Flock / Simulation / Watchlist

- Flock prefers unified input (retail + fmp attention, confirmation, divergence, breadth, context) instead of ApeWisdom-only when unified data exists.
- Simulation may actively use unified features: create watchlist candidates from `confirmed_attention`, caution-tag `divergent_attention`, rank by confirmation, lower-confidence `retail_only`, higher-confidence broad support. Production watchlist/advisory stays gated behind approved promotion.

## AI review

Inject into the existing once-daily consolidated review packet (no second call): unified crowd health, top confirmed / divergent / retail-only / FMP-only candidates, watchlist+advisory candidates affected, proposed simulation changes, and `ready_for_production_review` candidates if evidence supports. Preserve $0.50/day cap.

## Reddit cleanup

No scraping / no ToS bypass. Dead Reddit-direct path stays disabled; relabel status as `unavailable_no_credentials` / `disabled_no_official_api` / `not_used_in_unified_bus` so it never shows as an active source. ApeWisdom (legitimate free API) remains the live retail source.

## Tests

Unified bus (join both/A-only/B-only/empty/stale/PLAN_LOCKED; confirmation; divergence; crowd_state). Health (Lane B + unified covered; wiring probe no longer flags; stale/missing degrade honestly). Consumers (Crowd page unified section; advisory cards; Flock unified-first; sim/watchlist consume; memo summary). Governance (sim may change sim outputs; production ignores unsanctioned sim changes; AI review includes evidence w/o extra call; $0.50 cap preserved). Reddit (direct path disabled w/o creds; no scraping introduced).

## Out of scope / follow-ups

- Re-enabling paid social_sentiment (only via entitlement probe flip).
- Any production promotion (separate human-gated proposal).
- Renaming the packages to resolve the "crowd" naming collision (documented, not executed here).
