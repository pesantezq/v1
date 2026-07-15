# One-Shot Operator Approval Packet

**Date:** 2026-07-15
**Status:** design approved (operator, 2026-07-15); implementation pending
**Package:** `portfolio_automation/sim_governance/` + `gui_v2/`
**Ships:** GATED (`sim_governance.approval_packet.enabled=false`)
**Builds on:** PR #5 (merged to `main` as `77b6a89e`) — the bounded GPT simulation
auto-approval channel and the evening governance digest.

## 1. Purpose

Give the operator a single consolidated "approval packet" per governance cycle that
covers **both** governance tiers at once:

- **Tier-a (simulation):** items the GPT auto-approval channel already auto-applied to
  simulation state and are still inside their veto window — the operator may **veto**.
- **Tier-b (production):** production-promotion candidates the AI review marked
  `ready_for_production_review` (materialized as `pending_proposals`) — the operator may
  **approve or reject**, per-item or in bulk.

Delivery is by **evening email (read-only summary + deep link)**; all mutating actions
happen in the **authenticated GUI**. "One-shot" means one consolidated packet per cycle
awaiting a single operator visit — not a single-use token.

This is a **composition + notification + UX layer** over gates that already exist. It adds
**no new production-mutation path**: tier-b approval flows exclusively through the
already-sanctioned, human-gated `promotion_approvals.record_approval`.

## 2. Non-negotiable invariants

> **The approval packet consolidates and notifies. It never creates a new way to mutate
> production. Every production effect still requires an authenticated human approval
> through the existing `promotion_approvals` gate, which `schemas.is_human_approver`
> guards.**

- The packet **builder is read-only** (`observe_only: true`); it aggregates artifacts and
  writes only its own packet artifact. It never approves, vetoes, promotes, or mutates
  watchlist/strategy/production state.
- Tier-b production mutation happens **only** via the existing
  `promotion_approvals.record_approval(...)`, guarded by
  `schemas.is_valid_approval_record` → `is_human_approver`. The GUI passes the
  **authenticated session operator identity**, never a value from the form. Auto markers
  (`auto`/`gpt`/`system`/`llm`) remain rejected — unchanged, regression-tested.
- "Bulk approve" is a convenience **loop over per-item `record_approval` calls**; each item
  is individually validated and individually audited. Bulk is not a new gate.
- Tier-a (sim veto) reuses the merged `/dashboard/governance/veto` route +
  `auto_approval.record_veto` unchanged. Tier-a and tier-b never cross-contaminate.
- `decision_engine.py` and all score semantics (`signal_score`, `confidence_score`,
  `effective_score`, `conviction_score`, `final_rank_score`, `recommendation_score`) are
  untouched.
- Ships **gated**: `approval_packet.enabled=false`. Disabled ⇒ no Step 5c, approve routes
  respond disabled, email is unchanged. Fully backward compatible.
- **No new CLAUDE.md sanctioned exception** is required: this does not add a 4th mutating
  path; it reuses the existing human-gated promotion workflow.

## 3. Architecture & data flow

Single source of truth: the packet artifact is read by **both** the email and the GUI so
they cannot drift.

```
daily_governance_run pipeline (existing)
  ├─ Step 5b (auto_approval) → outputs/policy/auto_approval_audit.json   (tier-a source)
  └─ Step 5  (ai review)     → outputs/promotion_review/pending_proposals.json (tier-b source)
        │
        ▼  NEW Step 5c  (inert when disabled; wrapped try/except; never sinks the run)
  approval_packet.build_operator_packet(base_dir, now, config)
        → reads auto_approval_audit.json (within-veto items) + pending_proposals.json (pending)
        → writes outputs/promotion_review/operator_approval_packet.{json,md}
        │
        ├─ governance_digest email  → reads packet counts + injects approval_page_url deep link
        └─ GUI governance tab       → reads packet → renders actionable two-tier panel
```

## 4. Components

### New

