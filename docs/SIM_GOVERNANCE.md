# Simulation-Governance Lane

**Status:** shipped 2026-06-16. Package: `portfolio_automation/sim_governance/`.

Implements the operator's two-lane governance model. It is the second sanctioned
non-observe-only path in the repo (alongside `backtesting/auto_apply.py`):
experiments run hot in simulation, and **production behavior changes only after a
human approves a promotion proposal.**

## The two lanes

| Lane | State | May change | Gate |
|---|---|---|---|
| **Simulation / Test** | ACTIVE | `SANDBOX` + `SIMULATION` artifacts: advisory/watchlist/crowd/discovery/ranking/strategy experiments | tests pass |
| **Production** | PROTECTED | live watchlist + advisory loaders (via overlay artifacts only) | **human approval** |

`decision_engine.py` and all score semantics (`signal_score`, `confidence_score`,
`effective_score`, `conviction_score`, `final_rank_score`, `recommendation_score`)
are **never touched**. Production application happens at the *input boundary*
(the broker-overlay tradition), not inside scoring.

## Daily workflow

```
production baseline (existing pipeline)
  -> flock_intelligence.run_flock_intelligence   (Step 1; writes SIMULATION flock artifacts)
  -> simulation_lane.run_simulation_lane         (active; writes SANDBOX, may change sim outputs)
  -> daily_simulation_bundle.build_*             (outputs/simulation/daily_simulation_bundle.json)
  -> ai_review_packet.build/write_*              (outputs/promotion_review/daily_ai_review_packet.{json,md})
  -> daily_ai_review.run_daily_ai_review         (ONE call/day, <= $0.50, else deferred)
  -> promotion_proposals.generate_proposals      (pending proposals for READY candidates)
  -> [human approval]  promotion_approvals.record_approval
  -> production_application.apply_approved_proposals  (writes approved overlay artifacts + audit)
  -> production_overlays.load_production_{watchlist,advisory}  (gated, default OFF)
```

Orchestrated by `daily_governance_run.run_daily_governance`, wired as **Stage 10e**
of `scripts/run_daily_safe.sh` (non-blocking).

## The $0.50/day AI review gate

- Cost is **estimated before the call** via `ai_budget.estimate_ai_cost`.
- `estimated_cost <= cap` → run the single consolidated review (advisory + watchlist together).
- `estimated_cost > cap`  → **skip**; write `daily_ai_review_deferred.json`.
- A once-per-day guard (keyed on the review date) prevents a second call.
- The review classifies each candidate `reject | continue_testing | ready_for_production_review`.
  `ready_for_production_review` is a **recommendation only** — it creates a *pending*
  proposal. The AI can never approve production (`schemas.is_human_approver` rejects
  any AI-reviewer marker as an approver).

### Reviewer: LLM vs heuristic (`review_method`)

The reviewer is pluggable via `run_daily_governance(reviewer=…)`. When no reviewer is
injected, the entrypoint builds one from config (`daily_ai_review.build_configured_reviewer`):

- **`ai_review.llm_enabled: true`** (operator-approved 2026-07-02) + an `OPENAI_API_KEY`
  is resolvable + the kill-switch is off → a real OpenAI-backed reviewer runs and the
  result carries `review_method: "llm"`. Estimated spend ≈ $0.002/day, far under the cap.
- Otherwise (flag off / no key / kill-switch set) → the free deterministic
  `heuristic_reviewer`, `review_method: "heuristic_fallback"`.
- **Graceful degrade:** on any API failure or unparseable output the LLM reviewer falls
  back to the heuristic per-candidate so the run never loses verdicts; those verdicts are
  tagged `[llm-fallback:heuristic]` (whole-run failure) or `[llm-omitted:heuristic]` (a
  candidate the model skipped). The JSON parser also salvages complete verdict objects
  from a truncated array. Either way the AI still only *recommends* — human approval is
  unchanged.
- **Kill-switch:** `STOCKBOT_SIM_GOV_LLM_DISABLED=1` forces the heuristic even when
  `llm_enabled` is true.

## Human approval & production application

- Approvals are recorded to `outputs/promotion_approvals/approved_proposals.json`.
  Invalid metadata (no timestamp, unknown decision, AI approver) is **ignored**.
