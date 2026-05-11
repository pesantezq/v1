# GUI Operator Cockpit

## Overview

The **Operator Cockpit** is the Streamlit GUI in `gui/app.py`. It is a **read-only** dashboard for the operator: it surfaces structured artifacts produced by the rest of the system and never mutates portfolio state, watchlists, scoring, allocation, recommendations, or decisions.

This document describes the cockpit redesign direction (`gui_operator_cockpit_redesign`) and the first implementation slice, the **Automatic Promotion Review** panel (`gui_automatic_promotion_review_panel`).

## Intended Users

| User | Mode | What they see |
|---|---|---|
| **Beginner / non-technical operator** | Default cards-first view | Card grids with plain-English explanations and status badges. No raw JSON unless they expand it. |
| **Power operator** | Expanders on each card | Full per-candidate evidence summary, gate detail, risk/catalyst flags, replay/memory/operator context. |
| **Developer** | `st.json` blocks behind expanders | Raw decision records, gate dictionaries, log entries, source artifact paths. |

The same page serves all three modes — the difference is whether the user clicks the expanders.

## Pages

| Page | Status | Purpose |
|---|---|---|
| Dashboard | existing | High-level operator overview |
| Decision Center | existing | Decision plan + explanations + AI validation |
| **Automatic Promotion** | **new (this slice)** | Sandbox research governance: how discovery candidates were auto-classified |
| Run Controls | existing | Pipeline launchers |
| Outputs | existing | File browser for `outputs/` |
| Watchlist | existing | Watchlist editor (read-only view of state) |
| Run History | existing | Past runs from SQLite `run_history` |
| API Health | existing | API key + provider health |
| Config Editor | existing | View / edit `config.json` |
| Prompts | existing | View prompt templates |
| Logs | existing | Recent log files |
| Diagnostics | existing | Env + tests + maintenance |

## Reusable UI Helpers (added in this slice)

Implemented in `gui/app.py` and intended for reuse across future cockpit pages:

| Helper | Purpose |
|---|---|
| `render_status_badge(text, tone)` | Inline HTML status badge; tones: `good` / `warn` / `bad` / `neutral` |
| `render_metric_card(title, value, subtitle, badges)` | Card with label, value, subtitle, and optional badges |
| `render_section_header(title, subtitle)` | Section header with caption |
| `render_empty_state(message, icon)` | Friendly empty-state info panel |
| `render_safety_flags(safety_flags, missing)` | Safety boundary panel — one badge per expected flag |
| `render_candidate_card(decision, key_prefix)` | Single candidate card with expander for full detail |
| `_status_tone(status)` | Maps a status string to a card tone |
| `_status_explanation(status)` | Plain-English one-liner per status |

These helpers reuse the existing `_operator_dashboard_css()`, `_badge()`, and `_render_operator_card()` foundations, so all cockpit pages share the same visual language.

### Status color semantics

| Status | Tone | Hex |
|---|---|---|
| `MONITOR` | green / good | `#146c43` on `#e7f7ee` |
| `NEEDS_REVIEW`, `WATCH` | yellow / warn | `#8c5b00` on `#fff4de` |
| `REJECTED` | red / bad | `#a61b1b` on `#fde8e8` |
| `EXPIRED`, neutral | gray / info | `#344054` on `#edf2f7` |

## Automatic Promotion Review panel

### Input artifacts (sandbox, read-only)

| Artifact | Purpose |
|---|---|
| `outputs/sandbox/discovery/automatic_promotion_candidates.json` | Full structured snapshot — decisions, gates, gate_summary, safety flags |
| `outputs/sandbox/discovery/automatic_promotion_summary.md` | Producer-rendered Markdown summary (shown verbatim behind an expander) |
| `outputs/sandbox/discovery/automatic_promotion_decisions.jsonl` | Append-only audit log; last 50 lines shown behind an expander |

All three loaders degrade safely on missing, empty, malformed, or non-object input.

### Loader API

| Loader | Returns |
|---|---|
| `load_automatic_promotion_candidates(root)` | `dict` with `available` flag |
| `load_automatic_promotion_summary_markdown(root)` | `str` (empty if missing) |
| `load_automatic_promotion_decisions(root)` | `list[dict]` (malformed lines skipped) |
| `load_automatic_promotion_data(root)` | Aggregator with stable shape; never raises |

The aggregator is also wired into `load_operator_dashboard_data()` under the key `automatic_promotion`, so any cockpit page can read it in one call.

### Page layout