| File | Purpose |
|---|---|
| `portfolio_automation/sim_governance/approval_packet.py` | Pure builder `build_operator_packet(base_dir, now, config) -> dict` + `write_operator_packet(packet, *, base_dir) -> dict`. `observe_only: true`. Returns a degraded dict `{schema, observe_only, generated_at, error, tier_sim: [], tier_production: [], counts: {...}}` on failure. |
| `gui_v2/data/dash_approval_packet.py` | Thin reader: loads `operator_approval_packet.json`, shapes it for the template (best-effort, degraded-safe). |
| `tests/test_approval_packet.py` | Builder unit tests. |
| `tests/test_gui_governance_approve.py` | GUI approve-route tests. |

### Modified

| File | Change |
|---|---|
| `portfolio_automation/sim_governance/daily_governance_run.py` | New **Step 5c** after Step 5/5b: build + write the packet. Inert when `approval_packet.enabled=false`; `try/except` non-blocking; result recorded in `status["stages"]["approval_packet"]`. |
| `portfolio_automation/sim_governance/governance_digest.py` | Accept/derive `approval_page_url`; render a prominent "Review & approve today's packet →" link (text + HTML). Email stays read-only. |
| `gui_v2/app.py` | New `POST /dashboard/governance/approve` route family, mirroring the `/veto` security spine. |
| `gui_v2/templates/dashboard/governance.html` | "Today's Approval Packet" panel: tier-a veto items (existing controls) + tier-b candidates with per-item approve/reject and bulk approve-all/reject-all (with per-item exclusion checkboxes). |
| `gui_v2/data/dash_governance.py` | Provide the packet-derived context to the governance page (or delegate to `dash_approval_packet.py`). |
| `config.json` | New `sim_governance.approval_packet` block (see §8). |
| `docs/SIM_GOVERNANCE.md` | Document the packet, the two tiers, and the activation runbook. |
| `.claude/commands/daily-tool-analysis.md` | Health coverage (see §7). |
| `.claude/agents/portfolio-learning-loop-health.md` | Mirror the packet checks. |

## 5. Packet schema (`operator_approval_packet.v1`)

```json
{
  "schema": "operator_approval_packet.v1",
  "observe_only": true,
  "generated_at": "<iso8601>",
  "cycle_id": "<date or run id>",
  "approval_page_url": "<deep_link_base>/dashboard/governance",
  "tier_sim": [
    {
      "event_id": "...", "candidate_type": "watchlist|strategy", "symbol_or_strategy": "...",
      "applied_at": "...", "veto_deadline": "...", "within_veto_window": true,
      "confidence": 0.9, "gpt_reasoning": "...", "gate_summary": "...",
      "before_state": {...}, "after_state": {...},
      "target_lane": "simulation", "feeds_decision_engine": false,
      "status": "auto-applied in simulation · veto available"
    }
  ],
  "tier_production": [
    {
      "proposal_id": "...", "workflow": "advisory|watchlist", "symbol": "...",
      "what_changed": "...", "why_changed": "...", "before": {...}, "after": {...},
      "risk_impact": "...", "confidence": 0.8, "data_quality": "...",
      "evidence": [...], "approval_status": "pending",
      "status": "pending human review"
    }
  ],
  "counts": {"tier_sim_within_veto": 0, "tier_production_pending": 0}
}
```

Every item carries an explicit `status` string — never a bare "approved".

## 6. GUI action semantics

### Route: `POST /dashboard/governance/approve`

Mirrors the merged `/dashboard/governance/veto` security spine exactly:

1. `from operator_control import audit_log`.
2. `if not _operator_edit_enabled():` → audit + disabled redirect.
3. `if not _same_origin(request):` → audit + rejected redirect (CSRF).
4. Resolve the **actor from the authenticated session**, never from the form body.
5. Validate the action + inputs.
6. Domain call(s):
   - **Per-item:** `record_approval(proposal_id, decision, approver=actor, now, base_dir=..., notes=optional)`.
   - **Bulk:** for each `proposal_id` in `load_pending_proposals()` minus `excluded_ids`, call
     `record_approval(...)`; aggregate `{ok, reason}` results into a summary flash message.
