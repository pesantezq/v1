# Next-Stage Implementation — Status & Runbook

Implements `docs/NEXT_STAGE_PORTFOLIO_INTELLIGENCE_SPEC.md`. **All phases built,
tested, and live-validated on branch `feat/next-stage-impl`.** Advisory-only /
observe-only throughout: nothing trades, writes to a broker, moves money, changes
allocations, or writes `outputs/latest/decision_plan.json`.

## What shipped (by phase)

| Phase | Module(s) | Artifacts (namespace) |
|---|---|---|
| 1 | `next_stage/contracts.py` | 32 artifact contracts + record schemas + registry rows |
| 3 | `system_improvement.py` + `.claude/commands/daily-system-improvement.md` | `system_improvement_{ideas,brief,scorecard}` (latest), `..._history.jsonl` (policy) |
| 4 | `approval_queue.py`, `claude_code_prompts.py`, `docs/prompts/*` | `operator_action_queue`, `system_improvement_action_queue` (latest); `*_decisions.jsonl`, `user_action_log.jsonl` (policy) |
| 5 | `universe_scanner.py` + `config/universe_lists.yaml` | `universe_scan_candidates`, `theme_candidates`, `private_ipo_watchlist`, `opportunity_radar` (sandbox) |
| 6 | `opportunity_scoring.py` | (scores feed the radar) |
| 7 | `sandbox/shadow_tracker.py` | `shadow_opportunity_tracking`, `shadow_portfolios`, `candidate_promotion_review` (sandbox) |
| 8 | `market_opportunity_prompts.py` | `market_opportunity_prompts`, `market_opportunity_review_cards`, `opportunity_approval_queue` (sandbox) |
| 9 | `brokers/base.py` | read-only `ReadOnlyBrokerClient` Protocol (additive) |
| 10 | `holdings_resolver.py` | `broker_aware_portfolio` (portfolio, side-panel) |
| 11 | `event_store.py` | `pattern/opportunity/outcome/user_action` `*.jsonl` (policy, append-only) |
| 11A | `strategy/*` | `strategy_{profiles,comparison,shadow_results,risk_scorecard,tax_scorecard}` (sandbox), `strategy_review_queue` (latest) |
| 2/12/13 | `gui_v2/data/dash_next_stage.py` + `dashboard/strategy_lab.html` + route | Strategy Lab dashboard (`/dashboard/strategy-lab`) |
| 14/15 | `next_stage/run_next_stage.py` | orchestrator + this doc |

## How to run

Whole lane (sandbox/research + improvement + strategy), observe-only:

```bash
cd /opt/stockbot && .venv/bin/python -m portfolio_automation.next_stage.run_next_stage --root .
```

Every step is non-fatal and writes only SANDBOX / POLICY / PORTFOLIO + observe-only
LATEST review artifacts. Intended cron home: the **sandbox/research lane**
(alongside `daily_sandbox_run`), not the official daily pipeline — the official
pipeline and `decision_plan.json` are untouched.

Review surface: `/dashboard/strategy-lab` (opportunity radar, strategy comparison,
shadow portfolios, improvement backlog, approval queues, broker-aware side-panel).
Daily improvement review: `.claude/commands/daily-system-improvement.md`.

## Live validation (2026-06-09, against real artifacts)
- Orchestrator: 7/7 steps OK; 23 artifacts produced; `decision_plan.json` not created.
- 85 universe candidates scored; AMD QUALIFIED; SpaceX/OpenAI PRIVATE_WATCH_ONLY.
- 8 strategies ranked (Long-Term Compounding top; Boom Bucket bottom).
- 3 system-improvement ideas from real telemetry.
- Tests: 188 passed (next-stage + affected existing suites: registry, GUI, Schwab).

## Safety confirmation (spec §4)
- No auto-trading / order placement / broker writes / money movement / allocation
  changes (AST + behavioral tests).
- Research artifacts are SANDBOX-namespaced; none write `decision_plan.json` (tested).
- High boom alone cannot promote; private companies never tradeable; penalties surfaced.
- Approval queues + strategy review execute nothing; decisions append-only; cooldowns.
- `broker_aware_portfolio` / preferred-profile are read-only side-panels (§23.10).
- Secrets/account numbers redacted/masked (reused from the broker layer).
- Every producer degrades to a valid observe-only artifact on failure.

## Unified decision suppression (Phase 3 ↔ Phase 4, added 2026-06-26)

A recorded operator decision now suppresses an idea at **both** layers from a single
`approval_queue.record_decision(...)` call:

- **Action-queue layer** — `approval_queue.build_action_queues` already dropped decided
  items via `_suppressed()` keyed on `item_id` (from `system_improvement_decisions.jsonl`).
- **Producer/brief layer** — `system_improvement.build_system_improvement` now also calls
  `_decision_suppressed_ids()`, which **reuses `approval_queue._suppressed`** (one set of
  suppress/cooldown semantics) and matches the returned `item_id`s against the idea `id`
  the build computes (`"si-" + _slug(idea_key)`).

Semantics (shared): `mark_completed` / `mark_duplicate` / `approve_for_implementation`
suppress permanently; `reject` / `defer` suppress until `cooldown_until` (14 days) elapses,
then the idea resurfaces. The legacy `owner_decision`-in-`history.jsonl` path
(`_cooldown_state`) is unchanged and still honored for back-compat. Legacy hand-written
decision records keyed on `id` (not `item_id`) are tolerated without crashing — they simply
do not suppress (use `record_decision()` so the correct `item_id` field is written).

Observe-only; no scoring/decision-plan/broker behavior touched. Tests:
`tests/test_system_improvement.py` (decision suppress / defer-resurface / legacy-tolerance)
and `tests/test_opportunity_and_approval.py::test_one_decision_suppresses_both_brief_and_action_queue`.

## Open follow-ups
- Cron wiring into the sandbox runner (documented; not installed).
- Live forward-performance for shadow tracking (needs a price-series feed).
- Optional `main.py` non-blocking hook for `system_improvement` (currently via the
  orchestrator / skill); deferred to avoid official-pipeline churn.
