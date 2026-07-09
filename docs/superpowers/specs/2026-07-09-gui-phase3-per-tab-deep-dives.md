# GUI Improvements — Phase 3: Per-Tab Deep-Dives

**Date:** 2026-07-09
**Status:** implemented
**Owner change scope:** `gui_v2/` presentation layer only. Observe-only consumer.
No decision/scoring/allocation/broker code. No artifact schema changes. No
recompute.

---

## Program context

Phase 3 of the four-phase GUI program. Phases 1-2 built the primitives and closed
the quant-lens data gaps. Phase 3 deepens the three highest-value tabs (Today →
Portfolio → Memo), completing the operator-workflow data deferred from Phase 2.

## Scope — this PR

- **Today — Decision triage card.** `decision_triage.json` bucket counts
  (critical / action-candidate / monitor / ignore) as a verb-free `status_card`
  in the cockpit. Status: red if any critical, warning if any action-candidate,
  else ok. Action VERBS stay on the Portfolio decision queue per the observe-only
  contract — the cockpit shows only the workload.
- **Portfolio — Triage breakdown on the advisory-queue header.** The same
  `decision_triage` counts as a compact `ui.badge` row directly above the decision
  cards — the prioritization lens where decisions are actually worked. Verb-free.
- **Memo — Memo Coherence panel.** `memo_coherence.json` (the #1 unrendered
  artifact from the Phase-2 audit) surfaced above the memo prose: coherence-status
  verdict + reconciliation issue count, funding figures (portfolio value /
  available cash / reserve), the investor-summary posture paragraph, lead
  opportunity, key risk, and the "what changed" list. Pure consumer — numbers
  verbatim from the artifact; the status→severity map lives in the loader (single
  source), rendered via `ui.sev_rail` / `ui.badge` (no template severity ladder).

## Deferred / not done (with rationale)

- `news_intelligence.json` — the rendered `news_evidence_layer` already covers the
  news surface for the operator; the second layer is lower marginal value. Left
  for a future news-tab pass.
- Discovery funnel + `pipeline_wiring_status` — dev/ops lens, System-tab
  follow-up (noted in the Phase-2 spec).
- Per-decision triage-bucket badges on each Portfolio decision card — a larger
  symbol+decision join; the header breakdown delivers most of the value now.

## Invariants preserved

- Consumer, never author; recompute nothing.
- Observe-only: triage surfaces are verb-free; the memo panel contains no
  execution phrases (checked by the existing memo forbidden-label test).
- Every figure sourced: each surface names its artifact.
- Honest degradation: absent artifacts → the section/card is simply omitted;
  null figures render "—".

## Health-check pairing

GUI consumers of existing producers (`decision_triage`, `memo_coherence`).
Rendering covered by `portfolio-render-reviewer`; the memo/decision producers have
existing coverage. No new producer → no new health check; the every-artifact-has-a-
consumer corollary is advanced (three more artifacts now consumed).

## Tests

- `tests/test_gui_today_triage.py` (4)
- `tests/test_gui_portfolio_triage.py` (2)
- `tests/test_gui_memo_coherence.py` (4)