- `apply_approved_proposals` materializes only approved proposals into:
  - `outputs/latest/approved_watchlist_proposals.json`
  - `outputs/latest/approved_advisory_proposals.json`
  Each op carries its `proposal_id` + `rollback_plan`. Every application appends to
  `outputs/promotion_approvals/production_application_audit.jsonl`, and the prior
  overlay is snapshotted under `…/snapshots/` for `rollback_last`.
- The live loaders (`production_overlays.load_production_*`) are **gated by config
  flags that default OFF**:
  `config.json → sim_governance.production_application.apply_{watchlist,advisory}_overlay`.
  When off they are strict no-ops. Flipping them on is the final, explicit
  production-boundary step (wired in `watchlist_scanner/__main__.py` and `main.py`).

## Observe-only crowd-context annotation (not human-gated)

`crowd_state` is a **fast-refreshing daily signal** (it flips
`confirmed_attention` / `divergent_attention` / `insufficient_data` day to day).
Routing it through a permanent, one-proposal-per-symbol-per-day human-approval gate
made today's read stale tomorrow and accumulated a recurring pending backlog. Because
crowd context is a **pure observe-only display annotation** — it never feeds
`decision_engine` / `decision_plan.json` and never changes an allocation — it does not
belong behind the production gate.

Therefore `crowd_context_change` is handled as a **live, self-refreshing annotation**,
not a promotable proposal:

- `experiment_advisory_crowd_context` (`simulation_lane.py`) sources it from the
  unified crowd bus (`outputs/latest/unified_crowd_intelligence.json`) and emits the
  candidate with `ready_for_production_review=False`.
- `materialize_simulated_views` writes the annotation straight into the SANDBOX
  advisory view (`outputs/sandbox/sim_governance/simulated_advisory.json`) every run,
  so the annotation always matches the current unified bus with **no approval step**.
- `promotion_proposals.generate_proposals` **skips** `crowd_context_change` at the
  gate regardless of the reviewer's verdict (the AI review packet still sees every
  candidate, so this is the hard guarantee), and reports the count as
  `pending_proposals.json → skipped_observe_only`.

The human gate is **unchanged for every behavior-affecting proposal type** (watchlist
add/remove/rank/tag, `advisory_context_change`, flock overlays, …); only the
observe-only crowd annotation is exempt. `apply_approved_advisory` still recognizes a
legacy approved `crowd_context_change` overlay for backward compatibility, but the
sim lane no longer mints new ones.

## Config (`config.json → sim_governance`)

```json
{
  "enabled": true,
  "simulation_lane": {"enabled": true},
  "ai_review": {"enabled": true, "llm_enabled": true, "daily_cost_cap_usd": 0.5,
                "provider": "openai", "model": "gpt-4o-mini", "max_calls_per_day": 1},
  "production_application": {"apply_watchlist_overlay": false,
                            "apply_advisory_overlay": false}
}
```

## Namespaces (added to `data_governance.OutputNamespace`)

| Namespace | Dir | Holds |
|---|---|---|
| `SIMULATION` | `outputs/simulation/` | daily simulation bundle |
| `PROMOTION_REVIEW` | `outputs/promotion_review/` | review packet, verdicts, deferred, pending proposals, status |
| `PROMOTION_APPROVALS` | `outputs/promotion_approvals/` | human approvals, application state, audit, snapshots |

## GUI

`/dashboard/governance` (`gui_v2/data/dash_governance.py`): simulation/production
lane status, AI cost vs the $0.50/day cap + remaining, advisory/watchlist candidates
reviewed, and the pending/approved/rejected/deferred queue, labeled
*Simulation Active* / *Production Pending Approval* / *Approved for Production* /
*Applied to Production*.

## Flock Intelligence (simulation-only crowd flocking/dispersion)

`portfolio_automation/flock_intelligence/` detects when a theme/sector/ticker
cluster is **forming a flock**, becoming **crowded/exhausted**, **dispersing**, or
**broken** — simulation-only research context that never feeds `decision_plan.json`.

- **Inputs (no new paid data):** existing crowd velocity/breadth
  (`crowd_multi_source_velocity.json`), theme grouping (`theme_signals.json`),
  FMP-cache sectors, and `signal_outcomes.csv` returns. Degrades gracefully.
- **Metrics (`metrics.py`, pure):** crowd velocity, crowd/source breadth, mention
  concentration (HHI), average pairwise price correlation, return spread, group
  momentum/volatility, and three transparent 0..1 scores — `flock_score`,
  `dispersion_score`, `exhaustion_score`.
