# Bounded GPT Auto-Approval in the Simulation-Governance Lane

**Date:** 2026-07-14
**Status:** design approved; implementation in progress
**Package:** `portfolio_automation/sim_governance/`
**Ships:** INERT (`enabled=false`, all sub-flags false, `strategy_daily_cap=0`)

## 1. Purpose

Add a **distinct, bounded GPT auto-approval channel** that may automatically apply
bounded changes **only to authorized simulation / advisory state**, accelerating
simulation-lane experimentation without any production effect. A human operator can
**veto** any auto-applied event and safely roll it back.

This is the third sanctioned mutating path in the repo, alongside
`backtesting/auto_apply.py` (registry weights) and the existing
`sim_governance` two-lane promotion workflow (human-gated production). It mirrors
`auto_apply.py`'s "the safety IS the gates, not the human" posture.

## 2. Non-negotiable authority invariant

> **Auto-approval may accelerate bounded simulation and advisory changes. It can
> never authorize production promotion, production decision-engine input, or
> impersonate human approval. Human approval remains required before any production
> effect.**

Every candidate must satisfy **all** authority hard-gates or it does not mutate state:

```
target_lane        == "simulation"
production_mutation == false
feeds_decision_engine == false
is_human_approved  == false
```

A verdict classified `ready_for_production_review` is a recommendation for **human**
review only — it is **never** interpreted as permission to auto-promote to production.
Candidates that fail any authority gate remain / become pending-human proposals (the
existing `promotion_proposals` path) and the rejection reason is audited.

### Why simulation-only (architecture finding)

The production `ExtendedWatchlist` (`data/portfolio.db`) is read by the live scanner,
whose universe feeds `decision_plan`. Auto-applying there would set
`feeds_decision_engine == true`. Therefore the watchlist auto-apply targets a
**separate simulation DB** (`data/sim_governance_watchlist.db`) that the production
scanner never reads. Live-watchlist promotion stays exclusively on the human-approved
path (`ExtendedWatchlist.promote_operator_approved`). `is_human_approver`
(`schemas.py`) already rejects the tokens `auto`/`gpt`/`system`/`llm`, so the
`auto_approval` channel marker cannot impersonate a human — unchanged, regression-tested.

## 3. Components

| File | Purpose |
|---|---|
| `portfolio_automation/sim_governance/auto_approval.py` *(new)* | Orchestrator `run_auto_approval(...)`, deterministic gates, GPT approver, apply, audit, veto/rollback, circuit breaker, kill-switch. |
| `portfolio_automation/sim_governance/governance_digest.py` *(new)* | Pure digest builder → `{json, html}`; `send_governance_digest(...)` thin SMTP wrapper over `memo_email_sender`. |
| `watchlist_scanner/extended_watchlist.py` *(modify)* | `promote_auto_approved(...)`, `demote_vetoed(symbol)` helper, `get_symbol(symbol)` (exact-state read for CAS), event-aware rollback support. |
| `portfolio_automation/strategy/strategy_selection.py` *(modify)* | `record_auto_strategy_anchor(...)` distinct sim channel + CAS re-anchor helper (never through the human `record_strategy_decision`). |
| `gui_v2/app.py` + `gui_v2/templates/dashboard/governance.html` + `gui_v2/data/dash_governance.py` *(modify)* | `/dashboard/governance/veto` POST (strict operator pattern) + auto-applied cards. |
| `portfolio_automation/sim_governance/daily_governance_run.py` *(modify)* | New Step 5b wiring, inert when disabled. |
| `config.json`, `CLAUDE.md`, `docs/SIM_GOVERNANCE.md`, health skills *(modify)* | Config block, sanctioned exception, docs, oversight. |

## 4. GPT approver

Reuses the daily AI review's LLM plumbing pattern (`agent.llm_adapters.call_provider`),
injectable for tests. Runs **only after all deterministic gates pass** (so no GPT call
when nothing is eligible — preserves cost posture). Result is one of:

```
approve_in_bounds | veto | invalid_or_unavailable
```

Fail-closed: malformed output, timeout, exception, missing reasoning, uncertainty, or
unavailable service → treated as `veto`/`invalid` → no mutation. The approver may only
approve the pre-bounded change or veto; it can never widen a bound.

