# GUI Improvements — Backlog Completion

**Date:** 2026-07-09
**Status:** implemented
**Owner change scope:** `gui_v2/` presentation layer only. Observe-only consumer.
No decision/scoring/broker code. No artifact schema changes. No recompute.

---

## Context

Closes the three items explicitly deferred from the 4-phase GUI program
(Phases 1-4 already merged). All are pure consumers of existing artifacts.

## Scope

- **A. Per-decision triage badges (Portfolio).** Annotate each decision (by
  symbol, from `decision_triage.json` buckets) with its triage bucket badge —
  Critical / Action candidate / Monitor / Ignore → red / yellow / blue / gray.
  Verb-free labels; bucket→label/severity map in the loader; rendered on the
  mobile `decision_card` header and the desktop `_qrow` detail.
- **B. News Intelligence section (Portfolio).** `news_intelligence.json`
  per-entity packets (themes, risk/catalyst flags, sentiment, summary bullets,
  official/sandbox lanes) as a compact observe-only section: counts header +
  packets sorted flag-relevant-first then by article count, capped at 12 with an
  honest shown/total. Each packet is a collapsed `<details>`. Sentiment is a hint
  token (never a signal); explicit "not a buy/sell recommendation" disclaimer.
- **C. Pipeline Wiring + Discovery Pulse cards (System).**
  `pipeline_wiring_status.json` → wiring-audit card (healthy/unwired/idle/
  not-audited). `discovery_pulse_status.json` → discovery-funnel card (theme +
  watch-candidate counts, FMP/OpenAI budget usage vs caps), covering
  `theme_signals` / `watch_candidates` via tier_a counts. New "Discovery & Wiring"
  group; cards appended only when their artifact exists. Added `green` to the
  shared `_STATUS_MAP`.

## Invariants preserved

- Consumer, never author; recompute nothing.
- Observe-only: triage badge labels + news badges are verb-free; news headlines
  are attributed, collapsed, disclaimered research content (not system advice);
  the forbidden-execution-phrase set does not appear.
- Every figure sourced; honest degradation (absent artifact → omitted;
  null → "—"; capped lists show shown/total).

## Health-check pairing

GUI consumers of existing producers (`decision_triage`, `news_intelligence`,
`pipeline_wiring_status`, `discovery_pulse_status`). Rendering covered by
`portfolio-render-reviewer`; producers have existing daily/discovery-health
coverage. No new producer → no new health check. The every-artifact-has-a-consumer
corollary is advanced (four more artifacts consumed).

## Tests

- `tests/test_gui_portfolio_triage_badges.py` (3)
- `tests/test_gui_news_intelligence.py` (4)
- `tests/test_gui_system_discovery_wiring.py` (5)