- **States (`states.py`):** `flock_forming / flock_confirmed / flock_exhaustion /
  flock_dispersing / flock_broken / insufficient_data`, each with confidence +
  explanation. Dispersion/broken require a prior flock (tracked in
  `flock_state_history.json`).
- **Artifacts (SIMULATION namespace):** `flock_intelligence.json`,
  `flock_watchlist_candidates.json`, `flock_advisory_context.json`,
  `flock_state_history.json`. Written by the producer as **Step 1** of the daily
  governance run.
- **Governance:** `experiment_flock_intelligence` (registered in the active
  simulation lane) turns flock context into `SimulationCandidate`s that change the
  simulated watchlist + advisory. Five proposal types
  (`flock_context_production_display`, `flock_watchlist_candidate_logic`,
  `flock_advisory_context_logic`, `flock_simulation_scoring_adjustment`,
  `flock_risk_overlay`) flow through the **same** consolidated $0.50/day AI review
  (no extra call) and reach production only via human-approved proposals.
- **GUI:** Crowd page "Flock Intelligence" section + Portfolio per-pick "Flock"
  row (observe-only, honest fallbacks).
- **Health:** the 4 flock artifacts are registered in `artifact_registry.yaml`;
  `/daily-tool-analysis` emits a Flock heartbeat (line 6o) + a content-liveness
  check (groups built but all `insufficient_data` → dispatch
  `portfolio-discovery-health`).

## Tests

`tests/test_sim_governance.py` (24) + `tests/test_sim_governance_pipeline.py` (5)
cover every spec §11 assertion: active simulation, production protection, the
$0.50 single-call gate, the promotion workflow (incl. AI-cannot-self-approve and
invalid-approval rejection), the watchlist/advisory loaders, and rollback.

Flock Intelligence adds 36 tests: `tests/test_flock_metrics.py` (metrics +
classifier), `test_flock_producer.py` (producer + fallbacks + namespace
isolation), `test_flock_sim_governance.py` (active behavior, packet inclusion,
single AI call + cap, pending-only proposals, production gating), and
`test_flock_gui.py` (Crowd section, Portfolio per-pick context, fallbacks).

---

## Bounded GPT auto-approval (SIMULATION only) — shipped 2026-07-14

Module: `portfolio_automation/sim_governance/auto_approval.py`
(+ `governance_digest.py`). The **third** sanctioned mutating path. Ships **INERT**.

### What it does

Consumes the daily AI review's `ready_for_production_review` verdicts and, for
watchlist-eligible candidates, runs deterministic safety gates → a GPT approver
(approve-in-bounds / veto only) → and, if every gate clears, **auto-applies the change
to the SIMULATION lane**: a separate watchlist DB (`data/sim_governance_watchlist.db`)
that the production scanner never reads. It accelerates simulation experimentation; it
does **not** touch production. Strategy auto-anchoring exists but ships disabled
(`strategy_daily_cap=0`).

### Authority invariant (non-negotiable)

> Auto-approval may accelerate bounded simulation/advisory changes. It can never authorize
> production promotion, production decision-engine input, or impersonate human approval.
> Human approval remains required before any production effect.

Every candidate must satisfy `target_lane=="simulation"`, `production_mutation==false`,
`feeds_decision_engine==false`, `is_human_approved==false`. Failing candidates never mutate
state and remain pending-human proposals. `schemas.is_human_approver` is unchanged and rejects
the `auto_approval` marker — the channel is structurally non-human.

### Gates, GPT approver, cost posture

Universal + component (watchlist/strategy) gates return structured traces
`{gate_name, passed, reason, observed_value, required_value}`. The GPT approver runs
**only after all deterministic gates pass** (no model call when nothing is eligible → the
single-daily-review cost posture is preserved). Any malformed/empty/timeout/exception GPT
result fails closed to a veto.

### Audit, idempotency, circuit breaker

- Append-only ledger `outputs/policy/auto_approval_events.jsonl` (authoritative) + derived
  summary `outputs/policy/auto_approval_audit.json`. **Audit-before-mutate**: the durable
  event is written before the mutation; if the audit write fails, no mutation happens.
- Idempotency key = `sha256(source_verdict_id | candidate_type | target_id |
  source_artifact_hash | policy_version)`. A prior successful apply for the key → skip.