## 5. Deterministic gates (structured traces)

Each gate returns `{gate_name, passed, reason, observed_value, required_value}`.

**Universal:** feature_enabled, global_kill_switch_inactive, component_kill_switch_inactive,
target_lane_is_simulation, no_production_mutation, does_not_feed_decision_engine,
not_human_approved, supported_candidate_type, valid_source_verdict, source_verdict_not_stale,
source_artifact_hash_matches, valid_idempotency_key, not_previously_applied, daily_cap,
max_active_awaiting_veto, audit_destination_writable, state_store_available,
circuit_breaker_disengaged.

**Watchlist:** symbol_format, not_prohibited_or_static, capacity_below_max, watchlist_daily_cap,
min_confidence, source_proposal_eligible, no_conflicting_active_proposal, feeds_decision_engine_false.

**Strategy:** sandbox_only_assertion, strategy_daily_cap, one_active_strategy_invariant,
candidate_strategy_valid, prior_active_capturable, no_production_strategy_mutation,
feeds_decision_engine_false. Ships disabled with `strategy_daily_cap=0`.

## 6. Audit ledger (safety boundary)

- `outputs/policy/auto_approval_events.jsonl` — authoritative append-only event ledger.
  One event per attempt/apply/gpt_veto/deterministic_reject/human_veto/rollback/
  rollback_conflict/failure. Never rewritten or truncated.
- `outputs/policy/auto_approval_audit.json` — derived current-state summary for health /
  GUI / digest.

**Audit-before-mutate:** the durable event is written **before** the state mutation is
committed; if the audit write fails, no mutation happens (transaction-like flow). An
applied mutation without a corresponding durable audit record is impossible by construction.

Each applied event records: event_id, decision_id, idempotency_key, candidate_type,
symbol/strategy_id, source_verdict_id, source_artifact_path, source_artifact_hash,
source_verdict_timestamp, confidence, gate_trace, gpt_verdict, gpt_reasoning, model_id,
prompt_version, policy_version, config_version, before_state, after_state,
application_timestamp, actor/channel (`approval_channel="auto_approval"`,
`is_human_approved=false`, `target_lane="simulation"`, `production_mutation=false`,
`feeds_decision_engine=false`), application_status.

## 7. Idempotency & concurrency

Stable idempotency key = sha256 of `source_verdict_id | candidate_type | target_id |
source_artifact_hash | policy_version`. Before applying, scan the ledger for a successful
application with that key; if present, skip. Same verdict twice → applied once. Changed
artifact hash → new key (safe). Failed pre-mutation audit → not counted as applied (safe retry).

## 8. Safe rollback & veto (compare-and-swap)

`record_veto(event_id, operator_identity, reason=None)` + `rollback_auto_approval(event_id)`.
Restore `before_state` **only if** current state still equals the event's `after_state`.
If state changed since (human or another run), record `rollback_conflict`, preserve current
state, surface conflict, health AMBER (RED only on invariant/production-boundary breach).

Watchlist rollback is event-aware (uses before_state), handling: symbol absent before,
inactive before, active before, metadata-changed, later-changed. Strategy rollback
re-anchors prior active only if current sim strategy still matches the event's applied strategy.

## 9. Kill switches, config, circuit breaker

Config `sim_governance.auto_approval`: `enabled=false`, `watchlist_enabled=false`,
`strategy_enabled=false`, `live_watchlist_enabled=false` (permanently unsupported —
present for explicitness), `watchlist_daily_cap=2`, `strategy_daily_cap=0`,
`min_confidence=0.85`, `veto_window_hours=48`, `max_active_awaiting_veto=5`,
`evening_digest={enabled=false, send_hour_local=18, timezone="America/New_York"}`,
`sim_watchlist_db_path="data/sim_governance_watchlist.db"`.

Kill switches: `config/auto_approval.DISABLED` file, `STOCKBOT_AUTO_APPROVAL_DISABLED=1` env.
**Precedence (any disables):** env → file → global config `enabled` → component flag.
Any invalid value / unreadable kill file / config parse error → fail closed (disabled).