7. `audit_log.record_event(...)` on **every** branch (disabled, csrf-reject, per-item
   result, bulk summary, invalid-input).
8. Idempotent: a `proposal_id` already in `approved_proposal_ids()` / `rejected_proposal_ids()`
   is skipped with reason `already_decided`.
9. `return RedirectResponse("/dashboard/governance", status_code=303)` (POST→redirect→GET).

Tier-a veto continues to use the existing `/dashboard/governance/veto` route unchanged.

## 7. Health + analysis coverage (CLAUDE.md requirement)

- **Cadence:** daily (built in the daily governance run) → owning skill
  `daily-tool-analysis` (Step 1 artifacts read + Step 3 dispatch + Step 4 body grammar +
  a `content_liveness` check because the packet can emit "looks-fresh-but-empty").
- **Lenses:** process-analyst (operator decision-queue aging) + developer (liveness).
- **Signals / statuses:**
  - packet present and fresh (built this cycle) → informational.
  - `content_liveness`: packet file fresh but both tiers empty when upstream artifacts are
    non-empty → AMBER (build/wiring fault).
  - tier-b candidate pending and unactioned for > `stale_pending_days` (config, default 3) →
    **AMBER** (decision-queue aging).
  - **RED integrity:** packet marks a tier-b item as approved/rejected but no matching valid
    record exists in `promotion_approvals` — surfaces any drift between the packet view and
    the authoritative gate.
- Mirrored into `portfolio-learning-loop-health` (Layer: operator approval queue).
- A completed approval/rejection/veto is the control **working** — health agents VERIFY
  against the audit records; they never revert legitimate decisions.

## 8. Config (`sim_governance.approval_packet`)

```json
"approval_packet": {
  "enabled": false,
  "deep_link_base": "https://dashboard.portfolio-ops-center.com",
  "stale_pending_days": 3,
  "note": "One-shot operator approval packet. Read-only builder + GUI approve route reusing the human-gated promotion_approvals path. Ships GATED. Disabled => no Step 5c, approve route responds disabled, email unchanged. No new production-mutation path."
}
```

Enablement (final human step): flip `enabled=true`; optionally set `deep_link_base`. The
evening email link additionally requires the existing `evening_digest.enabled` +
`GOVERNANCE_DIGEST_ENABLED=1`.

## 9. Test plan

- `tests/test_approval_packet.py`
  - two-tier assembly from sqlite/json fixtures; correct `counts`.
  - only within-veto tier-a items and only `pending` tier-b items are included.
  - dedup / stable ordering (advisory before watchlist for tier-b).
  - degraded dict (missing/corrupt artifacts) with `observe_only: true` and empty tiers.
  - `observe_only` is hardcoded true; builder writes nothing but its own artifact.
- `tests/test_gui_governance_approve.py`
  - per-item approve and reject call `record_approval` with the **session** actor and the
    right `decision`.
  - bulk approve-all / reject-all with `excluded_ids` loops correctly and skips exclusions.
  - `_operator_edit_enabled()` false ⇒ blocked, no `record_approval`, audit recorded.
  - `_same_origin` false ⇒ CSRF-rejected, audit recorded.
  - **a form-supplied `approver` is ignored**; an `auto`/`gpt` actor would be rejected by
    `is_human_approver` (assert the gate still rejects it).
  - idempotency: an already-decided proposal is skipped with `already_decided`.
  - `audit_log.record_event` is invoked on every branch.
- `tests/test_governance_digest.py` (extend): deep link present when enabled, absent/blank
  when `deep_link_base` unset; email remains read-only.
- pipeline test (extend `test_auto_approval_stage.py` or governance-run test): Step 5c builds
  the packet, is inert when disabled, and a builder exception does not sink the run.

## 10. Delivery

Ships gated; production stays human-gated; no decision-engine or score-semantics change; no
new sanctioned exception; no unrelated refactor; existing dirty working-tree files untouched.
Full targeted suite green before commit; PR opened for operator review (do not merge without
approval).