- Circuit breaker halts further applies after a failed rollback, corrupt ledger,
  state/audit inconsistency, duplicate application, or invariant/production-boundary breach.

### Veto + compare-and-swap rollback

`record_veto(event_id, operator_identity, reason)` rolls back a specific event by restoring
its captured `before_state` **only if** the current state still equals the `after_state` it
applied. If state changed since (human or another run), it records a `rollback_conflict`,
preserves the current state, and surfaces it for operator resolution (health AMBER). Watchlist
and strategy both use event-aware CAS rollback, never a blind symbol-only demotion.

### Config, kill-switches, wiring

`config.json → sim_governance.auto_approval` (all inert by default): `enabled`,
`watchlist_enabled`, `strategy_enabled`, `live_watchlist_enabled` (unsupported; must stay false),
`watchlist_daily_cap=2`, `strategy_daily_cap=0`, `min_confidence=0.85`, `veto_window_hours=48`,
`max_active_awaiting_veto=5`, `sim_watchlist_db_path`, `evening_digest`.
Kill-switch precedence (any disables): env `STOCKBOT_AUTO_APPROVAL_DISABLED` → file
`config/auto_approval.DISABLED` → global `enabled` → component flag → component env kill.
Invalid config / unreadable kill file → fail closed. Wired as Step 5b inside
`daily_governance_run.run_daily_governance` (Stage 10e), immediately after the GPT review;
inert and side-effect-free when disabled.

### Evening governance digest

`governance_digest.py` builds a `{json, html, text}` digest (auto-applied sim items with GPT
reasoning + gates + confidence, within-veto-window items, vetoes, rollbacks, conflicts,
failures, authority rejections, pending-human, circuit/kill state). Items are always
simulation-qualified — "Auto-applied in simulation · veto available", never a bare "approved".
`send_governance_digest` reuses `memo_email_sender`'s SMTP core, gated on the DISTINCT
`GOVERNANCE_DIGEST_ENABLED` opt-in, local-time scheduled (default 18:00 America/New_York,
DST-safe). Disabled → skip; enabled-without-creds or send failure → recorded delivery failure,
health AMBER, credentials never logged; email failure never blocks/undoes a valid auto-approval.

### GUI

`/dashboard/governance` shows an "Auto-applied in simulation · veto available" card list; each
has a POST `/dashboard/governance/veto` form (auth actor, `GUI_V2_OPERATOR_EDIT` gate,
same-origin CSRF, event-id targeted, optional reason, `confirm()`, audited on every branch).

### Health

daily-tool-analysis + `portfolio-learning-loop-health` read both artifacts. RED: rollback
failed, production mutation / decision-engine feed / human-approved marking detected, unaudited
mutation, corrupt/inconsistent ledger, one-active-strategy breach, duplicate application,
authority-gate bypass, breaker-failed-to-engage, rollback overwrote newer state. AMBER: active
items in veto window, a successful veto/rollback this period, rollback conflict awaiting operator,
digest enabled+failed, pending-human fallback on unavailable GPT, breaker engaged w/o violation,
nearing caps. A successful veto+rollback is the control working — VERIFY, don't revert.

### Activation runbook (final human step)

1. `config.json`: set `sim_governance.auto_approval.enabled=true` + `watchlist_enabled=true`.
2. (optional, later) `strategy_enabled=true` + `strategy_daily_cap>0`.
3. (optional) `evening_digest.enabled=true` AND env `GOVERNANCE_DIGEST_ENABLED=1`; wire an
   18:00-local cron to `governance_digest.run_evening_digest`.
4. Kill instantly with `config/auto_approval.DISABLED` or `STOCKBOT_AUTO_APPROVAL_DISABLED=1`.

## Operator Approval Packet — shipped 2026-07-15

Module: `portfolio_automation/sim_governance/approval_packet.py`. Read-only artifact
consolidator, not a fourth mutating path — it never authors an approval, veto, or overlay; it
only assembles what already exists into one place an operator can act on quickly.

### What it does

Consolidates **both** governance tiers into ONE artifact that the evening digest email and the
GUI approval page both read:

- **tier-sim** — simulation items the GPT auto-approval channel (above) already auto-applied
  and that are still inside their veto window (source: `auto_approval.build_summary
  active_items`). Each entry is always labeled "auto-applied in simulation · veto available" —
  never a bare "approved".
- **tier-production** — production-promotion candidates still `approval_status=="pending"`
  human review (source: `promotion_proposals.load_pending_proposals`).

