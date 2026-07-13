# Sim-gov backlog review in daily-tool-analysis (2026-07-13)

## Goal
Turn the one-line `6n. Sim-gov` heartbeat into a per-proposal review of the
pending sim-governance promotion backlog that classifies each proposal,
recommends a **human** action, and **routes testing-ready proposals to human
approval** — while never approving anything (production stays human-gated).

Requested by the operator during a `/run-all-daily` run ("review sim-gov
backlog — add this step to daily run"; "can this route once testing is good and
receive human approval"). Realizes an increment of the standing
`observe_and_iterate` roadmap step.

## Context (state at design time)
The daily AI review (Stage 10e) already scores every simulation candidate and
emits `daily_ai_review_result.json` with per-candidate `verdicts`
(`ready_for_production_review` / `continue_testing` / `reject`),
`ready_candidate_ids`, and `ai_can_approve_production: false`. A pending proposal
is only generated when the AI marks a candidate `ready_for_production_review`, so
the pending queue (`pending_proposals.json`) *is* the ready-for-human set. The
engine runs; the **human hand-off** was the missing surface.

Human approval path (reused verbatim, not reinvented):
`promotion_approvals.record_approval(proposal_id, decision, approver, now, …)` —
`schemas.is_human_approver` rejects AI/heuristic approvers, making
"AI cannot self-approve" a structural invariant — then
`production_application.apply_approved_proposals()` materializes the gated,
default-OFF overlay.

## Approach (chosen: "surface + hand-off pointer", no new artifact)
1. **New pure helper** `portfolio_automation/sim_governance/backlog_review.py` —
   `review_pending_backlog(base_dir=".", now=None) -> dict`.
   - Reads the two existing artifacts (`pending_proposals.json`,
     `daily_ai_review_result.json`); joins by `candidate_id`.
   - Classifies readiness (`ready`/`hold`/`reject`/`unknown`), parses
     `risk_summary`, computes `age_days`, derives `recommendation`
     (`AWAITING_HUMAN_APPROVAL`/`HOLD`/`DROP_CANDIDATE`/`SURFACE_FOR_REVIEW`),
     and a per-ready `approval_hint`.
   - Returns counts (`ready/hold/reject/unknown`), `oldest_ready_age_days`,
     `ai_can_approve_production` (surfaced verbatim), `observe_only:true`,
     `human_gated:true`, and `items[]`.
   - **Invariants:** never writes a file; never approves. Degrades to
     `{available:false, reason}` when `pending_proposals.json` is absent; never
     raises (safe for the non-blocking daily check).
2. **Skill edit** `.claude/commands/daily-tool-analysis.md`:
   - Step-1 compute: add the helper call → `simgov_ready_count`,
     `simgov_hold_count`, `simgov_reject_count`, `simgov_oldest_ready_age_days`,
     `simgov_ai_can_approve`.
   - AMBER: add `simgov_ready_awaiting_approval` (`simgov_ready_count ≥ 1`,
     actionable). RED only if `simgov_ai_can_approve == true` (contract breach).
   - Step-4 body: new item `6n2` renders the per-proposal block + summary +
     routing hand-off (approval command inline for ready proposals).
3. **`run-all-daily` unchanged** — the deeper review rides inside member 1.

Rejected: auto-materialising approval records (would make Claude the approver —
forbidden). A durable `backlog_review.json` artifact (Approach 2) was declined to
keep surface area minimal; `pending_proposals.json` already serves as the queue.

## Data flow
```
Stage 10e ─▶ pending_proposals.json + daily_ai_review_result.json
   review_pending_backlog()  (read-only join + classify + age)
   daily-tool-analysis 6n2   (render per-proposal + summary + hand-off)
   operator reads "AWAITING HUMAN APPROVAL"
   promotion_approvals.record_approval(…, <human>, …)   ← HUMAN ONLY
   production_application.apply_approved_proposals()  ─▶ gated, default-off overlay
```

## Testing
`tests/test_simgov_backlog_review.py`: healthy (6/6 ready), mixed
(ready/hold/reject classification), missing-AI-review (→ unknown /
SURFACE_FOR_REVIEW), degraded (no proposals artifact → `available:false`),
write-nothing safety. Satisfies the healthy+degraded fixture rule.

## Coverage-requirement compliance
Daily cadence → owning skill `daily-tool-analysis` (extended per the CLAUDE.md
table). Lens: process-analyst + market-expert. Both source artifacts already had
6n as a consumer; this deepens it. No new producer/artifact ⇒ no new registry
entry.

## Notes / risks
- Editing `.claude/commands/daily-tool-analysis.md` (a daily-run skill around the
  sim-gov mutator) may trip the oversight-config classifier. This change is
  **pro-oversight** — it adds a human-approval surface, relaxes nothing, and can
  never approve.
- `simgov_ai_can_approve` must stay `false`; the skill treats `true` as a RED
  structural breach.