1. **Header** — page title + safety disclaimer ("This is sandbox research governance only. It is not a buy/sell/hold recommendation.")
2. **Top metrics row** (6 cards): Total Reviewed, Moved to Monitor, Needs Review, Rejected, Expired, Safety Status.
3. **Safety Boundary panel** — one badge per expected safety flag (`observe_only`, `no_trade`, `not_recommendation`, `discovery_only`, `no_portfolio_mutation`, `no_watchlist_mutation`, `no_decision_override`, `no_score_mutation`, `no_allocation_mutation`). A warning is shown if any flag is missing or False.
4. **What does each status mean?** — expander with beginner-friendly explanations.
5. **Grouped candidate sections** — one section per allowed status (MONITOR / NEEDS_REVIEW / REJECTED / EXPIRED). Each candidate is a card with an expander that reveals evidence score, corroboration, news relevance, source diversity, gates passed/failed, risk/catalyst flags, replay/memory/operator context, and raw JSON.
6. **Producer-rendered summary** — verbatim `automatic_promotion_summary.md` (collapsed by default).
7. **Recent decisions (audit log)** — last 50 JSONL records (collapsed by default).
8. **Governance gates in effect** — `gates` dict (collapsed by default).
9. **Footer** — generated_at, run_mode, run_id, source artifact paths.

## Safety Boundaries

The cockpit is strictly read-only:

- Never writes artifacts
- Never mutates portfolio state, watchlists, allocation, scoring, recommendations, or decision-engine state
- Never executes trades or calls broker/API endpoints
- Never makes LLM/AI calls
- Never invents action labels — the aggregator maps any unknown `proposed_status` value into an `OTHER` bucket; it never coerces upstream values into BUY/SELL/HOLD/ACTIONABLE/PROMOTED/VALIDATED
- The Automatic Promotion page never uses trading-instruction phrases (`buy now`, `sell now`, `add to watchlist`, etc.) outside the fixed safety disclaimer wording — verified by `tests/test_gui_automatic_promotion.py::TestGUIHelperImportSafety::test_helpers_avoid_forbidden_trading_language`.

## Tests

File: `tests/test_gui_automatic_promotion.py`
Count: 31 tests across 7 test classes

Coverage: missing/malformed/non-object/empty input degradation, valid artifact parsing, aggregator stable shape, candidate grouping by proposed status, safety flag detection (all-true, missing, explicit False), `load_operator_dashboard_data` wiring, read-only invariants (loaders do not write to disk; do not touch LATEST/POLICY/PORTFOLIO), aggregator content safety (no forbidden status emission, defensive `OTHER` bucket for unknown statuses), GUI helper smoke tests (module compiles, all helpers present, no forbidden trading language in cockpit helpers, page registered in nav).

## Dashboard cockpit summary (slice 2)

The Dashboard landing page now opens with an at-a-glance **Cockpit Summary** card grid that uses the same reusable helpers as the Automatic Promotion page. The summary is additive — every existing dashboard widget below it remains untouched.

### Cards

| # | Card | Source artifact | Tone logic |
|---|---|---|---|
| 1 | **Portfolio Status** | `outputs/latest/system_decision_summary.json` (`system_health` / `overall_health`) | `good` healthy / `warn` degraded / `bad` critical / `neutral` unknown |
| 2 | **Today's Market Narrative** | `outputs/latest/market_narrative_daily.json` (`top_headline`) | `good` when present, `neutral` when missing |
| 3 | **Decision Plan** | `outputs/latest/decision_plan.json` (`decisions` length) | `good` when count > 0, `neutral` when zero |
| 4 | **Data Quality** | `outputs/latest/data_quality_report.json` | `good` healthy / `warn` degraded / `bad` critical |
| 5 | **News Evidence** | `outputs/latest/news_evidence_layer.json` (`ticker_contexts` length) | `good` when populated, `neutral` when empty |
| 6 | **Automatic Promotion** | Aggregator over `outputs/sandbox/discovery/automatic_promotion_*` (`monitor_count`, `needs_review_count`) | `good` when monitor > 0, `warn` when review > 0, `neutral` otherwise |
| 7 | **Memo Delivery** | `outputs/latest/memo_delivery_status.json` | `good` sent / `warn` pending / `neutral` skipped/disabled |
| 8 | **Safety Boundary** | Fixed reminder | always `good` (system constant) |

The summary helper is `_render_cockpit_summary_grid(bundle)` in `gui/app.py`. It accepts the `bundle` already loaded by `load_operator_dashboard_data()`, so no extra I/O is required on the dashboard page.

### New loaders backing the summary

| Loader | Artifact |
|---|---|
| `load_news_evidence_layer(root)` | `outputs/latest/news_evidence_layer.json` |
| `load_market_narrative_daily(root)` | `outputs/latest/market_narrative_daily.json` |

Both are read-only, degrade safely on missing/malformed/non-object input, and are wired into `load_operator_dashboard_data()` under the keys `news_evidence_layer` and `market_narrative_daily`.

## Future Cockpit Roadmap

Remaining slices that can build on the helpers added so far:

1. ✅ ~~Dashboard landing card refresh~~ — complete (slice 2)
2. **News Evidence Layer** panel — beginner-friendly view of `outputs/latest/news_evidence_layer.json` with ticker context cards
3. **Market Narrative** panel — daily/weekly/monthly narrative surfaces with the same card-first style
4. **Unified Discovery Sandbox** panel — combine emerging/rejected candidates, news enrichment, replay, and automatic promotion into one research view

None of these require backend changes — they all read existing artifacts and use the helpers added so far.
