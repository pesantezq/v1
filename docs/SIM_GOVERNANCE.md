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

## Config (`config.json → sim_governance`)

```json
{
  "enabled": true,
  "simulation_lane": {"enabled": true},
  "ai_review": {"enabled": true, "daily_cost_cap_usd": 0.5,
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