Wired as **Step 8** of `daily_governance_run.run_daily_governance` (Stage 10e), immediately
after Step 7 (`production_application.apply_approved_proposals`). Both build and write are
wrapped in `try/except` (non-blocking; a failure degrades to `{"ok": False, "error": ...}` in
`status.stages.approval_packet` and never aborts the rest of the daily governance run).

Writes (only when `approval_packet.enabled=true`):
- `outputs/promotion_review/operator_approval_packet.json`
- `outputs/promotion_review/operator_approval_packet.md`

### Email-notifies / act-in-GUI flow

The evening digest (`governance_digest.run_evening_digest`) reads
`sim_governance.approval_packet.deep_link_base` and, when set, includes an
`approval_page_url` (`{deep_link_base}/dashboard/governance`) in the emailed digest so the
operator can jump straight from the notification to the GUI. The GUI itself
(`gui_v2/data/dash_approval_packet.load_packet_context`) reads the same JSON artifact to render
both tiers on `/dashboard/governance` (`gui_v2/templates/dashboard/governance.html`).

### Reuse of the human-gated approval path — no new mutation surface

The packet is display-only. Acting on a tier-production item still goes through the existing,
unchanged human-gated route: `POST /dashboard/governance/decide` → `promotion_approvals.
record_approval(proposal_id, decision, approver, now, ...)` → the SAME schema validation
(`schemas.is_human_approver` still rejects the `auto_approval` marker) and the SAME audit trail
(`outputs/promotion_approvals/approved_proposals.json` / `rejected_proposals.json`) as every
other promotion decision in this file. The packet builder itself has zero write access to
governance state — `build_operator_packet` / `write_operator_packet` only read and render.

### Config (`config.json → sim_governance.approval_packet`)

```json
"approval_packet": {
  "enabled": false,
  "deep_link_base": "https://dashboard.portfolio-ops-center.com",
  "stale_pending_days": 3,
  "note": "..."
}
```

Ships **GATED** (`enabled=false`). Disabled ⇒ Step 8 writes nothing (`status: "disabled"`),
the GUI approve route is unaffected (it still reads `pending_proposals.json` directly for its
own rendering — the packet is an additional consolidated view, not a dependency), and the
evening email is unchanged (no `approval_page_url`). `stale_pending_days` (default 3) is the
health-assessor's staleness threshold, below.

### Health

`assess_packet_health(base_dir, now, *, stale_pending_days=3) -> {"status", "reasons",
"counts"}` — never raises. **GREEN**: packet clean, no tier-production candidate older than
`stale_pending_days`. **AMBER**: a tier-production candidate has been pending longer than
`stale_pending_days` (`stale_pending:<proposal_id>:<age>d` — operator decision-queue aging), or
the packet is missing/unreadable while activated (`packet_missing_or_unreadable`). **RED**: the
packet marks an item decided (`approved`/`rejected` in its rendered `status`) but no matching
record exists in `promotion_approvals.approved_proposal_ids`/`rejected_proposal_ids`
(`packet_gate_drift:<proposal_id>` — a contract breach: either a desynced packet or a decision
path that bypassed `record_approval`; escalate).

`daily-tool-analysis` reads the artifact + `assess_packet_health` (artifacts-read item 27, body
line 6q) and dispatches `portfolio-learning-loop-health` (Layer 8 — "Operator approval queue")
on AMBER/RED. That layer VERIFIES packet entries against the `promotion_approvals` ledger — it
never reverts a legitimate approval or veto the packet correctly reflects, only confirms the
record exists and flags genuine drift.

### Activation runbook (final human step)

1. `config.json`: set `sim_governance.approval_packet.enabled=true`.
2. Set `sim_governance.approval_packet.deep_link_base` to the operator's dashboard base URL
   (e.g. `https://dashboard.portfolio-ops-center.com`) so the GUI approve link resolves.
3. For the emailed link specifically (optional, on top of the above): the evening digest must
   also be on — `sim_governance.auto_approval.evening_digest.enabled=true` AND env
   `GOVERNANCE_DIGEST_ENABLED=1` — otherwise the packet still builds and the GUI page still
   works, there is just no emailed link to it.
4. No dedicated kill-switch beyond `enabled=false` — the module has no mutation path to halt;
   flipping `enabled=false` simply stops Step 8 from writing the artifact.