**Circuit breaker:** after failed rollback, production-boundary attempt, one-active-strategy
violation, unaudited mutation, duplicate application, corrupt ledger, or state/audit
inconsistency → engage and block further applies until human clearance / explicit safe reset.
Reason recorded in the current-state summary.

## 10. Pipeline wiring

New Step 5b inside `daily_governance_run.run_daily_governance`, immediately after Step 5
(the GPT review), reusing the live `review` + `lane` objects. Inert & side-effect-free when
disabled; failure recorded in `status["stages"]["auto_approval"]` and never sinks the run;
no extra GPT call when no candidate passes deterministic gates.

## 11. Evening digest & email

`governance_digest.py` pure builder with sections: auto-applied sim items, gate results,
GPT reasoning, confidence, within-veto-window items, human vetoes, successful rollbacks,
rollback conflicts, failed applications, pending-human proposals, kill/circuit state,
production-boundary rejections. Every item carries an explicit status
(`auto-applied in simulation` / `pending human review` / `vetoed` / `rolled back` /
`rollback conflict` / `failed` / `rejected by authority gate`) — never bare "approved".
Preferred wording: `Auto-applied in simulation · veto available`. GUI links use event_id.

`send_governance_digest(...)` reuses `memo_email_sender` SMTP core; own `GOVERNANCE_DIGEST_ENABLED`
opt-in; local-time schedule (`18:00 America/New_York`, DST-safe). Disabled → skip cleanly;
enabled+no creds → delivery failure recorded + AMBER; send failure → sanitized log + AMBER,
never blocks/undoes a valid auto-approval. Delivery attempt/result/timestamp in audit summary.

## 12. GUI veto surface

`/dashboard/governance/veto` POST: auth actor (never from form), `_operator_edit_enabled()`
gate, `_same_origin()` CSRF, input validation, `record_veto` domain call, `audit_log.record_event`
on every branch, POST→redirect→GET, idempotent, event-id targeted (never symbol-only blind
demotion), optional reason, `confirm()` guard. Cards titled `Auto-applied in simulation ·
veto available` with event_id, symbol/strategy, timestamp, veto-window status, confidence,
GPT reasoning, gate summary, before/after, target_lane, feeds_decision_engine, rollback
availability, conflict. Outside the 48h window: manual-action path, not permanent lockout.

## 13. Health checks

Extend `daily-tool-analysis` (read both artifacts; RED conditions per §14; AMBER: active
within-veto items, a successful veto/rollback this period, rollback conflict awaiting operator,
digest enabled+failed, pending-human fallback on unavailable GPT, breaker engaged w/o violation,
nearing caps) and `portfolio-learning-loop-health`. A successful human veto + rollback is
**not** permanent RED — it proves the control worked. Distinguish `rolled_back_successfully` /
`rollback_conflict` / `rollback_failed`. Mirror the `auto_apply_audit.json` `rolled_back→RED,
applied→verify` template. Health agents VERIFY the sanctioned channel, never revert legitimate
simulation events.

RED: rollback_failed, production_mutation_detected, feeds_decision_engine_true,
marked_human_approved, unaudited_mutation, ledger_corrupt, state_ledger_inconsistent,
one_active_strategy_violation, duplicate_application, authority_gate_bypass,
circuit_breaker_failed_to_engage, rollback_overwrote_newer_state.

## 14. Test plan

Full matrix per implementation request §14 (authority, gates, GPT approver, idempotency,
audit safety, apply/rollback incl. CAS conflicts, kill/config precedence + circuit breaker,
digest/email, GUI, pipeline + regressions). Tests use real code + real sqlite fixtures;
GPT approver injected. New files: `tests/test_auto_approval.py`,
`tests/test_governance_digest.py`, additions to watchlist/strategy/gui/pipeline test files.

## 15. Delivery

Ships disabled; production stays human-gated; no decision-engine or score-semantics change;
no unrelated refactor; existing dirty working-tree files untouched. Operator enablement:
flip `enabled` + `watchlist_enabled` (and later `strategy_enabled`/`strategy_daily_cap`),
optionally `evening_digest.enabled` + `GOVERNANCE_DIGEST_ENABLED`, as the final human step.
